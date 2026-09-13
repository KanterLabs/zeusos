"""Source contracts for the event-driven AirPods connection card."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / (
    "desktop/rootfs/usr/share/gnome-shell/extensions/"
    "zeus-shell@kanterlabs/extension.js"
)
STYLESHEET = EXTENSION.with_name("stylesheet.css")


class AirPodsConnectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = EXTENSION.read_text(encoding="utf-8")
        cls.css = STYLESHEET.read_text(encoding="utf-8")
        cls.monitor = cls.source.split("class BluezAirPodsMonitor", 1)[1].split(
            "const ZeusMenuButton", 1
        )[0]
        cls.card = cls.source.split("class AirPodsConnectionCard", 1)[1].split(
            "class BluezAirPodsMonitor", 1
        )[0]

    def test_monitor_is_read_only_event_driven_bluez(self):
        for contract in (
            "Gio.BusType.SYSTEM",
            "org.freedesktop.DBus.ObjectManager",
            "org.freedesktop.DBus.Properties",
            "org.bluez.Device1",
            "GetManagedObjects",
            "InterfacesAdded",
            "InterfacesRemoved",
            "PropertiesChanged",
            "Gio.DBusCallFlags.NO_AUTO_START",
        ):
            self.assertIn(contract, self.source)
        self.assertNotIn("bluetoothctl", self.source)
        self.assertNotIn("StartDiscovery", self.source)
        self.assertNotIn("SetDiscoveryFilter", self.source)
        self.assertNotIn("Device1.Connect", self.source)

    def test_only_paired_named_airpods_connection_transition_notifies(self):
        self.assertIn("next.paired && isAirPodsName(next.name)", self.monitor)
        self.assertIn("!previous.connected && next.connected", self.monitor)
        self.assertIn("previous.connected && !next.connected", self.monitor)
        self.assertIn("_recordDevice(path, properties, false)", self.monitor)
        self.assertIn("/\\bairpods?\\b/i", self.source)

    def test_card_has_bounded_animation_and_accessible_motion_behavior(self):
        self.assertIn("AIRPODS_CARD_DISMISS_MS = 5200", self.source)
        self.assertIn("GLib.timeout_add_once", self.card)
        self.assertIn("animationsAllowed()", self.card)
        self.assertIn("EASE_OUT_BACK", self.card)
        self.assertIn("remove_all_transitions", self.card)
        self.assertIn("Open Bluetooth Settings", self.card)
        self.assertIn("[GNOME_CONTROL_CENTER, 'bluetooth']", self.card)

    def test_card_does_not_claim_battery_codec_or_audio_routing(self):
        self.assertNotIn("Battery1", self.monitor)
        self.assertNotIn("Percentage", self.monitor)
        self.assertNotIn("codec", self.card.lower())
        self.assertNotIn("audio ready", self.card.lower())
        self.assertNotIn("left battery", self.card.lower())
        self.assertNotIn("right battery", self.card.lower())

    def test_monitor_and_card_cleanup_on_disable(self):
        self.assertIn("this._connection?.signal_unsubscribe(id)", self.monitor)
        self.assertIn("this._cancellable?.cancel()", self.monitor)
        self.assertIn("this._airPodsMonitor?.destroy()", self.source)
        self.assertIn("this._airPodsCard?.destroy()", self.source)
        self.assertIn("GLib.source_remove(this._dismissId)", self.card)
        self.assertIn("Main.layoutManager.removeChrome(this.actor)", self.card)

    def test_card_styles_cover_theme_and_accessibility_states(self):
        for selector in (
            ".zeus-airpods-card",
            ".zeus-airpods-case-lid",
            ".zeus-airpods-pod-head",
            ".zeus-airpods-overlay.zeus-dark",
            ".zeus-airpods-overlay.zeus-high-contrast",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("this._airPodsCard?.actor", self.source)


if __name__ == "__main__":
    unittest.main()
