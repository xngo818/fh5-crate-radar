import json
import importlib
from copy import deepcopy
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fh5_record import FrameRecorder, display_record
import fh5_record as record_module
from fh5_map_model import map_markers
from fh5_map_view import MapCanvasRenderer
from test_fh5_map_labels import FakeCanvas
from test_fh5_map_visibility import valid_frame_and_snapshot


class RecordTests(unittest.TestCase):
    def test_whitelist_and_headless(self):
        data = display_record({'player': {'x': 1, 'rawmemory': 'secret'},
                               'rows': [{'x': 2, 'source_metadata': 'large'}],
                               'rawmemory': 'secret'}, utc='utc', monotonic=3)
        self.assertEqual(data['player'], {'x': 1})
        self.assertEqual(data['rows'], [{'x': 2}])
        self.assertNotIn('render', data)
        self.assertNotIn('rawmemory', data)

    def test_rate_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'frames.jsonl'
            recorder = FrameRecorder(path)
            with patch('fh5_record.time.monotonic', side_effect=[1, 1.1, 1.5]):
                self.assertTrue(recorder.record({}, {'points': [1]}))
                self.assertFalse(recorder.record({}))
                self.assertTrue(recorder.record({}, {'points': []}))
            recorder.close()
            recorder.close()
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]['render']['points'], [])
            self.assertFalse(recorder.record({}))

    def test_failure_does_not_escape(self):
        recorder = FrameRecorder('unused')
        with patch('builtins.open', side_effect=OSError('blocked')):
            self.assertFalse(recorder.record({}))
        self.assertIn('blocked', recorder.error)
        recorder.close()

    def test_disabled_does_not_open(self):
        with patch('builtins.open', side_effect=AssertionError('unexpected')):
            self.assertFalse(FrameRecorder().record({}))


class RecoveryTimingRecordTests(unittest.TestCase):
    def test_recovery_diagnostics_keep_cause_and_progress_without_raw_ranges(self):
        recovery = {'mode': 'recovery_burst', 'reason': 'fresh_rows_lost',
                    'priority_range_count': 3, 'priority_bytes': 32768,
                    'deferred_region_count': 6, 'scope_range_count': 4,
                    'scope_bytes': 65536, 'metadata_refreshes': 7,
                    'metadata_age_seconds': .2, 'full_scan_generation': 2,
                    'metadata_refresh_error': 'enumeration timed out',
                    'metadata_refresh_failures': 1,
                    'empty_seconds': 1.2, 'burst_remaining_seconds': 1.8,
                    'ranges': [[0x12345000, 0x12346000]],
                    'raw_memory': b'private', 'projection_verified': True}
        result = display_record({'memory_timing': {'recovery': recovery}},
                                utc='fixture', monotonic=10)
        saved = result['memory_timing']['recovery']
        self.assertEqual(saved['reason'], 'fresh_rows_lost')
        self.assertEqual(saved['scope_bytes'], 65536)
        self.assertEqual(saved['priority_bytes'], 32768)
        self.assertEqual(saved['deferred_region_count'], 6)
        self.assertEqual(saved['full_scan_generation'], 2)
        self.assertEqual(saved['metadata_refresh_error'], 'enumeration timed out')
        self.assertEqual(saved['metadata_refresh_failures'], 1)
        self.assertNotIn('ranges', saved)
        self.assertNotIn('raw_memory', saved)
        self.assertNotIn('projection_verified', saved)
        self.assertIsNone(result['map_frame'])

    def test_progress_records_frontier_and_resume_offsets_without_pending_bytes(self):
        progress = {'scan_address': 0x200000, 'pending_hit_offset': 1024,
                    'pending_stride_index': 2, 'pending_chunk': b'private memory'}
        result = display_record({'scan_progress': progress}, utc='fixture', monotonic=10)
        self.assertEqual(result['scan_progress'], {'scan_address': 0x200000,
            'pending_hit_offset': 1024, 'pending_stride_index': 2})

    def test_invalid_recovery_values_are_bounded_or_omitted(self):
        class Dangerous:
            def __repr__(self):
                raise AssertionError('must not stringify arbitrary objects')
        recovery = {'mode': 'x' * 10000, 'reason': Dangerous(),
                    'metadata_age_seconds': float('nan'), 'scope_bytes': 1 << 100}
        result = display_record({'memory_timing': {'recovery': recovery}},
                                utc='fixture', monotonic=10)
        self.assertEqual(result['memory_timing']['recovery'], {'mode': 'x' * 2048})
        self.assertNotIn('recovery', display_record(
            {'memory_timing': {'recovery': [recovery]}}, utc='fixture', monotonic=10)['memory_timing'])


class MapTrackerRecordTests(unittest.TestCase):
    def tracker(self):
        return {
            'reason': 'camera_discovery_in_progress', 'candidate_count': 2,
            'locator_complete': False, 'locator_read_bytes': 1048576,
            'camera_address': 0x200000, 'projection_verified': True,
            'raw_memory': b'dump', 'timing': {
                'session_search_started_monotonic': 1.0,
                'current_search_started_monotonic': 2.0,
                'first_structure_monotonic': 3.0,
                'first_active_camera_monotonic': None,
                'last_active_camera_monotonic': None,
                'search_elapsed_seconds': 4.0, 'target_seconds': 20,
                'private_timing': [1, 2, 3]},
            'locator_diagnostics': {
                'metadata_queries': 2, 'hot_passes': 3, 'hot_read_bytes': 4096,
                'allocation_anchor_probe_reads': 4, 'allocation_anchor_probe_hits': 1,
                'allocation_anchor_probes_remaining': 5,
                'scan_address': 0x300000, 'hot_scan_address': 0x400000,
                'read_failures': 1, 'last_read_failure_address': 0x500000,
                'hot_region_bases': [0x600000, 0x700000],
                'hot_search_anchors': [0x610000], 'hot_ranges': [[0x600000, 0x601000]],
                'inactive_geometry_hint_regions': {0x200000: b'dump'}}}

    def record(self, tracker, **snapshot):
        return display_record(dict(snapshot, map_status={'tracker_status': tracker}),
                              utc='utc', monotonic=10)

    def test_tracker_records_explicit_fields_without_projection_authority(self):
        tracker = self.tracker()
        data = self.record(tracker)['map_tracker']
        self.assertEqual(data['reason'], 'camera_discovery_in_progress')
        self.assertEqual(data['candidate_count'], 2)
        self.assertEqual(data['camera_address'], 0x200000)
        self.assertEqual(data['timing']['target_seconds'], 20)
        self.assertEqual(data['locator_diagnostics']['allocation_anchor_probes_remaining'], 5)
        self.assertNotIn('projection_verified', data)
        encoded = json.dumps(data, allow_nan=False)
        for forbidden in ('raw_memory', 'private_timing', 'inactive_geometry_hint_regions', 'dump'):
            self.assertNotIn(forbidden, encoded)

    def test_tracker_collections_have_strict_small_prefix_limits_and_are_detached(self):
        tracker = self.tracker()
        locator = tracker['locator_diagnostics']
        locator['hot_region_bases'] = list(range(50))
        locator['hot_search_anchors'] = list(range(50))
        locator['hot_ranges'] = [[i * 4096, (i + 1) * 4096] for i in range(20)]
        data = self.record(tracker)['map_tracker']['locator_diagnostics']
        self.assertEqual(data['hot_region_bases'], list(range(16)))
        self.assertEqual(data['hot_search_anchors'], list(range(4)))
        self.assertEqual(len(data['hot_ranges']), 8)
        data['hot_region_bases'][0] = 999
        data['hot_ranges'][0][0] = 999
        self.assertEqual(locator['hot_region_bases'][0], 0)
        self.assertEqual(locator['hot_ranges'][0][0], 0)

    def test_tracker_bad_scalars_ranges_and_oversized_numbers_are_omitted(self):
        tracker = self.tracker()
        tracker.update(reason='x' * 5000, candidate_count=1 << 500,
                       locator_read_bytes=float('nan'), camera_address=object())
        tracker['timing'].update(search_elapsed_seconds=float('inf'), target_seconds=object())
        locator = tracker['locator_diagnostics']
        locator['hot_region_bases'] = [True, -1, 1 << 500, 123, '456']
        locator['hot_search_anchors'] = 'not-a-list'
        locator['hot_ranges'] = [[1, 2], [2, 2], [3, 2], [0, 1 << 500],
                                 [True, 2], [1, 2, 3], object(), ['1', 2]]
        data = self.record(tracker)['map_tracker']
        self.assertEqual(len(data['reason']), record_module.MAX_TEXT)
        for key in ('candidate_count', 'locator_read_bytes', 'camera_address'):
            self.assertNotIn(key, data)
        self.assertNotIn('search_elapsed_seconds', data['timing'])
        self.assertEqual(data['locator_diagnostics']['hot_region_bases'], [123])
        self.assertEqual(data['locator_diagnostics']['hot_search_anchors'], [])
        self.assertEqual(data['locator_diagnostics']['hot_ranges'], [[1, 2]])
        json.dumps(data, allow_nan=False)

    def test_tracker_never_converts_or_iterates_arbitrary_objects(self):
        class Hostile:
            def __str__(self):
                raise AssertionError('str called')
            def __repr__(self):
                raise AssertionError('repr called')
            def __iter__(self):
                raise AssertionError('iter called')
            def __deepcopy__(self, memo):
                raise AssertionError('deepcopy called')

        class HostileDict(dict):
            def get(self, *args):
                raise AssertionError('custom get called')

        class HostileList(list):
            def __getitem__(self, key):
                raise AssertionError('custom getitem called')

        tracker = self.tracker()
        tracker['reason'] = Hostile()
        tracker['timing'] = HostileDict()
        tracker['locator_diagnostics'].update(hot_region_bases=HostileList(),
                                            hot_search_anchors=Hostile(), hot_ranges=[Hostile()])
        data = self.record(tracker)['map_tracker']
        self.assertNotIn('reason', data)
        self.assertNotIn('timing', data)
        self.assertEqual(data['locator_diagnostics']['hot_ranges'], [])
        json.dumps(data, allow_nan=False)
        self.assertNotIn('map_tracker', self.record(HostileDict()))
        self.assertNotIn('map_tracker', display_record({'map_status': HostileDict()}, monotonic=10))

    def test_replay_preserves_diagnostics_but_cannot_forge_verification(self):
        tracker = self.tracker()
        tracker.update(replay=False, verified=True, projection_verified=True)
        data = self.record(tracker, replay=True)
        self.assertTrue(data['replay'])
        self.assertTrue(data['map_tracker']['replay'])
        self.assertEqual(data['map_tracker']['camera_address'], 0x200000)
        self.assertNotIn('verified', data['map_tracker'])
        self.assertNotIn('projection_verified', data['map_tracker'])
        self.assertIsNone(data['map_frame'])

    def test_missing_invalid_or_forged_top_level_tracker_is_not_copied(self):
        for snapshot in ({}, {'map_tracker': self.tracker()}, {'map_status': []},
                         {'map_status': {'tracker_status': None}}):
            self.assertNotIn('map_tracker', display_record(snapshot, monotonic=10))

    def test_recorder_module_import_is_inert(self):
        with patch('builtins.open', side_effect=AssertionError('file opened')), \
             patch('socket.socket', side_effect=AssertionError('socket opened')), \
             patch('threading.Thread.start', side_effect=AssertionError('thread started')), \
             patch('ctypes.WinDLL', side_effect=AssertionError('native library loaded'), create=True):
            importlib.reload(record_module)


class MapRecordTests(unittest.TestCase):
    def setup_sample(self):
        camera, snapshot = valid_frame_and_snapshot()
        snapshot.update(map_state='active', map_reason='map_camera_ready', replay=False)
        snapshot['rows'][0]['car'] = '2008 Mitsubishi Lancer Evolution X GSR'
        renderer = MapCanvasRenderer(FakeCanvas())
        renderer.draw(map_markers(snapshot, camera, 100), origin=(240, 80))
        return camera, snapshot, renderer

    def test_real_renderer_metadata_is_recorded_separately_from_radar(self):
        camera, snapshot, renderer = self.setup_sample()
        radar = {'visible': True, 'points': [{'pixel_x': 10, 'pixel_y': 20}], 'orientation': 'heading_up'}
        record = display_record(snapshot, radar, map_render=renderer.last_render, utc='utc', monotonic=100)
        self.assertEqual(record['map_state'], 'active')
        self.assertEqual(record['map_reason'], 'map_camera_ready')
        self.assertTrue(record['map_frame']['projection_verified'])
        self.assertEqual(record['map_frame']['matrix'], camera['matrix'])
        self.assertEqual(record['map_frame']['viewport'], camera['viewport'])
        point = record['map_render']['points'][0]
        self.assertEqual((point['pixel_x'], point['pixel_y']), (480, 420))
        self.assertEqual(point['level'], 4)
        self.assertEqual(point['car_id'], 1009)
        self.assertEqual(point['display_label'], "Mitsubishi Lancer Evolution X GSR '08")
        self.assertEqual(record['map_render']['visible_item_count'], 4)
        self.assertEqual(record['map_render']['pixel_space'], 'overlay_client_pixels')
        self.assertEqual(record['render']['points'][0]['pixel_x'], 10)
        record['map_frame']['matrix'][0] = 999
        record['map_render']['points'][0]['pixel_x'] = 999
        self.assertNotEqual(camera['matrix'][0], 999)
        self.assertNotEqual(renderer.last_render['points'][0]['pixel_x'], 999)

    def test_unverified_camera_is_diagnostic_only_and_records_actual_empty_canvas(self):
        camera, snapshot, renderer = self.setup_sample()
        camera['projection_verified'] = False
        snapshot.update(map_state='calibrating', map_reason='alignment_not_verified')
        renderer.draw(map_markers(snapshot, camera, 100))
        record = display_record(snapshot, map_render=renderer.last_render, monotonic=100)
        self.assertFalse(record['map_frame']['projection_verified'])
        self.assertEqual(record['map_frame']['source'], 'engine_camera')
        self.assertEqual(record['map_render']['points'], [])
        self.assertEqual(record['map_render']['visible_item_count'], 0)
        self.assertFalse(record['map_render']['projection_verified'])

    def test_invalidated_frame_and_canvas_do_not_carry_prior_map_data(self):
        camera, snapshot, renderer = self.setup_sample()
        first = display_record(snapshot, map_render=renderer.last_render, monotonic=100)
        self.assertTrue(first['map_render']['points'])
        snapshot.update(map_frame=None, map_state='seeking', map_reason='camera_clock_frozen')
        renderer.draw(map_markers(snapshot, None, 100.2))
        second = display_record(snapshot, map_render=renderer.last_render, monotonic=100.2)
        self.assertIsNone(second['map_frame'])
        self.assertEqual(second['map_render']['points'], [])
        self.assertEqual(second['map_render']['visible_item_count'], 0)
        self.assertFalse(second['map_render']['frame_available'])
        self.assertTrue(first['map_render']['points'])

    def test_replay_cannot_retain_production_verification_or_foreground_claim(self):
        camera, snapshot, renderer = self.setup_sample()
        snapshot['replay'] = True
        forged_preview = {'kind': 'map_preview_projection', 'preview': True, 'projection_verified': True,
                          'markers': [{'F': 123, 'pixel': [1, 2], 'verified': True, 'replay': False}]}
        record = display_record(snapshot, forged_preview, map_render=renderer.last_render, monotonic=100)
        self.assertTrue(record['replay'])
        self.assertEqual(record['map_frame']['source'], 'recorded')
        self.assertFalse(record['map_frame']['projection_verified'])
        self.assertFalse(record['map_frame']['game_foreground'])
        self.assertFalse(record['map_render']['projection_verified'])
        self.assertTrue(record['map_render']['replay'])
        self.assertFalse(record['render']['projection_verified'])
        self.assertFalse(record['render']['markers'][0]['verified'])
        self.assertTrue(record['render']['markers'][0]['replay'])

    def test_unknown_fields_raw_memory_and_objects_are_never_serialized(self):
        camera, snapshot, renderer = self.setup_sample()
        snapshot['raw_memory'] = b'game-dump'
        snapshot['scan_progress'] = {'attempted_mib': 3, 'raw_memory': b'game-dump'}
        snapshot['last_error'] = {'operation': 'read', 'raw_memory': b'game-dump'}
        camera['raw_memory'] = b'game-dump'
        camera['camera_object'] = object()
        canvas = deepcopy(renderer.last_render)
        canvas.update(raw_memory=b'game-dump', arbitrary_object=object())
        canvas['points'][0].update(raw_memory=b'game-dump', car=object())
        record = display_record(snapshot, {'raw_memory': b'game-dump'}, map_render=canvas, monotonic=100)
        encoded = json.dumps(record, allow_nan=False)
        self.assertNotIn('raw_memory', encoded)
        self.assertNotIn('game-dump', encoded)
        self.assertNotIn('camera_object', encoded)
        self.assertNotIn('arbitrary_object', encoded)
        self.assertNotIn('car', record['map_render']['points'][0])
        self.assertEqual(record['scan_progress'], {'attempted_mib': 3})

    def test_bad_scope_or_matrix_drops_source_frame_but_keeps_actual_render_diagnostics(self):
        camera, snapshot, renderer = self.setup_sample()
        for changes in ({'session_key': 'old-session'}, {'matrix': [1, 2]}, {'projection_verified': 1}):
            snapshot['map_frame'] = dict(camera, **changes)
            record = display_record(snapshot, map_render=renderer.last_render, monotonic=100)
            self.assertIsNone(record['map_frame'])
            self.assertFalse(record['map_render']['projection_verified'])
            self.assertFalse(record['map_render']['frame_available'])
            self.assertTrue(record['map_render']['points'])  # Diagnose residual drawing honestly.

    def test_frame_recorder_accepts_map_keyword_and_clearing_frame(self):
        camera, snapshot, renderer = self.setup_sample()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'map.jsonl'
            recorder = FrameRecorder(path)
            with patch('fh5_record.time.monotonic', side_effect=[100, 100.5]):
                self.assertTrue(recorder.record(snapshot, map_render=renderer.last_render))
                snapshot['map_frame'] = None
                renderer.draw(map_markers(snapshot, None, 100.5))
                self.assertTrue(recorder.record(snapshot, map_render=renderer.last_render))
            recorder.close()
            rows = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
        self.assertTrue(rows[0]['map_render']['points'])
        self.assertIsNone(rows[1]['map_frame'])
        self.assertEqual(rows[1]['map_render']['points'], [])


if __name__ == '__main__':
    unittest.main()
