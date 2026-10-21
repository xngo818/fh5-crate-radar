"""Search restart hints from fake telemetry state; no process or socket I/O."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_runtime as module
from fh5_models import StateStore

VERSION = module.SUPPORTED_VERSION
IDENTITY = {'pid': 1, 'session_key': 'one', 'image_path': 'fake/ForzaHorizon5.exe'}


class CarRescanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = module.Runtime(state_dir=self.temp.name)
        self.clock = [0.0]
        self.runtime.store = StateStore(clock=lambda: self.clock[0])
        self.runtime.store.set_session(IDENTITY, VERSION)
        self.rescan_patch = patch.object(self.runtime, 'request_rescan', wraps=self.runtime.request_rescan)
        self.rescan = self.rescan_patch.start()

    def tearDown(self):
        self.runtime.close()
        self.rescan_patch.stop()
        self.temp.cleanup()

    def observe(self, ordinal, now, key='one'):
        self.clock[0] = now
        return self.runtime._observe_car_ordinal(key, ordinal, now=now)

    def test_brocky_to_tsuru_without_rows_restarts_existing_locator_once(self):
        self.assertFalse(self.observe(3128, 0))
        self.assertTrue(self.observe(3810, 1))
        self.assertFalse(self.observe(3810, 2))
        self.rescan.assert_called_once_with()
        self.assertTrue(self.runtime._rescan.is_set())
        self.assertTrue(self.runtime.wake.is_set())
        self.assertEqual(self.runtime.store.snapshot()['rows'], [])

    def test_debounce_is_at_least_ten_seconds_and_hold_does_not_retrigger(self):
        self.observe(3128, 0)
        self.assertTrue(self.observe(3810, 1))
        self.assertFalse(self.observe(1591, 5))
        self.assertFalse(self.observe(1009, 10.999))
        self.assertTrue(self.observe(2179, 11))
        self.assertFalse(self.observe(2179, 30))
        self.assertEqual(self.rescan.call_count, 2)

    def test_live_rows_prevent_restart_and_update_baseline(self):
        self.runtime.store.publish_rows('one', [{'x': 1, 'y': 2, 'z': 3}])
        self.observe(3128, 0)
        self.assertFalse(self.observe(3810, 1))
        self.rescan.assert_not_called()
        self.runtime.store.clear_rows('test')
        self.assertFalse(self.observe(3810, 1.1))
        self.assertTrue(self.observe(2179, 1.2))
        self.rescan.assert_called_once_with()

    def test_expired_rows_are_not_treated_as_live_candidates(self):
        self.runtime.store.publish_rows('one', [{'x': 1, 'y': 2, 'z': 3}])
        self.observe(3128, 0)
        self.assertFalse(self.observe(3810, 1))
        self.assertTrue(self.observe(3109, 2))

    def test_session_change_resets_previous_ordinal_and_debounce(self):
        self.observe(3128, 0)
        self.observe(3810, 1)
        self.runtime.store.set_session(dict(IDENTITY, session_key='two', pid=2), VERSION)
        self.assertFalse(self.observe(1591, 2, 'two'))
        self.assertTrue(self.observe(1009, 3, 'two'))
        self.assertEqual(self.rescan.call_count, 2)

    def test_invalid_ids_clock_source_and_version_do_not_trigger(self):
        self.observe(3128, 0)
        for ordinal in (None, 0, -1, True, 3810.0, '3810', 2**31):
            self.assertFalse(self.runtime._observe_car_ordinal('one', ordinal, now=1))
        for now in (True, float('nan'), float('inf'), -1, 10**1000):
            self.assertFalse(self.runtime._observe_car_ordinal('one', 3810, now=now))
        self.assertFalse(self.observe(3810, 1, 'other'))
        self.runtime.store.set_session(IDENTITY, 'unsupported')
        self.assertFalse(self.observe(3810, 1))
        self.rescan.assert_not_called()

    def test_validated_fake_telemetry_loop_calls_observer_after_timestamp_guard(self):
        base = {'is_race_on': True, 'position_x': 1, 'position_y': 2, 'position_z': 3,
                'yaw': 0, 'speed_m_per_s': 0, 'warnings': []}
        frames = [dict(base, timestamp_ms=1, car_ordinal=3128),
                  dict(base, timestamp_ms=2, car_ordinal=3810),
                  dict(base, timestamp_ms=2, car_ordinal=3128),  # duplicate rejected
                  dict(base, timestamp_ms=3, car_ordinal=3810)]
        remaining = list(frames)
        runtime = self.runtime

        class Listener:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def setsockopt(self, *args): pass
            def bind(self, *args): pass
            def settimeout(self, *args): pass
            def recvfrom(self, *args):
                if remaining:
                    remaining.pop()
                    return b'fake', ('127.0.0.1', 60000)
                runtime.stop.set()
                raise module.socket.timeout()

        with patch.object(module.socket, 'socket', return_value=Listener()), \
             patch.object(module, 'parse_packet', side_effect=frames), \
             patch.object(module, 'Process', side_effect=AssertionError('native')):
            self.runtime._telemetry_loop()
        self.rescan.assert_called_once_with()
        self.assertEqual(self.runtime._previous_car_ordinal, 3810)


if __name__ == '__main__':
    unittest.main()
