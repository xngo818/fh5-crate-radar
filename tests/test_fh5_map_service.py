"""Map service lifecycle checks using only fake process/camera/window objects."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_map_service as service_module
from fh5_map_service import MapService

VERSION = '3.688.109.0'


def source(key='one', pid=1, version=VERSION):
    return {'session_key': key, 'process_pid': pid, 'game_version': version}


def candidate(key='one', now=100):
    return {'session_key': key, 'package_version': VERSION,
            'sample_monotonic': now, 'activity_sample_monotonic': now - .05,
            'render_width': 2560, 'render_height': 1440, 'camera_address': 0x12345000,
            'flattened_matrix': [1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, .5, 1],
            'projection_verified': False}


def profile(**changes):
    value = {'schema_version': 1, 'package_version': VERSION,
             'alignment_verified': True, 'evidence': ['field-check.json'],
             'render_width': 2560, 'render_height': 1440,
             'client_width': 2560, 'client_height': 1440, 'dpi': 120,
             'coordinate_space': 'physical_screen_pixels',
             'mode': 'client_equals_render'}
    value.update(changes)
    return value


class FakeProcess:
    def __init__(self, pid, key):
        self.identity = {'pid': pid, 'session_key': key}
        self.alive, self.close_count = True, 0

    def is_alive(self):
        return self.alive

    def close(self):
        self.close_count += 1
        self.alive = False


class FakeTracker:
    def __init__(self, sample):
        self.sample = sample
        self.reason, self.status = 'camera_waiting', {'projection_verified': False}
        self.on_step = None

    def step(self, process, now=None):
        if self.on_step:
            self.on_step()
        return deepcopy(self.sample)


class FakeGeometry:
    def __init__(self):
        self.result = {'render_viewport': {'x': 0, 'y': 80, 'width': 2560, 'height': 1440},
                       'client_rect': {'x': 0, 'y': 80, 'width': 2560, 'height': 1440},
                       'game_foreground': True, 'dpi': 120,
                       'render_width': 2560, 'render_height': 1440,
                       'sample_monotonic': 100.0,
                       'coordinate_space': 'physical_screen_pixels'}

    def sample(self, w, h):
        return deepcopy(self.result)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.current = source()
        self.opened = []
        self.tracker = FakeTracker(candidate())
        self.geometry = FakeGeometry()

        def open_process(pid):
            process = FakeProcess(pid, self.current['session_key'])
            self.opened.append(process)
            return process

        self.service = MapService(lambda: deepcopy(self.current), open_process,
                                  lambda pid: self.geometry)
        self.service._new_tracker = lambda key: self.tracker
        self.service._clock = lambda: 100.0

    def tearDown(self):
        self.service.close()

    def test_constructor_is_inert(self):
        with patch.object(service_module, 'Process', side_effect=AssertionError('process')):
            instance = MapService(lambda: (_ for _ in ()).throw(AssertionError('getter')),
                                  profile_path='does-not-exist.json')
            self.assertIsNone(instance._thread)
            self.assertIsNone(instance._process)
            instance.close()

    def test_default_frame_is_unverified_and_detached(self):
        self.service._cycle(now=100)
        result = self.service.snapshot('one', now=100)
        self.assertEqual(result['map_state'], 'calibrating')
        self.assertFalse(result['map_frame']['projection_verified'])
        self.assertTrue(result['map_frame']['game_foreground'])
        self.assertEqual(result['map_frame']['viewport']['y'], 80)
        result['map_frame']['matrix'][0] = 999
        self.assertEqual(self.service.snapshot('one', now=100)['map_frame']['matrix'][0], 1)

    def test_ttl_scope_future_and_invalid_clock_fail_closed(self):
        self.service._cycle(now=100)
        for key, now in [('two', 100), ('one', 100.26), ('one', 99.99), ('one', float('nan')), ('one', True)]:
            with self.subTest(key=key, now=now):
                self.assertIsNone(self.service.snapshot(key, now=now)['map_frame'])

    def test_activity_ttl_is_independent(self):
        self.tracker.sample['activity_sample_monotonic'] = 99.74
        self.service._cycle(now=100)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.assertEqual(self.service.snapshot('one', now=100)['reason'], 'camera_sample_or_activity_stale')

    def test_no_session_or_unsupported_version_never_opens(self):
        for key, pid, version in [('', 1, VERSION), ('one', True, VERSION), ('one', 1, 'unknown')]:
            self.current = source(key, pid, version)
            self.service._cycle(now=100)
            self.assertEqual(self.opened, [])
            self.assertIsNone(self.service.snapshot(key, now=100)['map_frame'])

    def test_version_revoked_before_next_cycle_already_hides_frame(self):
        self.service._cycle(now=100)
        self.current['game_version'] = 'other'
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.service._cycle(now=100)
        self.assertEqual(self.opened[0].close_count, 1)

    def test_pid_change_before_next_cycle_hides_prior_frame(self):
        self.service._cycle(now=100)
        self.current['process_pid'] = 2
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])

    def test_process_death_and_missing_candidate_clear_prior_frame(self):
        self.service._cycle(now=100)
        self.tracker.sample = None
        self.service._cycle(now=100)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.assertEqual(self.service.snapshot('one', now=100)['reason'], 'camera_waiting')
        self.opened[0].alive = False
        self.service._cycle(now=100)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.assertEqual(self.opened[0].close_count, 1)

    def test_session_change_closes_handle_and_rebuilds_tracker(self):
        keys = []
        self.service._new_tracker = lambda key: keys.append(key) or self.tracker
        self.service._cycle(now=100)
        self.current = source('two', 2)
        self.tracker.sample = candidate('two')
        self.service._cycle(now=100)
        self.assertEqual(keys, ['one', 'two'])
        self.assertEqual(self.opened[0].close_count, 1)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.assertEqual(self.service.snapshot('two', now=100)['map_frame']['session_key'], 'two')

    def test_opened_identity_mismatch_is_closed_without_tracking(self):
        process = FakeProcess(1, 'different-creation-time')
        self.service._process_factory = lambda pid: process
        self.service._new_tracker = lambda key: self.fail('tracker must not start')
        self.service._cycle(now=100)
        self.assertEqual(process.close_count, 1)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])

    def test_source_change_mid_sample_discards_and_closes(self):
        self.tracker.on_step = lambda: self.current.update(session_key='two', process_pid=2)
        self.service._cycle(now=100)
        self.assertEqual(self.opened[0].close_count, 1)
        self.assertIsNone(self.service.snapshot('two', now=100)['map_frame'])

    def test_geometry_failure_clears_prior_frame(self):
        self.service._cycle(now=100)
        self.geometry.result = None
        self.service._cycle(now=100)
        result = self.service.snapshot('one', now=100)
        self.assertIsNone(result['map_frame'])
        self.assertEqual(result['reason'], 'window_geometry_unavailable')

    def test_background_frame_is_explicitly_not_foreground(self):
        self.geometry.result['game_foreground'] = False
        self.service._cycle(now=100)
        result = self.service.snapshot('one', now=100)
        self.assertEqual(result['map_state'], 'background')
        self.assertFalse(result['map_frame']['game_foreground'])

    def test_invalid_candidate_and_matrix_fail_closed(self):
        for changes in [{'session_key': 'other'}, {'package_version': 'other'},
                        {'flattened_matrix': [1, 2]}, {'sample_monotonic': True}]:
            self.tracker.sample = dict(candidate(), **changes)
            self.service._cycle(now=100)
            self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])

    def test_profile_requires_explicit_true_exact_version_and_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'profile.json'
            self.service.profile_path = path
            good = profile()
            for item in [{}, dict(good, alignment_verified=1), dict(good, package_version='other'),
                            dict(good, evidence=[]), dict(good, evidence=''), dict(good, evidence=[True]),
                            good]:
                path.write_text(json.dumps(item), encoding='utf-8')
                self.service._cycle(now=100)
                verified = self.service.snapshot('one', now=100)['map_frame']['projection_verified']
                self.assertEqual(verified, item == good and type(item.get('alignment_verified')) is bool)
            path.write_text('{malformed', encoding='utf-8')
            self.service._cycle(now=100)
            self.assertFalse(self.service.snapshot('one', now=100)['map_frame']['projection_verified'])

    def test_profile_authorizes_only_measured_display_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'profile.json'
            path.write_text(json.dumps(profile()), encoding='utf-8')
            self.service.profile_path = path
            cases = [
                ({'dpi': 96}, 'alignment_profile_dpi_mismatch'),
                ({'coordinate_space': 'logical_pixels'}, 'alignment_geometry_coordinate_space_mismatch'),
                ({'render_width': 1920}, 'alignment_geometry_render_size_mismatch'),
                ({'client_rect': {'x': 0, 'y': 80, 'width': 1920, 'height': 1080}},
                 'alignment_profile_client_size_mismatch'),
                ({'render_viewport': {'x': 50, 'y': 80, 'width': 2460, 'height': 1440}},
                 'alignment_profile_viewport_not_client_1to1')]
            for changes, expected in cases:
                self.geometry.result = dict(FakeGeometry().result, **changes)
                self.service._cycle(now=100)
                result = self.service.snapshot('one', now=100)
                self.assertEqual(result['map_state'], 'calibrating')
                self.assertEqual(result['reason'], expected)
                self.assertFalse(result['map_frame']['projection_verified'])
            self.geometry.result = FakeGeometry().result
            self.tracker.sample = dict(candidate(), render_width=1920, render_height=1080)
            self.service._cycle(now=100)
            self.assertEqual(self.service.snapshot('one', now=100)['reason'], 'alignment_profile_render_size_mismatch')
            self.tracker.sample = candidate()
            self.geometry.result['client_rect']['x'] = -2560
            self.geometry.result['render_viewport']['x'] = -2560
            self.service._cycle(now=100)
            result = self.service.snapshot('one', now=100)
            self.assertEqual(result['map_state'], 'active')
            self.assertTrue(result['map_frame']['projection_verified'])
            self.assertEqual(result['map_frame']['viewport']['x'], -2560)

    def test_schema_legacy_and_unmeasured_mode_are_rejected(self):
        for changes, expected in [
                ({'schema_version': None}, 'alignment_profile_schema_unsupported'),
                ({'schema_version': True}, 'alignment_profile_schema_unsupported'),
                ({'mode': 'aspect_fit_centered'}, 'alignment_profile_mode_unsupported'),
                ({'coordinate_space': 'logical_pixels'}, 'alignment_profile_coordinate_space_unsupported'),
                ({'client_width': 1920}, 'alignment_profile_mode_dimensions_inconsistent'),
                ({'render_width': True}, 'alignment_profile_dimensions_invalid'),
                ({'dpi': True}, 'alignment_profile_dpi_invalid')]:
            with self.subTest(changes=changes):
                allowed, reason = service_module.authorize_alignment_profile(
                    profile(**changes), VERSION, candidate(), self.geometry.result)
                self.assertFalse(allowed)
                self.assertEqual(reason, expected)

    def test_stale_geometry_cannot_authorize_or_publish(self):
        self.geometry.result['sample_monotonic'] = 99.74
        self.service._cycle(now=100)
        result = self.service.snapshot('one', now=100)
        self.assertIsNone(result['map_frame'])
        self.assertEqual(result['reason'], 'window_geometry_stale')

    def test_close_clears_and_closes_owned_handle_once(self):
        self.service._cycle(now=100)
        self.assertTrue(self.service.close())
        self.assertTrue(self.service.close())
        self.assertEqual(self.opened[0].close_count, 1)
        self.assertIsNone(self.service.snapshot('one', now=100)['map_frame'])
        self.assertFalse(self.service.start())

    def test_start_is_single_thread_and_close_joins_and_cleans(self):
        touched = threading.Event()
        original = self.service._cycle

        def cycle():
            original(now=100)
            touched.set()

        self.service._cycle = cycle
        self.assertTrue(self.service.start())
        self.assertFalse(self.service.start())
        self.assertTrue(touched.wait(1))
        self.assertTrue(self.service.close())
        self.assertFalse(self.service._thread.is_alive())
        self.assertEqual(self.opened[0].close_count, 1)

    def test_only_fresh_authorized_foreground_active_state_uses_16ms(self):
        self.service._cycle(now=100)
        frame = deepcopy(self.service._state['map_frame'])
        frame['projection_verified'] = True
        self.service._publish('active', 'map_camera_ready', frame)
        self.assertEqual(self.service._sample_interval(now=100), .016)
        for state, changes, now in [('calibrating', {}, 100), ('background', {}, 100),
                                    ('seeking', {}, 100), ('active', {'game_foreground': False}, 100),
                                    ('active', {'projection_verified': False}, 100),
                                    ('active', {}, 100.3)]:
            with self.subTest(state=state, changes=changes):
                self.service._publish(state, 'test', dict(frame, **changes))
                self.assertEqual(self.service._sample_interval(now=now), .05)

    def test_scanning_uses_fast_bounded_burst_without_relabeling_wait_as_success(self):
        status = {'reason': 'camera_discovery_in_progress',
                  'timing': {'search_elapsed_seconds': 0.0}}
        self.service._publish('seeking', 'camera_discovery_in_progress', tracker_status=status)
        self.assertEqual(self.service._sample_interval(now=100), .005)
        status['timing']['search_elapsed_seconds'] = 20.0
        self.service._publish('seeking', 'camera_discovery_in_progress', tracker_status=status)
        self.assertEqual(self.service._sample_interval(now=120), .05)
        status['timing']['search_elapsed_seconds'] = 1.0
        status['reason'] = 'activity_waiting_second_distinct_sample'
        self.service._publish('seeking', status['reason'], tracker_status=status)
        self.assertEqual(self.service._sample_interval(now=101), .05)
        self.assertIsNone(self.service._state['map_frame'])

    def test_timing_distinguishes_camera_from_authorized_foreground_frame_and_marker_visibility(self):
        self.tracker.sample = None
        self.service._cycle(now=100)
        result = self.service.snapshot('one', now=100)
        self.assertEqual(result['map_timing']['session_started_monotonic'], 100)
        self.assertIsNone(result['map_timing']['first_camera_ready_monotonic'])
        self.tracker.sample = candidate(now=101)
        self.geometry.result['sample_monotonic'] = 101
        self.service._cycle(now=101)
        timing = self.service.snapshot('one', now=101)['map_timing']
        self.assertEqual(timing['first_camera_ready_monotonic'], 101)
        self.assertIsNone(timing['first_authorized_foreground_frame_monotonic'])
        self.service._authorized = lambda *args: (True, 'alignment_profile_mode_verified')
        self.tracker.sample = candidate(now=102)
        self.geometry.result['sample_monotonic'] = 102
        self.service._cycle(now=102)
        timing = self.service.snapshot('one', now=102)['map_timing']
        self.assertEqual(timing['first_authorized_foreground_frame_monotonic'], 102)
        self.assertFalse(timing['actual_marker_visibility_measured'])


class MapScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'map_camera_scope.json'
        self.current = source()
        self.opened, self.tracker_inputs = [], []
        self.tracker = FakeTracker(candidate())
        self.tracker.locator = SimpleNamespace(hot_regions=set(), regions=[])

        def open_process(pid):
            process = FakeProcess(pid, self.current['session_key'])
            self.opened.append(process)
            return process

        self.service = MapService(lambda: deepcopy(self.current), open_process,
                                  lambda pid: FakeGeometry(), state_dir=self.directory)
        self.service._clock = lambda: 100.0

        def tracker_factory(key):
            self.tracker_inputs.append((key, tuple(self.service._hot_regions)))
            return self.tracker

        self.service._new_tracker = tracker_factory

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def cache(self, **changes):
        document = {'schema_version': 1, 'session_key': 'one', 'package_version': VERSION,
                    'hot_regions': [0x12000000, 0x20000000]}
        document.update(changes)
        self.path.write_text(json.dumps(document), encoding='utf-8')
        return document

    def test_matching_scope_supplies_hints_only_after_open_identity_is_verified(self):
        self.cache()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.tracker_inputs, [])
        self.service._cycle(now=100)
        self.assertEqual(self.tracker_inputs, [('one', (0x12000000, 0x20000000))])
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(self.service.snapshot('one', now=100)['map_frame']['projection_verified'])

    def test_wrong_session_version_schema_and_region_range_are_ignored(self):
        for bad in ({'session_key': 'older'}, {'package_version': 'older'}, {'schema_version': True},
                    {'hot_regions': [0]}, {'hot_regions': [-4096]}, {'hot_regions': [0x10001]},
                    {'hot_regions': [True]}, {'hot_regions': [1.0]}, {'hot_regions': ['0x12000000']},
                    {'hot_regions': [0x800000000000]}, {'hot_regions': [0x12000000] * 17},
                    {'hot_regions': {'base': 0x12000000}}):
            with self.subTest(bad=bad):
                self.cache(**bad)
                self.service._drop_process()
                self.service._cycle(now=100)
                self.assertEqual(self.tracker_inputs[-1], ('one', ()))

    def test_damaged_or_oversized_file_is_ignored(self):
        for raw in ('{malformed', '[]', 'x' * 65537):
            with self.subTest(length=len(raw)):
                self.path.write_text(raw, encoding='utf-8')
                self.service._drop_process()
                self.service._cycle(now=100)
                self.assertEqual(self.tracker_inputs[-1], ('one', ()))

    def test_lower_valid_page_and_duplicates_are_canonicalized(self):
        self.cache(hot_regions=[0x10000, 0x12000000, 0x12000000])
        self.service._cycle(now=100)
        self.assertEqual(self.tracker_inputs[-1], ('one', (0x10000, 0x12000000)))

    def test_invalid_opened_handle_never_uses_scope(self):
        self.cache()
        process = FakeProcess(1, 'different-process-creation')
        self.service._process_factory = lambda pid: process
        self.service._cycle(now=100)
        self.assertEqual(self.tracker_inputs, [])
        self.assertEqual(process.close_count, 1)

    def test_active_candidate_learns_atomically_and_throttles_only_changed_hints(self):
        self.tracker.locator.hot_regions = {0x12000000}
        self.tracker.locator.regions = [{'base': 0x12000000, 'size': 0x1000000}]
        self.service._cycle(now=100)
        first = json.loads(self.path.read_text('utf-8'))
        self.assertEqual(first['session_key'], 'one')
        self.assertEqual(first['package_version'], VERSION)
        self.assertEqual(first['hot_regions'], [0x12000000])
        self.assertNotIn('camera_address', first)
        self.assertEqual(list(self.directory.glob('.map_camera_scope.*.tmp')), [])
        self.tracker.locator.hot_regions.add(0x20000000)
        self.tracker.sample = candidate(now=100.4)
        self.service._cycle(now=100.4)
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['hot_regions'], [0x12000000])
        self.tracker.sample = candidate(now=101.1)
        self.service._cycle(now=101.1)
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['hot_regions'],
                         [0x12000000, 0x20000000])
        self.tracker.sample = candidate(now=102.2)
        with patch.object(Path, 'replace', side_effect=AssertionError('unchanged hints must not write')):
            self.service._cycle(now=102.2)

    def test_unproven_or_stale_candidate_cannot_create_scope(self):
        self.tracker.locator.hot_regions = {0x12000000}
        for sample in (None, dict(candidate(), activity_sample_monotonic=99),
                       dict(candidate(), session_key='other'),
                       dict(candidate(), flattened_matrix=[1, 2])):
            self.tracker.sample = sample
            self.service._cycle(now=100)
            self.assertFalse(self.path.exists())

    def test_learned_region_count_is_bounded_and_active_region_is_first(self):
        self.tracker.locator.hot_regions = {0x100000 + index * 4096 for index in range(20)} | {0x12300000}
        self.tracker.locator.regions = [{'base': 0x12300000, 'size': 0x100000}]
        self.service._cycle(now=100)
        regions = json.loads(self.path.read_text('utf-8'))['hot_regions']
        self.assertEqual(len(regions), 16)
        self.assertEqual(regions[0], 0x12300000)

    def test_session_change_drops_hints_and_close_preserves_file_for_same_session_restart(self):
        self.cache()
        self.service._cycle(now=100)
        self.current = source('two', 2)
        self.tracker.sample = candidate('two')
        self.service._cycle(now=100)
        self.assertEqual(self.tracker_inputs[-1], ('two', ()))
        self.assertEqual(self.opened[0].close_count, 1)
        self.service.close()
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['session_key'], 'one')

    def test_constructor_with_state_directory_performs_no_file_io(self):
        with patch.object(Path, 'stat', side_effect=AssertionError('stat')), \
             patch.object(Path, 'open', side_effect=AssertionError('read')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir')):
            fresh = MapService(lambda: {}, state_dir=self.directory)
            fresh.close()


if __name__ == '__main__':
    unittest.main()
