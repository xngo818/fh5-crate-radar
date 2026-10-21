"""Keep the Tk UI thread in verified Per-Monitor V2 physical coordinates.

Import/construction are inert. This changes only the calling thread's context,
not process defaults, system settings, or any game's window. Keep the guard
entered until every Tk window created under it has been destroyed.
"""
from __future__ import annotations

import ctypes as C
import os
import threading


class _DpiAPI:
    def __init__(self):
        if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
            raise OSError('64-bit Windows is required for physical UI coordinates')
        dll = C.WinDLL('user32', use_last_error=True)

        def bind(name, args, result):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn

        self.set_context = bind('SetThreadDpiAwarenessContext', [C.c_void_p], C.c_void_p)
        self.get_context = bind('GetThreadDpiAwarenessContext', [], C.c_void_p)
        self.awareness = bind('GetAwarenessFromDpiAwarenessContext', [C.c_void_p], C.c_int32)
        self.equal = bind('AreDpiAwarenessContextsEqual', [C.c_void_p, C.c_void_p], C.c_int32)


class UIThreadDpi:
    def __init__(self):
        self.ready = False
        self.reason = 'not_entered'
        self._api = self._previous = self._owner = None

    def enter(self):
        if self.ready:
            return self._owner == threading.get_ident()
        if self._previous is not None:
            self.reason = 'previous_context_not_restored'
            return False
        try:
            self._api = _DpiAPI()
            self._owner = threading.get_ident()
            previous = self._api.set_context(C.c_void_p(-4))
            if not previous:
                self.reason = 'per_monitor_v2_context_unavailable'
                return False
            self._previous = previous
            current = self._api.get_context()
            if not current or self._api.awareness(current) != 2:
                self.close()
                self.reason = 'per_monitor_awareness_not_verified'
                return False
            self.ready, self.reason = True, 'per_monitor_v2'
            return True
        except (OSError, AttributeError, ValueError, TypeError, OverflowError) as error:
            self.close()
            self.reason = f'dpi_query_failed:{type(error).__name__}'
            return False

    def close(self):
        self.ready = False
        if self._previous is None:
            return True
        if self._owner != threading.get_ident():
            self.reason = 'dpi_restore_requires_owner_thread'
            return False
        try:
            if (not self._api.set_context(self._previous)
                    or not self._api.equal(self._api.get_context(), self._previous)):
                self.reason = 'dpi_context_restore_failed'
                return False
            self._previous = None
            self.reason = 'restored'
            return True
        except (OSError, AttributeError, ValueError, TypeError, OverflowError):
            self.reason = 'dpi_context_restore_failed'
            return False
