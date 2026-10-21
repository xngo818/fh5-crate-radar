"""Offline provider checks: fake memory only, no native APIs, sockets or UI."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_runtime as runtime_module


VERSION = runtime_module.SUPPORTED_VERSION
IDENTITY = {'pid': 123, 'session_key': '123:creation:a.exe',
            'image_path': 'fake/ForzaHorizon5.exe'}


class FakeProcess:
    def __init__(self, base=0x100000, size=2 * 512 * 1024 + 64):
        self.base, self.size = base, size
        self.memory = bytearray(size)
        self.heap_base, self.heap = 0x800000, b'pickup_position_ref_component'
        self.identity = dict(IDENTITY)
        self.closed = False
        self.regions_calls = 0
        self.big_reads = []

    def read(self, address, size):
        if size >= 65536:
            self.big_reads.append((address, size))
        if self.base <= address and address + size <= self.base + self.size:
            offset = address - self.base
            return bytes(self.memory[offset:offset + size])
        if self.heap_base <= address and address + size <= self.heap_base + len(self.heap):
            offset = address - self.heap_base
            return self.heap[offset:offset + size]
        return None

    def regions(self, **kwargs):
        self.regions_calls += 1
        return [{'base': self.base, 'size': self.size}]

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def install_signature(self, first):
        for index, marker in enumerate((self.heap, b'flare_group', b'pickup_model')):
            descriptor = bytearray(32)
            if len(marker) > 15:
                struct.pack_into('<Q', descriptor, 0, self.heap_base)
                capacity = 31
            else:
                descriptor[:len(marker)] = marker
                capacity = 15
            struct.pack_into('<QQ', descriptor, 16, len(marker), capacity)
            offset = first - self.base + index * 32
            self.memory[offset:offset + 32] = descriptor


class OneLargeReadPerStep:
    def __init__(self, process, stop, **kwargs):
        self.process, self.used = process, False

    def __call__(self, address, size):
        if size >= 65536:
            if self.used:
                raise runtime_module.StepEnded()
            self.used = True
        return self.process.read(address, size)


class LocatorTests(unittest.TestCase):
    def test_signature_crossing_chunk_boundary_survives_next_step(self):
        process = FakeProcess()
        # Third inline string starts eight bytes before the chunk boundary.
        first = process.base + 512 * 1024 - 8 - 64
        process.install_signature(first)
        locator = runtime_module.Locator()
        found = []
        with patch.object(runtime_module, 'LimitedRead', OneLargeReadPerStep):
            for _ in range(8):
                candidates, progress = locator.step(process, threading.Event())
                found.extend(candidates)
                if progress['complete_selected_policy']:
                    break
        self.assertTrue(progress['complete_selected_policy'])
        self.assertEqual(process.regions_calls, 1)
        self.assertIn(first, [candidate['first_string_address'] for candidate in found])
        self.assertEqual(process.big_reads[1][0], process.base + 512 * 1024 - 32)
        self.assertFalse(progress['complete_world_coverage'])

    def test_chunk_validation_deadline_retries_instead_of_skipping(self):
        process = FakeProcess(size=512 * 1024)
        locator = runtime_module.Locator()
        candidate = {'first_string_address': process.base + 80}

        def interrupted(*args):
            yield candidate
            raise runtime_module.StepEnded()

        with patch.object(runtime_module, 'candidates_from_chunk', interrupted):
            found, progress = locator.step(process, threading.Event())
        self.assertEqual(found, [candidate])
        self.assertEqual(locator.index, 0)
        self.assertIsNone(locator.address)
        with patch.object(runtime_module, 'candidates_from_chunk', lambda *args: iter([candidate])):
            found, progress = locator.step(process, threading.Event())
        self.assertEqual(found, [candidate])
        self.assertTrue(progress['complete_selected_policy'])
        self.assertEqual([address for address, size in process.big_reads], [process.base, process.base])


class CandidatePoolTests(unittest.TestCase):
    def pool(self):
        pool = runtime_module.CandidatePool(IDENTITY['session_key'], VERSION)
        pool.register([{'first_string_address': 0x200000}, {'first_string_address': 0x200008}])
        return pool

    def test_refresh_uses_known_slots_without_pool_scan_and_resumes_after_deadline(self):
        pool, process = self.pool(), FakeProcess()
        calls = []

        def read(reader, first, session):
            calls.append(first)
            if len(calls) == 2:
                raise runtime_module.StepEnded()
            return {'row': {'first_string_address': first}}

        with patch.object(runtime_module, 'read_candidate', read), \
             patch.object(runtime_module, 'decorate_candidate', lambda raw, *args: raw.get('row')), \
             patch.object(runtime_module, 'candidates_from_chunk', side_effect=AssertionError('unnecessary scan')):
            first = pool.refresh(process, threading.Event(), None)
            second = pool.refresh(process, threading.Event(), None)
        self.assertTrue(first['limited'])
        self.assertEqual(first['rows'], [{'first_string_address': 0x200000}])
        self.assertEqual(calls, [0x200000, 0x200008, 0x200008, 0x200000])
        self.assertEqual(len(second['rows']), 2)
        self.assertFalse(second['limited'])

    def test_rejected_slot_is_removed_without_removing_successful_slots(self):
        pool, process = self.pool(), FakeProcess()

        def read(reader, first, session):
            return ({'reason': 'wrong_tag_backref'} if first == 0x200000
                    else {'row': {'first_string_address': first}})

        with patch.object(runtime_module, 'read_candidate', read), \
             patch.object(runtime_module, 'decorate_candidate', lambda raw, *args: raw.get('row')):
            sample = pool.refresh(process, threading.Event(), None)
        self.assertEqual(sample['removed'], [0x200000])
        self.assertEqual(pool.addresses, {0x200008})
        self.assertEqual(sample['rows'], [{'first_string_address': 0x200008}])

    def test_all_limited_visits_do_not_invent_empty_success_or_advance_cursor(self):
        pool = self.pool()
        with patch.object(runtime_module, 'read_candidate', side_effect=runtime_module.StepEnded):
            sample = pool.refresh(FakeProcess(), threading.Event(), None)
        self.assertTrue(sample['limited'])
        self.assertEqual((sample['rows'], sample['removed']), ([], []))
        self.assertIsNone(pool.cursor)
        self.assertEqual(len(pool.addresses), 2)

    def test_identity_race_retries_then_accepts_only_second_complete_sample(self):
        pool = self.pool()
        pool.addresses = {0x200000}
        with patch.object(runtime_module, 'read_candidate', side_effect=[
                {'reason': 'identity_changed_during_read'}, {'row': {'first_string_address': 0x200000}}]), \
             patch.object(runtime_module, 'decorate_candidate', lambda raw, *args: raw.get('row')):
            sample = pool.refresh(FakeProcess(), threading.Event(), None)
        self.assertEqual(sample['rows'], [{'first_string_address': 0x200000}])
        self.assertFalse(sample['removed'])

    def test_persistent_identity_race_hides_old_row_and_bounds_retry_slots(self):
        pool = self.pool()
        pool.addresses = {0x200000}
        with patch.object(runtime_module, 'read_candidate', return_value={'reason': 'identity_changed_during_read'}), \
             patch.object(runtime_module, 'decorate_candidate', return_value=None):
            first = pool.refresh(FakeProcess(), threading.Event(), None)
            self.assertEqual(first['removed'], [0x200000])
            self.assertEqual(pool.addresses, {0x200000})
            pool.refresh(FakeProcess(), threading.Event(), None)
            pool.refresh(FakeProcess(), threading.Event(), None)
        self.assertEqual(pool.addresses, set())

    def test_other_session_or_version_cannot_use_slot_index(self):
        pool = self.pool()
        process = FakeProcess()
        process.identity['session_key'] = 'different'
        with self.assertRaises(ValueError):
            pool.refresh(process, threading.Event(), None)
        pool.version = 'different'
        with self.assertRaises(ValueError):
            pool.refresh(FakeProcess(), threading.Event(), None)


class ProviderTests(unittest.TestCase):
    def make_runtime(self, directory, **kwargs):
        return runtime_module.Runtime(state_dir=directory,
            seed_scope=Path(directory) / 'seed.json',
            mapping_path=Path(directory) / 'mapping.json', **kwargs)

    def test_scope_cache_never_crosses_session_or_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            cached = Path(directory) / 'pool_scope.json'
            seed = Path(directory) / 'seed.json'
            scope = {'session_key': IDENTITY['session_key'],
                     'package_version': VERSION, 'ranges': [[0x200000, 0x202000]]}
            seed.write_text(json.dumps(scope), encoding='utf-8')
            cached.write_text(json.dumps(dict(scope, package_version='other')), encoding='utf-8')
            self.assertEqual(runtime._load_scope(IDENTITY, VERSION), scope['ranges'])
            self.assertEqual(runtime._load_scope(dict(IDENTITY, session_key='another'), VERSION), [])
            self.assertEqual(runtime._load_scope(IDENTITY, 'other-build'), [])
            scope['ranges'] = [[0x200000, 0xB00000]]
            seed.write_text(json.dumps(scope), encoding='utf-8')
            self.assertEqual(runtime._load_scope(IDENTITY, VERSION), [])

    def test_partial_pool_refresh_returns_no_partial_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            process = FakeProcess()

            def partial(*args):
                yield {'first_string_address': process.base + 96}
                raise runtime_module.StepEnded()

            with patch.object(runtime_module, 'candidates_from_chunk', partial):
                rows, gaps, limited = runtime._read_pool(process,
                    [[process.base, process.base + 4096]], VERSION)
            self.assertEqual(rows, [])
            self.assertTrue(limited)

    def test_memory_worker_keeps_unvisited_sample_on_deadline_then_removes_rejected_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            process = FakeProcess()
            runtime = self.make_runtime(directory,
                process_factory=lambda pid: process,
                process_finder=lambda: [{'pid': IDENTITY['pid']}])
            snapshots, visits = [], []
            publish = runtime.store.publish_row_updates

            def record(*args, **kwargs):
                result = publish(*args, **kwargs)
                snapshots.append(runtime.store.snapshot()['rows'])
                return result

            def refresh(*args, **kwargs):
                visits.append(True)
                index = len(visits)
                if index == 3:
                    runtime.stop.set()
                return {'rows': ([{'first_string_address': 0x200000, 'x': 1},
                                  {'first_string_address': 0x200008, 'x': 2}] if index == 1 else
                                 [{'first_string_address': 0x200008, 'x': 3}] if index == 3 else []),
                        'removed': [0x200000] if index == 3 else [], 'limited': index == 2,
                        'sample_monotonic': runtime_module.time.monotonic(),
                        'read_gaps': 0, 'elapsed_seconds': 0, 'known_slots': 2, 'visited_slots': 0}

            with patch.object(runtime_module, 'package_version', return_value=VERSION), \
                 patch.object(runtime, '_load_scope', return_value=[[0x200000, 0x201000]]), \
                 patch.object(runtime_module.Locator, 'step', return_value=(
                     [{'first_string_address': 0x200000}, {'first_string_address': 0x200008}], {})), \
                 patch.object(runtime_module.CandidatePool, 'refresh', side_effect=refresh), \
                 patch.object(runtime.store, 'publish_row_updates', side_effect=record), \
                 patch.object(runtime.wake, 'wait', return_value=False):
                runtime._memory_loop()
            self.assertEqual(len(snapshots), 3)
            self.assertEqual(snapshots[1], snapshots[0])
            self.assertEqual(snapshots[2], [{'first_string_address': 0x200008, 'x': 3}])

    def test_read_error_invalidates_session_rows_and_closes_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            process = FakeProcess()
            runtime = self.make_runtime(directory,
                process_factory=lambda pid: process,
                process_finder=lambda: [{'pid': IDENTITY['pid']}])

            def failed_read(*args, **kwargs):
                runtime.store.publish_rows(IDENTITY['session_key'], [{'x': 9}])
                runtime.stop.set()
                raise OSError('synthetic read failure')

            with patch.object(runtime_module, 'package_version', return_value=VERSION), \
                 patch.object(runtime, '_load_scope', return_value=[[0x200000, 0x201000]]), \
                 patch.object(runtime_module.Locator, 'step', side_effect=failed_read):
                runtime._memory_loop()
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot['rows'], [])
            self.assertIsNone(snapshot['session_key'])
            self.assertTrue(process.closed)

    def test_close_is_idempotent_and_clears_state_without_native_io(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            runtime.store.set_session(IDENTITY, VERSION)
            runtime.store.publish_rows(IDENTITY['session_key'], [{'x': 1}])
            runtime.close()
            runtime.close()
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot['rows'], [])
            self.assertIsNone(snapshot['session_key'])
            self.assertEqual(snapshot['state'], '已停止')

    def test_manifest_for_another_install_cannot_authorize_running_image(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            running, other = base / 'running', base / 'other'
            running.mkdir()
            other.mkdir()
            (other / 'appxmanifest.xml').write_text(
                '<Package><Identity Name="Microsoft.624F8B84B80" Version="' + VERSION + '"/></Package>',
                encoding='utf-8')
            with patch.object(runtime_module, 'DEFAULT_IMAGE', other / 'ForzaHorizon5.exe'):
                self.assertIsNone(runtime_module.package_version(running / 'ForzaHorizon5.exe'))

    def test_replay_uses_record_identity_and_never_calls_native_or_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            report.write_text(json.dumps({'source_identity': IDENTITY, 'samples': [
                {'telemetry': {'position_x': 1, 'position_y': 2, 'position_z': 3},
                 'candidates': []}]}), encoding='utf-8')
            with patch.object(runtime_module, 'Process', side_effect=AssertionError('native')), \
                 patch.object(runtime_module, 'find_processes', side_effect=AssertionError('native')), \
                 patch.object(runtime_module.socket, 'socket', side_effect=AssertionError('socket')):
                replay = runtime_module.ReplayRuntime(report)
                replay.start()
                snapshot = replay.snapshot()
                self.assertTrue(snapshot['replay'])
                self.assertEqual(snapshot['process_pid'], IDENTITY['pid'])
                self.assertTrue(snapshot['session_key'])
                replay.close()
                self.assertEqual(replay.snapshot()['rows'], [])
                self.assertIsNone(replay.snapshot()['player'])

    def replay_identity_snapshot(self, report_version=None, frame_version=None,
                                 report_extra=None, frame_extra=None):
        """Construct a capture and explicit local table without native I/O."""
        from fh5_models import IdentityCatalog
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            mapping = Path(directory) / 'mapping.json'
            frame = {'telemetry': {'position_x': 1, 'position_y': 2, 'position_z': 3},
                     'candidates': [{'status': 'experimental_coordinate_candidate',
                                     'coordinates': [10, 20, 30],
                                     'row_raw': 0x303831390000000f,
                                     'session_key': IDENTITY['session_key'],
                                     'first_string_address': 0x200008,
                                     'fingerprint': 'recorded'}]}
            document = {'source_identity': IDENTITY, 'samples': [frame]}
            if report_version is not None:
                document['package_version'] = report_version
            if frame_version is not None:
                frame['game_version'] = frame_version
            document.update(report_extra or {})
            frame.update(frame_extra or {})
            report.write_text(json.dumps(document), encoding='utf-8')
            mapping.write_text(json.dumps({'package_version': VERSION,
                'row_kind': 'uint32_at_F_minus_8', 'entries': {'15': {
                    'status': 'verified', 'evidence': ['local_table.json'],
                    'value': 15, 'car_id': 1009, 'level': 4,
                    'car': 'Lancer test row'}}}), encoding='utf-8')
            with patch.object(runtime_module, 'Process', side_effect=AssertionError('native')), \
                 patch.object(runtime_module, 'find_processes', side_effect=AssertionError('native')), \
                 patch.object(runtime_module.socket, 'socket', side_effect=AssertionError('socket')):
                replay = runtime_module.ReplayRuntime(report)
                replay.catalog = IdentityCatalog(mapping)
                return replay.snapshot()

    def test_replay_without_source_version_keeps_coordinates_and_unknown_identity(self):
        snapshot = self.replay_identity_snapshot(report_extra={'profile_version': VERSION})
        self.assertIsNone(snapshot['game_version'])
        self.assertEqual(len(snapshot['rows']), 1)
        self.assertEqual(snapshot['rows'][0]['x'], 10)
        self.assertEqual(snapshot['rows'][0]['identity_status'], 'unknown')

    def test_replay_mismatching_source_version_cannot_use_current_catalog(self):
        snapshot = self.replay_identity_snapshot(report_version='different-build')
        self.assertEqual(snapshot['game_version'], 'different-build')
        self.assertEqual(snapshot['rows'][0]['identity_status'], 'unknown')

    def test_replay_conflicting_report_and_frame_versions_fail_closed(self):
        for report_version, frame_version, extra in (
                ('different-build', VERSION, {}),
                (VERSION, 'different-build', {}),
                (VERSION, None, {'game_version': 'different-build'})):
            with self.subTest(report=report_version, frame=frame_version, extra=extra):
                snapshot = self.replay_identity_snapshot(report_version, frame_version,
                                                        report_extra=extra)
                self.assertIsNone(snapshot['game_version'])
                self.assertEqual(snapshot['rows'][0]['identity_status'], 'unknown')

    def test_replay_matching_explicit_source_version_resolves_legacy_padding(self):
        for report_version, frame_version in ((VERSION, None), (None, VERSION),
                                              (VERSION, VERSION)):
            with self.subTest(report=report_version, frame=frame_version):
                snapshot = self.replay_identity_snapshot(report_version, frame_version)
                row = snapshot['rows'][0]
                self.assertEqual(snapshot['game_version'], VERSION)
                self.assertEqual((row['loot_row_id'], row['car_id'], row['level']), (15, 1009, 4))
                self.assertEqual(row['identity_status'], 'verified')


if __name__ == '__main__':
    unittest.main()
