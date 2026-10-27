"""Read-only XInput display hotkeys, with no native library loaded on import."""
from __future__ import annotations

import ctypes

BUTTON_CHOICES = (
    ('禁用', 0),
    ('左摇杆按下（L3）', 0x0040),
    ('右摇杆按下（R3）', 0x0080),
    ('LB', 0x0100),
    ('RB', 0x0200),
    ('View / Back', 0x0020),
    ('Menu / Start', 0x0010),
    ('A', 0x1000),
    ('B', 0x2000),
    ('X', 0x4000),
    ('Y', 0x8000),
    ('十字键上', 0x0001),
    ('十字键下', 0x0002),
    ('十字键左', 0x0004),
    ('十字键右', 0x0008),
)
BUTTON_MASKS = frozenset(mask for _, mask in BUTTON_CHOICES)
DEFAULT_BUTTON = 0x4000


def valid_button(mask):
    """Only exact integer choices are accepted; booleans are not bindings."""
    return type(mask) is int and mask in BUTTON_MASKS


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [('wButtons', ctypes.c_uint16),
                ('bLeftTrigger', ctypes.c_uint8),
                ('bRightTrigger', ctypes.c_uint8),
                ('sThumbLX', ctypes.c_int16), ('sThumbLY', ctypes.c_int16),
                ('sThumbRX', ctypes.c_int16), ('sThumbRY', ctypes.c_int16)]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [('dwPacketNumber', ctypes.c_uint32),
                ('Gamepad', _XINPUT_GAMEPAD)]


class _XInputReader:
    """Lazy XInputGetState only: no hooks, writes, or synthetic input."""
    def __init__(self):
        self._attempted = False
        self._library = None
        self._get_state = None

    def _load(self):
        self._attempted = True
        loader = getattr(ctypes, 'WinDLL', None)
        if loader is None:
            return
        for name in ('xinput1_4.dll', 'xinput1_3.dll', 'xinput9_1_0.dll'):
            try:
                library = loader(name)
                function = library.XInputGetState
                function.argtypes = [ctypes.c_uint32, ctypes.POINTER(_XINPUT_STATE)]
                function.restype = ctypes.c_uint32
                self._library, self._get_state = library, function
                return
            except (OSError, AttributeError):
                continue

    def __call__(self, index):
        if not self._attempted:
            self._load()
        if self._get_state is None:
            return None
        state = _XINPUT_STATE()
        if self._get_state(index, ctypes.byref(state)) != 0:
            return None
        return int(state.Gamepad.wButtons)


class ControllerHotkey:
    """Return one event per press; each connected controller arms on release.

    ``reader(index)`` returns a button bitmask or None for a disconnected slot.
    Suppressing the first sample also prevents a held button from firing on
    reconnect or when the selected binding changes.
    """
    def __init__(self, mask=DEFAULT_BUTTON, reader=None):
        self.mask = mask if valid_button(mask) else DEFAULT_BUTTON
        self._reader = reader if reader is not None else _XInputReader()
        self._previous = {}

    def set_button(self, mask):
        mask = mask if valid_button(mask) else DEFAULT_BUTTON
        if mask != self.mask:
            self.mask = mask
            self._previous.clear()

    def pressed(self):
        if self.mask == 0:
            self._previous.clear()
            return False
        triggered = False
        for index in range(4):
            try:
                buttons = self._reader(index)
                if type(buttons) is not int or not 0 <= buttons <= 0xFFFF:
                    self._previous.pop(index, None)
                    continue
                down = bool(buttons & self.mask)
                previous = self._previous.get(index)
                if previous is False and down:
                    triggered = True
                self._previous[index] = down
            except Exception:
                # A controller/API failure cannot interrupt the UI refresh loop.
                self._previous.pop(index, None)
        return triggered
