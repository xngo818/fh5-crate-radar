from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fh5_upgrade import (UpgradeCatalog, LiveUpgradeSession, UpgradeService,
                         load_preferences, save_preferences, plan_changes)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = UpgradeCatalog()
        self.chain = []
        keys = [1, 2, 7, 12, 17, 22, 27, 32, 37, 42]
        for i, key in enumerate(keys):
            address = 0x100000+i*64
            raw = bytearray(48); struct.pack_into('<I', raw, 8, key)
            self.chain.append({'address': address, 'key': key, 'level': i+1, 'context': 0x9000, 'hex': raw.hex()})

    def test_catalog_has_exact_five_choices_per_target_tier(self):
        self.assertEqual(len(self.catalog.rows), 47)
        for n in range(2, 11):
            self.assertEqual(len(self.catalog.levels[n]), 5)
            self.assertTrue(all(r['level'] == n for r in self.catalog.levels[n]))

    def test_preferences_reject_cross_tier_and_wrong_types(self):
        for bad in [True, '2', 42, 1, [], {}, 2.0]:
            self.assertIsNone(self.catalog.validated({'2': bad})['2'])
        self.assertEqual(self.catalog.validated({'2': 6})['2'], 6)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'upgrade_choices.json'
            save_preferences(self.catalog, {'2': 6, '7': 31}, path)
            self.assertEqual(load_preferences(self.catalog, path)['7'], 31)
            self.assertNotIn('enabled', json.loads(path.read_text()))
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_all_future_tiers_are_planned_without_touching_current(self):
        selections = {str(n): self.catalog.levels[n][-1]['key'] for n in range(2, 11)}
        changes = plan_changes(self.catalog, self.chain, selections)
        self.assertEqual(len(changes), 9)
        self.assertNotIn(self.chain[0]['address'], [c['address'] for c in changes])
        for change in changes:
            self.assertEqual(self.catalog.rows[change['old_key']]['level'], self.catalog.rows[change['new_key']]['level'])

    def test_random_means_no_override_not_first_candidate(self):
        self.assertEqual(plan_changes(self.catalog, self.chain, {}), [])

    def test_reject_same_level_cycle_or_level_jump_chain(self):
        for key in [42, 1]:
            chain = deepcopy(self.chain)
            chain[1]['key'] = key
            with self.assertRaises(ValueError): plan_changes(self.catalog, chain, {'2': 6})
        chain = deepcopy(self.chain); chain[1]['address'] = chain[0]['address']
        with self.assertRaises(ValueError): plan_changes(self.catalog, chain, {})

    def test_write_guard_rejects_new_current_and_consumed_target(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        session.process = type('P', (), {'is_alive': lambda self: True})()
        before = {'address': 100, 'state': 200, 'chain': deepcopy(self.chain)}
        session.component = lambda address: dict(before, chain=deepcopy(self.chain[1:]))
        with self.assertRaises(ValueError):
            session.guard(before, self.chain[1]['address'], bytes.fromhex(self.chain[1]['hex']))
        session.component = lambda address: before
        with self.assertRaises(ValueError):
            session.guard(before, self.chain[0]['address'], bytes.fromhex(self.chain[0]['hex']))

    def test_disabling_restores_pending_but_never_current(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        session.component_address = 100
        chain = deepcopy(self.chain[6:])
        session.journal = {chain[0]['address']: {'old_key': 28, 'after': chain[0]['hex']},
                           chain[1]['address']: {'old_key': 33, 'after': chain[1]['hex']}}
        session.component = lambda address: {'address': address, 'state': 200, 'chain': chain}
        session.verify_table = lambda context: None
        writes = []
        session.write_key = lambda component, address, expected, key: writes.append((address, key))
        session.restore()
        self.assertEqual(writes, [(chain[1]['address'], 33)])
        self.assertEqual(session.journal, {})

    def test_replay_cannot_start_a_process_or_writer(self):
        constructed = []
        service = UpgradeService(None, self.catalog, session_factory=lambda *args: constructed.append(args))
        service.configure(True, {'2': 6})
        self.assertFalse(service.snapshot()['enabled'])
        self.assertIsNone(service._thread)
        self.assertEqual(constructed, [])
        service.close()

    def test_error_disables_auto_retry_instead_of_repeated_writes(self):
        attempts = []
        source = lambda: {'session_key': 'session', 'process_pid': 7, 'game_version': '3.688.109.0'}
        def fail(*args):
            attempts.append(1)
            raise ValueError('build mismatch')
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(source, self.catalog, directory, fail)
            service.configure(True, {'2': 6})
            deadline = time.monotonic()+2
            while service.snapshot()['enabled'] and time.monotonic() < deadline:
                time.sleep(.01)
            time.sleep(.3)
            self.assertFalse(service.snapshot()['enabled'])
            self.assertEqual(len(attempts), 1)
            service.close()

    def test_cached_empty_component_does_not_block_new_active_match(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        session.component_address = 0x1000
        session.vtable = 0x12345678
        session._ranges = [{'base': 0x2000, 'end': 0x3000}]
        session._range_index = 0; session._scan_address = None
        empty = {'address': 0x1000, 'state': 0x4000, 'chain': []}
        active = {'address': 0x2100, 'state': 0x5000, 'chain': deepcopy(self.chain)}
        session.component = lambda address: empty if address == 0x1000 else active
        data = bytearray(4096); struct.pack_into('<Q', data, 0x100, session.vtable)
        session.process = type('P', (), {'read': lambda self, a, n: bytes(data[:n])})()
        session.scan_bytes = 0
        session._next_region_refresh = time.monotonic()+10
        session._priority_ranges = []; session._priority_address = None; session._scan_turn = 0
        result = session.locate()
        self.assertEqual(result['address'], active['address'])
        self.assertTrue(result['chain'])
        self.assertEqual(session.component_address, active['address'])

    def test_metadata_timeout_preserves_existing_cursor(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        session._ranges = [{'base': 0x2000, 'end': 0x5000}]
        session._range_index = 0; session._scan_address = 0x3000
        session._next_region_refresh = 0
        def fail(self, **kwargs):raise TimeoutError('busy')
        session.process = type('P', (), {'regions': fail})()
        session._refresh_ranges()
        self.assertEqual(session._scan_address, 0x3000)
        self.assertEqual(session._range_index, 0)
        self.assertEqual(len(session._ranges), 1)

    def test_new_region_is_queued_without_resetting_full_scan(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        old = {'base': 0x4000, 'end': 0x8000, 'executable': False}
        added = {'base': 0x1000, 'end': 0x3000, 'executable': False}
        session._ranges = [old]; session._range_index = 0; session._scan_address = 0x6000
        session._next_region_refresh = 0; session._priority_ranges = []; session.component_address = None
        session.process = type('P', (), {'regions': lambda self, **kw: [added, old]})()
        session._refresh_ranges()
        self.assertEqual(session._scan_address, 0x6000)
        self.assertEqual(session._ranges[session._range_index], old)
        self.assertIn(added, session._priority_ranges)

    def test_level10_same_level_cache_is_left_alone_without_error(self):
        session = LiveUpgradeSession.__new__(LiveUpgradeSession)
        session.journal = {0x1000: {'old_key': 42}}
        component = {'chain': [{'level': 10}, {'level': 10}]}
        self.assertEqual(session.apply(component, {'10': 43}), (component, 0))
        self.assertEqual(session.journal, {})

    def test_transient_region_timeout_retries_without_disabling(self):
        calls = []; recovered = threading.Event()
        class FakeSession:
            def __init__(self, source, catalog):
                self.process = type('P', (), {'identity': {'session_key': 'session'}})()
                self.scan_bytes = 0
            def locate(self):
                calls.append(1)
                if len(calls) == 1:raise TimeoutError('metadata busy')
                recovered.set()
                return None
            def restore(self):pass
            def close(self):pass
        source = lambda: {'session_key': 'session', 'process_pid': 7, 'game_version': '3.688.109.0'}
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(source, self.catalog, directory, FakeSession)
            service.configure(True, {})
            self.assertTrue(recovered.wait(2))
            self.assertTrue(service.snapshot()['enabled'])
            service.close()


if __name__ == '__main__':unittest.main()
