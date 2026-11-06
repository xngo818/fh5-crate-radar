import struct
import time
import unittest
from unittest.mock import patch

from fh5_upgrade import LiveUpgradeSession, UpgradeRecordUnavailable, UpgradeService
import tempfile
import test_fh5_upgrade_recovery as recovery


class RoundTransitionTests(unittest.TestCase):
    def fixture(self):
        session = recovery.RecoveryTests().session()
        old, new = 0x4011400, 0x4015200  # Same 15,872-byte gap as captured logs.
        session.component_address = old
        session._ranges = [dict(base=0x4000000, end=0x4300000, executable=False),
                           dict(base=0x10000000, end=0x18000000, executable=False)]
        session._range_index = 1; session._scan_address = 0x11000000
        state = {'old_alive': True, 'new_alive': False}
        def component(address):
            if address == old and state['old_alive'] or address == new and state['new_alive']:
                return {'address':address, 'state':address+0x1000,
                        'chain':[dict(address=address+0x2000, key=47, level=1)]}
            raise UpgradeRecordUnavailable('component_invalid', 'destroyed', False)
        def read(address, size):
            data = bytearray(size)
            if state['new_alive'] and address <= new and new+8 <= address+size:
                struct.pack_into('<Q', data, new-address, session.vtable)
            return bytes(data)
        session.component = component; session.process.read = read
        return session, state, old, new

    def test_destroyed_object_is_replaced_nearby_without_waiting_for_global_sweep(self):
        s, state, old, new = self.fixture()
        self.assertEqual(s.locate()['address'], old)
        state.update(old_alive=False, new_alive=True)
        cursor = s._scan_address
        self.assertEqual(s.locate()['address'], new)
        self.assertEqual(s._scan_address, cursor)
        self.assertEqual(s.scan_bytes, 0)
        self.assertLessEqual(s.scope_scan_bytes, 0x10000)
        self.assertEqual(s.last_discovery, 'previous_component_neighbourhood')

    def test_neighbourhood_survives_long_lobby_gap_in_same_process(self):
        s, state, old, new = self.fixture()
        with patch('fh5_upgrade.time.monotonic', return_value=100):
            self.assertEqual(s.locate()['address'], old)
        state['old_alive'] = False
        with patch('fh5_upgrade.time.monotonic', return_value=101):
            self.assertIsNone(s.locate())
        self.assertEqual(s._recent_components, {})
        state['new_alive'] = True
        with patch('fh5_upgrade.time.monotonic', return_value=300):
            self.assertEqual(s.locate()['address'], new)

    def test_scopes_are_bounded_and_do_not_starve_global_progress(self):
        s, state, old, new = self.fixture()
        s.locate(); state['old_alive'] = False
        for n in range(10):
            s._remember_component_scope(0x4010000+n*0x10000)
        self.assertEqual(len(s._component_scopes), 4)
        for _ in range(3):
            before = s.scan_bytes
            s.locate()
            self.assertGreater(s.scan_bytes, before)
        self.assertLessEqual(s.scope_scan_bytes, 3*4*1048576)

    def test_read_failure_does_not_create_candidate_or_call_writer(self):
        s, state, old, new = self.fixture()
        s.locate(); state['old_alive'] = False
        s.process.read = lambda a,n: None
        self.assertIsNone(s.locate())
        self.assertGreater(s.search_snapshot()['read_failures'], 0)

    def test_removed_cursor_range_advances_to_successor_instead_of_start(self):
        s = recovery.RecoveryTests().session()
        small = dict(base=0x1000, end=0x2000, executable=False)
        removed = dict(base=0x8000, end=0xa000, executable=False)
        successor = dict(base=0xc000, end=0xe000, executable=False)
        s._ranges = [small, removed, successor]
        s._range_index = 1; s._scan_address = 0x9000; s._next_region_refresh = 0
        s.process.regions = lambda **kwargs:[small, successor]
        LiveUpgradeSession._refresh_ranges(s)
        self.assertEqual(s._range_index, 1)
        self.assertEqual(s._ranges[s._range_index]['base'], 0xc000)
        self.assertIsNone(s._scan_address)

    def test_exhausted_scan_survives_metadata_refresh_and_records_pass(self):
        s = recovery.RecoveryTests().session()
        region = dict(base=0x1000, end=0x2000, executable=False)
        s._ranges = [region]; s._range_index = 1; s._scan_address = None
        s._next_region_refresh = 0; s.process.regions = lambda **kwargs:[region]
        LiveUpgradeSession._refresh_ranges(s)
        self.assertEqual(s._range_index, 1)
        s.component_address = None; s.component = lambda a: None
        s.locate()
        self.assertEqual(s.full_scan_passes, 1)

    def test_scopes_are_not_shared_by_new_process_sessions(self):
        s, state, old, new = self.fixture()
        s.locate()
        other = recovery.RecoveryTests().session()
        self.assertEqual(other.search_snapshot()['neighbourhood_count'], 0)

    def test_game_disconnect_clears_old_pid_and_search_status(self):
        source = {'session_key':'a', 'process_pid':7, 'game_version':'3.688.109.0'}
        class Session:
            scan_bytes = 123456
            process = type('Process', (), {'identity':{'session_key':'a'}})()
            def locate(self):return None
            def close(self):pass
            def restore(self):pass
        def wait_for(predicate):
            deadline = time.monotonic()+2
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(predicate())
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(lambda:dict(source), state_dir=directory, session_factory=lambda *a:Session())
            try:
                service.configure(True, {})
                wait_for(lambda:service.snapshot().get('process_pid') == 7)
                source.clear(); service._wake.set()
                wait_for(lambda:service.snapshot().get('process_pid') is None)
                state = service.snapshot()
                self.assertIsNone(state['session_key'])
                self.assertEqual(state['scan_mib'], 0)
                self.assertEqual(state['search_progress'], {})
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
