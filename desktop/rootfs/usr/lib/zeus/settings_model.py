"""Dependency-free data for the native Zeus Settings home.

The Settings window is a small directory of native GNOME destinations.  This
module keeps the information that can be tested without GTK separate from the
view: panel identifiers are fixed, build identity is display-only, and the
hardware probe reads a bounded set of sysfs entries once when the window opens.
No value returned here is a control or a request to change device state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PRODUCT_VERSION = "0.1.0-preview.2"
DEFAULT_BUILD_ID = "development"
GNOME_CONTROL_CENTER_COMMAND = "/usr/bin/gnome-control-center"
VERSION_PATH = Path("/usr/share/zeus/version")
BUILD_ID_PATH = Path("/usr/share/zeus/build-id")

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
    "DESTINATIONS",
    "DESTINATIONS_BY_KEY",
    "Destination",
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
    "destination",
    "discover_hardware",
    "hardware_summary",
    "local_command",
    "panel_argv",
    "panel_attempt_order",
    "panel_desktop_id",
    "read_build_identity",
]
