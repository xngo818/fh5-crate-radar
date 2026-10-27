"""Offline state freshness and evidence-gated mapping checks; no game APIs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_models import IdentityCatalog, StateStore, decorate_candidate


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class StateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = StateStore(clock=self.clock)
        self.store.set_session({'pid': 10, 'session_key': '10:start-a'}, '3.688.109.0')

    def test_new_session_clears_and_rejects_late_publish(self):
        self.store.publish_rows('10:start-a', [{'x': 1}])
        self.store.publish_player('10:start-a', {'x': 2})
        self.store.set_session({'pid': 10, 'session_key': '10:start-b'}, '3.688.109.0')
        self.assertFalse(self.store.publish_rows('10:start-a', [{'x': 99}]))
        self.assertFalse(self.store.publish_player('10:start-a', {'x': 99}))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['rows'], [])
        self.assertIsNone(snapshot['player'])
        self.assertEqual(snapshot['session_key'], '10:start-b')
        self.store.set_session(None)
        self.assertFalse(self.store.publish_rows(None, []))
        self.assertIsNone(self.store.snapshot()['process_pid'])

    def test_ttls_are_independent_and_monotonic(self):
        self.store.publish_rows('10:start-a', [{'x': 1}])
        self.store.publish_player('10:start-a', {'x': 2})
        self.clock.now += 1.0
        snapshot = self.store.snapshot()
        self.assertIsNone(snapshot['player'])
        self.assertEqual(snapshot['telemetry_state'], 'stale')
        self.assertEqual(snapshot['telemetry_age'], 1.0)
        self.assertEqual(snapshot['rows'], [{'x': 1}])
        self.clock.now += 0.5
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['rows'], [])
        self.assertEqual(snapshot['memory_state'], 'stale_rows')
        self.assertEqual(snapshot['status_reason'], 'rows_ttl_expired')

    def test_future_time_cannot_extend_ttl_and_rewind_fails_closed(self):
        self.store.publish_rows('10:start-a', [{'x': 1}], now=1_000_000_000)
        self.clock.now += 1.5
        self.assertEqual(self.store.snapshot()['rows'], [])
        self.store.publish_rows('10:start-a', [{'x': 2}])
        self.store.publish_player('10:start-a', {'x': 2})
        self.clock.now -= 0.5
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['rows'], [])
        self.assertIsNone(snapshot['player'])

    def test_old_samples_cannot_replace_newer_same_session(self):
        self.store.publish_rows('10:start-a', [{'x': 1}], now=99)
        self.store.publish_player('10:start-a', {'x': 1}, now=99)
        self.assertFalse(self.store.publish_rows('10:start-a', [{'x': 2}], now=98))
        self.assertFalse(self.store.publish_player('10:start-a', {'x': 2}, now=98))
        self.assertEqual(self.store.snapshot()['rows'][0]['x'], 1)

    def test_samples_and_snapshot_are_detached(self):
        rows, player = [{'nested': [1]}], {'nested': [2]}
        self.store.publish_rows('10:start-a', rows)
        self.store.publish_player('10:start-a', player)
        rows[0]['nested'].append(3)
        player['nested'].append(4)
        snapshot = self.store.snapshot()
        snapshot['rows'][0]['nested'].append(5)
        snapshot['player']['nested'].append(6)
        self.assertEqual(self.store.snapshot()['rows'], [{'nested': [1]}])
        self.assertEqual(self.store.snapshot()['player'], {'nested': [2]})

    def test_inactive_player_clears_immediately_without_rows(self):
        self.store.publish_rows('10:start-a', [{'x': 1}])
        for inactive in (None, {'active': False}, {'is_race_on': 0}):
            self.store.publish_player('10:start-a', {'x': 2})
            self.store.publish_player('10:start-a', inactive)
            snapshot = self.store.snapshot()
            self.assertIsNone(snapshot['player'])
            self.assertEqual(snapshot['telemetry_state'], 'inactive')
            self.assertTrue(snapshot['rows'])

    def test_status_and_fail_closed_clear(self):
        self.store.publish_rows('10:start-a', [{'x': 1}])
        progress = {'done': 10}
        self.store.set_status(scan_progress=progress, memory_state='scanning')
        progress['done'] = 20
        self.assertEqual(self.store.snapshot()['scan_progress'], {'done': 10})
        self.store.clear_rows('scan_read_failed')
        self.assertEqual(self.store.snapshot()['rows'], [])
        self.assertEqual(self.store.snapshot()['memory_state'], 'scan_read_failed')
        with self.assertRaises(ValueError):
            self.store.set_status(rows=[{'x': 1}])

    def test_incremental_refresh_never_renews_unvisited_row(self):
        self.store.publish_row_updates('10:start-a', [
            {'first_string_address': 8, 'x': 1}, {'first_string_address': 16, 'x': 2}])
        self.clock.now += 1.0
        self.store.publish_row_updates('10:start-a', [{'first_string_address': 8, 'x': 3}])
        self.clock.now += .5
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['rows'], [{'first_string_address': 8, 'x': 3}])
        self.assertEqual(snapshot['rows_age'], .5)

    def test_limited_empty_update_is_not_disappearance_and_still_expires(self):
        self.store.publish_row_updates('10:start-a', [{'first_string_address': 8, 'x': 1}])
        self.clock.now += 1
        self.store.publish_row_updates('10:start-a', [])
        self.assertEqual(len(self.store.snapshot()['rows']), 1)
        self.clock.now += .5
        self.assertEqual(self.store.snapshot()['rows'], [])
        self.assertEqual(self.store.snapshot()['status_reason'], 'rows_ttl_expired')

    def test_rejected_slot_is_removed_immediately_without_clearing_other_rows(self):
        self.store.publish_row_updates('10:start-a', [
            {'first_string_address': 8}, {'first_string_address': 16}])
        self.store.publish_row_updates('10:start-a', [], removed=[8])
        self.assertEqual(self.store.snapshot()['rows'], [{'first_string_address': 16}])

    def test_incremental_samples_obey_session_order_and_backward_clock(self):
        self.store.publish_row_updates('10:start-a', [{'first_string_address': 8}])
        self.assertFalse(self.store.publish_row_updates('other-session', [], removed=[8]))
        self.assertFalse(self.store.publish_row_updates('10:start-a', [], removed=[8], now=99))
        self.assertTrue(self.store.snapshot()['rows'])
        self.clock.now -= .1
        self.assertEqual(self.store.snapshot()['rows'], [])

    def test_full_snapshot_then_incremental_update_keeps_original_other_row_age(self):
        self.store.publish_rows('10:start-a', [{'first_string_address': 8}, {'first_string_address': 16}])
        self.clock.now += 1
        self.store.publish_row_updates('10:start-a', [{'first_string_address': 8}])
        self.clock.now += .5
        self.assertEqual(self.store.snapshot()['rows'], [{'first_string_address': 8}])

    def test_lightweight_source_checks_freshness_without_row_payload(self):
        self.store.publish_row_updates('10:start-a', [{'first_string_address': 8, 'large': list(range(100))}])
        source = self.store.source_snapshot()
        self.assertEqual(source['fresh_row_count'], 1)
        self.assertNotIn('rows', source)
        source['session_key'] = 'modified'
        self.assertEqual(self.store.source_snapshot()['session_key'], '10:start-a')
        self.clock.now += 1.5
        self.assertEqual(self.store.source_snapshot()['fresh_row_count'], 0)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'identity.json'
        self.version = '3.688.109.0'
        self.entry = {'status': 'verified', 'evidence': ['before.json', 'after.json'],
                      'car': 'Peugeot 205 T16', 'level': 2, 'value': 17}
        self.document = {'package_version': self.version,
                         'row_kind': 'raw_value_at_F_minus_8',
                         'entries': {'4294967305': self.entry}}

    def catalog(self, document=None):
        self.path.write_text(json.dumps(self.document if document is None else document),
                             encoding='utf-8')
        return IdentityCatalog(self.path)

    def test_only_exact_complete_raw_value_matches(self):
        catalog = self.catalog()
        result = catalog.resolve(4294967305, self.version)
        self.assertEqual((result['level'], result['car'], result['value']),
                         (2, 'Peugeot 205 T16', 17))
        for raw in (9, 2, True, 4294967305.0, '04294967305', '9' * 5000):
            self.assertEqual(catalog.resolve(raw, self.version)['identity_status'], 'unknown')
        self.assertEqual(catalog.resolve('4294967305', self.version)['level'], 2)
        result['identity_evidence'].append('changed')
        self.assertEqual(len(catalog.resolve(4294967305, self.version)['identity_evidence']), 2)

    def test_version_or_row_kind_mismatch_has_no_mapping(self):
        catalog = self.catalog()
        for version in (None, '', '3.688.110.0'):
            self.assertEqual(catalog.resolve(4294967305, version)['level'], 0)
        wrong = deepcopy(self.document)
        wrong['row_kind'] = 'CarID'
        self.assertEqual(self.catalog(wrong).resolve(4294967305, self.version)['level'], 0)

    def test_unverified_and_bad_fields_cannot_display_identity(self):
        for field, values in {
            'status': ['candidate', None], 'evidence': ['', [], [''], None],
            'car': ['', ' ', 205], 'level': [0, 11, True, '2', 2.0],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    bad = deepcopy(self.document)
                    bad['entries']['4294967305'][field] = value
                    result = self.catalog(bad).resolve(4294967305, self.version)
                    self.assertEqual(result, {'level': 0, 'car': None, 'value': None,
                                              'identity_status': 'unknown'})

    def test_missing_broken_schema_and_name_only_tables_fail_closed(self):
        self.assertEqual(IdentityCatalog(self.path).resolve(2, self.version)['level'], 0)
        self.path.write_text('{', encoding='utf-8')
        self.assertIsNotNone(IdentityCatalog(self.path).error)
        for document in ([], {}, {'205': 'Peugeot 205 T16'},
                         {'package_version': self.version, 'row_kind': 'raw_value_at_F_minus_8',
                          'entries': {'2': 'Peugeot 205 T16'}}):
            self.assertEqual(self.catalog(document).resolve(2, self.version)['identity_status'],
                             'unknown')

    def test_candidate_unknown_still_keeps_point_without_level_inference(self):
        candidate = {'status': 'experimental_coordinate_candidate',
                     'coordinates': [123.0, 45.0, -789.0], 'row_raw': 5,
                     'first_string_address': 0x100008, 'fingerprint': 'fingerprint',
                     'session_key': '10:start-a', 'position_pointer': 0x200008,
                     'coordinates_verified': False}
        result = decorate_candidate(candidate, self.catalog(), self.version)
        self.assertEqual((result['x'], result['y'], result['z']), (123, 45, -789))
        self.assertEqual(result['level'], 0)  # raw 5 is not level 5.
        self.assertIsNone(result['car'])
        self.assertTrue(result['experimental'])
        self.assertFalse(result['coordinates_verified'])
        self.assertEqual(result['source_metadata']['position_pointer'], 0x200008)
        result['source_metadata']['coordinates'][0] = 0
        self.assertEqual(candidate['coordinates'][0], 123)

    def test_rejected_and_nonfinite_candidates_are_never_rows(self):
        catalog = self.catalog()
        for status, xyz in (('rejected', [1, 2, 3]),
                            ('experimental_coordinate_candidate', [1, float('nan'), 3]),
                            ('experimental_coordinate_candidate', [1, float('inf'), 3]),
                            ('experimental_coordinate_candidate', [1, 10 ** 500, 3]),
                            ('experimental_coordinate_candidate', [1, 2]),
                            ('experimental_coordinate_candidate', [True, 2, 3])):
            self.assertIsNone(decorate_candidate({'status': status, 'coordinates': xyz},
                                                 catalog, self.version))

    def uint32_document(self):
        return {'package_version': self.version, 'row_kind': 'uint32_at_F_minus_8',
                'entries': {'15': {'status': 'verified', 'evidence': ['runtime_table.json'],
                                   'car': "2008 Mitsubishi Lancer Evolution X GSR",
                                   'level': 4, 'value': 15, 'car_id': 1009}}}

    def test_uint32_mapping_returns_verified_car_id_and_explicit_level(self):
        catalog = self.catalog(self.uint32_document())
        result = catalog.resolve(15, self.version)
        self.assertEqual((result['car_id'], result['level'], result['value']), (1009, 4, 15))
        for raw in (-1, 2**32, 0x303831390000000f, True, 15.0, '015'):
            self.assertEqual(catalog.resolve(raw, self.version)['identity_status'], 'unknown')
        self.assertEqual(catalog.resolve(15, 'different-build')['identity_status'], 'unknown')

    def test_uint32_replay_padding_normalizes_but_explicit_bad_field_fails_closed(self):
        catalog = self.catalog(self.uint32_document())
        candidate = {'status': 'experimental_coordinate_candidate',
                     'coordinates': [1, 2, 3], 'row_raw': 0x303831390000000f}
        replay = decorate_candidate(candidate, catalog, self.version)
        self.assertEqual((replay['loot_row_id'], replay['car_id'], replay['level']), (15, 1009, 4))
        for bad in (True, -1, 2**32, 15.0, None):
            with self.subTest(bad=bad):
                failed = decorate_candidate(dict(candidate, loot_row_id=bad), catalog, self.version)
                self.assertEqual(failed['identity_status'], 'unknown')
        live = decorate_candidate(dict(candidate, loot_row_id=15, row_padding_u32=0x30383139),
                                  catalog, self.version)
        self.assertEqual((live['car_id'], live['level']), (1009, 4))
        # The old row kind must retain exact u64 identity despite a matching low32.
        legacy = deepcopy(self.uint32_document())
        legacy['row_kind'] = 'raw_value_at_F_minus_8'
        self.assertEqual(decorate_candidate(candidate, self.catalog(legacy), self.version)
                         ['identity_status'], 'unknown')

    def test_uint32_chain_requires_valid_car_id_value_and_evidence(self):
        for field, values in {'car_id': [None, 0, -1, True, 2**32, '1009'],
                              'value': [None, 0, -1, True, 2**32, 15.0],
                              'evidence': [None, [], ['']]} .items():
            for bad in values:
                with self.subTest(field=field, value=bad):
                    document = self.uint32_document()
                    document['entries']['15'][field] = bad
                    self.assertEqual(self.catalog(document).resolve(15, self.version)
                                     ['identity_status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
