"""Offline duplicate-mark guard tests; no game or desktop interaction."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_stats_latch import LATCH_FILE, MAX_FILE_BYTES, RoundLatch


def sample(identifier='match-one'):
    return {'match_id': identifier, 'snapshot_hash': 'a' * 64,
            'session_key': 'process-one', 'rows': [{'x': 111, 'y': 222, 'z': 333}]}


class RoundLatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / 'not-created'

    def tearDown(self):
        self.temporary.cleanup()

    def test_construction_and_empty_sync_are_read_only(self):
        latch = RoundLatch(self.directory)
        self.assertTrue(latch.allowed())
        self.assertEqual(latch.sync(None), {'locked': False, 'error': None, 'sample_id': None})
        self.assertFalse(self.directory.exists())

    def test_persisted_sample_locks_and_saves_only_identity(self):
        latch = RoundLatch(self.directory)
        self.assertEqual(latch.sync(sample()),
                         {'locked': True, 'error': None, 'sample_id': 'match-one'})
        self.assertFalse(latch.allowed())
        saved = json.loads((self.directory / LATCH_FILE).read_text(encoding='utf-8'))
        self.assertEqual(saved, {'schema_version': 1, 'locked': True,
                                 'sample_id': 'match-one', 'snapshot_hash': 'a' * 64})
        self.assertEqual(list(self.directory.iterdir()), [self.directory / LATCH_FILE])

    def test_restart_loads_without_writing(self):
        RoundLatch(self.directory).sync(sample())
        with patch.object(RoundLatch, '_atomic_write') as writer:
            latch = RoundLatch(self.directory)
            latch.sync(None)
            writer.assert_not_called()
        self.assertFalse(latch.allowed())
        self.assertEqual(latch.status()['sample_id'], 'match-one')

    def test_missing_gate_is_reconciled_from_existing_collector_sample(self):
        self.directory.mkdir()
        latch = RoundLatch(self.directory)
        self.assertTrue(latch.allowed())
        latch.sync(sample())
        self.assertFalse(latch.allowed())
        self.assertTrue((self.directory / LATCH_FILE).is_file())

    def test_repeated_sample_new_process_and_empty_data_never_unlock_or_rewrite(self):
        latch = RoundLatch(self.directory)
        with patch.object(latch, '_atomic_write', wraps=latch._atomic_write) as writer:
            latch.sync(sample())
            for observation in (sample(), None, dict(sample('match-two'), session_key='process-two'),
                                None, sample()):
                self.assertTrue(latch.sync(observation)['locked'])
                self.assertFalse(latch.allowed())
            self.assertEqual(writer.call_count, 1)
        self.assertEqual(latch.status()['sample_id'], 'match-one')

    def test_status_is_detached(self):
        latch = RoundLatch(self.directory)
        status = latch.status()
        status['locked'] = True
        self.assertTrue(latch.allowed())

    def test_corrupt_file_fails_closed_and_is_preserved(self):
        self.directory.mkdir()
        path = self.directory / LATCH_FILE
        for contents in ('not json', '{}', '{"schema_version":1,"locked":false}',
                         'x' * (MAX_FILE_BYTES + 1)):
            path.write_text(contents, encoding='utf-8')
            latch = RoundLatch(self.directory)
            self.assertFalse(latch.allowed())
            self.assertIn('load_failed', latch.status()['error'])
            latch.sync(sample())
            self.assertEqual(path.read_text(encoding='utf-8'), contents)

    def test_invalid_loaded_identity_and_unexpected_fields_fail_closed(self):
        self.directory.mkdir()
        path = self.directory / LATCH_FILE
        base = {'schema_version': 1, 'locked': True, 'sample_id': 'one'}
        for changes in ({'sample_id': ''}, {'schema_version': True}, {'snapshot_hash': 'wrong'},
                        {'rows': [{'x': 1}]}):
            path.write_text(json.dumps(dict(base, **changes)), encoding='utf-8')
            self.assertFalse(RoundLatch(self.directory).allowed())

    def test_invalid_collector_sample_fails_closed_without_writing(self):
        for observation in ({}, [], {'match_id': ' '}, {'match_id': 'x' * 129},
                            {'match_id': 'ok', 'snapshot_hash': 'wrong'}):
            latch = RoundLatch(self.directory)
            self.assertIn('save_failed', latch.sync(observation)['error'])
            self.assertFalse(latch.allowed())
            self.assertFalse(self.directory.exists())

    def test_write_error_is_reported_without_raising_or_repeated_writes(self):
        latch = RoundLatch(self.directory)
        with patch.object(latch, '_atomic_write', side_effect=PermissionError('disk unavailable')) as writer:
            status = latch.sync(sample())
            self.assertTrue(status['locked'])
            self.assertIn('PermissionError', status['error'])
            self.assertFalse(latch.allowed())
            latch.sync(sample())
            latch.sync(None)
            self.assertEqual(writer.call_count, 1)

    def test_atomic_replace_error_cleans_temporary_and_collector_recovers_next_start(self):
        latch = RoundLatch(self.directory)
        with patch('fh5_stats_latch.os.replace', side_effect=OSError('disk error')):
            self.assertIn('save_failed', latch.sync(sample())['error'])
        self.assertFalse(latch.allowed())
        self.assertEqual(list(self.directory.iterdir()), [])
        restarted = RoundLatch(self.directory)
        restarted.sync(sample())
        self.assertFalse(restarted.allowed())
        self.assertIsNone(restarted.status()['error'])

    def test_directory_read_failure_fails_closed(self):
        self.directory.mkdir()
        (self.directory / LATCH_FILE).mkdir()
        latch = RoundLatch(self.directory)
        self.assertFalse(latch.allowed())
        self.assertIn('load_failed', latch.status()['error'])

    def test_sample_id_alone_is_accepted_and_persists(self):
        RoundLatch(self.directory).sync({'match_id': 'identity-only'})
        latch = RoundLatch(self.directory)
        self.assertEqual(latch.status(), {'locked': True, 'error': None, 'sample_id': 'identity-only'})


if __name__ == '__main__':
    unittest.main()
