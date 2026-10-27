"""Offline opening-observation tests. No game or desktop interaction."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_models import CORE_SUCCESS
from fh5_opening_stats import (OpeningStatistics, classify_frame, sample_signature,
                              SAMPLE_FILE, ATTEMPT_FILE, SUMMARY_JSON, SUMMARY_CSV)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def row(address, level, *, verified=True, session='game:one'):
    return {'session_key': session, 'first_string_address': address,
            'fingerprint': f'object:{address}', 'loot_row_id': 400 + address,
            'status': CORE_SUCCESS, 'x': 200 + address, 'y': 5.0, 'z': -100.0,
            'level': level, 'identity_status': 'verified' if verified else 'unknown'}


def frame(stamp, rows=None, *, session='game:one'):
    return {'session_key': session, 'game_version': '3.688.109.0',
            'rows': deepcopy(rows if rows is not None else [row(index, {
                1: 7, 2: 8, 3: 8, 4: 9, 5: 10, 6: 10}.get(index, 4)) for index in range(1, 73)]),
            'rows_age': 0.01, 'last_error': None, 'read_gaps': 0, 'refresh_limited': False,
            'memory_timing': {'refreshes': [{'sample_monotonic': stamp,
                                          'limited': False, 'read_gaps': 0}]}}


class FrameClassificationTests(unittest.TestCase):
    def test_counts_are_verified_levels_and_deduplicated(self):
        sample = frame(101)
        sample['rows'].append(deepcopy(sample['rows'][0]))
        result = classify_frame(sample)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['counts'], {'7': 1, '8': 2, '9': 1, '10': 2, 'purple': 4})
        self.assertEqual(result['observed_count'], 72)
        self.assertEqual(result['unknown_count'], 0)

    def test_row_order_does_not_change_signature(self):
        sample = frame(101)
        self.assertEqual(sample_signature(sample['rows'], 'game:one'),
                         sample_signature(list(reversed(sample['rows'])), 'game:one'))

    def test_replay_never_eligible(self):
        for key, value in [('replay', True), ('session_key', 'replay:game')]:
            sample = frame(101)
            sample[key] = value
            self.assertFalse(classify_frame(sample)['eligible'])

    def test_health_must_be_explicit_and_healthy(self):
        for key in ('last_error', 'read_gaps', 'refresh_limited', 'rows_age', 'memory_timing'):
            sample = frame(101)
            del sample[key]
            self.assertFalse(classify_frame(sample)['eligible'], key)
        for changes in ({'last_error': 'read error'}, {'read_gaps': 1}, {'read_gaps': False},
                        {'refresh_limited': True}, {'rows_age': 1.5}, {'rows_age': -1},
                        {'rows_age': float('nan')}):
            self.assertFalse(classify_frame(dict(frame(101), **changes))['eligible'], changes)

    def test_unhealthy_refresh_not_hidden_by_top_level_health(self):
        for changes in ({'limited': True}, {'read_gaps': 1}, {'sample_monotonic': float('inf')}):
            sample = frame(101)
            sample['memory_timing']['refreshes'][0].update(changes)
            self.assertFalse(classify_frame(sample)['eligible'])

    def test_invalid_rows_do_not_become_zero_counts(self):
        for rows in ([], [row(10, 7, session='old')], [dict(row(10, 7), x=float('nan'))],
                     [dict(row(10, 7), fingerprint='')], [row(10, 7), row(10, 9)]):
            self.assertFalse(classify_frame(frame(101, rows))['eligible'])

    def test_unknown_seven_blocks_capture_instead_of_becoming_zero(self):
        sample = frame(101)
        sample['rows'][0]['identity_status'] = 'unknown'
        result = classify_frame(sample)
        self.assertFalse(result['eligible'])
        self.assertNotIn('counts', result)
        self.assertEqual(result['unknown_count'], 1)

    def test_71_and_73_unique_objects_are_not_truncated_or_counted(self):
        for rows in (frame(101)['rows'][:-1], frame(101)['rows'] + [row(73, 10)]):
            result = classify_frame(frame(101, rows))
            self.assertFalse(result['eligible'])
            self.assertNotIn('counts', result)
            self.assertEqual(result['observed_count'], len(rows))


class OpeningStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / 'not-created-yet'
        self.clock = Clock()
        self.stats = self.make_stats()

    def tearDown(self):
        self.tmp.cleanup()

    def make_stats(self, target=50):
        return OpeningStatistics(self.directory, target=target, clock=self.clock,
                                 utc_clock=lambda: '2026-10-22T10:00:00+00:00')

    def feed(self, stamp, *, rows=None, changes=None, stats=None):
        self.clock.now = stamp + .01
        sample = frame(stamp, rows)
        sample.update(changes or {})
        return (stats or self.stats).update(sample)

    def capture(self, *, start=101, rows=None, stats=None):
        output = None
        for step in range(7):
            output = self.feed(start + step * .5, rows=rows, stats=stats)
        return output

    def test_constructor_and_idle_updates_do_not_create_files(self):
        self.assertFalse(self.directory.exists())
        self.capture()
        self.assertEqual(self.stats.status()['count'], 0)
        self.assertFalse(self.directory.exists())

    def test_explicit_mark_three_seconds_and_persistence(self):
        self.stats.arm_match('game:one')
        result = self.capture()
        self.assertEqual(result['count'], 1)
        record = result['captured_sample']
        self.assertTrue(record['user_marked'])
        self.assertEqual(record['counts'], {'7': 1, '8': 2, '9': 1, '10': 2, 'purple': 4})
        self.assertEqual(record['stable_seconds'], 3)
        self.assertEqual(record['distinct_samples'], 7)
        self.assertNotIn('rows', record)
        self.assertNotIn('coordinates', record)
        self.assertEqual(self.make_stats().status()['count'], 1)
        summary = json.loads((self.directory / SUMMARY_JSON).read_text(encoding='utf-8'))
        self.assertEqual(summary['totals'], record['counts'])
        self.assertEqual(summary['means'], {'7': 1, '8': 2, '9': 1, '10': 2, 'purple': 4})
        self.assertEqual(summary['expected_counts'], summary['means'])
        self.assertEqual(summary['actual_n'], 1)
        self.assertEqual(summary['attempt_count'], 1)
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(record['schema_version'], 2)
        self.assertEqual(record['scope'], 'first_wave_72_observed')
        self.assertEqual(record['phase_source'], 'user_marked_new_match_button')
        self.assertEqual(record['expected_pool_size'], 72)
        self.assertTrue((self.directory / SUMMARY_CSV).read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_capture_preserves_partial_scan_and_observed_only_scope(self):
        self.stats.arm_match('game:one')
        progress = {'attempted_mib': 200, 'eligible_mib': 4000,
                    'complete_selected_policy': False, 'complete_world_coverage': False,
                    'private_debug_blob': 'not stored'}
        result = None
        for step in range(7):
            result = self.feed(101 + step * .5, changes={'scan_progress': progress})
        record = result['captured_sample']
        self.assertEqual(record['coverage'], 'observed_only')
        self.assertEqual(record['scan_progress']['attempted_mib'], 200)
        self.assertFalse(record['scan_progress']['complete_world_coverage'])
        self.assertNotIn('private_debug_blob', record['scan_progress'])
        csv_text = (self.directory / SUMMARY_CSV).read_text(encoding='utf-8-sig')
        self.assertIn('complete_selected_policy,complete_world_coverage', csv_text)
        self.assertIn('observed_only', csv_text)

    def test_repeated_ui_ticks_are_not_independent_samples(self):
        self.stats.arm_match('game:one')
        for _ in range(300):
            self.feed(101)
        self.assertEqual(self.stats.status()['distinct_samples'], 1)
        self.assertEqual(self.stats.status()['count'], 0)

    def test_pre_mark_and_future_samples_rejected(self):
        self.stats.arm_match('game:one')
        self.assertEqual(self.feed(100)['reason'], 'sample_not_after_mark_or_stale')
        self.clock.now = 101
        self.assertEqual(self.stats.update(frame(102))['distinct_samples'], 0)

    def test_session_change_cancels_instead_of_inventing_a_new_match(self):
        self.stats.arm_match('game:one')
        self.feed(101)
        result = self.feed(102, changes={'session_key': 'game:two'})
        self.assertIsNone(result['armed_session'])
        self.assertEqual(result['reason'], 'process_session_changed_rearm_required')
        self.capture(start=103)
        self.assertEqual(self.stats.status()['count'], 0)

    def test_no_session_or_replay_cannot_be_armed(self):
        for session in (None, '', 'replay:game'):
            self.assertIsNone(self.stats.arm_match(session)['armed_session'])

    def test_replay_frame_resets_window_and_never_saves(self):
        self.stats.arm_match('game:one')
        result = self.capture()
        self.assertEqual(result['count'], 1)
        self.clock.now += 6
        self.stats.arm_match('game:one')
        for index in range(8):
            self.feed(self.clock.now + .5, changes={'replay': True})
        self.assertEqual(self.stats.status()['count'], 1)

    def test_unhealthy_frame_breaks_continuity(self):
        self.stats.arm_match('game:one')
        for stamp in (101, 101.5, 102, 102.5):
            self.feed(stamp)
        self.feed(103, changes={'refresh_limited': True})
        self.assertEqual(self.feed(103.5)['stable_seconds'], 0)
        self.assertEqual(self.stats.status()['count'], 0)
        self.capture(start=104)
        self.assertEqual(self.stats.status()['count'], 1)

    def test_long_gap_is_not_continuous_stability(self):
        self.stats.arm_match('game:one')
        self.feed(101)
        self.feed(104)
        self.assertEqual(self.stats.status()['distinct_samples'], 1)
        self.assertEqual(self.stats.status()['count'], 0)

    def test_identity_or_position_change_restarts_window(self):
        for change in ({'fingerprint': 'new-id'}, {'x': 9900}, {'level': 9}):
            self.stats.cancel()
            self.clock.now = 100
            self.stats.arm_match('game:one')
            self.feed(101)
            altered = frame(101)['rows']
            altered[0].update(change)
            result = self.feed(102, rows=altered)
            self.assertEqual(result['stable_seconds'], 0)
            self.assertEqual(result['count'], 0)

    def test_arm_is_idempotent_and_expires_after_120_seconds(self):
        self.stats.arm_match('game:one', match_id='same')
        self.feed(101)
        self.clock.now = 102
        self.assertEqual(self.stats.arm_match('game:one')['distinct_samples'], 1)
        self.clock.now = 220.01
        result = self.stats.update(frame(220))
        self.assertEqual(result['reason'], 'opening_mark_expired')
        self.assertIsNone(result['armed_session'])

    def test_cancel_and_reset_do_not_delete_completed_records(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.stats.cancel()
        self.stats.reset()
        self.assertEqual(self.stats.status()['count'], 1)

    def test_repeated_same_observation_is_deduplicated_after_restart(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.clock.now = 110
        second = self.make_stats()
        second.arm_match('game:one')
        self.capture(start=111, stats=second)
        self.assertEqual(second.status()['count'], 1)
        self.assertIn('duplicate_observation', second.status()['reason'])
        self.assertIsNone(second.status()['armed_session'])
        self.assertEqual(second.status()['missing_count'], 1)

    def test_pending_button_clicks_do_not_restart_or_multiply_one_observation(self):
        self.stats.arm_match('game:one', match_id='button-click-one')
        for step in range(7):
            stamp = 101 + step * .5
            self.clock.now = stamp + .01
            # A burst of clicks while pending must preserve the original mark
            # and the independent read window instead of adding attempts.
            for _ in range(5):
                self.stats.arm_match('game:one', match_id='ignored-click')
            result = self.feed(stamp)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['attempt_count'], 1)
        self.assertEqual(result['captured_sample']['match_id'], 'button-click-one')
        self.assertEqual(result['captured_sample']['stable_seconds'], 3)

    def test_explicit_next_match_button_records_a_different_snapshot(self):
        self.stats.arm_match('game:one', match_id='first-manual-match')
        self.capture()
        self.clock.now = 110
        self.stats.arm_match('game:one', match_id='second-manual-match')
        second_rows = frame(111)['rows']
        second_rows[0].update(fingerprint='second-match-object', level=10)
        result = self.capture(start=111, rows=second_rows)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['attempt_count'], 2)
        self.assertEqual(result['missing_count'], 0)
        saved = [json.loads(line) for line in (self.directory / SAMPLE_FILE).read_text().splitlines()]
        self.assertEqual([item['match_id'] for item in saved],
                         ['first-manual-match', 'second-manual-match'])
        self.assertTrue(all(item['phase_source'] == 'user_marked_new_match_button' for item in saved))
        self.assertNotEqual(saved[0]['snapshot_hash'], saved[1]['snapshot_hash'])

    def test_snapshot_changes_without_a_new_button_click_never_record_next_match(self):
        self.stats.arm_match('game:one')
        self.capture()
        second_rows = frame(111)['rows']
        second_rows[0].update(fingerprint='new-set-without-click', level=10)
        self.capture(start=111, rows=second_rows)
        self.assertEqual(self.stats.status()['count'], 1)
        self.assertEqual(self.stats.status()['attempt_count'], 1)
        self.assertIsNone(self.stats.status()['armed_session'])

    def test_duplicate_click_is_consumed_before_later_object_changes(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.clock.now = 110
        self.stats.arm_match('game:one')
        result = self.capture(start=111)
        self.assertEqual(result['reason'], 'duplicate_observation_ignored')
        self.assertIsNone(result['armed_session'])
        changed_rows = frame(121)['rows']
        changed_rows[0].update(fingerprint='later-in-same-match', level=10)
        self.capture(start=121, rows=changed_rows)
        self.assertEqual(self.stats.status()['count'], 1)
        self.assertEqual(self.stats.status()['missing_count'], 1)

    def test_single_fresh_duplicate_consumes_click_before_three_second_stability(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.clock.now = 110
        self.stats.arm_match('game:one', match_id='accidental-second-click')
        # Only one healthy duplicate read arrives. An object changes on the
        # next read, before a duplicate could ever form a stable 3s window.
        duplicate = self.feed(111)
        self.assertEqual(duplicate['reason'], 'duplicate_observation_ignored')
        self.assertIsNone(duplicate['armed_session'])
        changed_rows = frame(112)['rows']
        changed_rows[0].update(fingerprint='changed-before-three-seconds', level=10)
        self.capture(start=112, rows=changed_rows)
        self.assertEqual(self.stats.status()['count'], 1)
        self.assertEqual(self.stats.status()['attempt_count'], 2)
        self.assertEqual(self.stats.status()['missing_count'], 1)

    def test_old_or_unhealthy_duplicate_does_not_consume_a_new_button_mark(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.clock.now = 110
        self.stats.arm_match('game:one')
        old = self.feed(110)
        self.assertEqual(old['reason'], 'sample_not_after_mark_or_stale')
        self.assertIsNotNone(old['armed_session'])
        unhealthy = self.feed(111, changes={'read_gaps': 1})
        self.assertIsNotNone(unhealthy['armed_session'])
        changed_rows = frame(112)['rows']
        changed_rows[0].update(fingerprint='actual-new-match')
        result = self.capture(start=112, rows=changed_rows)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['captured_sample']['stable_seconds'], 3)

    def test_legacy_v2_samples_and_attempts_keep_their_original_phase_source(self):
        self.stats.arm_match('game:one', match_id='legacy-v2-match')
        self.capture()
        sample_path, attempt_path = self.directory / SAMPLE_FILE, self.directory / ATTEMPT_FILE
        old_sample = json.loads(sample_path.read_text())
        old_attempt = json.loads(attempt_path.read_text())
        for item, path in ((old_sample, sample_path), (old_attempt, attempt_path)):
            item['phase_source'] = 'user_marked_controller_A_or_panel'
            path.write_text(json.dumps(item) + '\n', encoding='utf-8')
        before = (sample_path.read_bytes(), attempt_path.read_bytes())

        reloaded = self.make_stats()
        self.assertIsNone(reloaded.status()['error'])
        self.assertEqual(reloaded.status()['count'], 1)
        self.assertEqual(reloaded.status()['attempt_count'], 1)
        self.assertEqual(reloaded.status()['missing_count'], 0)
        self.assertEqual(before, (sample_path.read_bytes(), attempt_path.read_bytes()))

        self.clock.now = 110
        reloaded.arm_match('game:one', match_id='manual-button-match')
        second_rows = frame(111)['rows']
        second_rows[0].update(fingerprint='new-manual-button-match')
        result = self.capture(start=111, rows=second_rows, stats=reloaded)
        self.assertEqual(result['count'], 2)
        saved = [json.loads(line) for line in sample_path.read_text().splitlines()]
        attempts = [json.loads(line) for line in attempt_path.read_text().splitlines()]
        self.assertEqual(saved[0], old_sample)
        self.assertEqual(attempts[0], old_attempt)
        self.assertEqual(saved[1]['phase_source'], 'user_marked_new_match_button')
        self.assertEqual(attempts[1]['phase_source'], 'user_marked_new_match_button')
        csv_text = (self.directory / SUMMARY_CSV).read_text(encoding='utf-8-sig')
        self.assertIn('user_marked_controller_A_or_panel', csv_text)
        self.assertIn('user_marked_new_match_button', csv_text)

    def test_capture_cooldown_prevents_double_key_press(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.assertEqual(self.stats.arm_match('game:one')['reason'], 'capture_cooldown')
        self.assertIsNone(self.stats.status()['armed_session'])

    def test_target_stops_and_ineligible_frames_do_not_fake_zero_samples(self):
        limited = self.make_stats(target=1)
        limited.arm_match('game:one')
        self.capture(stats=limited)
        self.assertEqual(limited.status()['state'], 'complete')
        self.clock.now = 110
        self.assertIsNone(limited.arm_match('game:one')['armed_session'])
        self.capture(start=111, stats=limited)
        self.assertEqual(limited.status()['count'], 1)

    def test_invalid_target_rejected(self):
        for target in (0, 51, True, 1.5):
            with self.assertRaises(ValueError):
                self.make_stats(target)

    def test_samples_file_failure_does_not_raise_or_capture(self):
        self.stats.arm_match('game:one')
        with patch.object(self.stats, '_atomic_write', side_effect=OSError('disk full')):
            result = self.capture()
        self.assertEqual(result['count'], 0)
        self.assertIn('save_failed', result['error'])
        self.assertIsNotNone(result['armed_session'])

    def test_summary_failure_keeps_authoritative_sample_once(self):
        self.stats.arm_match('game:one')
        real_write = self.stats._atomic_write

        def fail_summary(name, *args, **kwargs):
            if name == SUMMARY_JSON:
                raise OSError('summary denied')
            return real_write(name, *args, **kwargs)

        with patch.object(self.stats, '_atomic_write', side_effect=fail_summary):
            result = self.capture()
        self.assertEqual(result['count'], 1)
        self.assertIn('summary_failed', result['error'])
        self.assertEqual(self.make_stats().status()['count'], 1)

    def test_corrupt_existing_file_is_preserved(self):
        self.directory.mkdir()
        path = self.directory / SAMPLE_FILE
        path.write_text('not valid JSON\n', encoding='utf-8')
        broken = self.make_stats()
        self.assertIn('load_failed', broken.status()['error'])
        broken.arm_match('game:one')
        self.capture(stats=broken)
        self.assertEqual(path.read_text(encoding='utf-8'), 'not valid JSON\n')
        self.assertEqual(broken.status()['count'], 0)

    def test_two_instances_do_not_append_duplicate_records(self):
        second = self.make_stats()
        self.stats.arm_match('game:one')
        second.arm_match('game:one')
        self.capture()
        self.clock.now = 101.01
        result = self.capture(stats=second)
        self.assertEqual(result['count'], 1)
        self.assertEqual(len((self.directory / SAMPLE_FILE).read_text().splitlines()), 1)

    def test_missing_attempts_are_reported_without_zero_imputation(self):
        self.stats.arm_match('game:one')
        incomplete = frame(101)['rows'][:-1]
        for step in range(7):
            result = self.feed(101 + step * .5, rows=incomplete)
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['observed_count'], 71)
        self.assertEqual(result['reason'], 'waiting_for_exactly_72_unique_objects')
        self.stats.cancel('user_cancelled')
        status = self.stats.status()
        self.assertEqual(status['actual_n'], 0)
        self.assertEqual(status['attempt_count'], 1)
        self.assertEqual(status['missing_count'], 1)
        summary = json.loads((self.directory / SUMMARY_JSON).read_text())
        self.assertEqual(summary['expectations'], {'7': None, '8': None, '9': None, '10': None, 'purple': None})
        attempts = [json.loads(line) for line in (self.directory / ATTEMPT_FILE).read_text().splitlines()]
        self.assertEqual(attempts[0]['outcome'], 'missing')
        self.assertEqual(attempts[0]['last_observed_count'], 71)

    def test_timeout_and_session_change_persist_missing_attempts(self):
        self.stats.arm_match('game:one')
        self.clock.now = 221
        self.stats.update(frame(221))
        self.assertEqual(self.stats.status()['missing_count'], 1)
        self.clock.now = 222
        self.stats.arm_match('game:one')
        self.feed(223, changes={'session_key': 'other'})
        reloaded = self.make_stats()
        self.assertEqual(reloaded.status()['attempt_count'], 2)
        self.assertEqual(reloaded.status()['missing_count'], 2)
        self.assertEqual(reloaded.status()['actual_n'], 0)

    def test_expectations_use_actual_valid_n_and_purple_excludes_ten(self):
        self.stats.arm_match('game:one')
        self.capture()
        self.clock.now = 110
        self.stats.arm_match('game:one')
        self.stats.cancel('missing_test')
        self.clock.now = 111
        self.stats.arm_match('game:one')
        second_rows = frame(112)['rows']
        second_rows[0].update(level=10, fingerprint='new-second-match')
        self.capture(start=112, rows=second_rows)
        summary = json.loads((self.directory / SUMMARY_JSON).read_text())
        self.assertEqual(summary['actual_n'], 2)
        self.assertEqual(summary['attempt_count'], 3)
        self.assertEqual(summary['missing_count'], 1)
        self.assertEqual(summary['expectations'], {'7': .5, '8': 2, '9': 1, '10': 2.5, 'purple': 3.5})
        self.assertFalse(summary['target_reached'])

    def test_old_v1_files_are_not_read_or_changed(self):
        self.directory.mkdir()
        old = self.directory / 'samples.jsonl'
        old.write_text('legacy v1 content is not v2 input\n', encoding='utf-8')
        reloaded = self.make_stats()
        self.assertEqual(reloaded.status()['actual_n'], 0)
        self.assertIsNone(reloaded.status()['error'])
        reloaded.arm_match('game:one')
        self.capture(stats=reloaded)
        self.assertEqual(old.read_text(), 'legacy v1 content is not v2 input\n')

    def test_restart_pending_attempt_is_missing_and_does_not_auto_arm(self):
        self.stats.arm_match('game:one')
        reloaded = self.make_stats()
        self.assertIsNone(reloaded.status()['armed_session'])
        self.assertEqual(reloaded.status()['attempt_count'], 1)
        self.assertEqual(reloaded.status()['missing_count'], 1)

    def test_attempt_history_has_a_bound_without_dropping_failures(self):
        with patch('fh5_opening_stats.MAX_ATTEMPTS', 2):
            for step in range(2):
                self.stats.arm_match('game:one')
                self.stats.cancel('test_missing')
            result = self.stats.arm_match('game:one')
            self.assertIsNone(result['armed_session'])
            self.assertEqual(result['attempt_count'], 2)
            self.assertEqual(result['missing_count'], 2)
            self.assertIn('attempt history limit', result['error'])


if __name__ == '__main__':
    unittest.main()
