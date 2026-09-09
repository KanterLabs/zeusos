"""Focused checks for the dependency-free parts of the Temp window UX."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'desktop/rootfs/usr/libexec/zeus-temp-window'


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        return _Dummy

    def __call__(self, *args, **kwargs):
        return _Dummy()


def load_window_module():
    gi = types.ModuleType('gi')
    gi.require_version = lambda *_args: None
    repository = types.ModuleType('gi.repository')
    adw = types.SimpleNamespace(
        ApplicationWindow=object,
        Application=object,
        AlertDialog=_Dummy,
        ResponseAppearance=types.SimpleNamespace(SUGGESTED=1),
    )
    gio = types.SimpleNamespace(
        FileMonitor=object,
        File=_Dummy,
        AppInfo=_Dummy,
        FileMonitorFlags=types.SimpleNamespace(WATCH_MOVES=1),
    )
    glib = types.SimpleNamespace(Error=Exception, SOURCE_REMOVE=False, PRIORITY_DEFAULT=0)
    gtk = types.SimpleNamespace(Widget=object)
    pango = types.SimpleNamespace()
    for name, value in (('Adw', adw), ('Gio', gio), ('GLib', glib), ('Gtk', gtk), ('Pango', pango)):
        setattr(repository, name, value)
    gi.repository = repository
    loader = importlib.machinery.SourceFileLoader('zeus_temp_window_test', str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {'gi': gi, 'gi.repository': repository, 'zeus_temp': types.ModuleType('zeus_temp')},
    ):
        loader.exec_module(module)
    return module


WINDOW = load_window_module()


class TempWindowUX(unittest.TestCase):
    @staticmethod
    def callback_window(*, visible):
        window = object.__new__(WINDOW.TempWindow)
        window._closed = False
        window._refresh_when_visible = False
        window._refresh_in_flight = False
        window._refresh_source = None
        window._refresh_again = False
        window._status = {"loaded": True}
        window._schedule_status_refresh = Mock()
        window._is_visible = Mock(return_value=visible)
        return window

    def test_policy_state_callback_is_filtered_to_engine_state_file(self):
        class File:
            def __init__(self, path):
                self.path = path

            def get_path(self):
                return self.path

        window = self.callback_window(visible=True)
        monitor_data = ('state', Path('/tmp/state/zeus'))
        window._on_monitor_changed(None, File('/tmp/state/zeus/other.json'), None, None, monitor_data)
        window._schedule_status_refresh.assert_not_called()
        window._on_monitor_changed(
            None,
            File(f'/tmp/state/zeus/{WINDOW.ENGINE_STATE_FILENAME}'),
            None,
            None,
            monitor_data,
        )
        window._schedule_status_refresh.assert_called_once_with()
        window._schedule_status_refresh.reset_mock()
        window._on_monitor_changed(
            None,
            File('/tmp/state/zeus/old-policy.json'),
            File(f'/tmp/state/zeus/{WINDOW.ENGINE_STATE_FILENAME}'),
            None,
            monitor_data,
        )
        window._schedule_status_refresh.assert_called_once_with()

    def test_monitor_event_is_deferred_while_hidden(self):
        class File:
            def get_path(self):
                return '/tmp/Temp/new-file'

        window = self.callback_window(visible=False)
        window._on_monitor_changed(None, File(), None, None, ('temp', Path('/tmp/Temp')))
        self.assertTrue(window._refresh_when_visible)
        window._schedule_status_refresh.assert_not_called()

    def test_temp_root_event_cancels_monitor_for_replaced_inode(self):
        class File:
            def __init__(self, path):
                self.path = path

            def get_path(self):
                return self.path

        old_monitor = Mock()
        state_monitor = Mock()
        window = self.callback_window(visible=True)
        window._file_monitors = {
            'temp:/tmp/home/Temp': (old_monitor, 'temp'),
            'state:/tmp/state/zeus': (state_monitor, 'state'),
        }
        window._on_monitor_changed(
            None,
            File('/tmp/home/old-temp'),
            File('/tmp/home/Temp'),
            None,
            ('home', Path('/tmp/home')),
        )
        old_monitor.cancel.assert_called_once_with()
        self.assertNotIn('temp:/tmp/home/Temp', window._file_monitors)
        self.assertIn('state:/tmp/state/zeus', window._file_monitors)
        window._schedule_status_refresh.assert_called_once_with()

    def test_restore_callback_flushes_one_deferred_refresh(self):
        window = self.callback_window(visible=False)
        window._on_visibility_changed(window, None)
        self.assertTrue(window._refresh_when_visible)
        window._is_visible.return_value = True
        window._on_surface_state_changed()
        window._schedule_status_refresh.assert_called_once_with()

    def test_disclosure_is_explicit_and_ui_state_is_separate_from_engine_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ui_state = root / 'temp-ui.json'
            engine_state = root / 'temp-policy.json'
            engine_state.write_text('{"schema_version": 1}\n')

            self.assertFalse(WINDOW._disclosure_acknowledged(ui_state))
            self.assertTrue(
                WINDOW._write_ui_state(
                    ui_state,
                    **{WINDOW.UI_ACKNOWLEDGEMENT_KEY: True},
                )
            )
            self.assertTrue(WINDOW._disclosure_acknowledged(ui_state))
            self.assertEqual(engine_state.read_text(), '{"schema_version": 1}\n')

        disclosure = WINDOW._disclosure_text()
        for phrase in ('OS update reboot', 'permanently deleted', 'Keep permanently', 'Downloads'):
            self.assertIn(phrase, disclosure)

    def test_expiration_label_names_permanent_cleanup(self):
        window = object.__new__(WINDOW.TempWindow)
        self.assertIn('permanently clears', window._expiration_text('boot', 0))
        self.assertIn('permanently clears every 15 minutes', window._expiration_text('custom', 0))

    def test_refreshes_are_monitor_driven_and_have_no_repeating_timer(self):
        source = SOURCE.read_text()
        self.assertIn('monitor_directory', source)
        self.assertIn("notify::visible", source)
        self.assertIn('"map"', source)
        self.assertIn('"unmap"', source)
        self.assertIn('REFRESH_DEBOUNCE_MS', source)
        self.assertNotIn('timeout_add_seconds', source)
        self.assertNotIn('SOURCE_CONTINUE', source)


if __name__ == '__main__':
    unittest.main()
