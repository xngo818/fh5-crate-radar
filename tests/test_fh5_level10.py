"""Offline state-transition tests; no process is opened by these fixtures."""
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
    load_preferences, save_preferences, load_level10_preferences,
    save_level10_preferences, plan_changes, plan_level10_change)


def reference(key, address, next_address=0):
    raw = bytearray(48)
    struct.pack_into('<Q', raw, 0, 0x100)
    struct.pack_into('<I', raw, 8, key)
    raw[12] = 1
    struct.pack_into('<Q', raw, 16, 0x9000)
    struct.pack_into('<Q', raw, 32, next_address)
    return dict(address=address, key=key, level=10, context=0x9000, hex=raw.hex())


def fixture(catalog, current=45, pending=44):
    session = LiveUpgradeSession.__new__(LiveUpgradeSession)
    session.catalog = catalog
    session.journal = {}
    session.component_address = 0x1000
    session.process = type('P', (), {'is_alive': lambda self: True})()
    nodes = [reference(current, 0x2000, 0x2040 if pending is not None else 0)]
    if pending is not None:
        nodes.append(reference(pending, 0x2040))
    live = dict(address=0x1000, state=0x3000, chain=nodes)
    session.component = lambda address: deepcopy(live)
    session.verify_table = lambda context: None
    writes = []
    def write(component, address, expected, key, permitted=lambda: True):
        # Use the actual current/pending guard, with only the OS writer replaced.
        session.guard(component, address, expected)
        if not permitted():
            raise ValueError('paused')
        node = next(n for n in live['chain'][1:] if n['address'] == address)
        raw = bytearray.fromhex(node['hex'])
        struct.pack_into('<I', raw, 8, key)
        node.update(key=key, level=catalog.rows[key]['level'], hex=raw.hex())
        writes.append((address, key))
    session.write_key = write
    return session, live, writes


class Level10Tests(unittest.TestCase):
    def setUp(self):
        self.catalog = UpgradeCatalog()

    def test_all_twenty_directed_choices_keep_head_and_grade(self):
        for source in range(42, 47):
            for target in range(42, 47):
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    old = next(k for k in range(42, 47) if k not in (source, target))
                    session, live, writes = fixture(self.catalog, source, old)
                    head = deepcopy(live['chain'][0])
                    updated, count, ready = session.apply_level10(deepcopy(live), {str(source): target})
                    self.assertEqual(updated['chain'][0], head)
                    self.assertEqual(updated['chain'][1]['key'], target)
                    self.assertEqual((count, ready), (1, True))
                    self.assertEqual(writes, [(0x2040, target)])
                    self.assertEqual(updated['chain'][1]['level'], 10)

    def test_invalid_cross_grade_self_and_noninteger_rules_are_ignored(self):
        for target in [45, 41, 47, True, '42', 42.0, [], {}]:
            with self.subTest(target=target):
                rules = self.catalog.validated_level10({'45': target})
                self.assertIsNone(rules['45'])
        self.assertEqual(self.catalog.validated_level10({'45': 42})['45'], 42)

    def test_default_is_no_override_and_does_not_choose_first_option(self):
        session, live, writes = fixture(self.catalog)
        self.assertEqual(session.apply_level10(deepcopy(live), {}), (live, 0, False))
        self.assertEqual(writes, [])

    def test_target_already_cached_is_ready_without_write(self):
        session, live, writes = fixture(self.catalog)
        result, count, ready = session.apply_level10(deepcopy(live), {'45': 44})
        self.assertEqual((count, ready, writes), (0, True, []))
        self.assertEqual(session.journal, {})

    def test_lazy_cache_is_waited_for_then_modified(self):
        session, live, writes = fixture(self.catalog, pending=None)
        self.assertEqual(session.apply_level10(deepcopy(live), {'45': 42})[1:], (0, False))
        self.assertEqual(writes, [])
        live['chain'] = [reference(45, 0x2000, 0x2040), reference(44, 0x2040)]
        self.assertEqual(session.apply_level10(deepcopy(live), {'45': 42})[1:], (1, True))

    def test_each_invalid_pending_shape_is_rejected(self):
        _, live, _ = fixture(self.catalog)
        original = live['chain']
        bad = []
        c = deepcopy(original); c[1]['address'] = c[0]['address']; bad.append(c)
        c = deepcopy(original); c[1]['address'] += 1; bad.append(c)
        c = deepcopy(original); c[1]['key'] = 41; bad.append(c)
        c = deepcopy(original); c[1]['key'] = 45; bad.append(c)
        c = deepcopy(original); c[1]['context'] += 8; bad.append(c)
        c = deepcopy(original); c.append(reference(43, 0x2080)); bad.append(c)
        for chain in bad:
            with self.subTest(chain=chain):
                with self.assertRaises(ValueError):
                    plan_level10_change(self.catalog, chain, {'45': 42})

    def test_repeat_poll_does_not_repeat_write(self):
        session, live, writes = fixture(self.catalog)
        session.apply_level10(deepcopy(live), {'45': 42})
        self.assertEqual(session.apply_level10(deepcopy(live), {'45': 42})[1:], (0, True))
        self.assertEqual(writes, [(0x2040, 42)])

    def test_reset_and_disable_restore_only_unconsumed_override(self):
        for rules, enabled in [({}, True), ({'45': 42}, False)]:
            session, live, writes = fixture(self.catalog)
            session.apply_level10(deepcopy(live), {'45': 42})
            _, count, ready = session.apply_level10(deepcopy(live), rules, enabled)
            self.assertEqual((count, ready), (0, False))
            self.assertEqual(writes, [(0x2040, 42), (0x2040, 44)])
            self.assertEqual(session.journal, {})

    def test_reselect_restores_original_before_new_target(self):
        session, live, writes = fixture(self.catalog)
        session.apply_level10(deepcopy(live), {'45': 42})
        session.apply_level10(deepcopy(live), {'45': 43})
        self.assertEqual(writes, [(0x2040, 42), (0x2040, 44), (0x2040, 43)])
        session.restore()
        self.assertEqual(live['chain'][1]['key'], 44)

    def test_consumed_override_never_restores_current_and_next_rule_applies(self):
        session, live, writes = fixture(self.catalog)
        session.apply_level10(deepcopy(live), {'45': 42})
        live['chain'] = [reference(42, 0x2040, 0x2080), reference(45, 0x2080)]
        session.apply_level10(deepcopy(live), {'42': 46})
        self.assertEqual(writes, [(0x2040, 42), (0x2080, 46)])
        session.restore()
        self.assertEqual(live['chain'][0]['key'], 42)
        self.assertEqual(live['chain'][1]['key'], 45)

    def test_restore_after_consumption_never_writes_current(self):
        session, live, writes = fixture(self.catalog)
        session.apply_level10(deepcopy(live), {'45': 42})
        live['chain'] = [reference(42, 0x2040)]
        session.restore()
        self.assertEqual(writes, [(0x2040, 42)])
        self.assertEqual(session.journal, {})

    def test_rebuilt_reference_drops_old_restore_value(self):
        session, live, writes = fixture(self.catalog)
        session.apply_level10(deepcopy(live), {'45': 42})
        live['chain'][1] = reference(43, 0x2040)
        session.apply_level10(deepcopy(live), {'45': 46})
        session.restore()
        self.assertEqual(writes, [(0x2040, 42), (0x2040, 46), (0x2040, 43)])

    def test_new_state_at_reused_addresses_does_not_restore_old_value(self):
        for restore_directly in [False, True]:
            session, live, writes = fixture(self.catalog)
            session.apply_level10(deepcopy(live), {'45': 42})
            live['state'] += 0x100
            if restore_directly:
                session.restore()
            else:
                session.apply_level10(deepcopy(live), {})
            self.assertEqual(writes, [(0x2040, 42)])
            self.assertEqual(session.journal, {})

    def test_pause_generation_guard_prevents_new_write(self):
        session, live, writes = fixture(self.catalog)
        with self.assertRaises(ValueError):
            session.apply_level10(deepcopy(live), {'45': 42}, permitted=lambda: False)
        session.restore()
        self.assertEqual(writes, [])

    def test_normal_upgrade_ignores_same_level_tail(self):
        chain = [reference(37, 0x2000, 0x2040), reference(42, 0x2040, 0x2080), reference(45, 0x2080)]
        chain[0]['level'] = 9
        changes = plan_changes(self.catalog, chain, {'10': 43})
        self.assertEqual([(c['address'], c['new_key']) for c in changes], [(0x2040, 43)])
        self.assertIsNone(plan_level10_change(self.catalog, chain, {'42': 46}))

    def test_separate_preference_files_preserve_normal_upgrade_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            normal, same = Path(directory)/'upgrade_choices.json', Path(directory)/'level10_swap_choices.json'
            save_preferences(self.catalog, {'10': 42, '7': 28}, normal)
            original = normal.read_bytes()
            save_level10_preferences(self.catalog, {'42': 43, '45': 46}, same)
            self.assertEqual(load_level10_preferences(self.catalog, same)['45'], 46)
            self.assertEqual(normal.read_bytes(), original)
            self.assertEqual(load_preferences(self.catalog, normal)['10'], 42)
            self.assertNotIn('enabled', json.loads(same.read_text()))
            save_level10_preferences(self.catalog, {}, same)
            self.assertEqual(normal.read_bytes(), original)
            same.write_text('invalid json')
            self.assertTrue(all(v is None for v in load_level10_preferences(self.catalog, same).values()))
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_replay_cannot_start_either_route(self):
        def forbidden(*args):
            self.fail('replay opened a process')
        service = UpgradeService(None, self.catalog, session_factory=forbidden)
        try:
            service.configure_level10(True, {'45': 42})
            service.configure(True, {'10': 42})
            state = service.snapshot()
            self.assertFalse(any(state[n] for n in ['enabled', 'normal_enabled', 'level10_enabled', 'level10_ready']))
            self.assertIsNone(service._thread)
        finally:
            service.close()

    def test_independent_switches_share_one_worker(self):
        service = UpgradeService(lambda: {}, self.catalog)
        with patch('fh5_upgrade.threading.Thread') as thread:
            service.configure(True, {'10': 42})
            service.configure_level10(True, {'45': 46})
            service.configure(False, {})
            state = service.snapshot()
            self.assertTrue(state['enabled'])
            self.assertFalse(state['normal_enabled'])
            self.assertTrue(state['level10_enabled'])
            self.assertEqual(thread.call_count, 1)
            service.configure_level10(False, {})
            self.assertFalse(service.snapshot()['enabled'])
        service._thread = None
        service.close()

    def test_worker_routes_level10_and_clears_readiness_when_match_ends(self):
        session, live, writes = fixture(self.catalog)
        session.process.identity = {'session_key': 'session'}
        session.scan_bytes = 0
        session.locate = lambda: deepcopy(live)
        session.close = lambda: None
        source = lambda: {'session_key': 'session', 'process_pid': 7, 'game_version': self.catalog.profile['package_version']}
        with tempfile.TemporaryDirectory() as directory:
            service = UpgradeService(source, self.catalog, directory, lambda *args: session)
            try:
                service.configure_level10(True, {'45': 42})
                deadline = time.monotonic()+2
                while not service.snapshot()['level10_ready'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(service.snapshot()['level10_ready'])
                events = [json.loads(s) for s in (Path(directory)/'upgrade_events.jsonl').read_text().splitlines()]
                applied = [e for e in events if e['event'] == 'level10_swap_applied']
                self.assertEqual(len(applied), 1)
                self.assertEqual(applied[0]['next_key'], 42)
                live['chain'] = []
                service._wake.set()
                deadline = time.monotonic()+2
                while service.snapshot()['level10_ready'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(service.snapshot()['level10_ready'])
            finally:
                service.close()


    def test_new_panel_keeps_pause_reason_visible_after_switch_clears(self):
        from fh5_upgrade_panel import Level10Panel
        class Value:
            def __init__(self, value):self.value=value
            def get(self):return self.value
            def set(self, value):self.value=value
        panel=Level10Panel.__new__(Level10Panel)
        panel.enabled=Value(True)
        panel.status=Value('正在接入')
        panel.current=Value('');panel.target=Value('');panel.pending=Value('')
        state={'level10_enabled':False,'text':'已暂停：记录失效','chain':[]}
        panel.service=type('Service',(),{'snapshot':lambda self:deepcopy(state)})()
        panel.tick()
        self.assertFalse(panel.enabled.get())
        self.assertEqual(panel.status.get(),'已暂停：记录失效')


if __name__ == '__main__':
    unittest.main()
