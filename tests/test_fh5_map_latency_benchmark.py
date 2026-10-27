"""Acceptance means a real matching high-tier marker, never an empty map."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from fh5_map_latency_benchmark import evaluate_record, synthetic_benchmark


def record(now=105):
    return {'monotonic': now, 'session_key': 'same-process', 'replay': False,
            'map_frame': {'session_key': 'same-process', 'package_version': '3.688.109.0',
                          'projection_verified': True, 'game_foreground': True, 'map_open': True,
                          'source': 'engine_camera', 'sample_monotonic': now-.01,
                          'activity_sample_monotonic': now-.02},
            'rows': [{'F': 0x10000, 'fingerprint': 'current', 'identity_status': 'verified', 'level': 7}],
            'map_render': {'preview': False, 'projection_verified': True,
                           'points': [{'F': 0x10000, 'fingerprint': 'current', 'level': 7,
                                       'pixel_x': 256, 'pixel_y': 512}]}}


class LatencyTests(unittest.TestCase):
    def test_actual_matching_high_tier_point_within_explicit_window_passes(self):
        result = evaluate_record([record(119.99)], 100, session_key='same-process')
        self.assertEqual(result['result'], 'passed')
        self.assertAlmostEqual(result['elapsed_seconds'], 19.99)

    def test_live_reader_address_field_matches_the_rendered_marker(self):
        row = record()
        # Live crate rows use this name; renderer points normalize it to F.
        row['rows'][0]['first_string_address'] = row['rows'][0].pop('F')
        self.assertEqual(evaluate_record([row], 100)['result'], 'passed')
        row['rows'][0]['first_string_address'] += 640
        self.assertEqual(evaluate_record([row], 100, seconds=5)['result'], 'failed')

    def test_explicit_invalid_primary_address_does_not_fall_back_to_alias(self):
        row = record(120)
        row['rows'][0].update(F=None, first_string_address=0x10000)
        self.assertEqual(evaluate_record([row], 100)['result'], 'failed')

    def test_empty_map_and_camera_alone_cannot_succeed(self):
        row = record(120)
        row['map_render']['points'] = []
        result = evaluate_record([row], 100)
        self.assertEqual(result['result'], 'failed')
        self.assertIsNone(result['first_marker_monotonic'])

    def test_preview_background_stale_wrong_session_identity_and_low_tier_fail(self):
        cases = []
        row = record(120); row['replay'] = True; cases.append(row)
        row = record(120); row['map_render']['preview'] = True; cases.append(row)
        row = record(120); row['map_frame']['game_foreground'] = False; cases.append(row)
        row = record(120); row['map_frame']['activity_sample_monotonic'] = 119; cases.append(row)
        row = record(120); row['map_frame']['session_key'] = 'old'; cases.append(row)
        row = record(120); row['rows'][0]['identity_status'] = 'unknown'; cases.append(row)
        row = record(120); row['map_render']['points'][0]['fingerprint'] = 'old'; cases.append(row)
        row = record(120); row['rows'][0]['level'] = row['map_render']['points'][0]['level'] = 3; cases.append(row)
        for row in cases:
            with self.subTest(row=row):
                self.assertEqual(evaluate_record([row], 100)['result'], 'failed')

    def test_after_deadline_fails_and_early_stopped_log_is_incomplete(self):
        self.assertEqual(evaluate_record([record(121)], 100)['result'], 'failed')
        row = record(119); row['map_render']['points'] = []
        self.assertEqual(evaluate_record([row], 100)['result'], 'incomplete')

    def test_explicit_valid_time_and_at_most_twenty_second_window_required(self):
        for start in (None, True, float('nan'), -1):
            with self.assertRaises(ValueError):
                evaluate_record([], start)
        for seconds in (0, 21, True, float('inf')):
            with self.assertRaises(ValueError):
                evaluate_record([], 100, seconds=seconds)

    def test_sparse_regression_covers_thirteen_gib_without_scanning_them(self):
        result = synthetic_benchmark()
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['real_game_acceptance'])
        self.assertGreater(result['eligible_layout_bytes'], 13*1024**3)
        self.assertLess(result['payload_read_bytes'], 10*1024**2)
        self.assertLessEqual(result['maximum_step_payload_bytes'], 2*1024**2)


if __name__ == '__main__':
    unittest.main()
