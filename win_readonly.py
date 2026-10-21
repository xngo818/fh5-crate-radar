"""Small, explicit, read-only Windows x64 interface for the FH5 probe.

Importing this module does not load a DLL, enumerate processes, open a process,
or read target memory. Only ``Process`` construction and ``find_processes``
instantiate the Win32 API wrapper. Process construction reads metadata only.

Access is always PROCESS_VM_READ | PROCESS_QUERY_INFORMATION (0x410). There is
no write, allocation, injection, debugger, privilege, driver, or ACL interface.
Reads are exact and bounded: a failed or short read returns None, never padded
bytes. A process handle belongs to one process object; its creation timestamp
also appears in session_key so saved addresses can be scoped to that instance.

Win32 references (Microsoft):
https://learn.microsoft.com/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory
https://learn.microsoft.com/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex
https://learn.microsoft.com/windows/win32/api/tlhelp32/ns-tlhelp32-moduleentry32w
https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-memory_basic_information
"""

from __future__ import annotations

import ctypes as C
import datetime as dt
import ntpath
import os
import threading
import time


TARGET_EXE = "ForzaHorizon5.exe"
PROCESS_ACCESS = 0x0410
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_REGION_QUERIES = 200_000

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
ERROR_NO_MORE_FILES = 18
ERROR_BAD_LENGTH = 24
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = frozenset((0x02, 0x04, 0x08, 0x20, 0x40, 0x80))
WRITABLE_PROTECTIONS = frozenset((0x04, 0x08, 0x40, 0x80))
EXECUTABLE_PROTECTIONS = frozenset((0x10, 0x20, 0x40, 0x80))
INVALID_HANDLE_VALUE = C.c_void_p(-1).value

DWORD = C.c_uint32
WORD = C.c_uint16
LONG = C.c_int32
BOOL = C.c_int32
HANDLE = C.c_void_p
SIZE_T = C.c_size_t
ULONG_PTR = C.c_size_t
LPVOID = C.c_void_p
LPCVOID = C.c_void_p
WCHAR = C.c_wchar


class FILETIME(C.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class PROCESSENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", DWORD),
        ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", LONG), ("dwFlags", DWORD),
        ("szExeFile", WCHAR * 260),
    ]


class MODULEENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("th32ModuleID", DWORD), ("th32ProcessID", DWORD),
        ("GlblcntUsage", DWORD), ("ProccntUsage", DWORD),
        ("modBaseAddr", LPVOID), ("modBaseSize", DWORD), ("hModule", HANDLE),
        ("szModule", WCHAR * 256), ("szExePath", WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(C.Structure):
    _fields_ = [
        ("BaseAddress", LPVOID), ("AllocationBase", LPVOID),
        ("AllocationProtect", DWORD), ("PartitionId", WORD),
        ("RegionSize", SIZE_T), ("State", DWORD),
        ("Protect", DWORD), ("Type", DWORD),
    ]


class _PROCESSOR_INFO(C.Structure):
    _fields_ = [("wProcessorArchitecture", WORD), ("wReserved", WORD)]


class _SYSTEM_INFO_UNION(C.Union):
    _fields_ = [("dwOemId", DWORD), ("processor", _PROCESSOR_INFO)]


class SYSTEM_INFO(C.Structure):
    _fields_ = [
        ("u", _SYSTEM_INFO_UNION), ("dwPageSize", DWORD),
        ("lpMinimumApplicationAddress", LPVOID),
        ("lpMaximumApplicationAddress", LPVOID),
        ("dwActiveProcessorMask", ULONG_PTR), ("dwNumberOfProcessors", DWORD),
        ("dwProcessorType", DWORD), ("dwAllocationGranularity", DWORD),
        ("wProcessorLevel", WORD), ("wProcessorRevision", WORD),
    ]


def _bad_handle(handle: int | None) -> bool:
    return handle in (None, 0, INVALID_HANDLE_VALUE)


def _filetime(value: FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _creation_iso(ticks: int) -> str:
    # Keep the original 100 ns integer for identity; this text has microseconds.
    base = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(microseconds=ticks // 10)).isoformat()


class _WinAPI:
    """DLL loading and all foreign function bindings happen at construction."""

    def __init__(self) -> None:
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("The FH5 reader requires 64-bit Python on Windows.")
        expected = ((PROCESSENTRY32W, 568), (MODULEENTRY32W, 1080),
                    (MEMORY_BASIC_INFORMATION, 48), (SYSTEM_INFO, 48))
        if C.sizeof(WCHAR) != 2 or any(C.sizeof(s) != n for s, n in expected):
            raise RuntimeError("Unexpected Win32 structure sizes; refusing API calls.")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)

        def bind(name, argtypes, restype):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = argtypes, restype
            return fn

        self.OpenProcess = bind("OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self.CloseHandle = bind("CloseHandle", [HANDLE], BOOL)
        self.QueryFullProcessImageNameW = bind(
            "QueryFullProcessImageNameW",
            [HANDLE, DWORD, C.POINTER(WCHAR), C.POINTER(DWORD)], BOOL)
        self.GetProcessTimes = bind(
            "GetProcessTimes", [HANDLE] + [C.POINTER(FILETIME)] * 4, BOOL)
        self.GetExitCodeProcess = bind(
            "GetExitCodeProcess", [HANDLE, C.POINTER(DWORD)], BOOL)
        self.IsWow64Process = bind(
            "IsWow64Process", [HANDLE, C.POINTER(BOOL)], BOOL)
        self.CreateToolhelp32Snapshot = bind(
            "CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE)
        self.Process32FirstW = bind(
            "Process32FirstW", [HANDLE, C.POINTER(PROCESSENTRY32W)], BOOL)
        self.Process32NextW = bind(
            "Process32NextW", [HANDLE, C.POINTER(PROCESSENTRY32W)], BOOL)
        self.Module32FirstW = bind(
            "Module32FirstW", [HANDLE, C.POINTER(MODULEENTRY32W)], BOOL)
        self.Module32NextW = bind(
            "Module32NextW", [HANDLE, C.POINTER(MODULEENTRY32W)], BOOL)
        self.VirtualQueryEx = bind(
            "VirtualQueryEx", [HANDLE, LPCVOID,
                               C.POINTER(MEMORY_BASIC_INFORMATION), SIZE_T], SIZE_T)
        self.ReadProcessMemory = bind(
            "ReadProcessMemory",
            [HANDLE, LPCVOID, LPVOID, SIZE_T, C.POINTER(SIZE_T)], BOOL)
        self.GetNativeSystemInfo = bind(
            "GetNativeSystemInfo", [C.POINTER(SYSTEM_INFO)], None)
        self.system = SYSTEM_INFO()
        self.GetNativeSystemInfo(C.byref(self.system))
        if self.system.u.processor.wProcessorArchitecture != 9:
            raise RuntimeError("Only native AMD64 Windows is supported by this reader.")

    def error(self, operation: str, code: int | None = None) -> OSError:
        code = C.get_last_error() if code is None else code
        error = C.WinError(code)
        error.add_note(operation)
        return error

    def snapshot(self, flags: int, pid: int) -> int:
        # Toolhelp documents transient ERROR_BAD_LENGTH during module snapshots.
        # Retrying the same metadata API adds no access rights or alternate path.
        for _ in range(3):
            C.set_last_error(0)
            handle = self.CreateToolhelp32Snapshot(flags, pid)
            if not _bad_handle(handle):
                return handle
            code = C.get_last_error()
            if code != ERROR_BAD_LENGTH:
                raise self.error("CreateToolhelp32Snapshot", code)
        raise self.error("CreateToolhelp32Snapshot", ERROR_BAD_LENGTH)


def find_processes() -> list[dict]:
    """Return only basename-exact FH5 matches: [{pid, name}]. No process reads."""
    api = _WinAPI()
    snapshot = api.snapshot(TH32CS_SNAPPROCESS, 0)
    result = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = C.sizeof(entry)
        C.set_last_error(0)
        ok = api.Process32FirstW(snapshot, C.byref(entry))
        while ok:
            name = str(entry.szExeFile)
            if ntpath.basename(name).casefold() == TARGET_EXE.casefold():
                result.append({"pid": int(entry.th32ProcessID), "name": name})
            C.set_last_error(0)
            ok = api.Process32NextW(snapshot, C.byref(entry))
        code = C.get_last_error()
        if code != ERROR_NO_MORE_FILES:
            raise api.error("Process32FirstW/Process32NextW", code)
    finally:
        api.CloseHandle(snapshot)
    return sorted(result, key=lambda item: item["pid"])


class Process:
    """One FH5 process instance, opened once with access 0x410.

    ``identity`` and ``main_module`` return metadata copies. ``last_error`` is
    None or a small structured dictionary after the most recent read failure.
    Module enumeration failure is nonfatal and appears in metadata_warnings.
    ``regions()`` returns metadata only for committed, readable AND writable,
    non-guard ranges. It raises on an incomplete/invalid enumeration.

    ``is_alive()`` checks GetExitCodeProcess and the retained handle's creation
    identity. As with this Win32 exit-code API generally, an application exiting
    with the reserved code STILL_ACTIVE (259) is ambiguous; callers must also
    invalidate all addresses after read errors and re-open for a new session.
    """

    def __init__(self, pid: int) -> None:
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ValueError("pid must be a positive 32-bit integer")
        self._lock = threading.RLock()
        self._api = _WinAPI()
        self._handle = None
        self.pid = pid
        self.last_error = None
        self.metadata_warnings = []
        self._identity = {}
        self._main_module = None
        C.set_last_error(0)
        handle = self._api.OpenProcess(PROCESS_ACCESS, False, pid)
        if _bad_handle(handle):
            raise self._api.error("OpenProcess(0x410)")
        self._handle = handle
        try:
            pathbuf = C.create_unicode_buffer(32768)
            pathlen = DWORD(len(pathbuf))
            if not self._api.QueryFullProcessImageNameW(
                    handle, 0, pathbuf, C.byref(pathlen)):
                raise self._api.error("QueryFullProcessImageNameW")
            image_path = pathbuf.value
            basename = ntpath.basename(image_path)
            if basename.casefold() != TARGET_EXE.casefold():
                raise ValueError("The selected process is not ForzaHorizon5.exe")
            wow64 = BOOL()
            if not self._api.IsWow64Process(handle, C.byref(wow64)):
                raise self._api.error("IsWow64Process")
            if wow64.value:
                raise RuntimeError("The selected process is not native x64.")
            self._creation = self._creation_time()
            self._identity = {
                "pid": pid,
                "image_path": image_path,
                "executable": basename,
                "creation_time_100ns": self._creation,
                "creation_time_utc": _creation_iso(self._creation),
                "session_key": f"{pid}:{self._creation}:{image_path.casefold()}",
                "access_mask": PROCESS_ACCESS,
                "architecture": "AMD64",
            }
            if not self.is_alive():
                raise ProcessLookupError("FH5 exited during process identification.")
            self._main_module = self._query_main_module()
            if not self.is_alive():
                raise ProcessLookupError("FH5 exited during module enumeration.")
        except BaseException:
            self.close()
            raise

    @property
    def identity(self) -> dict:
        return dict(self._identity)

    @property
    def main_module(self) -> dict | None:
        return dict(self._main_module) if self._main_module else None

    @property
    def closed(self) -> bool:
        return _bad_handle(self._handle)

    @property
    def min_address(self) -> int:
        return int(self._api.system.lpMinimumApplicationAddress or 0)

    @property
    def max_address(self) -> int:
        return int(self._api.system.lpMaximumApplicationAddress or 0)

    def _creation_time(self) -> int:
        times = [FILETIME() for _ in range(4)]
        if not self._api.GetProcessTimes(
                self._handle, *(C.byref(item) for item in times)):
            raise self._api.error("GetProcessTimes")
        return _filetime(times[0])

    def _set_error(self, operation: str, *, code: int = 0,
                   detail: str = "", **fields) -> None:
        self.last_error = {"operation": operation, "winerror": int(code),
                           "detail": detail, **fields}

    def is_alive(self) -> bool:
        with self._lock:
            if self.closed:
                self._set_error("is_alive", detail="Process handle is closed")
                return False
            status = DWORD()
            if not self._api.GetExitCodeProcess(self._handle, C.byref(status)):
                self._set_error("GetExitCodeProcess", code=C.get_last_error())
                return False
            if status.value != STILL_ACTIVE:
                self._set_error("is_alive", detail="Process exited",
                                exit_code=int(status.value))
                return False
            try:
                creation = self._creation_time()
            except OSError as error:
                self._set_error("GetProcessTimes", code=error.winerror or 0)
                return False
            if creation != self._creation:
                self._set_error("is_alive", detail="Process creation identity changed")
                return False
            return True

    def _query_main_module(self) -> dict | None:
        snapshot = None
        try:
            snapshot = self._api.snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                          self.pid)
            entry = MODULEENTRY32W()
            entry.dwSize = C.sizeof(entry)
            C.set_last_error(0)
            ok = self._api.Module32FirstW(snapshot, C.byref(entry))
            while ok:
                name, path = str(entry.szModule), str(entry.szExePath)
                if (int(entry.th32ProcessID) == self.pid
                        and name.casefold() == TARGET_EXE.casefold()
                        and ntpath.normcase(path) ==
                        ntpath.normcase(self._identity["image_path"])):
                    return {"base": int(entry.modBaseAddr or 0),
                            "size": int(entry.modBaseSize), "path": path,
                            "name": name}
                C.set_last_error(0)
                ok = self._api.Module32NextW(snapshot, C.byref(entry))
            code = C.get_last_error()
            if code != ERROR_NO_MORE_FILES:
                raise self._api.error("Module32FirstW/Module32NextW", code)
            self.metadata_warnings.append({"operation": "main_module",
                                           "detail": "Exact main-module match unavailable"})
        except OSError as error:
            self.metadata_warnings.append({"operation": "main_module",
                                           "winerror": error.winerror or 0,
                                           "detail": str(error)})
        finally:
            if not _bad_handle(snapshot):
                self._api.CloseHandle(snapshot)
        return None

    def read(self, addr: int, size: int) -> bytes | None:
        """Copy exactly 0..MAX_READ_BYTES bytes; no partial/padded result."""
        with self._lock:
            self.last_error = None
            if (type(addr) is not int or type(size) is not int or size < 0
                    or size > MAX_READ_BYTES or addr < self.min_address
                    or addr > self.max_address
                    or size > self.max_address - addr + 1):
                self._set_error("read", detail="Invalid or oversized address range")
                return None
            if not self.is_alive():
                return None
            if size == 0:
                return b""
            buffer = (C.c_ubyte * size)()
            actual = SIZE_T()
            C.set_last_error(0)
            ok = self._api.ReadProcessMemory(
                self._handle, C.c_void_p(addr), buffer, size, C.byref(actual))
            code = C.get_last_error()
            if not ok or actual.value != size:
                self._set_error("ReadProcessMemory", code=code,
                                detail="Read failed or was short", address=addr,
                                requested=size, actual=int(actual.value))
                return None
            if not self.is_alive():
                return None
            # Each call owns its destination buffer and returns an immutable copy.
            return bytes(buffer)

    def regions(self, *, deadline: float | None = None) -> list[dict]:
        """Enumerate eligible metadata, optionally before a monotonic deadline.

        A deadline limits enumeration as well as later payload reads. A timeout
        raises instead of returning a list that could look complete to callers.
        """
        with self._lock:
            self.last_error = None
            if not self.is_alive():
                raise ProcessLookupError("FH5 is not alive; discard cached addresses.")
            result = []
            cursor = self.min_address
            for _ in range(MAX_REGION_QUERIES):
                if deadline is not None and time.monotonic() >= deadline:
                    self._set_error("VirtualQueryEx", detail="Region enumeration time budget exhausted",
                                    address=cursor)
                    raise TimeoutError("Region enumeration time budget exhausted; result is incomplete.")
                if cursor > self.max_address:
                    break
                info = MEMORY_BASIC_INFORMATION()
                C.set_last_error(0)
                count = self._api.VirtualQueryEx(
                    self._handle, C.c_void_p(cursor), C.byref(info), C.sizeof(info))
                if count != C.sizeof(info):
                    code = C.get_last_error()
                    self._set_error("VirtualQueryEx", code=code,
                                    detail="Region enumeration incomplete", address=cursor)
                    raise self._api.error("VirtualQueryEx incomplete enumeration", code)
                base, size = int(info.BaseAddress or 0), int(info.RegionSize)
                end = base + size
                if size <= 0 or base > cursor or end <= cursor:
                    self._set_error("VirtualQueryEx", detail="Non-progressing region")
                    raise RuntimeError("VirtualQueryEx returned an invalid region.")
                protect = int(info.Protect)
                low = protect & 0xFF
                eligible = (int(info.State) == MEM_COMMIT
                            and not protect & (PAGE_GUARD | PAGE_NOACCESS)
                            and low in READABLE_PROTECTIONS
                            and low in WRITABLE_PROTECTIONS)
                if eligible:
                    result.append({
                        "base": base, "size": size, "end": end,
                        "allocation_base": int(info.AllocationBase or 0),
                        "allocation_protect": int(info.AllocationProtect),
                        "state": int(info.State), "protect": protect,
                        "type": int(info.Type), "readable": True,
                        "writable": True,
                        "executable": low in EXECUTABLE_PROTECTIONS,
                    })
                cursor = end
            else:
                self._set_error("VirtualQueryEx", detail="Region-query cap reached")
                raise RuntimeError("Region enumeration cap reached; result is incomplete.")
            if not self.is_alive():
                raise ProcessLookupError("FH5 exited during region enumeration.")
            return result

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if not _bad_handle(handle) and not self._api.CloseHandle(handle):
                self._set_error("CloseHandle", code=C.get_last_error())

    def __enter__(self) -> Process:
        if self.closed:
            raise ValueError("The process handle is closed.")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort fallback. Call close() or use a with block deterministically.
        try:
            if hasattr(self, "_lock") and hasattr(self, "_handle"):
                self.close()
        except Exception:
            pass
