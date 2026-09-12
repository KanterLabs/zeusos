"""Focused contracts for the dependency-free Zeus Settings home."""

import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "desktop/rootfs/usr/lib/zeus"
SOURCE = ROOT / "desktop/rootfs/usr/libexec/zeus-settings-window"
DESKTOP = ROOT / "desktop/rootfs/usr/share/applications/org.zeus.Settings.desktop"


sys.path.insert(0, str(MODEL_PATH))
import settings_model as MODEL


def load_window_module():
    """Load the GTK module with tiny GI stand-ins for launch-flow tests."""

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    adw = types.SimpleNamespace(ApplicationWindow=object, Application=object)
    gio = types.SimpleNamespace(Application=object)
    glib = types.SimpleNamespace(Error=Exception, PRIORITY_DEFAULT=0)
    gtk = types.SimpleNamespace(Widget=object)
    pango = types.SimpleNamespace()
    for name, value in (("Adw", adw), ("Gio", gio), ("GLib", glib), ("Gtk", gtk), ("Pango", pango)):
        setattr(repository, name, value)
    gi.repository = repository
    loader = importlib.machinery.SourceFileLoader("zeus_settings_window_test", str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "gi": gi,
            "gi.repository": repository,
            "settings_model": MODEL,
            loader.name: module,
        },
    ):
        loader.exec_module(module)
    return module


WINDOW = load_window_module()


def write_sysfs_fixture(root: Path, *, battery=False, backlight=False, wifi=False, bluetooth=False):
    if battery:
        supply = root / "class/power_supply/BAT0"
        supply.mkdir(parents=True)
        (supply / "type").write_text("Battery\n", encoding="ascii")
    if backlight:
        (root / "class/backlight/intel_backlight").mkdir(parents=True)
    if wifi:
        (root / "class/net/wlp1s0/wireless").mkdir(parents=True)
    if bluetooth:
        (root / "class/bluetooth/hci0").mkdir(parents=True)


class SettingsModelTests(unittest.TestCase):
    def test_panel_ids_match_fedora_gnome_50_inventory(self):
        by_key = MODEL.DESTINATIONS_BY_KEY
        self.assertEqual(by_key["network"].panel_ids, ("wifi", "network"))
        self.assertEqual(by_key["appearance"].panel_ids, ("background",))
        for key, panel_id, desktop_id in (
            ("bluetooth", "bluetooth", "gnome-bluetooth-panel.desktop"),
            ("display", "display", "gnome-display-panel.desktop"),
            ("power", "power", "gnome-power-panel.desktop"),
            ("sound", "sound", "gnome-sound-panel.desktop"),
            ("appearance", "background", "gnome-background-panel.desktop"),
        ):
            self.assertEqual(by_key[key].panel_ids, (panel_id,))
            self.assertEqual(by_key[key].panel_desktop_ids, (desktop_id,))
        self.assertEqual(MODEL.panel_argv(by_key["appearance"]), ("/usr/bin/gnome-control-center", "background"))

    def test_network_panel_order_follows_detected_wifi_without_querying_network(self):
        item = MODEL.destination("network")
        self.assertEqual(
            MODEL.panel_attempt_order(item, MODEL.HardwareState(False, False, True, False)),
            (0, 1),
        )
        self.assertEqual(
            MODEL.panel_attempt_order(item, MODEL.HardwareState(False, False, False, False)),
            (1, 0),
        )
        self.assertEqual(MODEL.panel_attempt_order(item, None), (1, 0))

    def test_all_hardware_is_detected_from_one_bounded_read_only_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sysfs_fixture(root, battery=True, backlight=True, wifi=True, bluetooth=True)
            state = MODEL.discover_hardware(sysfs_root=root)
        self.assertEqual(state, MODEL.HardwareState(True, True, True, True))
        self.assertIn("Battery detected", MODEL.hardware_summary(state))
        self.assertIn("Wi-Fi radio detected", MODEL.hardware_summary(state))

    def test_missing_vm_hardware_is_distinguished_from_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = MODEL.discover_hardware(sysfs_root=Path(directory))
        self.assertEqual(state, MODEL.HardwareState(False, False, False, False))
        self.assertIn("No battery detected here", MODEL.hardware_summary(state))
        self.assertIn("No laptop backlight detected here", MODEL.hardware_summary(state))

    def test_unreadable_or_truncated_class_does_not_claim_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            power = root / "class/power_supply"
            power.mkdir(parents=True)
            # A child without a readable type is an unknown device class, not
            # proof that the machine has no battery.
            (power / "mystery").mkdir()
            (power / "mystery/type").mkdir()
            self.assertIsNone(MODEL.discover_hardware(sysfs_root=root).battery)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            power = root / "class/power_supply"
            power.mkdir(parents=True)
            for index in range(MODEL.MAX_DISCOVERY_ENTRIES + 1):
                (power / f"device{index}").mkdir()
            self.assertIsNone(MODEL.discover_hardware(sysfs_root=root).battery)

    def test_build_identity_is_local_and_has_safe_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "version"
            build = root / "build-id"
            version.write_text("0.1.0-preview.2\n", encoding="utf-8")
            build.write_text("git-0123456789ab\n", encoding="utf-8")
            self.assertEqual(
                MODEL.read_build_identity(version_path=version, build_id_path=build),
                ("0.1.0-preview.2", "git-0123456789ab"),
            )
            self.assertEqual(
                MODEL.read_build_identity(version_path=root / "missing", build_id_path=root / "missing-build"),
                (MODEL.PRODUCT_VERSION, MODEL.DEFAULT_BUILD_ID),
            )

    def test_developer_status_normalises_provenance_and_required_activation(self):
        status = MODEL.developer_status_from_payload(
            {
                "state": "active",
                "base_build_id": "git-base-012345",
                "source_commit": "a" * 40,
                "active_artifact_digest": "sha256:" + "b" * 64,
                "required_activation": "log-out",
                "focused_test_receipt": {"result": "passed", "checked_at": "2026-09-11"},
                "application_time": "2026-09-11T01:00:00Z",
            }
        )
        self.assertEqual(status.state, "active")
        self.assertEqual(status.base_build, "git-base-012345")
        self.assertEqual(status.active_commit, "a" * 40)
        self.assertEqual(status.artifact_digest, "sha256:" + "b" * 64)
        self.assertEqual(status.required_action, "logout")
        self.assertEqual(status.focused_test_receipt, "passed · 2026-09-11")
        self.assertEqual(status.applied_at, "2026-09-11T01:00:00Z")

        status = MODEL.developer_status_from_json(
            '{"state":"needs-rebuild","base_build":"git-new","required_action":"logout"}'
        )
        self.assertEqual(status.state, "incompatible")
        self.assertEqual(status.required_action, "logout")

        status = MODEL.developer_status_from_payload(
            {
                "state": "disabled",
                "base_sysext_level": "git-base-012345",
                "required_activation": ["restart-settings"],
            }
        )
        self.assertEqual(status.state, "off")
        self.assertEqual(status.required_action, "restart")

    def test_developer_status_rejects_unreadable_payload_without_claiming_active(self):
        status = MODEL.developer_status_from_json("not json")
        self.assertEqual(status.state, "error")
        self.assertNotIn("active", status.error.lower())
        self.assertEqual(MODEL.developer_status_from_json("{}").state, "error")
        self.assertEqual(MODEL.developer_status_from_payload({"error": "broken"}).state, "error")
        self.assertEqual(MODEL.developer_argv("status", json_status=True), (MODEL.DEVELOPER_HELPER_PATH, "status", "--json"))
        self.assertEqual(
            MODEL.developer_argv("apply", artifact_digest="a" * 64),
            (MODEL.DEVELOPER_HELPER_PATH, "apply", "a" * 64),
        )
        with self.assertRaises(ValueError):
            MODEL.developer_argv("apply", json_status=True)
        with self.assertRaises(ValueError):
            MODEL.developer_argv("apply", artifact_digest="../unsafe")
        with self.assertRaises(ValueError):
            MODEL.developer_argv("apply", artifact_digest="A" * 64)


class SettingsLaunchTests(unittest.TestCase):
    def new_window(self):
        window = object.__new__(WINDOW.SettingsWindow)
        window._closed = False
        window._hardware = MODEL.HardwareState(False, False, False, False)
        window._show_toast = Mock()
        return window

    def test_missing_hidden_panel_tries_second_panel_then_native_fallback(self):
        window = self.new_window()
        item = MODEL.destination("network")
        window._launch_desktop_app = Mock(return_value=False)
        window._try_panel_command = Mock()
        window._try_panel_desktop(item, 0)
        self.assertEqual(window._launch_desktop_app.call_args_list[0][0][0], "gnome-network-panel.desktop")
        self.assertEqual(
            window._launch_desktop_app.call_args_list[1][0][0],
            "gnome-wifi-panel.desktop",
        )
        window._try_panel_command.assert_called_once_with(item, 0, (1, 0))

    def test_wifi_present_tries_focused_wifi_panel_first(self):
        window = self.new_window()
        window._hardware = MODEL.HardwareState(False, False, True, False)
        item = MODEL.destination("network")
        window._launch_desktop_app = Mock(return_value=False)
        window._try_panel_command = Mock()
        window._try_panel_desktop(item, 0, MODEL.panel_attempt_order(item, window._hardware))
        self.assertEqual(window._launch_desktop_app.call_args_list[0][0][0], "gnome-wifi-panel.desktop")
        self.assertEqual(window._launch_desktop_app.call_args_list[1][0][0], "gnome-network-panel.desktop")
        window._try_panel_command.assert_called_once_with(item, 0, (0, 1))

    def test_failed_panel_command_tries_next_fixed_argv_then_native_settings(self):
        window = self.new_window()
        item = MODEL.destination("network")
        window._spawn_fixed = Mock(return_value=None)
        window._open_native_settings = Mock()
        window._try_panel_command(item, 0)
        window._spawn_fixed.assert_any_call(("/usr/bin/gnome-control-center", "network"), "Wi-Fi & Network")
        window._spawn_fixed.assert_any_call(("/usr/bin/gnome-control-center", "wifi"), "Wi-Fi & Network")
        window._open_native_settings.assert_called_once_with()

    def test_failed_single_panel_process_falls_back_without_blocking(self):
        window = self.new_window()
        item = MODEL.destination("bluetooth")

        class FailedProcess:
            def wait_check_async(self, _priority, _cancellable, callback, context):
                callback(self, object(), context)

            def wait_check_finish(self, _result):
                return False

        window._spawn_fixed = Mock(return_value=FailedProcess())
        window._open_native_settings = Mock()
        window._try_panel_command(item, 0)
        window._spawn_fixed.assert_called_once_with(("/usr/bin/gnome-control-center", "bluetooth"), "Bluetooth")
        window._open_native_settings.assert_called_once_with()

    def test_false_desktop_launch_is_treated_as_unlaunchable(self):
        class FalseDesktop:
            def launch(self, _files, _context):
                return False

        class DesktopAppInfo:
            @staticmethod
            def new(_desktop_id):
                return FalseDesktop()

        original = getattr(WINDOW.Gio, "DesktopAppInfo", None)
        WINDOW.Gio.DesktopAppInfo = DesktopAppInfo
        try:
            window = self.new_window()
            window._launch_context = Mock(return_value=None)
            self.assertFalse(window._launch_desktop_app("gnome-network-panel.desktop", "Network"))
        finally:
            WINDOW.Gio.DesktopAppInfo = original

    def test_activation_refresh_updates_visible_hardware_labels_without_rebuilding(self):
        window = self.new_window()
        window._hardware_summary_label = Mock()
        window._brightness_label = Mock()
        updated = MODEL.HardwareState(False, True, False, False)
        with patch.object(WINDOW, "_safe_hardware_snapshot", return_value=updated):
            window._on_active_changed(window, None)
        self.assertEqual(window._hardware, updated)
        window._hardware_summary_label.set_text.assert_called_once_with(MODEL.hardware_summary(updated))
        window._brightness_label.set_text.assert_called_once_with(window._brightness_text())

    def test_activation_refresh_rechecks_developer_status_without_polling(self):
        window = self.new_window()
        window._developer_status = MODEL.default_developer_status()
        window._request_developer_status = Mock()
        with patch.object(WINDOW, "_safe_hardware_snapshot", return_value=window._hardware):
            window._on_active_changed(window, None)
        window._request_developer_status.assert_called_once_with(preserve_notice=True)

    def test_window_has_fixed_desktop_entry_and_no_shell_or_polling(self):
        desktop = DESKTOP.read_text(encoding="utf-8")
        self.assertIn("Name=Zeus Settings", desktop)
        self.assertIn("Exec=/usr/libexec/zeus-settings-window", desktop)
        self.assertIn("Icon=org.zeus.Settings", desktop)
        self.assertIn("Categories=GTK;GNOME;Settings;System;", desktop)
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("set_show_start_title_buttons(True)", source)
        self.assertIn("set_show_end_title_buttons(True)", source)
        self.assertIn('header.pack_end(all_settings)', source)
        self.assertIn("Gio.Subprocess.new(list(arguments), Gio.SubprocessFlags.NONE)", source)
        self.assertIn("if result is False:", source)
        self.assertIn('notify::is-active', source)
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("timeout_add", source)
        self.assertNotIn("Gio.Settings", source)

    def test_window_has_developer_provenance_card_and_fixed_actions(self):
        source = SOURCE.read_text(encoding="utf-8")
        for text in (
            "Advanced",
            "Developer Mode",
            "developer_status_summary",
            "_developer_activation_text",
            "focused_test_receipt",
            "Administrator authentication may be requested",
            "Cancel",
            "Apply prepared change",
            "Undo last apply",
            "Turn off Developer Mode",
            'DEVELOPER_COMMAND = "/usr/libexec/zeus-developer"',
            "Gio.Subprocess.new(list(arguments), flags)",
            "communicate_utf8_async",
        ):
            self.assertIn(text, source)
        self.assertNotIn("timeout_add", source)
        self.assertNotIn("subprocess.run", source)

    def test_developer_cancel_is_exposed_and_requests_cancellation(self):
        window = self.new_window()
        window._developer_action_in_flight = True
        window._developer_notice = ""
        window._developer_notice_kind = "info"
        cancellable = Mock()
        window._developer_cancellable = cancellable
        window._render_developer_status = Mock()
        window._on_developer_cancel_clicked(Mock())
        cancellable.cancel.assert_called_once_with()
        self.assertIn("Canceling", window._developer_notice)


if __name__ == "__main__":
    unittest.main()
