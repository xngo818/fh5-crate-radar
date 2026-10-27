"""The same inclusive tier threshold reaches both displayed layers and logs."""
from copy import deepcopy
import unittest

from fh5_map_model import map_markers
from fh5_map_view import MapCanvasRenderer
from fh5_record import display_record
from test_fh5_map_labels import FakeCanvas
from test_fh5_map_model import frame, snapshot, row, NOW
from test_fh5_radar_heading import headless_overlay
from tools.analyze_live_map_log import frame_summary


def sample():
    levels = [1, 2, 3, 4, 5, 10, None, True, '5']
    rows = [row(first_string_address=0x123400+i*256, fingerprint=str(i),
                x=float(i), y=0., z=10., level=level)
            for i, level in enumerate(levels)]
    rows.append(row(first_string_address=0x234500, fingerprint='unverified',
                    x=20., y=0., z=10., level=9, identity_status='unknown'))
    return snapshot(rows=rows, player={'x': 0., 'y': 0., 'z': 0., 'yaw': 0.},
                    telemetry_age=.01)


class DisplayFilterTests(unittest.TestCase):
    def test_four_is_inclusive_and_both_layers_reject_low_or_unknown_tiers(self):
        source = sample()
        radar = headless_overlay()
        radar.render(source, 1000, min_level=4)
        layout = map_markers(source, frame(), NOW, min_level=4)
        self.assertEqual([p['level'] for p in radar.last_render['points']], [4, 5, 10])
        self.assertEqual([p['level'] for p in layout['markers']], [4, 5, 10])
        self.assertEqual(len(source['rows']), 10)  # Source observation is retained.

    def test_raising_threshold_and_empty_rows_clear_previous_canvas_items(self):
        source = sample()
        radar = headless_overlay()
        renderer = MapCanvasRenderer(FakeCanvas())
        radar.render(source, 1000, min_level=4)
        renderer.draw(map_markers(source, frame(), NOW, min_level=4))
        self.assertEqual(renderer.last_render['visible_item_count'], 12)
        radar.render(source, 1000, min_level=10)
        renderer.draw(map_markers(source, frame(), NOW, min_level=10))
        self.assertEqual([p['level'] for p in radar.last_render['points']], [10])
        self.assertEqual(renderer.last_render['visible_item_count'], 4)
        source['rows'] = []
        radar.render(source, 1000, min_level=4)
        renderer.draw(map_markers(source, frame(), NOW, min_level=4))
        self.assertEqual(radar.frame.visible(), 0)
        self.assertEqual(renderer.last_render['visible_item_count'], 0)

    def test_invalid_threshold_fails_closed(self):
        for value in (True, False, '4', 4., -1, 11, None):
            with self.subTest(value=value):
                radar = headless_overlay()
                radar.render(sample(), 1000, min_level=value)
                self.assertEqual(radar.last_render['points'], [])
                result = map_markers(sample(), frame(), NOW, min_level=value)
                self.assertEqual(result['reason'], 'invalid_options')
                self.assertEqual(result['markers'], [])

    def test_record_and_analyzer_keep_filter_separate_from_source_rows(self):
        source = sample()
        source.update(map_frame=dict(frame(), activity_sample_monotonic=NOW-.05),
                      display_min_level=4, radar_visible=True, map_visible=True,
                      read_gaps=0, refresh_limited=False, last_error=None)
        renderer = MapCanvasRenderer(FakeCanvas())
        render = renderer.draw(map_markers(source, source['map_frame'], NOW, min_level=4))
        record = display_record(source, map_render=render, monotonic=NOW)
        self.assertEqual(len(record['rows']), 10)
        self.assertEqual(record['display_min_level'], 4)
        self.assertEqual(record['map_render']['min_level'], 4)
        report = frame_summary(record)
        self.assertEqual(report['eligible_source_points'], 3)
        self.assertEqual(report['actual_map_render_points'], 3)
        self.assertEqual(report['classification'], 'source_and_render_points')
        hidden = deepcopy(record)
        hidden['map_visible'] = False
        hidden['map_render']['points'] = []
        hidden['map_render']['visible_item_count'] = 0
        report = frame_summary(hidden)
        self.assertEqual(report['classification'], 'map_display_disabled')
        self.assertEqual(report['eligible_source_points'], 0)


if __name__ == '__main__':
    unittest.main()
