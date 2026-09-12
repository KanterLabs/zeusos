"""Dependency-free data for the native Zeus Settings home.

The Settings window is a small directory of native GNOME destinations.  This
module keeps the information that can be tested without GTK separate from the
view: panel identifiers are fixed, build identity is display-only, and the
hardware probe reads a bounded set of sysfs entries once when the window opens.
No value returned here is a control or a request to change device state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


PRODUCT_VERSION = "0.1.0-preview.2"
DEFAULT_BUILD_ID = "development"
GNOME_CONTROL_CENTER_COMMAND = "/usr/bin/gnome-control-center"
VERSION_PATH = Path("/usr/share/zeus/version")
BUILD_ID_PATH = Path("/usr/share/zeus/build-id")
DEVELOPER_HELPER_PATH = "/usr/libexec/zeus-developer"
DEVELOPER_ACTIONS = ("status", "enable", "apply", "undo", "disable")
DEVELOPER_STATUS_STATES = frozenset(
    {"off", "enabled", "active", "incompatible", "applying", "error"}
)
DEVELOPER_REQUIRED_ACTIONS = frozenset({"none", "restart", "logout", "reboot"})

POWER_SUPPLY_PATH = Path("/sys/class/power_supply")
BACKLIGHT_PATH = Path("/sys/class/backlight")
NETWORK_PATH = Path("/sys/class/net")
BLUETOOTH_PATH = Path("/sys/class/bluetooth")
MAX_DISCOVERY_ENTRIES = 32


@dataclass(frozen=True)
class Destination:
    """One compact Settings card and its fixed launch target."""

    key: str
    title: str
    summary: str
    icon_name: str
    panel_ids: tuple[str, ...] = ()
    panel_desktop_ids: tuple[str, ...] = ()
    desktop_id: str | None = None
    command: tuple[str, ...] = ()


# Fedora's GNOME Control Center 50 names the appearance panel ``background``.
# Network has both a focused Wi-Fi panel and a broader network panel; trying
# the focused panel first gives laptop users the shortest route while keeping
# the older/limited image fallback explicit.  Every other panel ID is the
# installed GNOME 50 identifier from the image capability inventory.
PANEL_DESTINATIONS: tuple[Destination, ...] = (
    Destination(
        "network",
        "Wi-Fi & Network",
        "Connections and Wi-Fi networks",
        "network-wireless-symbolic",
        ("wifi", "network"),
        ("gnome-wifi-panel.desktop", "gnome-network-panel.desktop"),
    ),
    Destination(
        "bluetooth",
        "Bluetooth",
        "Keyboards, headphones and devices",
        "bluetooth-active-symbolic",
        ("bluetooth",),
        ("gnome-bluetooth-panel.desktop",),
    ),
    Destination(
        "display",
        "Displays",
        "Resolution and connected displays",
        "video-display-symbolic",
        ("display",),
        ("gnome-display-panel.desktop",),
    ),
    Destination(
        "power",
        "Power & Battery",
        "Power profiles and battery",
        "battery-good-symbolic",
        ("power",),
        ("gnome-power-panel.desktop",),
    ),
    Destination(
        "sound",
        "Sound",
        "Speakers, microphones and volume",
        "audio-volume-high-symbolic",
        ("sound",),
        ("gnome-sound-panel.desktop",),
    ),
    Destination(
        "appearance",
        "Appearance",
        "Style, wallpaper and accent color",
        "applications-graphics-symbolic",
        ("background",),
        ("gnome-background-panel.desktop",),
    ),
)


LOCAL_DESTINATIONS: tuple[Destination, ...] = (
    Destination(
        "temp",
        "Temp",
        "Review temporary downloads and choose when they clear.",
        "folder-download-symbolic",
        desktop_id="org.zeus.Temp",
        command=("/usr/libexec/zeus-temp-window",),
    ),
    Destination(
        "updates",
        "Updates",
        "Review signed Zeus OS updates when you are ready.",
        "software-update-available-symbolic",
        desktop_id="org.zeus.Updates",
        command=("/usr/libexec/zeus-update-window",),
    ),
)


DESTINATIONS: tuple[Destination, ...] = PANEL_DESTINATIONS + LOCAL_DESTINATIONS
DESTINATIONS_BY_KEY = {destination.key: destination for destination in DESTINATIONS}


@dataclass(frozen=True)
class HardwareState:
    """Read-only hardware presence as tri-state values.

    ``False`` means the relevant sysfs class was inspected and no device was
    present.  ``None`` means discovery could not be completed, so the UI must
    avoid claiming that hardware is absent.
    """

    battery: bool | None
    backlight: bool | None
    wifi: bool | None
    bluetooth: bool | None


@dataclass(frozen=True)
class DeveloperStatus:
    """The bounded public status returned by the Developer Mode helper.

    The root helper owns the state file and authentication policy.  Settings
    only renders this data, so unknown or malformed fields are reduced to
    safe display fallbacks here.  ``focused_test_receipt`` is intentionally a
    short human-readable receipt rather than arbitrary command output.
    """

    state: str = "off"
    base_build: str = DEFAULT_BUILD_ID
    active_commit: str = ""
    artifact_digest: str = ""
    required_action: str = "none"
    focused_test_receipt: str = ""
    applied_at: str = ""
    message: str = ""
    error: str = ""


def _developer_text(value: Any, fallback: str = "", *, limit: int = 512) -> str:
    """Convert one helper field to bounded text without leaking objects."""

    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        return fallback
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        # Receipts are occasionally represented as a small object by helper
        # versions.  Keep only a compact JSON representation for display.
        try:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return fallback
    return text[:limit] if text else fallback


def _developer_field(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _developer_state(value: Any, *, enabled: Any = None) -> str:
    state = _developer_text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "disabled": "off",
        "inactive": "off",
        "needs_rebuild": "incompatible",
        "needs_attention": "error",
        "attention": "error",
        "paused": "enabled",
        "failed": "error",
        "interrupted": "error",
    }
    state = aliases.get(state, state)
    if not state:
        state = "enabled" if enabled is True else "off"
    return state if state in DEVELOPER_STATUS_STATES else "error"


def _developer_receipt(value: Any) -> str:
    if isinstance(value, Mapping):
        # Accept the common receipt shapes while keeping the display concise.
        result = _developer_text(_developer_field(value, "result", "status", "outcome"))
        checked_at = _developer_text(_developer_field(value, "checked_at", "completed_at", "timestamp"))
        if result and checked_at:
            return f"{result} · {checked_at}"
        if result:
            return result
    return _developer_text(value)


def _developer_required_action(value: Any) -> str:
    """Reduce one helper activation value or list to the public action."""

    values = value if isinstance(value, (list, tuple)) else (value,)
    for item in values:
        action = _developer_text(item, limit=32).lower().replace("-", "_").replace(" ", "_")
        if action in {"logout", "logout_login", "log_out", "log_out_in"}:
            return "logout"
        if action in {"reboot", "restart_system", "restart_os"}:
            return "reboot"
        if action.startswith("restart"):
            return "restart"
    return "none"


def default_developer_status() -> DeveloperStatus:
    """Return the safe status shown before the helper has answered."""

    return DeveloperStatus()


def developer_status_from_payload(value: Any) -> DeveloperStatus:
    """Normalize one helper JSON object to the Settings display contract.

    The helper may wrap the public object in ``status`` and may use the more
    explicit ``*_id``/``source_commit`` names.  Supporting those aliases
    keeps this unprivileged client compatible with the root contract while
    retaining one stable model for the view and tests.
    """

    payload: Mapping[str, Any]
    if isinstance(value, Mapping) and isinstance(value.get("status"), Mapping):
        payload = value["status"]
    elif isinstance(value, Mapping) and isinstance(value.get("developer"), Mapping):
        payload = value["developer"]
    elif isinstance(value, Mapping):
        payload = value
    else:
        return DeveloperStatus(state="error", error="The Developer Mode helper returned an unreadable status.")

    if not any(
        key in payload
        for key in ("state", "mode", "enabled", "active", "incompatible", "applying", "error")
    ):
        return DeveloperStatus(state="error", error="The Developer Mode helper returned an incomplete status.")

    enabled = payload.get("enabled")
    state = _developer_state(_developer_field(payload, "state", "mode"), enabled=enabled)
    if payload.get("active") is True and state in {"off", "enabled"}:
        state = "active"
    if not _developer_text(_developer_field(payload, "state", "mode")):
        if payload.get("incompatible") is True or payload.get("needs_rebuild") is True:
            state = "incompatible"
        elif payload.get("applying") is True:
            state = "applying"
        elif payload.get("active") is True:
            state = "active"
        elif _developer_text(_developer_field(payload, "error", "failure")):
            state = "error"
    required_action = _developer_required_action(
        _developer_field(
            payload,
            "required_action",
            "required_activation",
            "activation_actions",
            "activation",
        )
    )
    if payload.get("required_logout") is True or payload.get("requires_logout") is True:
        required_action = "logout"
    elif payload.get("required_restart") is True or payload.get("requires_restart") is True:
        required_action = "restart"
    required_action = required_action if required_action in DEVELOPER_REQUIRED_ACTIONS else "none"
    receipt = _developer_receipt(
        _developer_field(
            payload,
            "focused_test_receipt",
            "test_receipt",
            "focused_tests",
            "focused_test",
            "tests_receipt",
        )
    )
    return DeveloperStatus(
        state=state,
        base_build=_developer_text(
            _developer_field(
                payload,
                "base_build",
                "base_build_id",
                "base_sysext_level",
                "build_id",
            ),
            DEFAULT_BUILD_ID,
            limit=128,
        ),
        active_commit=_developer_text(
            _developer_field(payload, "active_commit", "source_commit", "commit"),
            limit=128,
        ),
        artifact_digest=_developer_text(
            _developer_field(
                payload,
                "artifact_digest",
                "active_artifact_digest",
                "active_digest",
                "artifact",
            ),
            limit=128,
        ),
        required_action=required_action,
        focused_test_receipt=receipt,
        applied_at=_developer_text(
            _developer_field(payload, "applied_at", "application_time", "applied_time"),
            limit=128,
        ),
        message=_developer_text(_developer_field(payload, "message", "summary")),
        error=_developer_text(_developer_field(payload, "error", "failure")),
    )


def developer_status_from_json(value: str) -> DeveloperStatus:
    """Parse one bounded JSON response from ``zeus-developer status``."""

    if not isinstance(value, str):
        return DeveloperStatus(state="error", error="The Developer Mode helper returned an unreadable status.")
    text = value.strip()
    if not text:
        return DeveloperStatus(state="error", error="The Developer Mode helper returned no status.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Permit a small amount of launcher noise without accepting arbitrary
        # trailing content as a status object.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return DeveloperStatus(state="error", error="The Developer Mode helper returned invalid JSON.")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return DeveloperStatus(state="error", error="The Developer Mode helper returned invalid JSON.")
    return developer_status_from_payload(parsed)


def developer_argv(
    action: str,
    *,
    json_status: bool = False,
    artifact_digest: str | None = None,
) -> tuple[str, ...]:
    """Build the only argv accepted by the Developer Mode helper."""

    if action not in DEVELOPER_ACTIONS:
        raise ValueError(f"unknown Developer Mode action: {action}")
    if json_status and action != "status":
        raise ValueError("JSON output is available only for Developer Mode status")
    if artifact_digest is not None:
        if action != "apply" or len(artifact_digest) != 64 or any(
            character not in "0123456789abcdef" for character in artifact_digest
        ):
            raise ValueError("Developer artifact selection must be a 64-hex digest for apply")
    arguments = [DEVELOPER_HELPER_PATH, action]
    if artifact_digest is not None:
        arguments.append(artifact_digest)
    if json_status:
        arguments.append("--json")
    return tuple(arguments)


def developer_state_title(state: str) -> str:
    """Return a short owner-facing title for a normalized state."""

    return {
        "off": "Developer Mode is off",
        "enabled": "Developer Mode is enabled",
        "active": "Developer Mode is active",
        "incompatible": "Developer artifact needs a rebuild",
        "applying": "Applying Developer Mode",
        "error": "Developer Mode needs attention",
    }.get(state, "Developer Mode status")


def developer_state_description(status: DeveloperStatus) -> str:
    """Return one concise description including actionable recovery context."""

    if status.error:
        return status.error
    if status.message:
        return status.message
    return {
        "off": "Enable it when you want to review a verified repository artifact.",
        "enabled": "Administrator authentication is complete; apply a verified prepared artifact when ready.",
        "active": "A verified repository artifact is active in the desktop.",
        "incompatible": "The active artifact targets another base build. Rebuild it for this installation.",
        "applying": "The helper is verifying and activating the selected artifact.",
        "error": "The helper could not read a usable Developer Mode status.",
    }.get(status.state, "Developer Mode status is unavailable.")


def developer_status_summary(status: DeveloperStatus) -> str:
    """Format the provenance fields for a compact card."""

    details = [f"Base build: {status.base_build}"]
    if status.active_commit:
        details.append(f"Commit: {status.active_commit}")
    else:
        details.append("Commit: none")
    if status.artifact_digest:
        details.append(f"Artifact: {status.artifact_digest}")
    else:
        details.append("Artifact: none")
    return " · ".join(details)


def _directory_entries(path: Path) -> tuple[Path, ...] | None:
    """Read at most ``MAX_DISCOVERY_ENTRIES`` directory children.

    A missing sysfs class is a normal absence (``()``).  A permission or other
    I/O failure is unknown (``None``), which lets the UI stay honest.  The
    ``MAX + 1`` check prevents an unexpectedly large directory from becoming a
    startup cost while still distinguishing a truncated scan from absence.
    """

    entries: list[Path] = []
    try:
        iterator = iter(path.iterdir())
        for _index in range(MAX_DISCOVERY_ENTRIES + 1):
            try:
                entries.append(next(iterator))
            except StopIteration:
                return tuple(entries)
        return None
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except (OSError, RuntimeError):
        return None


def _is_directory(path: Path) -> bool | None:
    try:
        return path.is_dir()
    except (OSError, RuntimeError):
        return None


def _battery_available(root: Path) -> bool | None:
    entries = _directory_entries(root)
    if entries is None:
        return None
    uncertain = False
    for entry in entries:
        try:
            value = (entry / "type").read_text(encoding="ascii").strip().lower()
        except FileNotFoundError:
            # A class entry without ``type`` is not enough evidence to call
            # it a battery; continue looking for a well-formed entry.
            continue
        except (OSError, UnicodeError):
            uncertain = True
            continue
        if value == "battery":
            return True
    return None if uncertain else False


def _backlight_available(root: Path) -> bool | None:
    entries = _directory_entries(root)
    if entries is None:
        return None
    uncertain = False
    for entry in entries:
        present = _is_directory(entry)
        if present is True:
            return True
        if present is None:
            uncertain = True
    return None if uncertain else False


def _wifi_available(root: Path) -> bool | None:
    entries = _directory_entries(root)
    if entries is None:
        return None
    uncertain = False
    for entry in entries:
        wireless = _is_directory(entry / "wireless")
        if wireless is True:
            return True
        if wireless is None:
            uncertain = True
    return None if uncertain else False


def _bluetooth_available(root: Path) -> bool | None:
    entries = _directory_entries(root)
    if entries is None:
        return None
    uncertain = False
    for entry in entries:
        present = _is_directory(entry)
        if present is True:
            return True
        if present is None:
            uncertain = True
    return None if uncertain else False


def discover_hardware(*, sysfs_root: Path = Path("/sys")) -> HardwareState:
    """Take one bounded, read-only snapshot of laptop-related device classes."""

    root = Path(sysfs_root)
    return HardwareState(
        battery=_battery_available(root / "class/power_supply"),
        backlight=_backlight_available(root / "class/backlight"),
        wifi=_wifi_available(root / "class/net"),
        bluetooth=_bluetooth_available(root / "class/bluetooth"),
    )


def availability_text(label: str, available: bool | None) -> str:
    """Return a concise, honest status suitable for a visible UI label."""

    if available is True:
        return f"{label} detected"
    if available is False:
        return f"No {label.lower()} detected here"
    return f"{label} availability could not be determined"


def hardware_summary(state: HardwareState) -> str:
    """Describe the snapshot without implying that absent devices are broken."""

    details = (
        availability_text("Battery", state.battery),
        availability_text("Laptop backlight", state.backlight),
        availability_text("Wi-Fi radio", state.wifi),
        availability_text("Bluetooth adapter", state.bluetooth),
    )
    return " · ".join(details) + "."


def _read_text(path: Path, fallback: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return fallback
    return value or fallback


def read_build_identity(
    *,
    version_path: Path = VERSION_PATH,
    build_id_path: Path = BUILD_ID_PATH,
) -> tuple[str, str]:
    """Read display-only local image identity without any external activity."""

    return (
        _read_text(Path(version_path), PRODUCT_VERSION),
        _read_text(Path(build_id_path), DEFAULT_BUILD_ID),
    )


def destination(key: str) -> Destination | None:
    """Return an allowlisted destination by key for tests and view routing."""

    return DESTINATIONS_BY_KEY.get(key)


def panel_argv(destination_item: Destination, panel_index: int = 0) -> tuple[str, ...]:
    """Build one fixed GNOME Control Center argv tuple."""

    if not destination_item.panel_ids:
        raise ValueError("destination has no GNOME panel")
    try:
        panel_id = destination_item.panel_ids[panel_index]
    except IndexError as error:
        raise ValueError("panel index is outside destination candidates") from error
    return (GNOME_CONTROL_CENTER_COMMAND, panel_id)


def panel_desktop_id(destination_item: Destination, panel_index: int = 0) -> str | None:
    """Return one fixed hidden GNOME panel desktop ID, when shipped."""

    try:
        return destination_item.panel_desktop_ids[panel_index]
    except IndexError:
        return None


def panel_attempt_order(
    destination_item: Destination,
    hardware: HardwareState | None = None,
) -> tuple[int, ...]:
    """Choose a useful fixed panel order from the one open-time snapshot.

    Fedora exposes separate ``wifi`` and ``network`` panels.  A confirmed
    wireless adapter makes the focused Wi-Fi panel the useful first choice;
    Ethernet-only or unknown environments start with the broader Network
    panel.  Other destinations retain their declared order.
    """

    count = max(len(destination_item.panel_ids), len(destination_item.panel_desktop_ids))
    order = tuple(range(count))
    if destination_item.key != "network" or count < 2:
        return order
    if hardware is not None and hardware.wifi is True:
        return order
    return (1, 0) + tuple(index for index in order if index not in (0, 1))


def local_command(destination_item: Destination) -> tuple[str, ...]:
    """Return the fixed command fallback for a local Zeus destination."""

    if not destination_item.command:
        raise ValueError("destination has no local command")
    return destination_item.command


__all__ = [
    "BACKLIGHT_PATH",
    "BLUETOOTH_PATH",
    "BUILD_ID_PATH",
    "DEFAULT_BUILD_ID",
    "DEVELOPER_ACTIONS",
    "DEVELOPER_HELPER_PATH",
    "DEVELOPER_REQUIRED_ACTIONS",
    "DEVELOPER_STATUS_STATES",
    "DESTINATIONS",
    "DESTINATIONS_BY_KEY",
    "Destination",
    "DeveloperStatus",
    "GNOME_CONTROL_CENTER_COMMAND",
    "HardwareState",
    "LOCAL_DESTINATIONS",
    "MAX_DISCOVERY_ENTRIES",
    "NETWORK_PATH",
    "PANEL_DESTINATIONS",
    "POWER_SUPPLY_PATH",
    "PRODUCT_VERSION",
    "VERSION_PATH",
    "availability_text",
    "default_developer_status",
    "destination",
    "developer_argv",
    "developer_state_description",
    "developer_state_title",
    "developer_status_from_json",
    "developer_status_from_payload",
    "developer_status_summary",
    "discover_hardware",
    "hardware_summary",
    "local_command",
    "panel_argv",
    "panel_attempt_order",
    "panel_desktop_id",
    "read_build_identity",
]
