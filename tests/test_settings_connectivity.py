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

    def get_active_connection(self):
        return None


class FakeClient:
    def __init__(self, connections, devices=()):
        self._connections = connections
        self._devices = devices
        self.props = types.SimpleNamespace(wireless_enabled=True)

    def get_connections(self):
        return self._connections

    def get_devices(self):
        return self._devices


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


class FakeBluezObject:
    def __init__(self, path, *, adapter=None, device=None):
        self._path = path
        self._adapter = adapter
        self._device = device

    def get_object_path(self):
        return self._path

    def get_interface(self, name):
        if name == CONNECTIVITY.BLUEZ_ADAPTER:
            return self._adapter
        if name == CONNECTIVITY.BLUEZ_DEVICE:
            return self._device
        return None


class FakeBluezManager:
    def __init__(self, objects):
        self._objects = objects

    def get_objects(self):
        return self._objects


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

    def test_active_bssid_wins_even_when_stronger_duplicate_arrives_later(self):
        active = FakeAccessPoint("/active", b"Zeus", 24, rsn=_Flags.KEY_MGMT_PSK)
        stronger = FakeAccessPoint("/stronger", b"Zeus", 90, rsn=_Flags.KEY_MGMT_PSK)
        controller = CONNECTIVITY.WifiController(lambda: None, lambda _message: None)
        controller._client = FakeClient([])
        controller._device = FakeDevice([active, stronger], active=active)

        state = controller.snapshot()

        self.assertEqual(len(state.networks), 1)
        self.assertEqual(state.networks[0].token, "/active")
        self.assertTrue(state.networks[0].active)
        self.assertEqual(state.networks[0].strength, 24)
        self.assertEqual(set(controller._access_points), {"/active"})

    def test_wifi_off_hides_stale_networks_and_wpa3_is_distinct(self):
        sae = FakeAccessPoint("/sae", b"Zeus WPA3", 75, rsn=_Flags.KEY_MGMT_SAE)
        controller = CONNECTIVITY.WifiController(lambda: None, lambda _message: None)
        client = FakeClient([])
        client.props.wireless_enabled = False
        controller._client = client
        controller._device = FakeDevice([sae])

        state = controller.snapshot()

        self.assertTrue(state.available)
        self.assertFalse(state.enabled)
        self.assertEqual(state.networks, ())
        self.assertEqual(controller._security(sae), "sae")

    def test_loaded_network_manager_without_radio_is_unavailable(self):
        controller = CONNECTIVITY.WifiController(lambda: None, lambda _message: None)
        controller._client = FakeClient([])
        controller._status = "ready"

        controller._select_device()

        self.assertEqual(controller.snapshot().status, "unavailable")

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

    def test_bluez_off_hides_unsaved_cached_devices(self):
        controller = CONNECTIVITY.BluetoothController(lambda: None, lambda _message: None)
        controller._adapter = FakeBluezProxy(Powered=False)
        controller._devices = {
            "/device/saved": FakeBluezProxy(Alias="Saved", Paired=True),
            "/device/stale": FakeBluezProxy(Alias="Stale", Paired=False, RSSI=-50),
        }

        state = controller.snapshot()

        self.assertFalse(state.powered)
        self.assertEqual([device.name for device in state.devices], ["Saved"])

    def test_bluez_uses_one_deterministic_adapter_and_its_devices_only(self):
        hci0 = FakeBluezProxy(Powered=True)
        hci1 = FakeBluezProxy(Powered=True)
        own = FakeBluezProxy(Alias="Own", Paired=True)
        other = FakeBluezProxy(Alias="Other", Paired=True)
        controller = CONNECTIVITY.BluetoothController(lambda: None, lambda _message: None)
        controller._manager = FakeBluezManager(
            [
                FakeBluezObject("/org/bluez/hci1", adapter=hci1),
                FakeBluezObject("/org/bluez/hci1/dev_OTHER", device=other),
                FakeBluezObject("/org/bluez/hci0/dev_OWN", device=own),
                FakeBluezObject("/org/bluez/hci0", adapter=hci0),
            ]
        )

        controller._refresh_objects()

        self.assertIs(controller._adapter, hci0)
        self.assertEqual(controller._adapter_path, "/org/bluez/hci0")
        self.assertEqual(set(controller._devices), {"/org/bluez/hci0/dev_OWN"})

    def test_pending_device_call_is_not_repeated(self):
        changed = Mock()
        controller = CONNECTIVITY.BluetoothController(changed, lambda _message: None)
        controller._adapter = FakeBluezProxy(Powered=True)
        proxy = FakeBluezProxy(Alias="AirPods", Paired=False, RSSI=-40)
        proxy.call = Mock()
        controller._devices = {"/device/1": proxy}
        controller._agent_ready = True

        controller.pair("/device/1")
        controller.pair("/device/1")

        proxy.call.assert_called_once()
        self.assertEqual(controller._pending, "pair:/device/1")

    def test_pairing_confirmation_waits_for_zeus_answer(self):
        requests = []
        controller = CONNECTIVITY.BluetoothController(
            lambda: None,
            lambda _message: None,
            lambda prompt, respond: requests.append((prompt, respond)),
        )
        controller._devices = {
            "/device/1": FakeBluezProxy(Alias="Keyboard", Paired=False)
        }
        invocation = Mock()

        controller._on_agent_method_call(
            None,
            None,
            None,
            CONNECTIVITY.BLUEZ_AGENT,
            "RequestConfirmation",
            FakeVariant(("/device/1", 1234)),
            invocation,
        )

        self.assertEqual(requests[0][0].name, "Keyboard")
        self.assertEqual(requests[0][0].code, "001234")
        invocation.return_value.assert_not_called()
        requests[0][1](True)
        invocation.return_value.assert_called_once()

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
            '"RegisterAgent"',
            '"sae": "sae"',
            'GLib.Variant("s", "volatile")',
        ):
            self.assertIn(contract, source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("nmcli", source)
        self.assertNotIn("bluetoothctl", source)
        self.assertIn("Gtk.PasswordEntry()", window)
        self.assertIn('entry.set_text("")', window)
        self.assertIn('dialog.set_response_enabled("connect", False)', window)
        self.assertIn('dialog.set_response_enabled("start", False)', window)
        self.assertIn("self._bluetooth_controller.leave()", window)


if __name__ == "__main__":
    unittest.main()
