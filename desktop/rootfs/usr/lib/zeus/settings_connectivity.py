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
BLUEZ_AGENT_MANAGER = "org.bluez.AgentManager1"
BLUEZ_AGENT = "org.bluez.Agent1"
BLUEZ_AGENT_PATH = "/org/zeus/Settings/BluetoothAgent"
DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS_TIMEOUT_MS = 15_000

BLUEZ_AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="out"/>
    </method>
    <method name="DisplayPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="in"/>
    </method>
    <method name="RequestPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="out"/>
    </method>
    <method name="DisplayPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
      <arg name="entered" type="q" direction="in"/>
    </method>
    <method name="RequestConfirmation">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg name="device" type="o" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg name="device" type="o" direction="in"/>
      <arg name="uuid" type="s" direction="in"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""


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
    status: str = "ready"
    pending: str | None = None
    hotspot_active: bool = False


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
    status: str = "ready"
    pending: str | None = None
    pairing_supported: bool = False


@dataclass(frozen=True)
class BluetoothPairingPrompt:
    token: str
    name: str
    kind: str
    code: str = ""


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
        self._status = "loading"
        self._starting = False
        self._pending: str | None = None

    def start(self):
        if self._starting or self._client is not None:
            return
        if NM is None:
            self._status = "unavailable"
            self._changed()
            return
        self._starting = True
        self._status = "loading"
        self._changed()
        NM.Client.new_async(self._cancellable, self._on_client_ready, None)

    def retry(self):
        """Retry initialization after a transient backend failure."""

        if self._client is not None or self._starting:
            self._select_device()
            self._changed()
            return
        self.start()

    def _on_client_ready(self, _source, result, _data):
        self._starting = False
        try:
            self._client = NM.Client.new_finish(result)
        except (GLib.Error, RuntimeError):
            self._client = None
            self._status = "error"
            self._failed("Wi-Fi controls are unavailable right now.")
            self._changed()
            return
        self._status = "ready"
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
            self._status = "ready" if device is not None else "unavailable"
            return
        self._disconnect_device_signals()
        self._device = device
        self._status = "ready" if device is not None else "unavailable"
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
        if self._client is None or not bool(self._client.props.wireless_enabled):
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
            return WifiState(
                False,
                False,
                self._scanning,
                False,
                (),
                self._status,
                self._pending,
                False,
            )
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
            # The active BSSID is authoritative even when a later BSSID for the
            # same SSID advertises a stronger signal. Only compare strength
            # when neither representative is active.
            if previous is None or (network.active and not previous.active) or (
                not previous.active and not network.active and network.strength > previous.strength
            ):
                strongest[raw_ssid] = network
        self._access_points = {
            network.token: next(
                access_point
                for access_point in self._device.get_access_points()
                if access_point.get_path() == network.token
            )
            for network in strongest.values()
            if network.visible
        }
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
        enabled = bool(self._client.props.wireless_enabled)
        return WifiState(
            True,
            enabled,
            self._scanning,
            hotspot,
            networks if enabled else (),
            self._status,
            self._pending,
            self._hotspot_is_active(),
        )

    def _hotspot_is_active(self) -> bool:
        try:
            active = self._device.get_active_connection()
            connection = active.get_connection() if active is not None else None
            wireless = connection.get_setting_wireless() if connection is not None else None
            mode = wireless.get_mode() if wireless is not None else None
            return mode == "ap"
        except (AttributeError, RuntimeError, TypeError):
            return False

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
        if rsn & NM_AP_SECURITY_FLAGS.KEY_MGMT_SAE:
            return "sae"
        if rsn & NM_AP_SECURITY_FLAGS.KEY_MGMT_PSK:
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
            self._changed()

    def connect_network(self, token: str, password: str | None = None):
        if self._pending is not None:
            return
        if self._client is None or self._device is None:
            self._failed("Wi-Fi controls are unavailable right now.")
            return
        if not bool(self._client.props.wireless_enabled):
            self._failed("Turn Wi-Fi on before connecting.")
            return
        saved_token = self._saved_tokens.get(token)
        if saved_token is not None:
            self._begin_pending(f"connect:{token}")
            try:
                self._client.activate_connection_async(
                    saved_token,
                    self._device,
                    None,
                    self._cancellable,
                    self._on_activate_finished,
                    None,
                )
            except (GLib.Error, RuntimeError, TypeError):
                self._failed("Could not connect to that Wi-Fi network.")
                self._finish_pending()
            return
        access_point = self._access_points.get(token)
        if access_point is None:
            self._failed("That Wi-Fi network is no longer available. Refresh and try again.")
            return
        raw_ssid = _ssid_bytes(access_point.get_ssid())
        saved = self._saved.get(raw_ssid)
        if saved is not None:
            self._begin_pending(f"connect:{token}")
            try:
                self._client.activate_connection_async(
                    saved,
                    self._device,
                    access_point.get_path(),
                    self._cancellable,
                    self._on_activate_finished,
                    None,
                )
            except (GLib.Error, RuntimeError, TypeError):
                self._failed("Could not connect to that Wi-Fi network.")
                self._finish_pending()
            return
        security = self._security(access_point)
        if security in {"enterprise", "legacy"}:
            self._failed("This network needs an advanced authentication method.")
            return
        if security in {"personal", "sae"} and not validate_personal_password(password):
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
        if security in {"personal", "sae", "owe"}:
            wireless_security = NM.SettingWirelessSecurity.new()
            wireless_security.props.key_mgmt = {
                "owe": "owe",
                "sae": "sae",
            }.get(security, "wpa-psk")
            if security in {"personal", "sae"}:
                wireless_security.props.psk = password
            connection.add_setting(wireless_security)
        ipv4 = NM.SettingIP4Config.new()
        ipv4.props.method = "auto"
        connection.add_setting(ipv4)
        ipv6 = NM.SettingIP6Config.new()
        ipv6.props.method = "auto"
        connection.add_setting(ipv6)
        self._begin_pending(f"connect:{token}")
        try:
            self._client.add_and_activate_connection2(
                connection,
                self._device,
                access_point.get_path(),
                GLib.Variant("a{sv}", {}),
                self._cancellable,
                self._on_add_activate_finished,
                "Could not connect to that Wi-Fi network.",
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not connect to that Wi-Fi network.")
            self._finish_pending()

    def _begin_pending(self, action: str):
        self._pending = action
        self._changed()

    def _finish_pending(self):
        self._pending = None
        self._changed()

    def _on_activate_finished(self, client, result, _data):
        try:
            client.activate_connection_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not connect to that Wi-Fi network.")
        self._finish_pending()

    def _on_add_activate_finished(self, client, result, message):
        try:
            client.add_and_activate_connection2_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed(message or "Could not complete that Wi-Fi change.")
        self._finish_pending()

    def disconnect(self):
        if self._device is None or self._pending is not None:
            return
        self._begin_pending("disconnect")
        try:
            self._device.disconnect_async(
                self._cancellable, self._on_disconnect_finished, "Could not disconnect Wi-Fi."
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not disconnect Wi-Fi.")
            self._finish_pending()

    def _on_disconnect_finished(self, device, result, message):
        try:
            device.disconnect_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed(message or "Could not disconnect Wi-Fi.")
        self._finish_pending()

    def forget(self, token: str):
        if self._pending is not None:
            return
        connection = self._saved_tokens.get(token)
        if connection is not None:
            self._begin_pending(f"forget:{token}")
            try:
                connection.delete_async(
                    self._cancellable, self._on_forget_finished, None
                )
            except (GLib.Error, RuntimeError, TypeError):
                self._failed("Could not forget that Wi-Fi network.")
                self._finish_pending()
            return
        access_point = self._access_points.get(token)
        if access_point is None:
            return
        connection = self._saved.get(_ssid_bytes(access_point.get_ssid()))
        if connection is None:
            return
        self._begin_pending(f"forget:{token}")
        try:
            connection.delete_async(self._cancellable, self._on_forget_finished, None)
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not forget that Wi-Fi network.")
            self._finish_pending()

    def _on_forget_finished(self, connection, result, _data):
        try:
            connection.delete_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Could not forget that Wi-Fi network.")
        self._finish_pending()

    def start_hotspot(self, name: str, password: str):
        if self._pending is not None:
            return
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
        self._begin_pending("hotspot-start")
        try:
            self._client.add_and_activate_connection2(
                connection,
                self._device,
                None,
                GLib.Variant("a{sv}", {"persist": GLib.Variant("s", "volatile")}),
                self._cancellable,
                self._on_add_activate_finished,
                "Could not start the Wi-Fi hotspot.",
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not start the Wi-Fi hotspot.")
            self._finish_pending()

    def stop_hotspot(self):
        if self._pending is not None or not self._hotspot_is_active():
            return
        self._begin_pending("hotspot-stop")
        try:
            self._device.disconnect_async(
                self._cancellable,
                self._on_disconnect_finished,
                "Could not stop the Wi-Fi hotspot.",
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not stop the Wi-Fi hotspot.")
            self._finish_pending()

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
        self._pending = None
        self._saved_tokens.clear()


class BluetoothController:
    """Event-driven BlueZ ObjectManager client with explicit page scanning."""

    def __init__(
        self,
        changed: Callable[[], None],
        failed: Callable[[str], None],
        pairing_request: Callable[[BluetoothPairingPrompt, Callable[[object | None], None]], None]
        | None = None,
    ):
        self._changed = changed
        self._failed = failed
        self._pairing_request = pairing_request
        self._cancellable = Gio.Cancellable()
        self._manager = None
        self._adapter = None
        self._adapter_path = ""
        self._signals: list[int] = []
        self._devices: dict[str, object] = {}
        self._page_active = False
        self._scanning = False
        self._status = "loading"
        self._starting = False
        self._pending: str | None = None
        self._agent_registration_id = 0
        self._agent_ready = False
        self._agent_node = None
        self._agent_responses: set[Callable[[object | None], None]] = set()

    def start(self):
        if self._starting or self._manager is not None:
            return
        self._starting = True
        self._status = "loading"
        self._changed()
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
        self._starting = False
        try:
            self._manager = Gio.DBusObjectManagerClient.new_for_bus_finish(result)
        except (GLib.Error, RuntimeError):
            self._manager = None
            self._status = "error"
            self._failed("Bluetooth controls are unavailable right now.")
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
        self._register_agent()
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
        self._adapter_path = ""
        self._devices = {}
        if self._manager is None:
            return
        objects = sorted(self._manager.get_objects(), key=lambda item: item.get_object_path())
        for item in objects:
            adapter = item.get_interface(BLUEZ_ADAPTER)
            if adapter is not None:
                self._adapter = adapter
                self._adapter_path = item.get_object_path()
                break
        self._status = "ready" if self._adapter is not None else "unavailable"
        if not self._adapter_path:
            return
        device_prefix = f"{self._adapter_path}/dev_"
        for item in objects:
            path = item.get_object_path()
            if not path.startswith(device_prefix):
                continue
            device = item.get_interface(BLUEZ_DEVICE)
            if device is not None:
                self._devices[path] = device

    def retry(self):
        if self._manager is None and not self._starting:
            self.start()
            return
        self._refresh_objects()
        self._changed()

    def _register_agent(self):
        if self._manager is None or self._agent_registration_id:
            return
        connection = self._manager.get_connection()
        try:
            self._agent_node = Gio.DBusNodeInfo.new_for_xml(BLUEZ_AGENT_XML)
            self._agent_registration_id = connection.register_object(
                BLUEZ_AGENT_PATH,
                self._agent_node.interfaces[0],
                self._on_agent_method_call,
                None,
                None,
            )
            capability = "KeyboardDisplay" if self._pairing_request is not None else "NoInputNoOutput"
            connection.call(
                BLUEZ_NAME,
                "/org/bluez",
                BLUEZ_AGENT_MANAGER,
                "RegisterAgent",
                GLib.Variant("(os)", (BLUEZ_AGENT_PATH, capability)),
                None,
                Gio.DBusCallFlags.NONE,
                DBUS_TIMEOUT_MS,
                self._cancellable,
                self._on_agent_registered,
                None,
            )
        except (AttributeError, GLib.Error, RuntimeError, TypeError, ValueError):
            self._agent_registration_id = 0
            self._agent_ready = False
            self._failed("Bluetooth pairing authorization could not be prepared.")
            self._changed()

    def _on_agent_registered(self, connection, result, _data):
        try:
            connection.call_finish(result)
            self._agent_ready = True
        except (GLib.Error, RuntimeError):
            self._agent_ready = False
            self._failed("Bluetooth pairing authorization could not be prepared.")
        self._changed()

    def _device_name(self, token: str) -> str:
        proxy = self._devices.get(token)
        if proxy is None:
            return "Bluetooth device"
        return clean_display_text(
            _variant_value(proxy.get_cached_property("Alias"), ""),
            "Bluetooth device",
        )

    @staticmethod
    def _reject_pairing(invocation):
        invocation.return_dbus_error("org.bluez.Error.Rejected", "Pairing was declined.")

    def _request_pairing_answer(self, prompt, invocation, return_type: str):
        if self._pairing_request is None:
            self._reject_pairing(invocation)
            return

        answered = False

        def respond(value):
            nonlocal answered
            if answered:
                return
            answered = True
            self._agent_responses.discard(respond)
            if value is None:
                self._reject_pairing(invocation)
            elif return_type == "s":
                invocation.return_value(GLib.Variant("(s)", (str(value)[:16],)))
            elif return_type == "u":
                try:
                    passkey = int(value)
                except (TypeError, ValueError):
                    self._reject_pairing(invocation)
                else:
                    if 0 <= passkey <= 999999:
                        invocation.return_value(GLib.Variant("(u)", (passkey,)))
                    else:
                        self._reject_pairing(invocation)
            else:
                invocation.return_value(None)

        self._agent_responses.add(respond)
        try:
            self._pairing_request(prompt, respond)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            respond(None)

    def _on_agent_method_call(
        self,
        _connection,
        _sender,
        _object_path,
        _interface_name,
        method_name,
        parameters,
        invocation,
    ):
        values = _variant_value(parameters, ()) or ()
        token = values[0] if values and isinstance(values[0], str) else ""
        name = self._device_name(token)
        if method_name in {"Release", "Cancel"}:
            invocation.return_value(None)
            if method_name == "Cancel":
                for respond in tuple(self._agent_responses):
                    respond(None)
                self._failed("Bluetooth pairing was canceled.")
            return
        if method_name == "RequestPinCode":
            self._request_pairing_answer(
                BluetoothPairingPrompt(token, name, "pin"), invocation, "s"
            )
            return
        if method_name == "RequestPasskey":
            self._request_pairing_answer(
                BluetoothPairingPrompt(token, name, "passkey"), invocation, "u"
            )
            return
        if method_name in {"DisplayPinCode", "DisplayPasskey"}:
            code = str(values[1]).zfill(6) if len(values) > 1 else ""
            invocation.return_value(None)
            if self._pairing_request is not None:
                self._pairing_request(
                    BluetoothPairingPrompt(token, name, "display", code),
                    lambda _value: None,
                )
            return
        if method_name == "RequestConfirmation":
            code = str(values[1]).zfill(6) if len(values) > 1 else ""
            self._request_pairing_answer(
                BluetoothPairingPrompt(token, name, "confirm", code), invocation, ""
            )
            return
        if method_name in {"RequestAuthorization", "AuthorizeService"}:
            self._request_pairing_answer(
                BluetoothPairingPrompt(token, name, "authorize"), invocation, ""
            )
            return
        invocation.return_dbus_error("org.bluez.Error.NotSupported", "Unsupported pairing request.")

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
            return BluetoothState(
                False,
                False,
                self._scanning,
                (),
                self._status,
                self._pending,
                self._agent_ready,
            )
        powered = bool(_variant_value(self._adapter.get_cached_property("Powered"), False))
        devices = []
        for path, proxy in self._devices.items():
            alias = _variant_value(proxy.get_cached_property("Alias"), "")
            name = clean_display_text(alias, "Bluetooth device")
            paired = bool(_variant_value(proxy.get_cached_property("Paired"), False))
            connected = bool(_variant_value(proxy.get_cached_property("Connected"), False))
            trusted = bool(_variant_value(proxy.get_cached_property("Trusted"), False))
            visible = proxy.get_cached_property("RSSI") is not None
            if not paired and not connected and (not powered or not self._scanning or not visible):
                continue
            devices.append(BluetoothDevice(path, name, paired, connected, trusted))
        devices.sort(key=lambda item: (not item.connected, not item.paired, item.name.lower()))
        return BluetoothState(
            True,
            powered,
            self._scanning,
            tuple(devices),
            self._status,
            self._pending,
            self._agent_ready,
        )

    def set_powered(self, enabled: bool):
        if self._manager is None or self._adapter is None or self._pending is not None:
            return
        self._begin_pending("power")
        connection = self._manager.get_connection()
        try:
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
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Bluetooth could not be changed.")
            self._finish_pending()

    def _on_property_set(self, connection, result, _data):
        try:
            connection.call_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed("Bluetooth could not be changed.")
        self._finish_pending()

    def pair(self, token: str):
        if not self._agent_ready:
            self._failed("Bluetooth pairing is still being prepared. Try again in a moment.")
            return
        self._device_call(token, "Pair", "Could not pair that Bluetooth device.")

    def connect_device(self, token: str):
        self._device_call(token, "Connect", "Could not connect that Bluetooth device.")

    def disconnect_device(self, token: str):
        self._device_call(token, "Disconnect", "Could not disconnect that Bluetooth device.")

    def _device_call(self, token: str, method: str, message: str):
        if self._pending is not None:
            return
        proxy = self._devices.get(token)
        if proxy is None:
            return
        powered = self._adapter is not None and bool(
            _variant_value(self._adapter.get_cached_property("Powered"), False)
        )
        if not powered:
            self._failed("Turn Bluetooth on before changing a device.")
            return
        self._begin_pending(f"{method.lower()}:{token}")
        try:
            proxy.call(
                method,
                None,
                Gio.DBusCallFlags.NONE,
                DBUS_TIMEOUT_MS,
                self._cancellable,
                self._on_device_call_finished,
                message,
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed(message)
            self._finish_pending()

    def _on_device_call_finished(self, proxy, result, message):
        try:
            proxy.call_finish(result)
        except (GLib.Error, RuntimeError):
            self._failed(message)
        self._finish_pending()

    def _begin_pending(self, action: str):
        self._pending = action
        self._changed()

    def _finish_pending(self):
        self._pending = None
        self._changed()

    def set_trusted(self, token: str, trusted: bool):
        proxy = self._devices.get(token)
        if proxy is None or self._manager is None or self._pending is not None:
            return
        self._begin_pending(f"trust:{token}")
        try:
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
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Bluetooth trust could not be changed.")
            self._finish_pending()

    def remove(self, token: str):
        if self._adapter is None or token not in self._devices or self._pending is not None:
            return
        self._begin_pending(f"remove:{token}")
        try:
            self._adapter.call(
                "RemoveDevice",
                GLib.Variant("(o)", (token,)),
                Gio.DBusCallFlags.NONE,
                DBUS_TIMEOUT_MS,
                self._cancellable,
                self._on_device_call_finished,
                "Could not remove that Bluetooth device.",
            )
        except (GLib.Error, RuntimeError, TypeError):
            self._failed("Could not remove that Bluetooth device.")
            self._finish_pending()

    def close(self):
        self.leave()
        for respond in tuple(self._agent_responses):
            respond(None)
        self._agent_responses.clear()
        self._cancellable.cancel()
        if self._manager is not None:
            connection = self._manager.get_connection()
            if self._agent_registration_id:
                try:
                    connection.call(
                        BLUEZ_NAME,
                        "/org/bluez",
                        BLUEZ_AGENT_MANAGER,
                        "UnregisterAgent",
                        GLib.Variant("(o)", (BLUEZ_AGENT_PATH,)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        DBUS_TIMEOUT_MS,
                        None,
                        None,
                        None,
                    )
                    connection.unregister_object(self._agent_registration_id)
                except (AttributeError, GLib.Error, RuntimeError, TypeError):
                    pass
            for signal_id in self._signals:
                try:
                    self._manager.disconnect(signal_id)
                except (RuntimeError, TypeError):
                    pass
        self._signals.clear()
        self._manager = None
        self._adapter = None
        self._adapter_path = ""
        self._pending = None
        self._agent_registration_id = 0
        self._agent_ready = False
        self._devices.clear()


__all__ = [
    "BluetoothController",
    "BluetoothDevice",
    "BluetoothPairingPrompt",
    "BluetoothState",
    "WifiController",
    "WifiNetwork",
    "WifiState",
    "clean_display_text",
    "ssid_text",
    "validate_personal_password",
]
