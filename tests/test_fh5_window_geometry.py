"""Pure fit tests and fake-query lifecycle checks; no native API calls."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_window_geometry as geometry


class FitTests(unittest.TestCase):
    def test_exact_aspect_preserves_screen_offset(self):
        rect = {'x': 120, 'y': 80, 'width': 1920, 'height': 1080}
        self.assertEqual(geometry.fit_render_viewport(rect, 2560, 1440), rect)

    def test_wide_client_has_centered_vertical_bars(self):
        self.assertEqual(geometry.fit_render_viewport(
            {'x': 100, 'y': 50, 'width': 2400, 'height': 1080}, 1920, 1080),
            {'x': 340, 'y': 50, 'width': 1920, 'height': 1080})

    def test_tall_client_has_centered_horizontal_bars(self):
        self.assertEqual(geometry.fit_render_viewport(
            {'x': 100, 'y': 50, 'width': 1920, 'height': 1200}, 2560, 1440),
            {'x': 100, 'y': 110, 'width': 1920, 'height': 1080})

    def test_negative_monitor_origin_and_fractional_fit(self):
        actual = geometry.fit_render_viewport(
            {'x': -1920, 'y': -100, 'width': 1000, 'height': 1000}, 16, 9)
        self.assertEqual(actual, {'x': -1920, 'y': 118.75, 'width': 1000, 'height': 562.5})

    def test_input_is_not_mutated(self):
        rect = {'x': 1, 'y': 2, 'width': 800, 'height': 600, 'extra': [1]}
        old = deepcopy(rect)
        geometry.fit_render_viewport(rect, 1920, 1080)
        self.assertEqual(rect, old)

    def test_bad_rect_shapes_fail_closed(self):
        for bad in (None, [], (), 123, {}, {'x': 0, 'y': 0, 'width': 100}):
            with self.subTest(bad=bad):
                self.assertIsNone(geometry.fit_render_viewport(bad, 1920, 1080))

    def test_nonfinite_bool_string_and_oversized_values_fail_closed(self):
        rect = {'x': 0, 'y': 0, 'width': 1920, 'height': 1080}
        for key in rect:
            for bad in (True, '100', None, float('nan'), float('inf'), -float('inf'), 10**1000):
                with self.subTest(key=key, bad=str(bad)[:20]):
                    self.assertIsNone(geometry.fit_render_viewport(dict(rect, **{key: bad}), 1920, 1080))
        for key in ('width', 'height'):
            for bad in (0, -1, .5, 32769):
                self.assertIsNone(geometry.fit_render_viewport(dict(rect, **{key: bad}), 1920, 1080))
        for key in ('x', 'y'):
            for bad in (-65537, 65537):
                self.assertIsNone(geometry.fit_render_viewport(dict(rect, **{key: bad}), 1920, 1080))

    def test_bad_render_size_fails_closed(self):
        rect = {'x': 0, 'y': 0, 'width': 1920, 'height': 1080}
        for bad in (0, -1, .5, 32769, True, '1920', None, float('nan'), float('inf'), 10**1000):
            self.assertIsNone(geometry.fit_render_viewport(rect, bad, 1080))
            self.assertIsNone(geometry.fit_render_viewport(rect, 1920, bad))


class FakeAPI:
    def __init__(self, windows=None, metadata=True, restore=True, physical=True):
        self.matches = [0x100001234] if windows is None else windows
        self.good_metadata, self.restore, self.physical = metadata, restore, physical
        self.restored = False

    def begin_physical(self):
        return (123, 'per_monitor_v2') if self.physical else None

    def end_physical(self, prior):
        self.restored = prior == 123
        return self.restore

    def windows(self, pid):
        return self.matches

    def metadata(self, hwnd, pid):
        if not self.good_metadata:
            return None
        return {'hwnd': hwnd, 'client_rect': {'x': -1280, 'y': 100, 'width': 1280, 'height': 800},
                'foreground_hwnd': hwnd, 'foreground': True, 'game_foreground': True, 'dpi': 144}


class QueryTests(unittest.TestCase):
    def test_construction_is_inert_and_pid_is_validated(self):
        with patch.object(geometry, '_User32', side_effect=AssertionError('native query')):
            instance = geometry.WindowGeometry(5360)
            self.assertEqual(instance.pid, 5360)
        for pid in (True, 0, -1, '5360', 2**32):
            with self.assertRaises(ValueError):
                geometry.WindowGeometry(pid)

    def test_invalid_sample_does_not_construct_native_api(self):
        with patch.object(geometry, '_User32', side_effect=AssertionError('native query')):
            self.assertIsNone(geometry.WindowGeometry(5360).sample(0, 1440))

    def test_one_window_result_and_dpi_restoration(self):
        api = FakeAPI()
        with patch.object(geometry, '_User32', return_value=api):
            result = geometry.WindowGeometry(5360).sample(2560, 1440)
        self.assertTrue(api.restored)
        self.assertEqual(result['hwnd'], 0x100001234)
        self.assertEqual(result['render_viewport'],
                         {'x': -1280, 'y': 140, 'width': 1280, 'height': 720})
        self.assertEqual(result['coordinate_space'], 'physical_screen_pixels')
        self.assertTrue(result['foreground'])

    def test_missing_multiple_or_failed_window_query_is_hidden(self):
        for windows in ([], [1, 2], False):
            api = FakeAPI(windows=windows)
            if windows is False:
                api.matches = None
            with patch.object(geometry, '_User32', return_value=api):
                self.assertIsNone(geometry.WindowGeometry(5360).sample(2560, 1440))
            self.assertTrue(api.restored)

    def test_metadata_and_dpi_failure_are_hidden(self):
        for api in (FakeAPI(metadata=False), FakeAPI(restore=False), FakeAPI(physical=False)):
            with patch.object(geometry, '_User32', return_value=api):
                self.assertIsNone(geometry.WindowGeometry(5360).sample(2560, 1440))


if __name__ == '__main__':
    unittest.main()
