"""Headless map visibility regression checks; no Tk window or game provider."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_app import Application
from fh5_map_model import map_markers
from fh5_map_view import MapOverlay


def valid_frame_and_snapshot():
    matrix = [0.0] * 16
    matrix[0], matrix[9], matrix[14], matrix[15] = .01, .01, .5, 1.0
    camera = {
        'map_open': True, 'matrix': matrix,
        'viewport': {'x': 240, 'y': 80, 'width': 800, 'height': 600},
        'session_key': 'test-session', 'package_version': '3.688.109.0',
        'sample_monotonic': 99.9, 'source': 'engine_camera',
        'projection_verified': True, 'game_foreground': True,
    }
    snapshot = {
        'session_key': 'test-session', 'game_version': '3.688.109.0',
        'map_frame': camera, 'player': None, 'telemetry_state': 'inactive',
        'rows': [{'x': 20.0, 'y': 150.0, 'z': -40.0,
                  'first_string_address': '0x123400', 'fingerprint': 'test-row',
                  'session_key': 'test-session', 'identity_status': 'verified',
                  'level': 4, 'car': 'Lancer GSR 2008', 'car_id': 1009}],
    }
    return camera, snapshot


def bare_application(visible):
    # Bypass Application.__init__: these tests must never construct Tk/Hotkeys
    # or start a Runtime, process reader, listener, timer, or real window.
    app = Application.__new__(Application)
    app.visible = Mock()
    flags = {'radar': visible, 'map': True}
    app.visible.get.side_effect = lambda: flags['radar']
    app.visible.set.side_effect = lambda value: flags.update(radar=value)
    app.map_visible = Mock()
    app.map_visible.get.side_effect = lambda: flags['map']
    app.map_visible.set.side_effect = lambda value: flags.update(map=value)
    app.overlay = Mock()
    app.overlay.last_render = {'kind': 'test-radar'}
    app.map_overlay = Mock()
    app.toggle_button = Mock()
    app.map_toggle_button = Mock()
    app.root = Mock()
    app.root.after.return_value = 'fake-after-id'
    app.runtime = Mock()
    app.recorder = Mock()
    app.keys = Mock()
    app.keys.pressed.return_value = False
    app.pad = Mock()
    app.pad.pressed.return_value = False
    app.map_pad = Mock()
    app.map_pad.pressed.return_value = False
    app.status = Mock()
    app.config = {'range': 5000, 'min_level': 4}
    app.closed = False
    app.map_dpi_ready = True
    app.table_next = float('inf')  # The table is outside these visibility tests.
    return app


class ApplicationMapVisibilityTests(unittest.TestCase):
    def test_stopping_radar_does_not_clear_map(self):
        app = bare_application(False)
        app._set_visibility()
        app.overlay.set_visible.assert_called_once_with(False)
        app.map_overlay.render.assert_not_called()
        app.toggle_button.configure.assert_called_once_with(text='启动雷达（F9）')
        app.runtime.snapshot.assert_not_called()
        app.root.after.assert_not_called()

    def test_enabling_display_does_not_force_a_map_without_camera_data(self):
        app = bare_application(True)
        app._set_visibility()
        app.overlay.set_visible.assert_called_once_with(True)
        app.map_overlay.render.assert_not_called()
        app.toggle_button.configure.assert_called_once_with(text='停止雷达（F9）')

    def test_stopping_map_clears_map_without_hiding_radar(self):
        app = bare_application(True)
        app.map_visible.set(False)
        app._set_map_visibility()
        app.map_overlay.render.assert_called_once_with({'rows': []}, None, min_level=4, enabled=False)
        app.overlay.set_visible.assert_not_called()
        app.map_toggle_button.configure.assert_called_once_with(text='地图车包：已关闭（Y）')

    def test_tick_preserves_map_camera_while_radar_is_disabled(self):
        app = bare_application(False)
        camera, snapshot = valid_frame_and_snapshot()
        self.assertTrue(map_markers(snapshot, camera, 100.0)['visible'])
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        app.map_overlay.render.assert_called_once_with(snapshot, camera, min_level=4, enabled=True)
        self.assertFalse(snapshot['radar_visible'])
        self.assertTrue(snapshot['map_visible'])
        app.runtime.snapshot.assert_called_once_with()
        app.status.set.assert_not_called()  # No swallowed exception/clear branch.
        app.root.after.assert_called_once_with(50, app.tick)
        self.assertEqual(app.after_id, 'fake-after-id')

    def test_tick_preserves_original_camera_when_display_is_enabled(self):
        app = bare_application(True)
        camera, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        app.map_overlay.render.assert_called_once_with(snapshot, camera, min_level=4, enabled=True)
        args = app.map_overlay.render.call_args.args
        self.assertIs(args[0], snapshot)
        self.assertIs(args[1], camera)
        app.recorder.record.assert_called_once_with(snapshot, app.overlay.last_render,
                                                    map_render=app.map_overlay.last_render)
        app.status.set.assert_not_called()
        app.root.after.assert_called_once_with(50, app.tick)

    def test_tick_passes_no_map_camera_only_when_map_display_is_disabled(self):
        app = bare_application(True)
        app.map_visible.set(False)
        camera, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        app.map_overlay.render.assert_called_once_with(snapshot, camera, min_level=4, enabled=False)
        app.overlay.render.assert_called_once_with(snapshot, 5000, min_level=4)
        self.assertTrue(snapshot['radar_visible'])
        self.assertFalse(snapshot['map_visible'])

    def test_unverified_ui_dpi_hides_map_and_records_explicit_reason(self):
        app = bare_application(True)
        app.map_dpi_ready = False
        camera, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.tick()
        drawn_snapshot, drawn_camera = app.map_overlay.render.call_args.args
        self.assertIsNone(drawn_camera)
        self.assertIsNone(drawn_snapshot['map_frame'])
        self.assertEqual(drawn_snapshot['map_reason'], 'ui_physical_dpi_unavailable')
        self.assertIs(snapshot['map_frame'], camera)
        self.assertEqual(app.recorder.record.call_args.args[0]['map_reason'], 'ui_physical_dpi_unavailable')
        app.status.set.assert_not_called()


class TransparentOverlayTests(unittest.TestCase):
    def test_missing_transparency_clears_and_withdraws_even_with_valid_camera(self):
        camera, snapshot = valid_frame_and_snapshot()
        self.assertTrue(map_markers(snapshot, camera, 100.0)['visible'])
        overlay = MapOverlay.__new__(MapOverlay)
        overlay.transparent_supported = False
        overlay.visible = True
        overlay.window = Mock()
        overlay.renderer = Mock()
        empty_render = {'points': [], 'visible_item_count': 0,
                        'reason': 'transparent_overlay_unavailable'}
        overlay.renderer.draw.return_value = empty_render
        overlay.last_render = {'points': ['old-marker'], 'visible_item_count': 3}
        # The unsupported path must not reach projection/native placement at all.
        with patch('fh5_map_view.map_markers',
                   side_effect=AssertionError('must reject before projection')):
            result = overlay.render(snapshot, camera, now=100.0)
        overlay.renderer.draw.assert_called_once_with(
            {'markers': [], 'reason': 'transparent_overlay_unavailable'})
        overlay.window.withdraw.assert_called_once_with()
        overlay.window.deiconify.assert_not_called()
        overlay.window.geometry.assert_not_called()
        self.assertFalse(overlay.visible)
        self.assertIs(result, empty_render)
        self.assertIs(overlay.last_render, empty_render)


if __name__ == '__main__':
    unittest.main()
