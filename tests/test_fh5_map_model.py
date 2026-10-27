"""Projection/lifecycle checks using analytic cameras, without game access."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_model import MapFrame, Viewport, map_markers, project_world


VERSION = '3.688.109.0'
NOW = 100.0
VIEWPORT = {'x': 240, 'y': 80, 'width': 800, 'height': 600}


def camera_matrix(center_x=0, center_z=0, half_width=100, half_height=100):
    # Known orthographic camera: +X moves right, +Z moves up, altitude ignored.
    return [1 / half_width, 0, 0, 0,
            0, 0, 0, 0,
            0, 1 / half_height, 0, 0,
            -center_x / half_width, -center_z / half_height, .5, 1]


def frame(**changes):
    result = dict(map_open=True, matrix=camera_matrix(), viewport=VIEWPORT,
                  session_key='pid:start', package_version=VERSION,
                  sample_monotonic=NOW - .1, source='engine_camera',
                  projection_verified=True, game_foreground=True)
    result.update(changes)
    return result


def row(**changes):
    result = dict(first_string_address='0x123400', fingerprint='sample-a',
                  session_key='pid:start', x=20.0, y=150.0, z=-40.0,
                  identity_status='verified', level=4, car='Lancer GSR 2008',
                  car_id=1009)
    result.update(changes)
    return result


def snapshot(**changes):
    result = dict(session_key='pid:start', game_version=VERSION, rows=[row()],
                  player=None, telemetry_state='inactive', replay=False)
    result.update(changes)
    return result


class ProjectionTests(unittest.TestCase):
    def test_nonzero_viewport_origin_and_direct3d_axis_directions(self):
        self.assertEqual(project_world(camera_matrix(), (0, 0, 0), VIEWPORT),
                         (640, 380))
        self.assertEqual(project_world(camera_matrix(), (20, 500, -40), VIEWPORT),
                         (720, 500))
        self.assertEqual(project_world(camera_matrix(), (-100, 5, 100), VIEWPORT),
                         (240, 80))
        negative_origin = dict(VIEWPORT, x=-1280, y=-100)
        self.assertEqual(project_world(camera_matrix(), (0, 0, 0), negative_origin),
                         (-880, 200))

    def test_pan_and_zoom_follow_matrix_without_fitted_player_offset(self):
        xyz = (20, 0, -40)
        self.assertEqual(project_world(camera_matrix(20, -40), xyz, VIEWPORT),
                         (640, 380))
        self.assertEqual(project_world(camera_matrix(10, -30), xyz, VIEWPORT),
                         (680, 410))
        zoomed = project_world(camera_matrix(10, -30, 50, 50), xyz, VIEWPORT)
        self.assertAlmostEqual(zoomed[0], 720)
        self.assertAlmostEqual(zoomed[1], 440)

    def test_clip_edges_and_depth_are_not_clamped_to_screen_edges(self):
        for xyz in ((100.001, 0, 0), (-100.001, 0, 0),
                    (0, 0, 100.001), (0, 0, -100.001)):
            self.assertIsNone(project_world(camera_matrix(), xyz, VIEWPORT))
        for depth in (-.001, 1.001):
            matrix = camera_matrix()
            matrix[14] = depth
            self.assertIsNone(project_world(matrix, (0, 0, 0), VIEWPORT))
        for depth in (0, 1):
            matrix = camera_matrix()
            matrix[14] = depth
            self.assertEqual(project_world(matrix, (0, 0, 0), VIEWPORT), (640, 380))

    def test_nonpositive_w_nonfinite_and_malformed_inputs(self):
        for w in (-1, 0, 1e-10):
            matrix = camera_matrix()
            matrix[15] = w
            self.assertIsNone(project_world(matrix, (0, 0, 0), VIEWPORT))
        for bad in (float('nan'), float('inf'), True, '1'):
            matrix = camera_matrix()
            matrix[0] = bad
            self.assertIsNone(project_world(matrix, (0, 0, 0), VIEWPORT))
            self.assertIsNone(project_world(camera_matrix(), (bad, 0, 0), VIEWPORT))
        for viewport in (None, {'width': 800, 'height': 600},
                         dict(VIEWPORT, width=0), dict(VIEWPORT, height=-1),
                         dict(VIEWPORT, x=float('inf'))):
            self.assertIsNone(project_world(camera_matrix(), (0, 0, 0), viewport))
        self.assertIsNone(project_world([], (0, 0, 0), VIEWPORT))
        matrix = camera_matrix()
        matrix[0] = 1e308
        self.assertIsNone(project_world(matrix, (1e308, 0, 0), VIEWPORT))


class MarkerTests(unittest.TestCase):
    def test_map_uses_fresh_rows_independently_of_inactive_telemetry(self):
        result = map_markers(snapshot(), frame(), NOW)
        self.assertTrue(result['visible'])
        self.assertTrue(result['verified'])
        self.assertFalse(result['replay'])
        marker, = result['markers']
        self.assertEqual(marker['F'], '0x123400')
        self.assertEqual(marker['pixel'], [720, 500])
        self.assertEqual(marker['level'], 4)
        self.assertEqual(marker['car_id'], 1009)
        self.assertEqual(marker['label'], '4级 Lancer GSR 2008')

    def test_closed_stale_and_foreground_change_clear_whole_frame(self):
        for changes, reason in (
                ({'map_open': False}, 'map_closed'),
                ({'sample_monotonic': NOW - .25}, 'stale_map_frame'),
                ({'sample_monotonic': NOW + .001}, 'stale_map_frame'),
                ({'game_foreground': False}, 'game_not_foreground')):
            result = map_markers(snapshot(), frame(**changes), NOW)
            self.assertEqual(result['markers'], [])
            self.assertFalse(result['visible'])
            self.assertEqual(result['reason'], reason)

    def test_restart_and_unknown_or_mismatched_build_clear(self):
        for sample, camera, reason in (
                (snapshot(session_key='new:session'), frame(), 'session_mismatch'),
                (snapshot(game_version='3.999.0.0'),
                 frame(package_version='3.999.0.0'), 'package_version_mismatch'),
                (snapshot(game_version='3.688.108.0'), frame(), 'package_version_mismatch')):
            result = map_markers(sample, camera, NOW)
            self.assertEqual(result['markers'], [])
            self.assertEqual(result['reason'], reason)

    def test_preview_is_explicit_and_never_claims_verified_engine_alignment(self):
        for source in ('recorded', 'synthetic_preview'):
            camera = frame(source=source, projection_verified=False, game_foreground=False)
            self.assertEqual(map_markers(snapshot(), camera, NOW)['markers'], [])
            result = map_markers(snapshot(), camera, NOW, allow_preview=True)
            self.assertEqual(len(result['markers']), 1)
            self.assertTrue(result['preview'])
            self.assertTrue(result['replay'])
            self.assertFalse(result['verified'])
            self.assertFalse(result['projection_verified'])
            self.assertFalse(result['markers'][0]['verified'])
        self.assertEqual(map_markers(snapshot(), frame(projection_verified=False), NOW)
                         ['reason'], 'projection_not_verified')
        self.assertEqual(map_markers(snapshot(replay=True), frame(), NOW)
                         ['reason'], 'projection_not_verified')
        forced_preview = map_markers(snapshot(), frame(), NOW, allow_preview=True)
        self.assertTrue(forced_preview['replay'])
        self.assertFalse(forced_preview['verified'])

    def test_bad_single_rows_do_not_remove_other_markers_or_survive_new_frame(self):
        rows = [None, row(x=float('nan')), row(session_key='old'),
                row(first_string_address=None), row(fingerprint=''), row(x=1000),
                row(), row()]
        result = map_markers(snapshot(rows=rows), frame(), NOW)
        self.assertEqual(len(result['markers']), 1)
        self.assertEqual(map_markers(snapshot(rows=[]), frame(), NOW)['markers'], [])
        self.assertEqual(len(result['markers']), 1)  # New frame did not mutate the old one.

    def test_identity_label_and_minimum_level_come_from_verified_identity(self):
        unknown = row(identity_status='unknown', level=10, car='invented name', car_id=10)
        result = map_markers(snapshot(rows=[unknown]), frame(), NOW)
        marker, = result['markers']
        self.assertEqual(marker['level'], 0)
        self.assertEqual(marker['label'], '?级 未知车型')
        self.assertIsNone(marker['car_id'])
        self.assertEqual(map_markers(snapshot(rows=[unknown]), frame(), NOW, min_level=1)
                         ['markers'], [])
        self.assertEqual(map_markers(snapshot(), frame(), NOW, min_level=5)['markers'], [])
        self.assertEqual(len(map_markers(snapshot(), frame(), NOW, min_level=4)['markers']), 1)

    def test_optional_row_timestamps_are_checked_without_changing_store_contract(self):
        for changes in ({'rows_sample_monotonic': NOW - 1.5},
                        {'rows_sample_monotonic': NOW + .01},
                        {'rows_sample_monotonic': float('nan')},
                        {'rows_age': 1.5}, {'rows_age': -1}, {'rows_age': True}):
            self.assertEqual(map_markers(snapshot(**changes), frame(), NOW)['reason'],
                             'stale_rows')
        self.assertTrue(map_markers(snapshot(rows_age=1.49), frame(), NOW)['visible'])

    def test_missing_input_and_strict_boolean_fields_fail_closed(self):
        for camera in (None, {}, frame(map_open=1), frame(projection_verified=1),
                       frame(game_foreground=1), frame(source='guess'),
                       frame(session_key=''), frame(sample_monotonic=None)):
            self.assertEqual(map_markers(snapshot(), camera, NOW)['markers'], [])
        for sample in (None, {}, snapshot(rows=None)):
            self.assertEqual(map_markers(sample, frame(), NOW)['markers'], [])
        for options in ({'allow_preview': 1}, {'max_age': 0}, {'min_level': True},
                        {'min_level': 11}, {'max_age': float('nan')}):
            self.assertEqual(map_markers(snapshot(), frame(), NOW, **options)['reason'],
                             'invalid_options')

    def test_inputs_detached_and_immutable_frame_normalization(self):
        sample, camera = snapshot(), frame()
        original_sample, original_camera = deepcopy(sample), deepcopy(camera)
        model = MapFrame.from_mapping(camera)
        camera['matrix'][0] = 99
        self.assertEqual(model.matrix[0], .01)
        self.assertIsInstance(model.viewport, Viewport)
        result = map_markers(sample, model, NOW)
        result['markers'][0]['pixel'][0] = -1
        self.assertEqual(sample, original_sample)
        self.assertEqual(camera['viewport'], original_camera['viewport'])
        alias = deepcopy(original_camera)
        alias['matrix16'] = alias.pop('matrix')
        self.assertTrue(map_markers(sample, alias, NOW)['visible'])


if __name__ == '__main__':
    unittest.main()
