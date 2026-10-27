"""Panel binding and simultaneous-input checks without Tk or native input."""
import unittest
from unittest.mock import Mock
from test_fh5_map_visibility import bare_application, valid_frame_and_snapshot


class ControllerIntegrationTests(unittest.TestCase):
    def test_binding_change_updates_controller_and_saved_config(self):
        app = bare_application(True)
        app.pad_options = {'L3': 0x40, 'RB': 0x200}
        app.pad_selection = Mock()
        app.pad_selection.get.return_value = 'RB'
        app.changed = Mock()
        app.controller_changed()
        self.assertEqual(app.config['pad_button'], 0x200)
        app.pad.set_button.assert_called_once_with(0x200)
        app.changed.assert_called_once_with()

    def test_keyboard_and_controller_same_frame_toggle_only_once(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.keys.pressed.side_effect = [False, True]  # END, F9
        app.pad.pressed.return_value = True
        app.toggle = Mock()
        app.tick()
        app.toggle.assert_called_once_with()
        app.pad.pressed.assert_called_once_with()
        app.recorder.record.assert_called_once()
        app.status.set.assert_not_called()

    def test_controller_can_toggle_without_keyboard_event(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.pad.pressed.return_value = True
        app.toggle = Mock()
        app.tick()
        app.toggle.assert_called_once_with()
        app.status.set.assert_not_called()


if __name__ == '__main__':
    unittest.main()
