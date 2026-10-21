"""DPI lifetime checks with fake API objects only; no DLL or window access."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_ui_dpi as dpi


class FakeAPI:
    def __init__(self, set_ok=True, awareness=2, restore_ok=True):
        self.current, self.calls = 101, []
        self.set_ok, self.level, self.restore_ok = set_ok, awareness, restore_ok

    def set_context(self, context):
        value = context.value if hasattr(context, 'value') else context
        self.calls.append(value)
        if value == 101 and not self.restore_ok:
            return None
        if value != 101 and not self.set_ok:
            return None
        old = self.current
        self.current = value
        return old

    def get_context(self):
        return self.current

    def awareness(self, context):
        return self.level

    def equal(self, first, second):
        return first == second


class DpiTests(unittest.TestCase):
    def test_constructor_is_inert(self):
        with patch.object(dpi, '_DpiAPI', side_effect=AssertionError('native')):
            guard = dpi.UIThreadDpi()
            self.assertFalse(guard.ready)
            self.assertTrue(guard.close())


class AppDpiLifetimeTests(unittest.TestCase):
    def test_application_enters_before_ui_init_and_restores_after_destroy(self):
        from fh5_app import Application
        events = []
        guard = Mock()
        guard.enter.side_effect = lambda: events.append('enter') or True
        guard.close.side_effect = lambda: events.append('restore') or True
        with patch('fh5_ui_dpi.UIThreadDpi', return_value=guard), \
             patch.object(Application, '_initialize', side_effect=lambda *args: events.append('ui_init')):
            app = Application(Mock(), {})
        self.assertEqual(events, ['enter', 'ui_init'])
        app.closed, app.after_id, app.save_id = False, None, None
        app.recorder, app.runtime = Mock(), Mock()
        app.overlay, app.map_overlay, app.root = Mock(), Mock(), Mock()
        app.root.destroy.side_effect = lambda: events.append('ui_destroy')
        app.close()
        self.assertEqual(events[-2:], ['ui_destroy', 'restore'])
        self.assertFalse(app.map_dpi_ready)
        self.assertTrue(app.dpi_restore_complete)

    def test_application_initialization_failure_restores_context(self):
        from fh5_app import Application
        guard = Mock()
        guard.enter.return_value = True
        with patch('fh5_ui_dpi.UIThreadDpi', return_value=guard), \
             patch.object(Application, '_initialize', side_effect=RuntimeError('synthetic init failure')):
            with self.assertRaises(RuntimeError):
                Application(Mock(), {})
        guard.close.assert_called_once_with()

    def test_enters_once_and_restores_after_ui_lifetime(self):
        api = FakeAPI()
        with patch.object(dpi, '_DpiAPI', return_value=api):
            guard = dpi.UIThreadDpi()
            self.assertTrue(guard.enter())
            self.assertTrue(guard.enter())
            self.assertEqual(len(api.calls), 1)
            self.assertTrue(guard.ready)
            self.assertEqual(guard.reason, 'per_monitor_v2')
            self.assertTrue(guard.close())
            self.assertEqual(api.current, 101)
            self.assertFalse(guard.ready)
            self.assertTrue(guard.close())
            self.assertEqual(len(api.calls), 2)

    def test_set_context_failure_never_authorizes_map(self):
        api = FakeAPI(set_ok=False)
        with patch.object(dpi, '_DpiAPI', return_value=api):
            guard = dpi.UIThreadDpi()
            self.assertFalse(guard.enter())
            self.assertFalse(guard.ready)
            self.assertEqual(api.current, 101)

    def test_awareness_failure_restores_previous_context(self):
        api = FakeAPI(awareness=1)
        with patch.object(dpi, '_DpiAPI', return_value=api):
            guard = dpi.UIThreadDpi()
            self.assertFalse(guard.enter())
            self.assertFalse(guard.ready)
            self.assertEqual(api.current, 101)

    def test_unavailable_api_does_not_escape_or_authorize(self):
        with patch.object(dpi, '_DpiAPI', side_effect=OSError('unavailable')):
            guard = dpi.UIThreadDpi()
            self.assertFalse(guard.enter())
            self.assertFalse(guard.ready)

    def test_restore_failure_is_reported_and_can_be_retried(self):
        api = FakeAPI(restore_ok=False)
        with patch.object(dpi, '_DpiAPI', return_value=api):
            guard = dpi.UIThreadDpi()
            self.assertTrue(guard.enter())
            self.assertFalse(guard.close())
            self.assertFalse(guard.ready)
            self.assertFalse(guard.enter())
            api.restore_ok = True
            self.assertTrue(guard.close())

    def test_restore_on_another_thread_is_rejected_without_changing_it(self):
        api = FakeAPI()
        with patch.object(dpi, '_DpiAPI', return_value=api), \
             patch.object(dpi.threading, 'get_ident', side_effect=[1, 2, 1]):
            guard = dpi.UIThreadDpi()
            self.assertTrue(guard.enter())
            self.assertFalse(guard.close())
            self.assertEqual(len(api.calls), 1)
            self.assertTrue(guard.close())


if __name__ == '__main__':
    unittest.main()
