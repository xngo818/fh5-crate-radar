"""Statistics UI wiring with fake controls; no windows, game reads or input."""
from unittest.mock import Mock
import unittest
from pathlib import Path
import tempfile

from test_fh5_map_visibility import bare_application, valid_frame_and_snapshot


class StatisticsUiTests(unittest.TestCase):
    def test_real_provider_snapshot_contract_can_create_an_opening_sample(self):
        import test_fh5_recovery as fixtures
        from fh5_opening_stats import OpeningStatistics
        clock = fixtures.Clock()
        process = fixtures.SparseMemory(clock)
        for index in range(72):
            process.install(process.layout[0]['base'] + 8192 + index * 16384, row=2)
        samples = fixtures.RecoveryRegressionTests().run_memory(process, clock, seconds=8)
        self.assertTrue(samples)
        with tempfile.TemporaryDirectory() as directory:
            collector = OpeningStatistics(Path(directory), clock=clock.monotonic)
            clock.now = samples[0][0] - .01
            collector.arm_match(fixtures.IDENTITY['session_key'])
            for stamp, snapshot in samples:
                clock.now = stamp + .001
                collector.update(snapshot)
            self.assertEqual(collector.status()['count'], 1)

    def app(self):
        app = bare_application(True)
        _, snapshot = valid_frame_and_snapshot()
        app.runtime.snapshot.return_value = snapshot
        app.opening_stats = Mock()
        status = {'count': 0, 'target': 50, 'armed_session': None}
        app.opening_stats.status.return_value = status
        app.opening_stats.arm_match.return_value = dict(status, armed_session='test-session')
        app.opening_stats.update.return_value = status
        app.stats_status = Mock()
        app.stats_pad = Mock()
        app.stats_pad.pressed.return_value = False
        app.stats_next = 0
        return app, snapshot

    def test_handbrake_a_cannot_start_statistics_or_change_display_toggles(self):
        app, snapshot = self.app()
        app.stats_pad.pressed.return_value = True
        app.toggle, app.toggle_map = Mock(), Mock()
        app.tick()
        app.opening_stats.arm_match.assert_not_called()
        app.stats_pad.pressed.assert_not_called()
        app.toggle.assert_not_called()
        app.toggle_map.assert_not_called()
        app.opening_stats.update.assert_called_once_with(snapshot)

    def test_manual_new_match_button_works_with_existing_saved_sample_and_legacy_latch(self):
        from fh5_stats_latch import RoundLatch
        app, snapshot = self.app()
        sample = {'match_id': 'current-round', 'snapshot_hash': 'a' * 64}
        app.opening_stats.status.return_value = {
            'count': 1, 'target': 50, 'last_sample': sample, 'armed_session': None}
        with tempfile.TemporaryDirectory() as directory:
            app.stats_latch = RoundLatch(directory)
            app.stats_latch.sync(sample)
            app.mark_opening_match(snapshot)
            app.opening_stats.arm_match.assert_called_once_with('test-session')
            self.assertFalse(RoundLatch(directory).allowed())
            self.assertIn('本局已标记', app.stats_status.set.call_args.args[0])

    def test_existing_saved_progress_is_visible_without_pressing_the_button(self):
        app, snapshot = self.app()
        app.opening_stats.update.return_value = {
            'count': 1, 'target': 50, 'last_sample': {'counts': {'7': 2, '8': 0, '9': 1, '10': 0}}}
        app.tick()
        app.opening_stats.arm_match.assert_not_called()
        self.assertIn('1/50', app.stats_status.set.call_args.args[0])
        self.assertIn('下一局第一波', app.stats_status.set.call_args.args[0])
        self.assertIn('A不触发统计', app.stats_status.set.call_args.args[0])

    def test_replay_and_disconnected_frames_cannot_mark_a_new_match(self):
        app, snapshot = self.app()
        snapshot['replay'] = True
        app.mark_opening_match(snapshot)
        app.opening_stats.arm_match.assert_not_called()
        del snapshot['replay']
        snapshot['session_key'] = None
        app.mark_opening_match(snapshot)
        app.opening_stats.arm_match.assert_not_called()

    def test_statistics_failure_does_not_clear_current_radar_or_map(self):
        app, snapshot = self.app()
        app.opening_stats.update.side_effect = OSError('test disk failure')
        app.tick()
        app.overlay.render.assert_called_once()
        app.map_overlay.render.assert_called_once()
        self.assertIn('统计暂停', app.stats_status.set.call_args.args[0])
        app.root.after.assert_called_once()

    def test_replay_cancel_cannot_modify_collector_or_rebuild_saved_latch(self):
        app, snapshot = self.app()
        snapshot['replay'] = True
        app.stats_latch = Mock()
        app.cancel_opening_match()
        app.opening_stats.cancel.assert_not_called()
        app.stats_latch.sync.assert_not_called()
        self.assertIn('离线回放不修改', app.stats_status.set.call_args.args[0])

    def test_stats_get_all_source_rows_independent_of_display_filter(self):
        app, snapshot = self.app()
        app.config['min_level'] = 10
        snapshot['rows'].append(dict(snapshot['rows'][0], level=7, fingerprint='seven'))
        app.tick()
        recorded = app.opening_stats.update.call_args.args[0]
        self.assertEqual([row['level'] for row in recorded['rows']], [4, 7])
        self.assertEqual(recorded['display_min_level'], 10)


if __name__ == '__main__':
    unittest.main()
