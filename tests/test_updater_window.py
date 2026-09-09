"""Focused tests for the dependency-free parts of the Updates window."""

import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "desktop/rootfs/usr/libexec/zeus-update-window"
DESKTOP = ROOT / "desktop/rootfs/usr/share/applications/org.zeus.Updates.desktop"


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        return _Dummy

    def __call__(self, *args, **kwargs):
        return _Dummy()


def load_window_module():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")

    class _ApplicationWindow:
        pass

    class _Application:
        pass

    adw = types.SimpleNamespace(
        ApplicationWindow=_ApplicationWindow,
        Application=_Application,
        AlertDialog=_Dummy,
        ResponseAppearance=types.SimpleNamespace(SUGGESTED=1),
    )
    gio = types.SimpleNamespace(
        FileMonitor=object,
        FileMonitorEvent=object,
        File=_Dummy,
        FileMonitorFlags=types.SimpleNamespace(WATCH_MOVES=1),
    )
    glib = types.SimpleNamespace(
        Error=Exception,
        SOURCE_REMOVE=False,
        PRIORITY_DEFAULT=0,
    )
    gtk = types.SimpleNamespace(Widget=object)
    pango = types.SimpleNamespace()
    for name, value in (("Adw", adw), ("Gio", gio), ("GLib", glib), ("Gtk", gtk), ("Pango", pango)):
        setattr(repository, name, value)
    gi.repository = repository
    loader = importlib.machinery.SourceFileLoader("zeus_update_window_test", str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"gi": gi, "gi.repository": repository, loader.name: module},
    ):
        loader.exec_module(module)
    return module


WINDOW = load_window_module()


def candidate(**updates):
    value = {
        "version": "0.1.0-preview.2",
        "build_id": "git-abcdef012345",
        "source_commit": "abcdef012345abcdef012345abcdef012345abcd",
        "sequence": 2,
        "published_at": "2026-09-09T00:00:00Z",
        "notes": "Release notes",
        "archive_size": 12345,
        "archive_sha256": "a" * 64,
    }
    value.update(updates)
    return value


class UpdateWindowLogic(unittest.TestCase):
    def test_status_normalisation_clamps_progress_and_keeps_release_details(self):
        status = WINDOW._normalise_status(
            {
                "ok": True,
                "state": "downloading",
                "current": {"version": "0.1.0-preview.2", "build_id": "git-0123456789ab", "sequence": 1},
                "candidate": candidate(),
                "progress": {"bytes": 1500, "total": 1000},
            }
        )
        self.assertEqual(status["state"], "downloading")
        self.assertEqual(status["progress"], {"bytes": 1000, "total": 1000})
        self.assertEqual(status["candidate"]["build_id"], "git-abcdef012345")
        self.assertTrue(status["candidate"]["installable"])

    def test_candidate_with_bad_digest_cannot_be_installed(self):
        status = WINDOW._normalise_status(
            {"state": "available", "candidate": candidate(archive_sha256="not-a-digest")}
        )
        self.assertFalse(status["candidate"]["installable"])
        self.assertFalse(WINDOW._candidate_is_installable(status["candidate"]))

    def test_candidate_build_id_matches_backend_shape(self):
        self.assertTrue(WINDOW._candidate_is_installable({"build_id": "git-abcdef012345", "archive_sha256": "a" * 64}))
        self.assertFalse(WINDOW._candidate_is_installable({"build_id": "git-ABCDEF012345", "archive_sha256": "a" * 64}))
        self.assertFalse(WINDOW._candidate_is_installable({"build_id": "git-abcdef01234", "archive_sha256": "a" * 64}))

    def test_root_job_overrides_stale_network_check(self):
        queued = WINDOW._normalise_status(
            {
                "state": "downloading",
                "candidate": candidate(),
                "progress": {"bytes": 400, "total": 1000},
                "message": "Downloading the signed update.",
            }
        )
        network = WINDOW._normalise_status(
            {"state": "available", "candidate": candidate(build_id="git-fedcba987654"), "message": "New build"}
        )
        merged = WINDOW._merge_authoritative_status(network, queued)
        self.assertEqual(merged["state"], "downloading")
        self.assertEqual(merged["progress"], {"bytes": 400, "total": 1000})
        self.assertEqual(merged["candidate"]["build_id"], "git-abcdef012345")

    def test_structured_terminal_status_clears_busy_snapshot_and_allows_retry(self):
        for terminal_state in ("error", "interrupted"):
            with self.subTest(state=terminal_state):
                window = object.__new__(WINDOW.UpdatesWindow)
                window._closed = False
                window._status_request_number = 1
                window._check_request_number = 0
                window._status_in_flight = True
                window._refresh_again = False
                window._initial_check_started = True
                window._authoritative_status = WINDOW._normalise_status(
                    {"state": "downloading", "progress": {"bytes": 2, "total": 10}}
                )
                window._status = dict(window._authoritative_status)
                window._operation_notice = ""
                window._operation_notice_kind = "info"
                window._render_status = lambda: None
                payload = {
                    "ok": False,
                    "state": terminal_state,
                    "message": "Installation was interrupted. Check again to retry.",
                    "error": "interrupted",
                }
                outcome = WINDOW.CommandOutcome(payload, 1, "Installation was interrupted. Check again to retry.")

                window._after_status(1, outcome, None, False)

                self.assertEqual(window._status["state"], terminal_state)
                self.assertIsNone(window._authoritative_status)
                window._check_in_flight = False
                window._install_in_flight = False
                window._submit_async = lambda *_args, **_kwargs: None
                window._request_check()
                self.assertTrue(window._check_in_flight)

    def test_authentication_denial_is_not_success(self):
        outcome = WINDOW.CommandOutcome(None, 126, "Not authorized")
        self.assertTrue(WINDOW._is_authentication_denial(outcome))
        self.assertFalse(WINDOW._is_authentication_denial(WINDOW.CommandOutcome({}, 0)))

    def test_human_structured_message_precedes_error_code(self):
        payload = {"error": "bad_signature", "message": "The release signature is invalid."}
        self.assertEqual(WINDOW._command_error(payload), "The release signature is invalid.")
        self.assertTrue(WINDOW._looks_trust_error(WINDOW._command_error(payload)))
        self.assertFalse(WINDOW._looks_offline(WINDOW._command_error(payload)))

    def test_network_failures_have_an_honest_offline_state(self):
        self.assertTrue(WINDOW._looks_offline("Could not reach the update service"))
        self.assertTrue(WINDOW._looks_offline("network unavailable"))
        self.assertFalse(WINDOW._looks_offline("The signed build is not newer"))

    def test_fixed_command_uses_json_and_no_shell(self):
        completed = subprocess.CompletedProcess(
            ["/usr/libexec/zeus-update", "status", "--json"],
            0,
            '{"ok":true,"state":"up_to_date"}',
            "",
        )
        with patch.object(WINDOW.subprocess, "run", return_value=completed) as run:
            result = WINDOW._run_json_command((WINDOW.UPDATE_COMMAND, "status", "--json"))
        self.assertEqual(result.returncode, 0)
        run.assert_called_once_with(
            ["/usr/libexec/zeus-update", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=WINDOW.COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_ui_uses_monitor_debounce_and_explicit_restart_warning(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("monitor_directory", source)
        self.assertIn("REFRESH_DEBOUNCE_MS", source)
        self.assertIn('UPDATE_COMMAND, "status", "--json"', source)
        self.assertIn('UPDATE_COMMAND, "check", "--json"', source)
        self.assertIn('"--disable-internal-agent"', source)
        self.assertIn('PKEXEC_COMMAND = "/usr/bin/pkexec"', source)
        self.assertIn("gnome-session-quit", source)
        self.assertIn("Temp uses the On boot policy", source)
        self.assertIn("close this window and it will continue", source)
        self.assertNotIn("timeout_add_seconds", source)

    def test_desktop_entry_is_native_settings_application(self):
        desktop = DESKTOP.read_text(encoding="utf-8")
        self.assertIn("Name=Updates", desktop)
        self.assertIn("Exec=/usr/libexec/zeus-update-window", desktop)
        self.assertIn("Icon=system-software-update", desktop)
        self.assertIn("Categories=GTK;GNOME;Settings;System;", desktop)
        self.assertNotIn("Terminal=true", desktop)


if __name__ == "__main__":
    unittest.main()
