"""Independent display toggles and map pacing with fake UI/controller readers."""
from unittest.mock import Mock, patch
import unittest

from test_fh5_map_visibility import bare_application, valid_frame_and_snapshot
from fh5_controller import ControllerHotkey


class IndependentDisplayTests(unittest.TestCase):
    def test_x_and_y_edges_toggle_independently_without_hold_repeats(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        buttons = [0]
        reader = lambda index: buttons[0] if index == 0 else None
        app.pad = ControllerHotkey(0x4000, reader=reader)
        app.map_pad = ControllerHotkey(0x8000, reader=reader)
        app.toggle, app.toggle_map = Mock(), Mock()
        for mask in (0, 0x4000, 0x4000, 0xc000, 0xc000, 0, 0x8000):
            buttons[0] = mask
            app.tick()
        self.assertEqual(app.toggle.call_count, 1)
        self.assertEqual(app.toggle_map.call_count, 2)

    def test_f9_toggles_only_radar(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.keys.pressed.side_effect = [False, True]
        app.toggle, app.toggle_map = Mock(), Mock()
        app.tick()
        app.toggle.assert_called_once_with()
        app.toggle_map.assert_not_called()

    def test_actual_radar_and_map_toggles_preserve_other_layer(self):
        app = bare_application(True)
        app.changed = Mock()
        app.toggle()
        self.assertFalse(app.visible.get())
        self.assertTrue(app.map_visible.get())
        app.map_overlay.render.assert_not_called()
        app.overlay.set_visible.assert_called_once_with(False)
        app.toggle_map()
        self.assertFalse(app.map_visible.get())
        self.assertFalse(app.visible.get())
        self.assertEqual(app.overlay.set_visible.call_count, 1)
        app.map_overlay.render.assert_called_once_with({'rows': []}, None, min_level=4, enabled=False)

    def test_map_binding_and_minimum_text_are_saved_independently(self):
        app = bare_application(True)
        app.pad_options = {'X': 0x4000, 'Y': 0x8000, 'RB': 0x200}
        app.map_pad_selection = Mock()
        app.map_pad_selection.get.return_value = 'RB'
        app.changed = Mock()
        app.map_controller_changed()
        self.assertEqual(app.config['map_pad_button'], 0x200)
        app.map_pad.set_button.assert_called_once_with(0x200)
        app.pad.set_button.assert_not_called()

    def test_filter_metadata_preserves_raw_rows_and_reaches_both_renderers(self):
        app = bare_application(True)
        app.config['min_level'] = 7
        _, snapshot = valid_frame_and_snapshot()
        snapshot['rows'].append(dict(snapshot['rows'][0], level=2))
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        self.assertEqual(len(snapshot['rows']), 2)
        self.assertEqual(snapshot['display_min_level'], 7)
        self.assertTrue(snapshot['radar_visible'])
        self.assertTrue(snapshot['map_visible'])
        self.assertEqual(app.overlay.render.call_args.kwargs['min_level'], 7)
        self.assertEqual(app.map_overlay.render.call_args.kwargs['min_level'], 7)
        self.assertIs(app.recorder.record.call_args.args[0], snapshot)

    def test_only_active_visible_map_uses_16ms_even_with_radar_off(self):
        app = bare_application(False)
        _, snapshot = valid_frame_and_snapshot()
        snapshot['map_state'] = 'active'
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        app.root.after.assert_called_with(16, app.tick)
        app.map_visible.set(False)
        app.tick()
        app.root.after.assert_called_with(50, app.tick)
        app.map_visible.set(True)
        snapshot['map_frame']['projection_verified'] = False
        app.tick()
        app.root.after.assert_called_with(50, app.tick)

    def test_fast_map_keeps_table_half_second_and_failure_uses_50ms(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        snapshot['map_state'] = 'active'
        app.runtime.snapshot.return_value = snapshot
        app.table_next, app._refresh_table = 0, Mock()
        with patch('fh5_app.time.monotonic', side_effect=[100, 100.016]):
            app.tick()
            app.tick()
        app._refresh_table.assert_called_once_with(snapshot)
        self.assertEqual(app.table_next, 100.5)
        app.root.after.assert_called_with(16, app.tick)
        app.overlay.render.side_effect = RuntimeError('synthetic drawing failure')
        app.tick()
        app.root.after.assert_called_with(50, app.tick)


if __name__ == '__main__':
    unittest.main()
