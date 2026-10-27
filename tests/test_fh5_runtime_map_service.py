"""Runtime/map wiring using inert fake workers; no native calls, sockets, UI."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_app
import fh5_runtime as module

VERSION = module.SUPPORTED_VERSION
IDENTITY = {'pid': 123, 'session_key': '123:creation:fh5', 'image_path': 'fake/ForzaHorizon5.exe'}


def map_frame(**updates):
    frame = {'session_key': IDENTITY['session_key'], 'package_version': VERSION,
             'sample_monotonic': 100, 'activity_sample_monotonic': 99.99,
             'projection_verified': False, 'game_foreground': True,
             'map_open': True, 'source': 'engine_camera',
             'matrix': [1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, .5, 1],
             'viewport': {'x': 0, 'y': 80, 'width': 2560, 'height': 1440}}
    frame.update(updates)
    return frame


class FakeMapService:
    def __init__(self, getter, events, **kwargs):
        self.getter, self.events, self.kwargs = getter, events, kwargs
        self.start_calls = self.close_calls = 0
        self.keys = []
        self.frame = map_frame()
        self.on_snapshot = None

    def start(self):
        self.start_calls += 1
        self.events.append('map_start')
        return True

    def close(self):
        self.close_calls += 1
        self.events.append('map_close')
        self.frame = None
        return True

    def snapshot(self, key):
        self.keys.append(key)
        if self.on_snapshot:
            self.on_snapshot()
        return {'session_key': 'must-not-overwrite-store', 'rows': ['must-not-overwrite'],
                'map_state': 'calibrating', 'reason': 'alignment_not_verified',
                'map_frame': deepcopy(self.frame)}


class RuntimeMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.events, self.created = [], []

        def factory(getter, **kwargs):
            service = FakeMapService(getter, self.events, **kwargs)
            self.created.append(service)
            return service

        self.factory = factory
        self.runtime = module.Runtime(state_dir=self.directory / 'state',
                                      map_profile_path=self.directory / 'profile.json',
                                      map_service_factory=factory)
        self.runtime.store.set_session(IDENTITY, VERSION)
        self.runtime.store.publish_rows(IDENTITY['session_key'], [{'x': 5}])
        self.worker_patch = patch.object(module.threading, 'Thread', self.make_thread)
        self.worker_patch.start()
        self.clock_patch = patch.object(module.time, 'monotonic', return_value=100.0)
        self.clock_patch.start()

    def tearDown(self):
        self.runtime.close()
        self.clock_patch.stop()
        self.worker_patch.stop()
        self.temp.cleanup()

    def make_thread(self, target, daemon, name):
        events = self.events

        class Worker:
            def start(self):
                events.append('worker_start:' + name)

            def join(self, timeout=None):
                events.append('worker_join:' + name)

        return Worker()

    def test_constructor_does_not_create_service_thread_or_state_directory(self):
        self.assertEqual(self.created, [])
        self.assertEqual(self.runtime._threads, [])
        self.assertIsNone(self.runtime._map_service)
        self.assertFalse((self.directory / 'state').exists())
        self.assertIsNone(self.runtime.snapshot()['map_frame'])

    def test_start_once_uses_raw_store_getter_and_passes_profile(self):
        self.assertTrue(self.runtime.start())
        self.assertFalse(self.runtime.start())
        self.assertEqual(len(self.created), 1)
        service = self.created[0]
        self.assertEqual(service.start_calls, 1)
        self.assertIs(service.getter.__self__, self.runtime.store)
        self.assertNotIn('map_frame', service.getter())
        self.assertEqual(service.kwargs['profile_path'], self.directory / 'profile.json')
        self.assertEqual(service.kwargs['state_dir'], self.directory / 'state')
        self.assertEqual(len(self.runtime._threads), 2)

    def test_merge_keeps_main_session_rows_and_detaches_frame(self):
        self.runtime.start()
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot['session_key'], IDENTITY['session_key'])
        self.assertEqual(snapshot['rows'], [{'x': 5}])
        self.assertEqual(snapshot['map_state'], 'calibrating')
        self.assertEqual(snapshot['map_reason'], 'alignment_not_verified')
        self.assertFalse(snapshot['map_frame']['projection_verified'])
        snapshot['map_frame']['matrix'][0] = 999
        self.assertEqual(self.runtime.snapshot()['map_frame']['matrix'][0], 1)
        self.assertEqual(self.created[0].keys[-1], IDENTITY['session_key'])

    def test_runtime_defensively_drops_stale_future_or_wrong_scope(self):
        self.runtime.start()
        bad = ({'session_key': 'older'}, {'package_version': 'different'},
               {'sample_monotonic': 99.75}, {'sample_monotonic': 100.1},
               {'activity_sample_monotonic': 99}, {'sample_monotonic': True},
               {'sample_monotonic': float('nan')}, {'sample_monotonic': 10**1000})
        for changes in bad:
            self.created[0].frame = map_frame(**changes)
            snapshot = self.runtime.snapshot()
            self.assertIsNone(snapshot['map_frame'])
            self.assertEqual(snapshot['map_reason'], 'map_frame_stale_or_wrong_session')

    def test_source_change_during_map_merge_returns_latest_rows_and_no_camera(self):
        self.runtime.start()
        changed = dict(IDENTITY, pid=456, session_key='456:new:fh5')

        def swap():
            self.runtime.store.set_session(changed, VERSION)
            self.runtime.store.publish_rows(changed['session_key'], [{'x': 99}])

        self.created[0].on_snapshot = swap
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot['session_key'], changed['session_key'])
        self.assertEqual(snapshot['rows'], [{'x': 99}])
        self.assertIsNone(snapshot['map_frame'])
        self.assertEqual(snapshot['map_reason'], 'source_changed_while_merging_map')

    def test_unsupported_source_or_service_exception_is_hidden(self):
        self.runtime.start()
        self.runtime.store.set_session(IDENTITY, 'unsupported')
        self.assertIsNone(self.runtime.snapshot()['map_frame'])
        self.runtime.store.set_session(IDENTITY, VERSION)
        self.created[0].snapshot = Mock(side_effect=RuntimeError('synthetic service error'))
        snapshot = self.runtime.snapshot()
        self.assertIsNone(snapshot['map_frame'])
        self.assertEqual(snapshot['map_reason'], 'map_service_snapshot_failed')

    def test_map_factory_failure_does_not_start_native_map_or_break_old_workers(self):
        self.runtime._map_service_factory = Mock(side_effect=RuntimeError('synthetic startup error'))
        self.runtime.start()
        self.assertEqual(len(self.runtime._threads), 2)
        snapshot = self.runtime.snapshot()
        self.assertIsNone(snapshot['map_frame'])
        self.assertEqual(snapshot['map_reason'], 'map_service_start_failed')

    def test_ordinary_launch_status_preserves_camera_failure_diagnostics(self):
        self.runtime.start()
        diagnostics = {'reason': 'camera_discovery_in_progress',
                       'locator_read_bytes': 8 * 1024**2,
                       'locator_diagnostics': {'metadata_refresh_error': 'synthetic timeout'}}
        self.created[0].snapshot = Mock(return_value={
            'map_state': 'seeking', 'reason': 'camera_discovery_in_progress',
            'map_frame': None, 'tracker_status': diagnostics,
            'error': 'synthetic service diagnostic'})
        self.runtime._write_scan_status(force=True)
        path = self.directory / 'state' / 'scan_status.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(report['map_tracker'], diagnostics)
        self.assertEqual(report['map_error'], 'synthetic service diagnostic')
        self.assertEqual(report['fresh_row_count'], 1)
        self.assertNotIn('map_frame', report)
        self.assertNotIn('rows', report)

    def test_close_stops_map_before_worker_join_and_is_idempotent(self):
        self.runtime.start()
        self.runtime.close()
        self.runtime.close()
        service = self.created[0]
        self.assertEqual(service.close_calls, 1)
        joins = [i for i, value in enumerate(self.events) if value.startswith('worker_join:')]
        self.assertTrue(joins)
        self.assertLess(self.events.index('map_close'), min(joins))
        snapshot = self.runtime.snapshot()
        self.assertIsNone(snapshot['session_key'])
        self.assertIsNone(snapshot['map_frame'])
        self.assertTrue(snapshot['map_shutdown_complete'])
        self.assertFalse(self.runtime.start())

    def test_map_close_failure_still_joins_other_workers(self):
        self.runtime.start()
        self.created[0].close = Mock(side_effect=RuntimeError('synthetic close error'))
        self.runtime.close()
        self.assertEqual(sum(value.startswith('worker_join:') for value in self.events), 2)
        self.assertFalse(self.runtime.snapshot()['map_shutdown_complete'])


class AppMapProfileTests(unittest.TestCase):
    def test_cli_parses_profile_as_path_without_starting_a_provider(self):
        seen = []
        with patch.object(fh5_app.fh5_config, 'load', return_value={'port': 53005}), \
             patch.object(fh5_app, '_runtime', side_effect=lambda args, config: seen.append(args) or object()), \
             patch.object(fh5_app, 'headless', return_value=17):
            self.assertEqual(fh5_app.main(['--headless', '--map-profile', 'profile.json', '--seconds', '.1']), 17)
        self.assertEqual(seen[0].map_profile, Path('profile.json'))

    def test_live_factory_receives_profile_and_replay_does_not_construct_runtime(self):
        args = Namespace(replay=None, port=None, seed_scope=None, mapping=None, map_profile=Path('verified.json'))
        with patch.object(module, 'Runtime', return_value='fake-provider') as factory:
            self.assertEqual(fh5_app._runtime(args, {'port': 53005}), 'fake-provider')
            self.assertEqual(factory.call_args.kwargs['map_profile_path'], Path('verified.json'))
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            report.write_text(json.dumps({'source_identity': IDENTITY, 'samples': [{'candidates': []}]}), encoding='utf-8')
            args.replay = str(report)
            with patch.object(module, 'Runtime', side_effect=AssertionError('live runtime')), \
                 patch.object(module, 'Process', side_effect=AssertionError('native')), \
                 patch.object(module.socket, 'socket', side_effect=AssertionError('socket')), \
                 patch('fh5_map_service.MapService', side_effect=AssertionError('map service')):
                replay = fh5_app._runtime(args, {'port': 53005})
                replay.start()
                self.assertTrue(replay.snapshot()['replay'])
                replay.close()

    def test_live_default_profile_is_resource_file_without_reading_it(self):
        args = Namespace(replay=None, port=None, seed_scope=None, mapping=None, map_profile=None)
        with patch('fh5_paths.RESOURCE_DIR', Path('fake-resources')), \
             patch.object(module, 'Runtime', return_value='fake-provider') as factory, \
             patch('builtins.open', side_effect=AssertionError('profile must be loaded by service later')):
            self.assertEqual(fh5_app._runtime(args, {'port': 53005}), 'fake-provider')
            self.assertEqual(factory.call_args.kwargs['map_profile_path'],
                             Path('fake-resources/data/fh5_map_profile.json'))

    def test_offline_map_preview_bypasses_config_runtime_and_profile_file(self):
        run_preview = Mock(return_value=9)
        with patch.dict(sys.modules, {'fh5_map_view': SimpleNamespace(run_preview=run_preview)}), \
             patch.object(fh5_app.fh5_config, 'load', side_effect=AssertionError('config')), \
             patch.object(fh5_app, '_runtime', side_effect=AssertionError('provider')):
            self.assertEqual(fh5_app.main(['--map-preview', '--headless', '--map-profile', 'missing.json']), 9)
        self.assertTrue(run_preview.call_args.kwargs['headless'])


if __name__ == '__main__':
    unittest.main()
