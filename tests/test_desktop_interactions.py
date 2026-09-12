"""Contract checks for the lightweight desktop interaction layer."""

import configparser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / (
    "desktop/rootfs/usr/share/gnome-shell/extensions/"
    "zeus-shell@kanterlabs/extension.js"
)
DCONF = ROOT / "desktop/rootfs/etc/dconf/db/local.d/00-zeus"
WELCOME = ROOT / "zeus/assets/welcome.py"
GNOME_SETTINGS_ICON = ROOT / (
    "desktop/rootfs/usr/share/icons/Zeus/scalable/apps/"
    "org.gnome.Settings.svg"
)
ZEUS_SETTINGS_ICON = ROOT / (
    "desktop/rootfs/usr/share/icons/hicolor/scalable/apps/"
    "org.zeus.Settings.svg"
)
GTK4_THEME = ROOT / "desktop/rootfs/usr/share/themes/Zeus/gtk-4.0/gtk.css"


class DesktopInteractions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extension = EXTENSION.read_text(encoding="utf-8")
        cls.dconf = configparser.ConfigParser(interpolation=None, strict=True)
        cls.dconf.read(DCONF, encoding="utf-8")
        cls.welcome = WELCOME.read_text(encoding="utf-8")

    def test_shell_discovery_uses_actor_signals(self):
        self.assertIn("child-added", self.extension)
        self.assertIn("_watchPanel", self.extension)
        self.assertIn("_watchDock", self.extension)
        self.assertNotIn("DOCK_LOOKUP_ATTEMPTS", self.extension)
        self.assertNotIn("_panelRetryId", self.extension)
        self.assertNotIn("_dockLookupId", self.extension)
        self.assertNotIn("GLib.timeout_add(", self.extension)

    def test_search_refresh_is_coalesced_and_motion_aware(self):
        self.assertIn("_queueRefreshResults", self.extension)
        self.assertIn("timeout_add_once", self.extension)
        self.assertIn("enable_animations", self.extension)
        self.assertIn("reduced_motion", self.extension)
        self.assertIn("zeus-reduced-motion", self.extension)

    def test_dock_destroy_rediscovery_waits_for_actor_removal(self):
        self.assertIn("_dockRediscoveryId", self.extension)
        self.assertIn("_scheduleDockRediscovery", self.extension)
        self.assertIn("_cancelDockRediscovery", self.extension)
        self.assertIn("GLib.idle_add_once", self.extension)
        self.assertIn("GLib.source_remove(this._dockRediscoveryId)", self.extension)

        destroy_callback = self.extension.split("dock.connectObject('destroy',", 1)[1]
        destroy_callback = destroy_callback.split("}, this);", 1)[0]
        self.assertIn("_scheduleDockRediscovery()", destroy_callback)
        self.assertNotIn("this._watchDock()", destroy_callback)
        self.assertIn("_cancelDockRediscovery()", self.extension)

    def test_shortcut_defaults_and_visible_help_match(self):
        media_keys = self.dconf["org/gnome/settings-daemon/plugins/media-keys"]
        bindings = media_keys["custom-keybindings"]
        self.assertIn("zeus-files", bindings)
        self.assertIn("zeus-temp", bindings)
        self.assertIn("zeus-terminal", bindings)

        files = self.dconf[
            "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/zeus-files"
        ]
        self.assertEqual(files["binding"].strip("'"), "<Super>e")
        self.assertEqual(files["command"].strip("'"), "/usr/bin/nautilus")

        temp = self.dconf[
            "org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/zeus-temp"
        ]
        self.assertEqual(temp["binding"].strip("'"), "<Super><Shift>t")
        self.assertEqual(temp["command"].strip("'"), "/usr/libexec/zeus-temp-window")

        for shortcut in (
            "Super + Space", "Super + E", "Super + Shift + T", "Super + Return"
        ):
            self.assertIn(shortcut, self.welcome)
        self.assertIn("customized in Settings", self.welcome)
        self.assertIn("existing personal settings are preserved", self.welcome)

    def test_dock_defaults_stay_fixed_and_visible(self):
        dock = self.dconf["org/gnome/shell/extensions/dash-to-dock"]
        self.assertEqual(dock["dock-fixed"], "true")
        self.assertEqual(dock["autohide"], "false")
        self.assertEqual(dock["intellihide"], "false")
        self.assertEqual(dock["show-favorites"], "true")

    def test_settings_destinations_have_distinct_identity(self):
        self.assertIn(
            "_addLauncher('Zeus Settings', 'org.zeus.Settings.desktop')",
            self.extension,
        )
        self.assertIn(
            "_addLauncher('GNOME Settings', 'org.gnome.Settings.desktop')",
            self.extension,
        )
        self.assertTrue(ZEUS_SETTINGS_ICON.is_file())
        self.assertFalse(
            GNOME_SETTINGS_ICON.exists(),
            "The Zeus icon theme must not replace the native GNOME Settings icon",
        )

    def test_sidebar_header_reserves_room_for_its_title(self):
        css = GTK4_THEME.read_text(encoding="utf-8")
        self.assertIn(".sidebar-pane windowcontrols button.minimize", css)
        self.assertIn(".sidebar-pane windowcontrols button.maximize", css)
        self.assertIn(".sidebar-pane windowcontrols button.restore", css)


if __name__ == "__main__":
    unittest.main()
