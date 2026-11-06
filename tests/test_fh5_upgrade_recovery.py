from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from fh5_upgrade import LiveUpgradeSession, UpgradeCatalog, UpgradeRecordUnavailable, UpgradeService


class RecoveryTests(unittest.TestCase):
    def session(self):
        s = LiveUpgradeSession.__new__(LiveUpgradeSession)
        s.component_address = 0x1100
        s.vtable = 0x12345678
        s._recent_components = {}; s.locate_diagnostics = {}
        s._ranges = [{'base': 0x1000, 'end': 0x8001000}]
        s._range_index = 0; s._scan_address = None; s.scan_bytes = 0
        s._priority_ranges = []; s._priority_address = None; s._scan_turn = 0
        s._refresh_ranges = lambda: None
        s.process = type('P', (), {'read': lambda self, a, n: bytes(n)})()
        return s

    def active(self, address=0x1100, key=28):
        return {'address': address, 'state': 0x9000,
                'chain': [{'address': 0xa000, 'key': key, 'level': 7}]}

    def test_transient_failure_rechecks_known_address_before_more_scanning(self):
        s = self.session(); calls = []; ready = False
        def component(address):
            calls.append(address)
            if not ready:
                raise UpgradeRecordUnavailable('row_unready', 'switch in progress')
            return self.active(key=31)
        s.component = component
        self.assertIsNone(s.locate())
        scanned = s.scan_bytes
        self.assertGreater(scanned, 0)
        ready = True
        self.assertEqual(s.locate()['chain'][0]['key'], 31)
        self.assertEqual(calls, [0x1100, 0x1100])
        self.assertEqual(s.scan_bytes, scanned)
        self.assertEqual(s._recent_components, {})

    def test_failed_hint_does_not_block_discovery_of_new_match(self):
        s = self.session()
        def component(address):
            if address == 0x1100:
                raise UpgradeRecordUnavailable('read_failed', 'temporarily unreadable')
            return self.active(address)
        def read(address, size):
            data = bytearray(size)
            if address <= 0x2100 < address+size:
                struct.pack_into('<Q', data, 0x2100-address, s.vtable)
            return bytes(data)
        s.component = component; s.process.read = read
        self.assertEqual(s.locate()['address'], 0x2100)
        self.assertEqual(s._recent_components, {})

    def test_permanent_identity_mismatch_is_not_kept_as_retry_hint(self):
        s = self.session()
        def component(address):
            raise UpgradeRecordUnavailable('component_invalid', 'different vtable', False)
        s.component = component
        self.assertIsNone(s.locate())
        self.assertIsNone(s.component_address)
        self.assertEqual(s._recent_components, {})
        self.assertEqual(s.locate_diagnostics['reason'], 'component_invalid')

    def test_retry_expiry_is_not_extended_on_failure(self):
        s = self.session(); calls = []
        def component(address):
            calls.append(address)
            raise UpgradeRecordUnavailable('row_unready', 'not ready')
        s.component = component
        with patch('fh5_upgrade.time.monotonic', return_value=100):
            s.locate()
        with patch('fh5_upgrade.time.monotonic', return_value=114):
            s.locate()
        self.assertEqual(s._recent_components[0x1100], 115)
        with patch('fh5_upgrade.time.monotonic', return_value=116):
            s.locate()
        self.assertEqual(calls, [0x1100, 0x1100])
        self.assertEqual(s._recent_components, {})
        self.assertGreaterEqual(s.scan_bytes, 48*1048576)

    def test_retry_cache_is_bounded_and_session_local(self):
        s = self.session(); other = self.session()
        def component(address):
            raise UpgradeRecordUnavailable('row_unready', 'not ready')
        s.component = component
        for address in range(0x1000, 0x2000, 8):
            self.assertIsNone(s._probe_component(address))
        self.assertEqual(len(s._recent_components), 8)
        self.assertEqual(other._recent_components, {})

    def test_failed_retry_never_returns_previous_chain(self):
        s = self.session(); ready = True
        def component(address):
            if ready:
                return self.active()
            raise UpgradeRecordUnavailable('read_failed', 'unreadable')
        s.component = component
        self.assertTrue(s.locate()['chain'])
        ready = False
        self.assertIsNone(s.locate())
        self.assertIsNone(s.locate())

    def test_logs_loss_recovery_and_current_and_pending_changes_without_poll_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(state_dir=directory)
            before = self.active()
            before['chain'].append({'address': 0xb000, 'key': 32, 'level': 8})
            after = deepcopy(before); after['chain'][1]['key'] = 33
            service._observe_chain('a', before)
            service._observe_chain('a', before)
            service._observe_chain('a', after, phase='after_apply')
            service._observe_chain('a', None, reason={'reason': 'row_unready'})
            service._observe_chain('a', None)
            current = dict(after, chain=after['chain'][1:])
            service._observe_chain('a', current)
            service._observe_chain('b', None)
            service._observe_chain('b', current)
            events = [json.loads(s) for s in (Path(directory)/'upgrade_events.jsonl').read_text().splitlines()]
            kinds = [e['event'] for e in events]
            self.assertEqual(kinds.count('upgrade_connection_lost'), 1)
            self.assertEqual(kinds.count('upgrade_connection_restored'), 1)
            self.assertEqual(kinds.count('upgrade_connected'), 2)
            snapshots = [e for e in events if e['event'] == 'upgrade_chain_observed']
            self.assertEqual(len(snapshots), 4)
            self.assertEqual(snapshots[1]['phase'], 'after_apply')
            self.assertEqual(snapshots[2]['nodes'][0]['key'], 33)
            lost = next(e for e in events if e['event'] == 'upgrade_connection_lost')
            self.assertEqual(lost['reason']['reason'], 'row_unready')

    def test_worker_skips_apply_during_loss_then_recovers_without_disabling(self):
        ready = True; reads = []; writes = []
        s = self.session()
        s.process.identity = {'session_key': 'a'}
        def component(address):
            reads.append(ready)
            if not ready:
                raise UpgradeRecordUnavailable('row_unready', 'switch')
            return self.active()
        s.component = component
        s.apply = lambda c, choices, permitted: (writes.append(c) or c, 0)
        s.journal = {}; s.close = lambda: None; s.restore = lambda: None
        source = lambda: {'session_key': 'a', 'process_pid': 7, 'game_version': '3.688.109.0'}
        def wait_for(predicate):
            deadline = time.monotonic()+3
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(predicate())
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(source, UpgradeCatalog(), directory, lambda *a:s)
            try:
                service.configure(True, {'8':33})
                wait_for(lambda: bool(service.snapshot()['chain']))
                ready = False; service._wake.set()
                wait_for(lambda: not service.snapshot()['chain'])
                count = len(writes)
                service._wake.set(); time.sleep(.1)
                self.assertEqual(len(writes), count)
                self.assertTrue(service.snapshot()['enabled'])
                ready = True; service._wake.set()
                wait_for(lambda: bool(service.snapshot()['chain']))
                self.assertGreater(len(writes), count)
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
