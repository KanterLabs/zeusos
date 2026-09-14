"""Contracts for Zeus Settings NetworkManager and BlueZ integration."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop/rootfs/usr/lib/zeus/settings_connectivity.py"
WINDOW_PATH = ROOT / "desktop/rootfs/usr/libexec/zeus-settings-window"

SPEC = importlib.util.spec_from_file_location("settings_connectivity_test", MODULE_PATH)
CONNECTIVITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONNECTIVITY
SPEC.loader.exec_module(CONNECTIVITY)


class _Flags:
    KEY_MGMT_802_1X = 1
    KEY_MGMT_OWE = 2
    KEY_MGMT_PSK = 4
    KEY_MGMT_SAE = 8


class _ApFlags:
    PRIVACY = 1


class _Capabilities:
    AP = 1


class _DeviceType:
    WIFI = 2


class FakeAccessPoint:
    def __init__(self, path, ssid, strength, *, flags=0, wpa=0, rsn=0):
        self._path = path
        self._ssid = ssid
        self._strength = strength
        self._flags = flags
        self._wpa = wpa
        self._rsn = rsn

    def get_path(self):
        return self._path

    def get_ssid(self):
        return self._ssid

    def get_strength(self):
        return self._strength

    def get_flags(self):
        return self._flags

    def get_wpa_flags(self):
        return self._wpa

    def get_rsn_flags(self):
        return self._rsn


class FakeWirelessSetting:
    def __init__(self, ssid):
        self._ssid = ssid

    def get_ssid(self):
        return self._ssid


class FakeConnection:
    def __init__(self, ssid):
        self._ssid = ssid

    def get_connection_type(self):
        return "802-11-wireless"

    def get_setting_wireless(self):
        return FakeWirelessSetting(self._ssid)

    def get_uuid(self):
        return self._ssid.decode("utf-8") + "-uuid"


class FakeDevice:
    def __init__(self, access_points, active=None):
        self._access_points = access_points
        self._active = active

    def get_access_points(self):
        return self._access_points

    def get_active_access_point(self):
        return self._active

    def get_capabilities(self):
        return _Capabilities.AP


class FakeClient:
    def __init__(self, connections):
        self._connections = connections
        self.props = types.SimpleNamespace(wireless_enabled=True)

    def get_connections(self):
        return self._connections


class FakeVariant:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeBluezProxy:
    def __init__(self, **properties):
        self._properties = properties

    def get_cached_property(self, name):
        value = self._properties.get(name)
        return None if value is None else FakeVariant(value)


class SettingsConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.original_nm = CONNECTIVITY.NM
        self.original_flags = CONNECTIVITY.NM_AP_FLAGS
        self.original_security = CONNECTIVITY.NM_AP_SECURITY_FLAGS
        CONNECTIVITY.NM = types.SimpleNamespace(
            SETTING_WIRELESS_SETTING_NAME="802-11-wireless",
            DeviceWifiCapabilities=_Capabilities,
            DeviceType=_DeviceType,
        )
        CONNECTIVITY.NM_AP_FLAGS = _ApFlags
        CONNECTIVITY.NM_AP_SECURITY_FLAGS = _Flags

    def tearDown(self):
        CONNECTIVITY.NM = self.original_nm
        CONNECTIVITY.NM_AP_FLAGS = self.original_flags
        CONNECTIVITY.NM_AP_SECURITY_FLAGS = self.original_security

    def test_password_validation_is_bounded(self):
        self.assertFalse(CONNECTIVITY.validate_personal_password("short"))
        self.assertTrue(CONNECTIVITY.validate_personal_password("eight888"))
        self.assertTrue(CONNECTIVITY.validate_personal_password("a" * 63))
        self.assertFalse(CONNECTIVITY.validate_personal_password("g" * 64))
        self.assertTrue(CONNECTIVITY.validate_personal_password("A0" * 32))

    def test_display_text_strips_controls_and_is_bounded(self):
        value = CONNECTIVITY.clean_display_text("  Zeus\x00  Wi-Fi  " + "x" * 100, "fallback")
        self.assertNotIn("\x00", value)
        self.assertLessEqual(len(value), CONNECTIVITY.MAX_DISPLAY_TEXT)
        self.assertEqual(CONNECTIVITY.ssid_text(b"Cafe Wi-Fi"), "Cafe Wi-Fi")
        self.assertEqual(CONNECTIVITY.ssid_text(b""), "Hidden network")

    def test_snapshot_deduplicates_ssids_and_marks_saved_active_network(self):
        weak = FakeAccessPoint("/weak", b"Zeus", 30, rsn=_Flags.KEY_MGMT_PSK)
        strong = FakeAccessPoint("/strong", b"Zeus", 82, rsn=_Flags.KEY_MGMT_PSK)
        guest = FakeAccessPoint("/guest", b"Guest", 60)
        controller = CONNECTIVITY.WifiController(lambda: None, lambda _message: None)
        controller._client = FakeClient(
            [FakeConnection(b"Zeus"), FakeConnection(b"Away")]
        )
        controller._device = FakeDevice([weak, strong, guest], active=strong)
        state = controller.snapshot()
        self.assertTrue(state.available)
        self.assertTrue(state.enabled)
        self.assertTrue(state.hotspot_supported)
        self.assertEqual(
            [network.name for network in state.networks], ["Zeus", "Guest", "Away"]
        )
        self.assertTrue(state.networks[0].saved)
        self.assertTrue(state.networks[0].active)
        self.assertEqual(state.networks[0].strength, 82)
        self.assertEqual(state.networks[0].security, "personal")
        self.assertFalse(state.networks[2].visible)
        self.assertTrue(state.networks[2].saved)

    def test_bluez_snapshot_is_bounded_and_disconnect_stops_page_scan(self):
        controller = CONNECTIVITY.BluetoothController(lambda: None, lambda _message: None)
        controller._adapter = FakeBluezProxy(Powered=True)
        controller._devices = {
            "/device/1": FakeBluezProxy(
                Alias="  AirPods\x00 Pro  " + "x" * 100,
                Paired=True,
                Connected=True,
                Trusted=True,
            )
        }
        state = controller.snapshot()
        self.assertTrue(state.available)
        self.assertTrue(state.powered)
        self.assertEqual(len(state.devices), 1)
        self.assertLessEqual(len(state.devices[0].name), CONNECTIVITY.MAX_DISPLAY_TEXT)
        self.assertTrue(state.devices[0].connected)
        controller._page_active = True
        controller._stop_discovery = Mock()
        controller.leave()
        self.assertFalse(controller._page_active)
        controller._stop_discovery.assert_called_once_with()

    def test_source_uses_native_event_driven_backends_and_no_cli_secrets(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        window = WINDOW_PATH.read_text(encoding="utf-8")
        for contract in (
            "NM.Client.new_async",
            "request_scan_async",
            "activate_connection_async",
            "add_and_activate_connection2",
            "Gio.DBusObjectManagerClient.new_for_bus",
            '"StartDiscovery"',
            '"StopDiscovery"',
            '"Pair"',
            '"Connect"',
            '"Disconnect"',
            '"RemoveDevice"',
        ):
            self.assertIn(contract, source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("nmcli", source)
        self.assertNotIn("bluetoothctl", source)
        self.assertIn("Gtk.PasswordEntry()", window)
        self.assertIn('entry.set_text("")', window)
        self.assertIn("self._bluetooth_controller.leave()", window)


if __name__ == "__main__":
    unittest.main()
