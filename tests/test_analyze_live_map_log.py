"""Pure production display-log schema diagnostics; no game/UI/network imports."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.analyze_live_map_log import compact, frame_summary, read_tail, summarize


def record(now=100):
    matrix = [0.]*16
    matrix[0] = matrix[9] = .01
    matrix[14], matrix[15] = .5, 1
    return {'monotonic': now, 'session_key': 'session', 'game_version': '3.688.109.0',
            'process_pid': 42, 'map_state': 'active', 'map_reason': 'map_camera_ready',
            'replay': False, 'read_gaps': 0, 'refresh_limited': False, 'last_error': None,
            'rows': [{'F': '0x12340000', 'fingerprint': 'one', 'x': 10., 'y': 0., 'z': 10.,
                      'identity_status': 'verified', 'level': 4, 'car_id': 1009, 'car': 'Lancer'}],
            'map_frame': {'map_open': True, 'matrix': matrix,
                          'viewport': {'x': 20, 'y': 50, 'width': 800, 'height': 600},
                          'session_key': 'session', 'package_version': '3.688.109.0',
                          'source': 'engine_camera', 'sample_monotonic': now-.05,
                          'activity_sample_monotonic': now-.06, 'camera_address': 0x12345000,
                          'projection_verified': True, 'game_foreground': True},
            'map_render': {'points': [{'F': '0x12340000'}], 'visible_item_count': 4,
                           'reason': 'ready', 'frame_available': True}}


class DiagnosticTests(unittest.TestCase):
    def test_source_and_actual_render_are_separate(self):
        value = frame_summary(record())
        self.assertEqual(value['classification'], 'source_and_render_points')
        self.assertEqual(value['eligible_source_points'], 1)
        self.assertEqual(value['actual_map_render_points'], 1)
        self.assertEqual(value['read_health'], 'healthy')
        self.assertAlmostEqual(value['camera_age_seconds'], .05)
        self.assertAlmostEqual(value['activity_age_seconds'], .06)

    def test_source_points_but_empty_or_missing_renderer(self):
        value = record()
        value['map_render']['points'] = []
        self.assertEqual(frame_summary(value)['classification'], 'source_points_but_render_empty')
        value['map_render'] = None
        self.assertEqual(frame_summary(value)['classification'], 'source_points_but_renderer_not_recorded')

    def test_actual_residual_points_are_not_invented_as_cleared(self):
        value = record()
        value['map_frame'] = None
        result = frame_summary(value)
        self.assertEqual(result['actual_map_render_points'], 1)
        self.assertEqual(result['classification'], 'render_points_without_current_eligible_source')

    def test_activity_ttl_is_independent_of_camera_ttl(self):
        value = record()
        value['map_frame']['activity_sample_monotonic'] = 99.7
        value['map_render']['points'] = []
        result = frame_summary(value)
        self.assertTrue(result['camera_ttl_fresh'])
        self.assertFalse(result['activity_ttl_fresh'])
        self.assertEqual(result['eligible_source_points'], 0)
        self.assertEqual(result['source_policy_reason'], 'activity_timestamp_missing_stale_or_invalid')

    def test_no_rows_offscreen_background_and_unverified_are_distinct(self):
        value = record()
        value['map_render']['points'] = []
        value['rows'] = []
        self.assertEqual(frame_summary(value)['classification'], 'valid_camera_no_rows')
        value = record()
        value['map_render']['points'] = []
        value['rows'][0]['x'] = 10000
        self.assertEqual(frame_summary(value)['classification'], 'valid_camera_rows_not_projectable')
        value['map_frame']['game_foreground'] = False
        self.assertEqual(frame_summary(value)['classification'], 'game_background_hidden')
        value['map_frame']['game_foreground'] = True
        value['map_frame']['projection_verified'] = False
        self.assertEqual(frame_summary(value)['classification'], 'projection_not_authorized')

    def test_missing_read_health_is_unknown_not_healthy(self):
        value = record()
        value.pop('last_error')
        self.assertEqual(frame_summary(value)['read_health'], 'unknown')
        value['read_gaps'] = 1
        self.assertEqual(frame_summary(value)['read_health'], 'unhealthy')

    def test_recent_window_and_log_stream_age_are_separate(self):
        values = [record(now) for now in (80, 95, 100)]
        report = summarize(values, 10, now=102)
        self.assertEqual(report['record_count'], 2)
        self.assertEqual(report['observed_window_seconds'], 5)
        self.assertEqual(report['log_age_seconds'], 2)
        self.assertIn('source_points=1 render_points=1', compact(report))

    def test_partial_append_tail_and_partial_prefix_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'display.jsonl'
            content = ' '*4096+'\n'+json.dumps(record(90))+'\n'+json.dumps(record(100))+'\n'+ '{"monotonic":'
            path.write_text(content, encoding='utf-8')
            values, meta = read_tail(path, max_bytes=2048)
        self.assertTrue(meta['skipped_partial_prefix'])
        self.assertEqual(values[-1]['monotonic'], 100)
        self.assertGreaterEqual(meta['ignored_nonrecord_or_partial_lines'], 1)

    def test_invalid_timestamp_and_empty_log_have_explicit_result(self):
        self.assertEqual(summarize([{'monotonic': True}])['record_count'], 0)
        value = record()
        value['map_frame']['sample_monotonic'] = 101
        value['map_render']['points'] = []
        self.assertEqual(frame_summary(value)['classification'], 'source_frame_invalid_or_expired')


if __name__ == '__main__':
    unittest.main()
