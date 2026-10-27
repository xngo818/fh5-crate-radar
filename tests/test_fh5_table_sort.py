"""Candidate table sorting with fake controls, no native input or game reader."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import fh5_config
from test_fh5_map_visibility import bare_application, valid_frame_and_snapshot


def row(name, level, verified=True):
    return {'car': name, 'level': level, 'identity_status': 'verified' if verified else 'unknown',
            'x': 1.0, 'y': 2.0, 'z': 3.0}


class TableSortingTests(unittest.TestCase):
    def app(self, rows):
        app = bare_application(True)
        app.table = Mock()
        app.table.get_children.return_value = ()
        app.save_id = None
        _, snapshot = valid_frame_and_snapshot()
        snapshot['rows'] = rows
        app.runtime.snapshot.return_value = snapshot
        return app, snapshot

    def rendered_names(self, app):
        return [call.kwargs['values'][1] for call in app.table.insert.call_args_list]

    def test_numeric_sort_both_directions_keeps_unknowns_last_and_source_unchanged(self):
        rows = [row('unknown', None), row('ten', 10), row('two-a', 2),
                row('unverified-nine', 9, False), row('two-b', 2), row('four', 4)]
        app, snapshot = self.app(rows)
        original = deepcopy(snapshot)
        for direction, expected in (
            ('asc', ['two-a', 'two-b', 'four', 'ten', 'unknown', 'unverified-nine']),
            ('desc', ['ten', 'four', 'two-a', 'two-b', 'unknown', 'unverified-nine'])):
            app.config['table_level_sort'] = direction
            app.table.insert.reset_mock()
            app._refresh_table(snapshot)
            self.assertEqual(self.rendered_names(app), expected)
            self.assertEqual(snapshot, original)

    def test_header_click_toggles_sort_and_refreshes_without_rescan_or_display_changes(self):
        app, snapshot = self.app([row('ten', 10), row('two', 2)])
        app.toggle_level_sort()
        self.assertEqual(self.rendered_names(app), ['two', 'ten'])
        app.table.heading.assert_called_with('level', text='等级 ↑')
        app.table.insert.reset_mock()
        app.toggle_level_sort()
        self.assertEqual(self.rendered_names(app), ['ten', 'two'])
        app.table.heading.assert_called_with('level', text='等级 ↓')
        app.runtime.request_rescan.assert_not_called()
        app.overlay.render.assert_not_called()
        app.map_overlay.render.assert_not_called()
        app.root.after_cancel.assert_called_once_with('fake-after-id')

    def test_periodic_refresh_retains_sort_and_shows_levels_below_map_filter(self):
        app, snapshot = self.app([row('two', 2), row('ten', 10)])
        app.config.update(table_level_sort='desc', min_level=6)
        for rows, expected in (([row('two', 2), row('ten', 10)], ['ten', 'two']),
                               ([row('one', 1), row('seven', 7), row('four', 4)], ['seven', 'four', 'one'])):
            snapshot['rows'] = rows
            app.table_next = 0
            app.table.insert.reset_mock()
            app.tick()
            self.assertEqual(self.rendered_names(app), expected)
            self.assertEqual(app.config['table_level_sort'], 'desc')
            self.assertEqual(snapshot['rows'], rows)
        app.status.set.assert_called()  # Normal status updates still run.

    def test_sort_preference_survives_save_reload_and_legacy_files_keep_source_order(self):
        rows = [row('ten', 10), row('two', 2)]
        app, snapshot = self.app(rows)
        app.config = fh5_config.validated({'min_level': 6})
        app._refresh_table(snapshot)
        self.assertEqual(self.rendered_names(app), ['ten', 'two'])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(fh5_config, 'STATE_DIR', folder), patch.object(fh5_config, 'PATH', folder / 'settings.json'):
                app.toggle_level_sort()
                app.save()
                restored, same_snapshot = self.app(rows)
                restored.config = fh5_config.load()
                restored._update_level_sort_heading()
                restored._refresh_table(same_snapshot)
                self.assertEqual(self.rendered_names(restored), ['two', 'ten'])
                self.assertEqual(restored.config['min_level'], 6)
                restored.table.heading.assert_called_with('level', text='等级 ↑')


if __name__ == '__main__':
    unittest.main()
