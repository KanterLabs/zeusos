"""Event-driven NetworkManager and BlueZ adapters for Zeus Settings.

The desktop keeps the supported Fedora services as the source of truth.  This
module owns no persistent state and never invokes a shell or command-line
client.  UI callbacks receive bounded display models; Wi-Fi secrets are passed
directly to NetworkManager and are never included in status or error text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

try:
    gi.require_version("NM", "1.0")
    from gi.repository import NM
except (ImportError, ValueError):  # Source-test hosts need not ship libnm GI.
    NM = None

NM_AP_FLAGS = getattr(NM, "80211ApFlags", None)
NM_AP_SECURITY_FLAGS = getattr(NM, "80211ApSecurityFlags", None)


MAX_DISPLAY_TEXT = 80
BLUEZ_NAME = "org.bluez"
BLUEZ_ROOT = "/"
BLUEZ_ADAPTER = "org.bluez.Adapter1"
BLUEZ_DEVICE = "org.bluez.Device1"
DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS_TIMEOUT_MS = 15_000


def clean_display_text(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    clean = " ".join(value.replace("\x00", " ").split())[:MAX_DISPLAY_TEXT]
    return clean or fallback


def validate_personal_password(value: object) -> bool:
    """Accept WPA/WPA2/WPA3 personal passphrases without retaining them."""

    if not isinstance(value, str):
        return False
    if 8 <= len(value) <= 63:
        return True
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


@dataclass(frozen=True)
class WifiNetwork:
    token: str
    name: str
    strength: int
    security: str
    saved: bool
    active: bool
    visible: bool = True


@dataclass(frozen=True)
class WifiState:
    available: bool
    enabled: bool
    scanning: bool
    hotspot_supported: bool
    networks: tuple[WifiNetwork, ...]


@dataclass(frozen=True)
class BluetoothDevice:
    token: str
    name: str
    paired: bool
    connected: bool
    trusted: bool


@dataclass(frozen=True)
class BluetoothState:
    available: bool
    powered: bool
    scanning: bool
    devices: tuple[BluetoothDevice, ...]


def _variant_value(value: object, fallback=None):
    try:
        return value.unpack() if value is not None else fallback
    except (AttributeError, TypeError, ValueError):
        return fallback


def _ssid_bytes(value: object) -> bytes:
    try:
        data = value.get_data()
    except (AttributeError, TypeError, ValueError):
        data = value
    if isinstance(data, tuple):
        data = data[0]
    if isinstance(data, bytes):
        return data[:32]
    if isinstance(data, (list, tuple)):
        try:
            return bytes(data[:32])
        except (TypeError, ValueError):
            return b""
    return b""


def ssid_text(value: object) -> str:
    data = _ssid_bytes(value)
    if not data:
        return "Hidden network"
    try:
        text = data.decode("utf-8", errors="replace")
    except (AttributeError, UnicodeError):
        return "Wi-Fi network"
    return clean_display_text(text, "Wi-Fi network")


class WifiController:
    """Small event-driven view of the first NetworkManager Wi-Fi device."""

    def __init__(self, changed: Callable[[], None], failed: Callable[[str], None]):
        self._changed = changed
        self._failed = failed
        self._cancellable = Gio.Cancellable()
        self._client = None
        self._device = None
        self._signals: list[tuple[object, int]] = []
        self._access_points: dict[str, object] = {}
        self._saved: dict[bytes, object] = {}
        self._saved_tokens: dict[str, object] = {}
        self._page_active = False
        self._scanning = False

    def start(self):
        if NM is None:
            self._changed()
            return
        NM.Client.new_async(self._cancellable, self._on_client_ready, None)

    def _on_client_ready(self, _source, result, _data):
        try:
            self._client = NM.Client.new_finish(result)
        except (GLib.Error, RuntimeError):
            self._client = None
            self._failed("Wi-Fi controls are unavailable right now.")
            self._changed()
            return
        for signal in (
            "notify::wireless-enabled",
            "device-added",
            "device-removed",
            "connection-added",
            "connection-removed",
        ):
            self._signals.append((self._client, self._client.connect(signal, self._on_changed)))
        self._select_device()
        self._changed()

    def _on_changed(self, *_args):
        self._select_device()
        self._changed()

    def _select_device(self):
        if self._client is None:
            return
        device = next(
            (
                item
                for item in self._client.get_devices()
                if item.get_device_type() == NM.DeviceType.WIFI
            ),
            None,
        )
        if device is self._device:
            return
        self._disconnect_device_signals()
        self._device = device
        if device is not None:
            for signal in (
                "access-point-added",
                "access-point-removed",
                "notify::active-access-point",
                "notify::state",
            ):
                self._signals.append((device, device.connect(signal, self._on_changed)))
            if self._page_active:
                self.request_scan()

    def _disconnect_device_signals(self):
        retained: list[tuple[object, int]] = []
        for owner, signal_id in self._signals:
            if owner is self._device:
                try:
                    owner.disconnect(signal_id)
                except (AttributeError, RuntimeError, TypeError):
                    pass
            else:
                retained.append((owner, signal_id))
        self._signals = retained

    def enter(self):
        self._page_active = True
        self.request_scan()

    def leave(self):
        self._page_active = False

    def request_scan(self):
        if self._device is None or self._scanning:
            return
        self._scanning = True
        self._changed()
        try:
            self._device.request_scan_async(
                self._cancellable, self._on_scan_finished, None
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._scanning = False
            self._changed()

    def _on_scan_finished(self, device, result, _data):
        try:
            device.request_scan_finish(result)
        except (GLib.Error, RuntimeError):
            if self._page_active:
                self._failed("Wi-Fi scan could not be refreshed.")
        self._scanning = False
        self._changed()

    def snapshot(self) -> WifiState:
        if self._client is None or self._device is None:
            return WifiState(False, False, self._scanning, False, ())
        self._saved = {}
        self._saved_tokens = {}
        for connection in self._client.get_connections():
            try:
                if connection.get_connection_type() != NM.SETTING_WIRELESS_SETTING_NAME:
                    continue
                wireless = connection.get_setting_wireless()
                raw_ssid = _ssid_bytes(wireless.get_ssid())
                if raw_ssid:
                    self._saved[raw_ssid] = connection
            except (AttributeError, RuntimeError, TypeError):
                continue

        active = self._device.get_active_access_point()
        strongest: dict[bytes, WifiNetwork] = {}
        self._access_points = {}
        for access_point in self._device.get_access_points():
            raw_ssid = _ssid_bytes(access_point.get_ssid())
            if not raw_ssid:
                continue
            path = access_point.get_path()
            security = self._security(access_point)
            network = WifiNetwork(
                token=path,
                name=ssid_text(raw_ssid),
                strength=max(0, min(100, int(access_point.get_strength()))),
                security=security,
                saved=raw_ssid in self._saved,
                active=access_point is active or (
                    active is not None and access_point.get_path() == active.get_path()
                ),
            )
            previous = strongest.get(raw_ssid)
            if previous is None or network.active or network.strength > previous.strength:
                strongest[raw_ssid] = network
                self._access_points[path] = access_point
        for raw_ssid, connection in self._saved.items():
            if raw_ssid in strongest:
                continue
            token = f"saved:{connection.get_uuid()}"
            self._saved_tokens[token] = connection
            strongest[raw_ssid] = WifiNetwork(
                token=token,
                name=ssid_text(raw_ssid),
                strength=0,
                security="saved",
                saved=True,
                active=False,
                visible=False,
            )
        networks = tuple(
            sorted(
                strongest.values(),
                key=lambda item: (not item.active, -item.strength, item.name.lower()),
            )
        )
        capabilities = self._device.get_capabilities()
        hotspot = bool(capabilities & NM.DeviceWifiCapabilities.AP)
        return WifiState(
            True,
            bool(self._client.props.wireless_enabled),
            self._scanning,
            hotspot,
            networks,
        )

    @staticmethod
    def _security(access_point) -> str:
        flags = access_point.get_flags()
        wpa = access_point.get_wpa_flags()
        rsn = access_point.get_rsn_flags()
        if (
            rsn & NM_AP_SECURITY_FLAGS.KEY_MGMT_802_1X
            or wpa & NM_AP_SECURITY_FLAGS.KEY_MGMT_802_1X
        ):
            return "enterprise"
        if rsn & NM_AP_SECURITY_FLAGS.KEY_MGMT_OWE:
            return "owe"
        if rsn & (NM_AP_SECURITY_FLAGS.KEY_MGMT_PSK | NM_AP_SECURITY_FLAGS.KEY_MGMT_SAE):
            return "personal"
        if wpa & NM_AP_SECURITY_FLAGS.KEY_MGMT_PSK:
            return "personal"
        if flags & NM_AP_FLAGS.PRIVACY:
            return "legacy"
        return "open"

    def set_enabled(self, enabled: bool):
        if self._client is None:
            return
        try:
            self._client.props.wireless_enabled = bool(enabled)
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Wi-Fi could not be changed.")

    def connect_network(self, token: str, password: str | None = None):
        if self._client is None or self._device is None:
            self._failed("Wi-Fi controls are unavailable right now.")
            return
        saved_token = self._saved_tokens.get(token)
        if saved_token is not None:
            self._client.activate_connection_async(
                saved_token,
                self._device,
                None,
                self._cancellable,
                self._on_activate_finished,
                None,
            )
            return
        access_point = self._access_points.get(token)
        if access_point is None:
            self._failed("That Wi-Fi network is no longer available. Refresh and try again.")
            return
        raw_ssid = _ssid_bytes(access_point.get_ssid())
        saved = self._saved.get(raw_ssid)
        if saved is not None:
            self._client.activate_connection_async(
                saved,
                self._device,
                access_point.get_path(),
                self._cancellable,
                self._on_activate_finished,
                None,
            )
            return
        security = self._security(access_point)
        if security in {"enterprise", "legacy"}:
            self._failed("This network needs an advanced authentication method.")
            return
        if security == "personal" and not validate_personal_password(password):
            self._failed("Enter a valid Wi-Fi password of at least 8 characters.")
            return
        connection = NM.SimpleConnection.new()
        setting = NM.SettingConnection.new()
        setting.props.id = ssid_text(raw_ssid)
        setting.props.uuid = NM.utils_uuid_generate()
        setting.props.type = NM.SETTING_WIRELESS_SETTING_NAME
        connection.add_setting(setting)
        wireless = NM.SettingWireless.new()
        wireless.props.ssid = GLib.Bytes.new(raw_ssid)
        wireless.props.mode = "infrastructure"
        connection.add_setting(wireless)
        if security in {"personal", "owe"}:
            wireless_security = NM.SettingWirelessSecurity.new()
            wireless_security.props.key_mgmt = "owe" if security == "owe" else "wpa-psk"
            if security == "personal":
                wireless_security.props.psk = password
            connection.add_setting(wireless_security)
        ipv4 = NM.SettingIP4Config.new()
        ipv4.props.method = "auto"
        connection.add_setting(ipv4)
        ipv6 = NM.SettingIP6Config.new()
        ipv6.props.method = "auto"
        connection.add_setting(ipv6)
        self._client.add_and_activate_connection2(
            connection,
            self._device,
            access_point.get_path(),
            GLib.Variant("a{sv}", {}),
            self._cancellable,
            self._on_add_activate_finished,
            None,
        )

    def _on_activate_finished(self, client, result, _data):
        try:
            client.activate_connection_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not connect to that Wi-Fi network.")

    def _on_add_activate_finished(self, client, result, _data):
        try:
            client.add_and_activate_connection2_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not connect to that Wi-Fi network.")

    def disconnect(self):
        if self._device is None:
            return
        self._device.disconnect_async(
            self._cancellable, self._on_disconnect_finished, None
        )

    def _on_disconnect_finished(self, device, result, _data):
        try:
            device.disconnect_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not disconnect Wi-Fi.")

    def forget(self, token: str):
        connection = self._saved_tokens.get(token)
        if connection is not None:
            connection.delete_async(
                self._cancellable, self._on_forget_finished, None
            )
            return
        access_point = self._access_points.get(token)
        if access_point is None:
            return
        connection = self._saved.get(_ssid_bytes(access_point.get_ssid()))
        if connection is None:
            return
        connection.delete_async(self._cancellable, self._on_forget_finished, None)

    def _on_forget_finished(self, connection, result, _data):
        try:
            connection.delete_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not forget that Wi-Fi network.")

    def start_hotspot(self, name: str, password: str):
        if self._client is None or self._device is None:
            self._failed("Wi-Fi controls are unavailable right now.")
            return
        name = clean_display_text(name, "Zeus Hotspot")
        if not validate_personal_password(password):
            self._failed("Enter a hotspot password of at least 8 characters.")
            return
        connection = NM.SimpleConnection.new()
        setting = NM.SettingConnection.new()
        setting.props.id = name
        setting.props.uuid = NM.utils_uuid_generate()
        setting.props.type = NM.SETTING_WIRELESS_SETTING_NAME
        connection.add_setting(setting)
        wireless = NM.SettingWireless.new()
        wireless.props.ssid = GLib.Bytes.new(name.encode("utf-8")[:32])
        wireless.props.mode = "ap"
        connection.add_setting(wireless)
        security = NM.SettingWirelessSecurity.new()
        security.props.key_mgmt = "wpa-psk"
        security.props.psk = password
        connection.add_setting(security)
        ipv4 = NM.SettingIP4Config.new()
        ipv4.props.method = "shared"
        connection.add_setting(ipv4)
        ipv6 = NM.SettingIP6Config.new()
        ipv6.props.method = "ignore"
        connection.add_setting(ipv6)
        self._client.add_and_activate_connection2(
            connection,
            self._device,
            None,
            GLib.Variant("a{sv}", {}),
            self._cancellable,
            self._on_add_activate_finished,
            None,
        )

    def close(self):
        self._cancellable.cancel()
        self._disconnect_device_signals()
        for owner, signal_id in self._signals:
            try:
                owner.disconnect(signal_id)
            except (AttributeError, RuntimeError, TypeError):
                pass
        self._signals.clear()
        self._client = None
        self._device = None
        self._saved_tokens.clear()


class BluetoothController:
    """Event-driven BlueZ ObjectManager client with explicit page scanning."""

    def __init__(self, changed: Callable[[], None], failed: Callable[[str], None]):
        self._changed = changed
        self._failed = failed
        self._cancellable = Gio.Cancellable()
        self._manager = None
        self._adapter = None
        self._signals: list[int] = []
        self._devices: dict[str, object] = {}
        self._page_active = False
        self._scanning = False

    def start(self):
        Gio.DBusObjectManagerClient.new_for_bus(
            Gio.BusType.SYSTEM,
            Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START,
            BLUEZ_NAME,
            BLUEZ_ROOT,
            None,
            None,
            self._cancellable,
            self._on_manager_ready,
            None,
        )

    def _on_manager_ready(self, _source, result, _data):
        try:
            self._manager = Gio.DBusObjectManagerClient.new_for_bus_finish(result)
        except (GLib.Error, RuntimeError):
            self._manager = None
            self._changed()
            return
        for signal in (
            "object-added",
            "object-removed",
            "interface-added",
            "interface-removed",
            "interface-proxy-properties-changed",
        ):
            self._signals.append(self._manager.connect(signal, self._on_changed))
        self._refresh_objects()
        if self._page_active:
            self._start_discovery()
        self._changed()

    def _on_changed(self, *_args):
        self._refresh_objects()
        powered = self._adapter is not None and bool(
            _variant_value(self._adapter.get_cached_property("Powered"), False)
        )
        if not powered:
            self._scanning = False
        elif self._page_active and not self._scanning:
            self._start_discovery()
        self._changed()

    def _refresh_objects(self):
        self._adapter = None
        self._devices = {}
        if self._manager is None:
            return
        for item in self._manager.get_objects():
            if self._adapter is None:
                adapter = item.get_interface(BLUEZ_ADAPTER)
                if adapter is not None:
                    self._adapter = adapter
            device = item.get_interface(BLUEZ_DEVICE)
            if device is not None:
                self._devices[item.get_object_path()] = device

    def enter(self):
        self._page_active = True
        self._start_discovery()

    def leave(self):
        self._page_active = False
        self._stop_discovery()

    def _start_discovery(self):
        if self._adapter is None or self._scanning:
            return
        if not bool(_variant_value(self._adapter.get_cached_property("Powered"), False)):
            return
        self._scanning = True
        self._adapter.call(
            "StartDiscovery",
            None,
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._on_discovery_started,
            None,
        )
        self._changed()

    def _on_discovery_started(self, proxy, result, _data):
        try:
            proxy.call_finish(result)
        except (GLib.Error, RuntimeError):
            self._scanning = False
            if self._page_active:
                self._failed("Bluetooth scan could not be started.")
        self._changed()

    def _stop_discovery(self):
        if self._adapter is None or not self._scanning:
            return
        self._scanning = False
        self._adapter.call(
            "StopDiscovery",
            None,
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._finish_ignored,
            None,
        )
        self._changed()

    @staticmethod
    def _finish_ignored(proxy, result, _data):
        try:
            proxy.call_finish(result)
        except (GLib.Error, RuntimeError):
            pass

    def snapshot(self) -> BluetoothState:
        if self._adapter is None:
            return BluetoothState(False, False, self._scanning, ())
        devices = []
        for path, proxy in self._devices.items():
            alias = _variant_value(proxy.get_cached_property("Alias"), "")
            name = clean_display_text(alias, "Bluetooth device")
            paired = bool(_variant_value(proxy.get_cached_property("Paired"), False))
            connected = bool(_variant_value(proxy.get_cached_property("Connected"), False))
            trusted = bool(_variant_value(proxy.get_cached_property("Trusted"), False))
            devices.append(BluetoothDevice(path, name, paired, connected, trusted))
        devices.sort(key=lambda item: (not item.connected, not item.paired, item.name.lower()))
        powered = bool(_variant_value(self._adapter.get_cached_property("Powered"), False))
        return BluetoothState(True, powered, self._scanning, tuple(devices))

    def set_powered(self, enabled: bool):
        if self._manager is None or self._adapter is None:
            return
        connection = self._manager.get_connection()
        connection.call(
            BLUEZ_NAME,
            self._adapter.get_object_path(),
            DBUS_PROPERTIES,
            "Set",
            GLib.Variant("(ssv)", (BLUEZ_ADAPTER, "Powered", GLib.Variant("b", bool(enabled)))),
            None,
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._on_property_set,
            None,
        )

    def _on_property_set(self, connection, result, _data):
        try:
            connection.call_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Bluetooth could not be changed.")

    def pair(self, token: str):
        self._device_call(token, "Pair", "Could not pair that Bluetooth device.")

    def connect_device(self, token: str):
        self._device_call(token, "Connect", "Could not connect that Bluetooth device.")

    def disconnect_device(self, token: str):
        self._device_call(token, "Disconnect", "Could not disconnect that Bluetooth device.")

    def _device_call(self, token: str, method: str, message: str):
        proxy = self._devices.get(token)
        if proxy is None:
            return
        proxy.call(
            method,
            None,
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._on_device_call_finished,
            message,
        )

    def _on_device_call_finished(self, proxy, result, message):
        try:
            proxy.call_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed(message)

    def set_trusted(self, token: str, trusted: bool):
        proxy = self._devices.get(token)
        if proxy is None or self._manager is None:
            return
        self._manager.get_connection().call(
            BLUEZ_NAME,
            token,
            DBUS_PROPERTIES,
            "Set",
            GLib.Variant("(ssv)", (BLUEZ_DEVICE, "Trusted", GLib.Variant("b", bool(trusted)))),
            None,
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._on_property_set,
            None,
        )

    def remove(self, token: str):
        if self._adapter is None or token not in self._devices:
            return
        self._adapter.call(
            "RemoveDevice",
            GLib.Variant("(o)", (token,)),
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            self._cancellable,
            self._on_device_call_finished,
            "Could not remove that Bluetooth device.",
        )

    def close(self):
        self.leave()
        self._cancellable.cancel()
        if self._manager is not None:
            for signal_id in self._signals:
                try:
                    self._manager.disconnect(signal_id)
                except (RuntimeError, TypeError):
                    pass
        self._signals.clear()
        self._manager = None
        self._adapter = None
        self._devices.clear()


__all__ = [
    "BluetoothController",
    "BluetoothDevice",
    "BluetoothState",
    "WifiController",
    "WifiNetwork",
    "WifiState",
    "clean_display_text",
    "ssid_text",
    "validate_personal_password",
]
