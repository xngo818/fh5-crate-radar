import ctypes
import unittest
from unittest.mock import patch

from fh5_controller import (BUTTON_CHOICES, DEFAULT_BUTTON, ControllerHotkey,
                            _XINPUT_GAMEPAD, _XINPUT_STATE)
from fh5_config import DEFAULTS, validated


class FakeReader:
    def __init__(self):
        self.states = {0: 0}
        self.calls = 0

    def __call__(self, index):
        self.calls += 1
        value = self.states.get(index)
        if isinstance(value, Exception):
            raise value
        return value


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.hotkey = ControllerHotkey(reader=self.reader)

    def test_edge_and_hold(self):
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertTrue(self.hotkey.pressed())
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = 0
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertTrue(self.hotkey.pressed())

    def test_initial_held_suppressed(self):
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertFalse(self.hotkey.pressed())
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = 0
        self.hotkey.pressed()
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertTrue(self.hotkey.pressed())

    def test_reconnect_held_suppressed(self):
        self.hotkey.pressed()
        self.reader.states[0] = None
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = 0
        self.hotkey.pressed()
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertTrue(self.hotkey.pressed())

    def test_rebinding_held_suppressed(self):
        self.hotkey.pressed()
        self.reader.states[0] = 0x80
        self.hotkey.set_button(0x80)
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = 0
        self.hotkey.pressed()
        self.reader.states[0] = 0x80
        self.assertTrue(self.hotkey.pressed())

    def test_disabled_does_not_poll(self):
        self.hotkey.set_button(0)
        self.assertFalse(self.hotkey.pressed())
        self.assertEqual(self.reader.calls, 0)

    def test_all_slots_and_simultaneous_edge(self):
        self.reader.states = {2: 0, 3: 0}
        self.hotkey.pressed()
        self.reader.states = {2: DEFAULT_BUTTON, 3: DEFAULT_BUTTON}
        self.assertTrue(self.hotkey.pressed())
        self.assertFalse(self.hotkey.pressed())

    def test_errors_do_not_break_polling_or_rearm_held(self):
        self.hotkey.pressed()
        self.reader.states[0] = OSError('disconnected')
        self.assertFalse(self.hotkey.pressed())
        self.reader.states[0] = DEFAULT_BUTTON
        self.assertFalse(self.hotkey.pressed())

    def test_invalid_reader_values_are_disconnected(self):
        for value in (True, '64', -1, 0x10000):
            self.reader.states[0] = value
            self.assertFalse(self.hotkey.pressed())

    def test_default_construction_does_not_load_native(self):
        with patch('fh5_controller._XInputReader._load',
                   side_effect=AssertionError('native loading forbidden')):
            ControllerHotkey()

    def test_win32_structure_layout(self):
        self.assertEqual(ctypes.sizeof(_XINPUT_GAMEPAD), 12)
        self.assertEqual(ctypes.sizeof(_XINPUT_STATE), 16)
        self.assertEqual(_XINPUT_STATE.Gamepad.offset, 4)

    def test_config_accepts_known_masks(self):
        self.assertEqual(DEFAULTS['pad_button'], 0x4000)
        for _, mask in BUTTON_CHOICES:
            self.assertEqual(validated({'pad_button': mask})['pad_button'], mask)

    def test_config_rejects_unknown_or_non_integer(self):
        for value in (True, False, 64.0, '64', None, [], {}, -1, 0x60, 999):
            self.assertEqual(validated({'pad_button': value})['pad_button'], 0x4000)


class IndependentControllerTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.radar = ControllerHotkey(0x4000, reader=self.reader)
        self.map = ControllerHotkey(0x8000, reader=self.reader)

    def poll(self, buttons):
        self.reader.states[0] = buttons
        return self.radar.pressed(), self.map.pressed()

    def test_x_and_y_fire_only_their_bound_action(self):
        self.assertEqual(self.poll(0), (False, False))
        self.assertEqual(self.poll(0x4000), (True, False))
        self.assertEqual(self.poll(0x4000), (False, False))
        self.assertEqual(self.poll(0), (False, False))
        self.assertEqual(self.poll(0x8000), (False, True))
        self.assertEqual(self.poll(0x8000), (False, False))

    def test_simultaneous_press_fires_each_action_once(self):
        self.poll(0)
        self.assertEqual(self.poll(0xC000), (True, True))
        self.assertEqual(self.poll(0xC000), (False, False))
        self.assertEqual(self.poll(0), (False, False))
        self.assertEqual(self.poll(0xC000), (True, True))

    def test_hold_one_binding_does_not_block_other_binding(self):
        self.poll(0)
        self.assertEqual(self.poll(0x4000), (True, False))
        self.assertEqual(self.poll(0xC000), (False, True))
        self.assertEqual(self.poll(0x4000), (False, False))
        self.assertEqual(self.poll(0xC000), (False, True))

    def test_initial_and_reconnected_hold_suppressed_independently(self):
        self.assertEqual(self.poll(0xC000), (False, False))
        self.assertEqual(self.poll(None), (False, False))
        self.assertEqual(self.poll(0xC000), (False, False))
        # Releasing X arms only X while Y remains held after reconnect.
        self.assertEqual(self.poll(0x8000), (False, False))
        self.assertEqual(self.poll(0xC000), (True, False))
        self.assertEqual(self.poll(0x4000), (False, False))
        self.assertEqual(self.poll(0xC000), (False, True))

    def test_disabling_map_preserves_radar_state(self):
        self.poll(0)
        self.map.set_button(0)
        self.assertEqual(self.poll(0xC000), (True, False))
        self.assertEqual(self.poll(0xC000), (False, False))


if __name__ == '__main__':
    unittest.main()
