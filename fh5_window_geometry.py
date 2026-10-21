"""Physical screen client geometry, obtained without controlling a window.

Import and construction are inert. ``sample`` queries only; it never focuses,
moves, resizes, captures, or sends input to a window. DPI awareness is changed
only for this calling thread while querying, then restored. No process/system
DPI setting is modified. Unsupported DPI queries fail closed.

GetClientRect / ClientToScreen use the calling thread's DPI coordinate space:
https://learn.microsoft.com/windows/win32/hidpi/high-dpi-improvements-for-desktop-applications
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setthreaddpiawarenesscontext
"""
from __future__ import annotations

from collections.abc import Mapping
import ctypes as C
import math
import os
import time

MAX_DIMENSION = 32768
MAX_ORIGIN = 65536


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _dimension(value):
    return _number(value) and 1 <= value <= MAX_DIMENSION


def _rect(value):
    if not isinstance(value, Mapping):
        return None
    try:
        x, y, w, h = (value[k] for k in ('x', 'y', 'width', 'height'))
    except KeyError:
        return None
    if (not all(_number(v) for v in (x, y, w, h))
            or abs(x) > MAX_ORIGIN or abs(y) > MAX_ORIGIN
            or not _dimension(w) or not _dimension(h)):
        return None
    return {'x': float(x), 'y': float(y), 'width': float(w), 'height': float(h)}


def fit_render_viewport(client_rect, render_w, render_h):
    """Fit a render surface into a physical client rect with centered bars.

    Return a new ``{x, y, width, height}`` in physical screen pixels, or None
    for invalid input. This is an aspect-preserving geometric fit, not evidence
    that a particular game's internal renderer uses this letterbox policy.
    """
    client = _rect(client_rect)
    if client is None or not _dimension(render_w) or not _dimension(render_h):
        return None
    scale = min(client['width'] / render_w, client['height'] / render_h)
    width, height = float(render_w) * scale, float(render_h) * scale
    return {'x': client['x'] + (client['width'] - width) / 2,
            'y': client['y'] + (client['height'] - height) / 2,
            'width': width, 'height': height}


class _POINT(C.Structure):
    _fields_ = [('x', C.c_int32), ('y', C.c_int32)]


class _RECT(C.Structure):
    _fields_ = [('left', C.c_int32), ('top', C.c_int32),
                ('right', C.c_int32), ('bottom', C.c_int32)]


class _User32:
    """Late-bound query API; never instantiated by module import."""

    def __init__(self):
        if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
            raise OSError('64-bit Windows is required')
        dll = C.WinDLL('user32', use_last_error=True)
        hwnd, boolean, dword = C.c_void_p, C.c_int32, C.c_uint32
        self.callback_type = C.WINFUNCTYPE(boolean, hwnd, C.c_ssize_t)

        def bind(name, args, result):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn

        self.enum = bind('EnumWindows', [self.callback_type, C.c_ssize_t], boolean)
        self.pid_for = bind('GetWindowThreadProcessId', [hwnd, C.POINTER(dword)], dword)
        self.visible = bind('IsWindowVisible', [hwnd], boolean)
        self.iconic = bind('IsIconic', [hwnd], boolean)
        self.client = bind('GetClientRect', [hwnd, C.POINTER(_RECT)], boolean)
        self.to_screen = bind('ClientToScreen', [hwnd, C.POINTER(_POINT)], boolean)
        self.foreground_window = bind('GetForegroundWindow', [], hwnd)
        self.set_thread_dpi = bind('SetThreadDpiAwarenessContext', [hwnd], hwnd)
        self.get_thread_dpi = bind('GetThreadDpiAwarenessContext', [], hwnd)
        self.awareness = bind('GetAwarenessFromDpiAwarenessContext', [hwnd], C.c_int32)
        self.equal_dpi = bind('AreDpiAwarenessContextsEqual', [hwnd, hwnd], boolean)
        self.get_window_dpi = bind('GetDpiForWindow', [hwnd], dword)

    def begin_physical(self):
        # The returned old context is an opaque pointer-sized value, not HWND.
        # V2 preferred; V1 also guarantees physical per-monitor coordinates.
        for context, label in ((-4, 'per_monitor_v2'), (-3, 'per_monitor_v1')):
            old = self.set_thread_dpi(C.c_void_p(context))
            if old:
                if self.awareness(self.get_thread_dpi()) == 2:
                    return old, label
                self.set_thread_dpi(old)
                return None
        return None

    def end_physical(self, old):
        return bool(self.set_thread_dpi(old)
                    and self.equal_dpi(self.get_thread_dpi(), old))

    def window_pid(self, hwnd):
        pid = C.c_uint32()
        return int(pid.value) if self.pid_for(hwnd, C.byref(pid)) else None

    def windows(self, pid):
        matches, errors, count = [], [], [0]

        @self.callback_type
        def visit(hwnd, _):
            count[0] += 1
            if count[0] > 8192:
                errors.append('enumeration_limit')
                return False
            owner = self.window_pid(hwnd)
            if owner is None:
                errors.append('window_pid_failed')
                return False
            if owner == pid and self.visible(hwnd):
                matches.append(int(hwnd))
            return True

        if not self.enum(visit, 0) or errors:
            return None
        return matches

    def screen_client(self, hwnd):
        if not self.visible(hwnd) or self.iconic(hwnd):
            return None
        rect = _RECT()
        if not self.client(hwnd, C.byref(rect)):
            return None
        first, last = _POINT(rect.left, rect.top), _POINT(rect.right, rect.bottom)
        if (not self.to_screen(hwnd, C.byref(first))
                or not self.to_screen(hwnd, C.byref(last))):
            return None
        return _rect({'x': first.x, 'y': first.y,
                      'width': last.x - first.x, 'height': last.y - first.y})

    def metadata(self, hwnd, pid):
        # Recheck PID and client geometry so a moving/destroyed/reused HWND does
        # not create a rectangle from different instants or a different owner.
        if self.window_pid(hwnd) != pid:
            return None
        first = self.screen_client(hwnd)
        foreground = int(self.foreground_window() or 0)
        dpi = int(self.get_window_dpi(hwnd))
        second = self.screen_client(hwnd)
        if (first is None or first != second or not foreground
                or not 48 <= dpi <= 960 or self.window_pid(hwnd) != pid):
            return None
        return {'hwnd': hwnd, 'client_rect': first, 'foreground_hwnd': foreground,
                'foreground': foreground == hwnd, 'game_foreground': foreground == hwnd,
                'dpi': dpi}


class WindowGeometry:
    """One PID's unique visible top-level client window, queried on demand.

    Results are fresh metadata only; callers must still guard process/session
    identity and discard stale camera samples. No window handle is cached.
    """

    def __init__(self, pid):
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError('pid must be a positive 32-bit integer')
        self.pid = pid
        self.last_error = None

    def sample(self, render_w, render_h):
        self.last_error = None
        if not _dimension(render_w) or not _dimension(render_h):
            self.last_error = 'invalid_render_dimensions'
            return None
        api, prior, result = None, None, None
        try:
            api = _User32()
            physical = api.begin_physical()
            if physical is None:
                self.last_error = 'physical_dpi_context_unavailable'
                return None
            prior, dpi_mode = physical
            windows = api.windows(self.pid)
            if windows is None or len(windows) != 1:
                self.last_error = 'visible_window_not_unique_or_enumeration_failed'
            else:
                result = api.metadata(windows[0], self.pid)
                if result is not None:
                    viewport = fit_render_viewport(result['client_rect'], render_w, render_h)
                    if viewport is None:
                        result = None
                    else:
                        result.update(pid=self.pid, render_viewport=viewport,
                                      render_width=float(render_w), render_height=float(render_h),
                                      sample_monotonic=time.monotonic(),
                                      coordinate_space='physical_screen_pixels', dpi_mode=dpi_mode)
                if result is None:
                    self.last_error = 'window_metadata_unavailable_or_changed'
        except (OSError, AttributeError, ValueError, TypeError, OverflowError) as error:
            self.last_error = f'{type(error).__name__}: {error}'
            result = None
        finally:
            if api is not None and prior is not None:
                try:
                    if not api.end_physical(prior):
                        self.last_error = 'dpi_context_restore_failed'
                        result = None
                except (OSError, AttributeError, ValueError, TypeError, OverflowError):
                    self.last_error = 'dpi_context_restore_failed'
                    result = None
        return result
