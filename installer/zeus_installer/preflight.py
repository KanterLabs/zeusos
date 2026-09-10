"""Read-only feasibility checks for the Fedora-launched Zeus installer.

The preflight is deliberately split into two operations:

``collect()``
    Reads the current host through Linux's read-only inspection interfaces and
    returns a JSON-serialisable inventory.  A command or file that cannot be
    read is recorded in ``errors``; it is never silently treated as empty or
    safe.

``plan(inventory)``
    Validates a collected (or fixture) inventory and returns a reviewable
    installation proposal.  The proposal contains no executable commands and
    does not mutate the host.  The eventual installer must collect a fresh
    inventory and compare its fingerprint immediately before any partition
    operation.

The fixture-friendly optional arguments on :func:`collect` are intentionally
small.  Production callers can use the no-argument form; tests can provide a
command runner and temporary ``/sys``/``/proc``/``/etc`` trees without
monkeypatching process-wide state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
GIB = 1024**3

# The installer now shrinks the live Fedora filesystem, reboots Fedora, and
# installs from an on-disk OCI payload.  It does not reserve RAM for a
# maintenance image.  Keep the advertised host floor while allowing the
# normal MemTotal rounding loss seen on an 8 GiB machine.
MIN_TOTAL_RAM_GIB = 8
MIN_TOTAL_RAM_TOLERANCE_GIB = 0.5
MIN_TOTAL_RAM_MEASURED_GIB = MIN_TOTAL_RAM_GIB - MIN_TOTAL_RAM_TOLERANCE_GIB
MIN_AVAILABLE_RAM_GIB = 2
MIN_STAGING_GIB = 8

# The image definition calls for at least 56 GiB for its root filesystem.  A
# one-GiB ESP and two-GiB /boot are included in the smallest accepted layout;
# Zeus /var/home is created inside its root filesystem.
MIN_ZEUS_ROOT_GIB = 56
MIN_ZEUS_ESP_GIB = 1
MIN_ZEUS_BOOT_GIB = 2
MIN_ZEUS_HOME_GIB = 1
# Keep the public policy floor in step with the exact-sector storage planner:
# 56 GiB root plus boot partitions and a small layout margin.
MIN_ALLOCATION_GIB = 64

_COMMAND_TIMEOUT_SECONDS = 15
_OS_RELEASE_PATH = "/etc/os-release"
_DEFAULT_STAGING_PATHS = ("/var/tmp", "/tmp")

_LSBLK_COLUMNS = (
    "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,PARTTYPE,PARTTYPENAME,"
    "PKNAME,MOUNTPOINTS,MODEL,SERIAL,WWN,PTTYPE,PTUUID,START,PARTN,LOG-SEC,"
    "PHY-SEC,MIN-IO,OPT-IO,ALIGNMENT"
)

_LSBLK_COMMAND = "/usr/bin/lsblk"
_FINDMNT_COMMAND = "/usr/bin/findmnt"
_EFIBOOTMGR_COMMAND = "/usr/sbin/efibootmgr"
_BTRFS_COMMAND = "/usr/sbin/btrfs"
_SFDISK_COMMAND = "/usr/sbin/sfdisk"
_UNAME_COMMAND = "/usr/bin/uname"
_VIRTUALIZATION_COMMAND = "/usr/bin/systemd-detect-virt"
_SECURE_BOOT_VARIABLE = "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"


Runner = Callable[..., Any]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _command_name(command: Sequence[str]) -> str:
    return Path(str(command[0])).name if command else "unknown"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _short_text(value: Any, limit: int = 300) -> str:
    text = " ".join(_text(value).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _run_read_only(command: Sequence[str], runner: Runner | None = None) -> tuple[int, str, str, dict[str, Any] | None]:
    """Run one fixed argument vector and normalize common test doubles.

    The fallback call supports tiny fixture runners that accept only the
    argument vector.  Production ``subprocess.run`` always receives
    ``shell=False`` implicitly and ``check=False`` so failures can be emitted
    as inventory errors instead of raising through the caller.
    """

    execute = runner or subprocess.run
    args = list(command)
    try:
        try:
            result = execute(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                shell=False,
            )
        except TypeError:
            # A deliberately minimal fixture runner often accepts ``args``
            # only.  Do not make production behavior depend on this path.
            result = execute(args)
    except FileNotFoundError:
        return 127, "", "command not found", {
            "code": "command_unavailable",
            "command": _command_name(args),
            "message": f"Required read-only command {_command_name(args)!r} is unavailable",
        }
    except PermissionError as error:
        return 126, "", _short_text(error), {
            "code": "command_permission_denied",
            "command": _command_name(args),
            "message": f"Cannot execute read-only command {_command_name(args)!r}",
        }
    except (OSError, subprocess.SubprocessError) as error:
        return 125, "", _short_text(error), {
            "code": "command_failed",
            "command": _command_name(args),
            "message": f"Read-only command {_command_name(args)!r} could not run",
        }

    if isinstance(result, (str, bytes)):
        return 0, _text(result), "", None
    if isinstance(result, (tuple, list)):
        returncode = result[0] if result else 0
        stdout = result[1] if len(result) > 1 else ""
        stderr = result[2] if len(result) > 2 else ""
    elif isinstance(result, Mapping):
        returncode = result.get("returncode", result.get("code", 0))
        stdout = result.get("stdout", result.get("output", ""))
        stderr = result.get("stderr", result.get("error", ""))
    else:
        returncode = getattr(result, "returncode", 0)
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
    try:
        numeric_returncode = int(returncode)
    except (TypeError, ValueError):
        numeric_returncode = 125
    return numeric_returncode, _text(stdout), _text(stderr), None


def _command_error(command: Sequence[str], returncode: int, stderr: str) -> dict[str, Any]:
    name = _command_name(command)
    return {
        "code": "command_failed",
        "command": name,
        "returncode": returncode,
        "message": f"Read-only command {name!r} returned {returncode}",
        "stderr": _short_text(stderr),
    }


def _read_file(path: Path, *, errors: list[dict[str, Any]] | None = None, required: bool = False) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required and errors is not None:
            errors.append({
                "code": "file_unavailable",
                "path": str(path),
                "message": f"Required inspection file {path} is unavailable",
            })
        return None
    except (OSError, UnicodeError) as error:
        if errors is not None:
            errors.append({
                "code": "file_unreadable",
                "path": str(path),
                "message": f"Cannot read inspection file {path}",
                "detail": _short_text(error),
            })
        return None


def _parse_json_command(
    command: Sequence[str], runner: Runner | None, errors: list[dict[str, Any]]
) -> Any:
    returncode, stdout, stderr, immediate_error = _run_read_only(command, runner)
    if immediate_error is not None:
        errors.append(immediate_error)
        return None
    if returncode != 0:
        errors.append(_command_error(command, returncode, stderr))
        return None
    try:
        return json.loads(stdout)
    except (TypeError, ValueError) as error:
        errors.append({
            "code": "malformed_json",
            "command": _command_name(command),
            "message": f"Read-only command {_command_name(command)!r} returned malformed JSON",
            "detail": _short_text(error),
        })
        return None


def _parse_text_command(
    command: Sequence[str], runner: Runner | None, errors: list[dict[str, Any]]
) -> str | None:
    returncode, stdout, stderr, immediate_error = _run_read_only(command, runner)
    if immediate_error is not None:
        errors.append(immediate_error)
        return None
    if returncode != 0:
        errors.append(_command_error(command, returncode, stderr))
        return None
    return stdout


def _read_sysfs_value(
    path: Path,
    *,
    errors: list[dict[str, Any]],
    required: bool = False,
) -> str | None:
    return _read_file(path, errors=errors, required=required)


def _parse_int(value: Any, *, minimum: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            # Sysfs and lsblk values are integral, but accepting an integral
            # float in fixtures avoids losing a valid value after JSON decode.
            number = float(stripped)
        else:
            number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    result = int(number)
    if minimum is not None and result < minimum:
        return None
    return result


_SIZE_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(bytes?|b|kib?|mib?|gib?|tib?|kb|mb|gb|tb)?\s*$",
    re.IGNORECASE,
)


def _bytes(value: Any, *, default_unit: str = "bytes") -> int | None:
    """Parse byte values from lsblk, fixtures, or human-readable fields."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not number.is_integer() and default_unit == "bytes":
            return None
        unit = default_unit
    else:
        match = _SIZE_RE.match(_text(value))
        if match is None:
            return None
        try:
            number = float(match.group(1))
        except (TypeError, ValueError, OverflowError):
            return None
        unit = match.group(2) or default_unit
    if not math.isfinite(number) or number < 0:
        return None
    unit_key = str(unit).lower().replace(" ", "")
    multipliers = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1000,
        "kib": 1024,
        "k": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "m": 1024**2,
        "gb": 1000**3,
        "gib": GIB,
        "g": GIB,
        "tb": 1000**4,
        "tib": 1024**4,
        "t": 1024**4,
    }
    multiplier = multipliers.get(unit_key)
    if multiplier is None:
        return None
    result = number * multiplier
    if not math.isfinite(result) or result < 0 or result > (1 << 63) - 1:
        return None
    return int(result)


def _gib(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        parsed = _bytes(value, default_unit="gib")
        if parsed is None:
            return None
        result = parsed / GIB
    return result if math.isfinite(result) and result >= 0 else None


def _normal_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _text(key).lower())


def _mapping_value(mapping: Mapping[str, Any], *names: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    wanted = {_normal_key(name) for name in names}
    for key, value in mapping.items():
        if _normal_key(key) in wanted:
            return value
    return None


def _mapping_has(mapping: Mapping[str, Any], *names: str) -> bool:
    if not isinstance(mapping, Mapping):
        return False
    wanted = {_normal_key(name) for name in names}
    return any(_normal_key(key) in wanted for key in mapping)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _read_os_release(path: Path, errors: list[dict[str, Any]]) -> dict[str, Any]:
    content = _read_file(path, errors=errors, required=True)
    result: dict[str, Any] = {}
    if content is None:
        return result
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip().lower()] = value
    if not result:
        errors.append({
            "code": "malformed_os_release",
            "path": str(path),
            "message": "The operating-system identity file is empty or malformed",
        })
    return result


def _parse_meminfo(path: Path, errors: list[dict[str, Any]]) -> dict[str, Any]:
    content = _read_file(path, errors=errors, required=True)
    values: dict[str, int] = {}
    if content is None:
        return values
    malformed_critical = False
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key_name = key.strip().lower()
        match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]*)\s*", raw)
        if not match:
            if key_name in {"memtotal", "memavailable"}:
                malformed_critical = True
            continue
        number = int(match.group(1))
        unit = match.group(2).lower()
        multipliers = {
            "": 1,
            "kb": 1024,
            "mb": 1024**2,
            "gb": GIB,
            "tb": 1024**4,
        }
        multiplier = multipliers.get(unit)
        if multiplier is None:
            if key_name in {"memtotal", "memavailable"}:
                malformed_critical = True
            continue
        number *= multiplier
        values[key_name] = number
    if "memtotal" not in values or "memavailable" not in values or malformed_critical:
        errors.append({
            "code": "malformed_meminfo",
            "path": str(path),
            "message": "MemTotal or MemAvailable is missing or malformed in /proc/meminfo",
        })
    return values


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = _text(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "online", "connected"}:
        return True
    if normalized in {"0", "false", "no", "off", "offline", "disconnected"}:
        return False
    return None


def _collect_power(sysfs_root: Path, errors: list[dict[str, Any]]) -> dict[str, Any]:
    power_root = sysfs_root / "class" / "power_supply"
    sources: list[dict[str, Any]] = []
    try:
        entries = sorted(power_root.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        entries = []
    except OSError as error:
        errors.append({
            "code": "file_unreadable",
            "path": str(power_root),
            "message": "Cannot inspect power-supply sysfs",
            "detail": _short_text(error),
        })
        entries = []

    for entry in entries:
        if not entry.is_dir():
            continue
        source_type = _read_sysfs_value(entry / "type", errors=errors)
        source_type = source_type.strip() if source_type else None
        online_raw = _read_sysfs_value(entry / "online", errors=errors)
        capacity_raw = _read_sysfs_value(entry / "capacity", errors=errors)
        status_raw = _read_sysfs_value(entry / "status", errors=errors)
        source: dict[str, Any] = {
            "name": entry.name,
            "type": source_type,
            "online": _parse_bool(online_raw),
            "capacity_percent": _parse_int(capacity_raw, minimum=0),
            "status": status_raw.strip() if status_raw else None,
        }
        sources.append(source)

    mains = [
        source for source in sources
        if (source.get("type") or "").lower() in {"mains", "ac", "usb", "usb-c"}
    ]
    online_values = [source["online"] for source in mains if source.get("online") is not None]
    ac_online: bool | None
    if online_values:
        ac_online = any(online_values)
    else:
        ac_online = None
    batteries = [source for source in sources if (source.get("type") or "").lower() == "battery"]
    capacities = [
        source["capacity_percent"]
        for source in batteries
        if isinstance(source.get("capacity_percent"), int)
    ]
    statuses = [source["status"] for source in batteries if source.get("status")]
    return {
        "ac_online": ac_online,
        "ac_sources": mains,
        "battery_present": bool(batteries),
        "battery_capacity_percent": min(capacities) if capacities else None,
        "battery_status": statuses[0] if statuses else None,
        "sources": sources,
    }


def _read_secure_boot(
    firmware_path: Path, errors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Read the fixed efivarfs SecureBoot variable without exposing payload bytes."""

    path = firmware_path / "efivars" / _SECURE_BOOT_VARIABLE
    result: dict[str, Any] = {
        "path": str(path),
        "enabled": None,
        "state": "unknown",
        "attributes": None,
    }
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        # A missing variable is represented as an unknown state and is gated
        # by plan(); it is common for efivarfs to be unavailable in firmware
        # that otherwise boots UEFI.
        return result
    except (OSError, UnicodeError) as error:
        errors.append({
            "code": "secure_boot_unavailable",
            "path": str(path),
            "message": "Cannot read the fixed UEFI SecureBoot variable",
            "detail": _short_text(error),
        })
        return result
    if len(payload) < 5:
        errors.append({
            "code": "malformed_secure_boot",
            "path": str(path),
            "message": "The UEFI SecureBoot variable is shorter than its attributes and state fields",
            "size": len(payload),
        })
        return result
    result["attributes"] = int.from_bytes(payload[:4], byteorder="little", signed=False)
    state = payload[4]
    if state in (0, 1):
        result["enabled"] = bool(state)
        result["state"] = "enabled" if state else "disabled"
    else:
        errors.append({
            "code": "malformed_secure_boot",
            "path": str(path),
            "message": "The UEFI SecureBoot state byte is not 0 or 1",
            "state_byte": state,
        })
    return result


def _flatten_devices(value: Any, parent: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Flatten lsblk's nested children while retaining parent context."""

    result: list[dict[str, Any]] = []
    candidates: list[Any]
    if isinstance(value, Mapping):
        nested = _mapping_value(value, "blockdevices", "block_devices", "devices", "children")
        candidates = _as_list(nested) if nested is not None else []
    else:
        candidates = _as_list(value)
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        copied = dict(item)
        if parent is not None:
            copied.setdefault("parent_name", _mapping_value(parent, "name", "kname"))
            copied.setdefault("parent_path", _mapping_value(parent, "path"))
        result.append(copied)
        children = _mapping_value(item, "children")
        if children is not None:
            result.extend(_flatten_devices(children, item))
    return result


def _parse_mounts(value: Any) -> list[dict[str, Any]]:
    """Flatten findmnt's nested mount tree without losing subvolume fields."""

    if isinstance(value, Mapping):
        nested = _mapping_value(value, "filesystems", "mounts", "findmnt")
        if nested is not None:
            value = nested
    result: list[dict[str, Any]] = []

    def visit(candidate: Any) -> None:
        for item in _as_list(candidate):
            if not isinstance(item, Mapping):
                continue
            copied = dict(item)
            children = _mapping_value(item, "children")
            copied.pop("children", None)
            result.append(copied)
            if children is not None:
                visit(children)

    visit(value)
    return result


def _parse_efi(text: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "raw": text or "",
        "boot_current": None,
        "boot_order": [],
        "entries": [],
        "variables_supported": bool(text),
    }
    if text is None:
        result["variables_supported"] = None
        return result
    if re.search(r"EFI variables are not supported|Could not set variable", text, re.IGNORECASE):
        result["variables_supported"] = False
    for line in text.splitlines():
        current = re.match(r"\s*BootCurrent:\s*([0-9A-Fa-f]{4})", line)
        if current:
            result["boot_current"] = current.group(1).upper()
            continue
        order = re.match(r"\s*BootOrder:\s*([^\r\n]*)", line)
        if order:
            result["boot_order"] = [
                item.upper()
                for item in re.findall(r"[0-9A-Fa-f]{4}", order.group(1))
            ]
            continue
        entry = re.match(r"\s*Boot([0-9A-Fa-f]{4})([*!]?)[ \t]+(.*)", line)
        if entry:
            identifier, marker, description = entry.groups()
            path_match = re.search(r"File\(([^)]*)\)", description, re.IGNORECASE)
            result["entries"].append({
                "id": identifier.upper(),
                "active": marker == "*",
                "description": description.strip(),
                "path": path_match.group(1) if path_match else None,
            })
    return result


def _find_first_number(value: Any, names: Iterable[str]) -> int | None:
    wanted = {_normal_key(name) for name in names}
    if isinstance(value, Mapping):
        for key, candidate in value.items():
            if _normal_key(key) in wanted:
                parsed = _bytes(candidate)
                if parsed is not None:
                    return parsed
                nested = _find_first_number(candidate, ("bytes", "value", "size"))
                if nested is not None:
                    return nested
        for candidate in value.values():
            found = _find_first_number(candidate, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_first_number(candidate, names)
            if found is not None:
                return found
    return None


def _find_first_value(value: Any, names: Iterable[str]) -> Any:
    wanted = {_normal_key(name) for name in names}
    if isinstance(value, Mapping):
        for key, candidate in value.items():
            if _normal_key(key) in wanted:
                return candidate
        for candidate in value.values():
            found = _find_first_value(candidate, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_first_value(candidate, names)
            if found is not None:
                return found
    return None


_BTRFS_VALUE_RE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z0-9_() ,/.-]*):\s*"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*"
    r"(?:bytes?|[KMGT]i?B|[KMGT]B)?)",
    re.IGNORECASE,
)


def _parse_btrfs_usage_text(text: str) -> dict[str, Any]:
    """Parse the stable numeric fields from ``btrfs filesystem usage --raw``.

    btrfs-progs intentionally exposes this command as human-readable text,
    including on Fedora 43; there is no ``--json`` mode.  The parser accepts
    both raw byte output and human-readable fixture output so tests can cover
    version differences without changing the production command.
    """

    result: dict[str, Any] = {"raw": text, "format": "text"}
    labels = {
        "devicesize": "device_size_bytes",
        "devicesizebytes": "device_size_bytes",
        "deviceallocated": "allocated_bytes",
        "deviceallocatedbytes": "allocated_bytes",
        "deviceunallocated": "unallocated_bytes",
        "deviceunallocatedbytes": "unallocated_bytes",
        "devicemissing": "missing_bytes",
        "used": "used_bytes",
        "usedbytes": "used_bytes",
        "freeestimated": "free_bytes",
        "freeestimatedbytes": "free_bytes",
        "free": "free_bytes",
        "freebytes": "free_bytes",
        "minimumsize": "minimum_size_bytes",
        "minimumsizebytes": "minimum_size_bytes",
        "minsize": "minimum_size_bytes",
        "minsizebytes": "minimum_size_bytes",
    }
    for match in _BTRFS_VALUE_RE.finditer(text):
        key = labels.get(_normal_key(match.group("label")))
        if key is None:
            continue
        parsed = _bytes(match.group("value"))
        if parsed is not None and key not in result:
            result[key] = parsed

    # Keep a conservative estimate available when the command exposes only
    # ``Used`` and ``Free (estimated)``.  The margin accounts for relocation
    # and metadata slack; an exact minimum is not promised by btrfs-progs.
    device_size = result.get("device_size_bytes")
    used = result.get("used_bytes")
    free = result.get("free_bytes")
    if "minimum_size_bytes" not in result and isinstance(used, int):
        occupied = used
        if isinstance(device_size, int) and isinstance(free, int):
            occupied = max(occupied, device_size - free)
        margin = max(GIB, math.ceil(occupied * 0.10))
        estimate = occupied + margin
        if isinstance(device_size, int):
            estimate = min(estimate, device_size)
        result["minimum_size_bytes"] = estimate
        result["minimum_size_method"] = "used_plus_10_percent_margin"
    return result


def _parse_btrfs_show_text(text: str) -> dict[str, Any]:
    """Parse UUID, device count, and paths from ``btrfs filesystem show``."""

    result: dict[str, Any] = {"raw": text, "format": "text", "devices": []}
    uuid_match = re.search(r"\buuid:\s*([0-9A-Fa-f-]{8,})", text, re.IGNORECASE)
    if uuid_match:
        result["filesystem_uuid"] = uuid_match.group(1)
    count_match = re.search(r"\bTotal\s+devices\s+(\d+)", text, re.IGNORECASE)
    if count_match:
        result["device_count"] = int(count_match.group(1))
    device_re = re.compile(
        r"^\s*devid\s+(?P<devid>\d+)\s+size\s+(?P<size>\S+)"
        r"(?:\s+used\s+(?P<used>\S+))?\s+path\s+(?P<path>\S+)\s*$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = device_re.match(line)
        if match is None:
            continue
        device: dict[str, Any] = {
            "devid": int(match.group("devid")),
            "size": _bytes(match.group("size")),
            "path": match.group("path"),
        }
        if match.group("used") is not None:
            device["used"] = _bytes(match.group("used"))
        result["devices"].append(device)
    if result.get("device_count") is None and result["devices"]:
        result["device_count"] = len(result["devices"])
    if result.get("filesystem_uuid") is None:
        result["filesystem_uuid"] = None
    return result


def _parse_btrfs_command(
    command: Sequence[str], runner: Runner | None, errors: list[dict[str, Any]]
) -> Any:
    """Run a read-only Btrfs command and accept JSON only for fixture input."""

    returncode, stdout, stderr, immediate_error = _run_read_only(command, runner)
    if immediate_error is not None:
        errors.append(immediate_error)
        return None
    if returncode != 0:
        errors.append(_command_error(command, returncode, stderr))
        return None
    stripped = stdout.strip()
    if not stripped:
        errors.append({
            "code": "malformed_btrfs_output",
            "command": _command_name(command),
            "message": f"Read-only command {_command_name(command)!r} returned no data",
        })
        return None
    if stripped[:1] in {"{", "["}:
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            errors.append({
                "code": "malformed_btrfs_output",
                "command": _command_name(command),
                "message": f"Read-only command {_command_name(command)!r} returned malformed JSON",
            })
            return None
        if not isinstance(parsed, (Mapping, list)) or not parsed:
            errors.append({
                "code": "malformed_btrfs_output",
                "command": _command_name(command),
                "message": f"Read-only command {_command_name(command)!r} returned an empty or invalid result",
            })
            return None
        return parsed
    if command[2] == "usage":
        parsed = _parse_btrfs_usage_text(stdout)
        if not any(key in parsed for key in ("device_size_bytes", "used_bytes", "free_bytes")):
            errors.append({
                "code": "malformed_btrfs_output",
                "command": _command_name(command),
                "message": "Btrfs usage output did not contain recognizable size fields",
            })
            return None
        return parsed
    parsed = _parse_btrfs_show_text(stdout)
    if not parsed.get("devices") and parsed.get("filesystem_uuid") is None and parsed.get("device_count") is None:
        errors.append({
            "code": "malformed_btrfs_output",
            "command": _command_name(command),
            "message": "Btrfs filesystem-show output did not contain recognizable topology",
        })
        return None
    return parsed


def _collect_btrfs(
    root_mount: str,
    *,
    runner: Runner | None,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    usage_command = [_BTRFS_COMMAND, "filesystem", "usage", "--raw", root_mount]
    show_command = [_BTRFS_COMMAND, "filesystem", "show", "--raw", root_mount]
    usage = _parse_btrfs_command(usage_command, runner, errors)
    show = _parse_btrfs_command(show_command, runner, errors)

    devices: list[Any] = []
    if isinstance(show, Mapping):
        candidate = _mapping_value(show, "filesystem-show", "filesystems", "devices")
        # A parsed text show result has a direct ``devices`` list.  JSON
        # fixture producers may wrap it one level deeper.
        records = _as_list(candidate if candidate is not None else show)
    else:
        records = _as_list(show)
    for record in records:
        if not isinstance(record, Mapping):
            continue
        nested = _mapping_value(record, "devices")
        if nested is not None:
            devices.extend(item for item in _as_list(nested) if isinstance(item, Mapping))
        elif _mapping_has(record, "name", "path", "devid", "device_id", "size"):
            devices.append(record)

    device_count = _find_first_number(show, ("total_devices", "device_count", "num_devices"))
    if device_count is None and devices:
        device_count = len(devices)
    filesystem_uuid = _find_first_value(show, ("uuid", "filesystem_uuid", "fsid"))
    device_size_bytes = _find_first_number(
        usage,
        ("device_size", "device_size_bytes", "total_bytes", "filesystem_size", "total_size"),
    )
    if device_size_bytes is None and devices:
        # ``filesystem show`` is a useful fallback when usage does not report
        # the aggregate device size.  For a supported single-device layout it
        # is unambiguous.
        sizes = [_find_first_number(item, ("size", "device_size", "bytes")) for item in devices]
        if sizes and all(size is not None for size in sizes):
            device_size_bytes = sum(size for size in sizes if size is not None)
    used_bytes = _find_first_number(usage, ("used", "used_bytes", "bytes_used"))
    free_bytes = _find_first_number(
        usage,
        ("free_estimated", "free_(estimated)", "free", "free_bytes"),
    )
    minimum_size_bytes = _find_first_number(
        usage,
        ("minimum_size", "minimum_size_bytes", "min_size", "min_size_bytes"),
    )
    # Some fixture producers place a precomputed conservative estimate next to
    # usage.  Preserve it explicitly instead of pretending ``used`` alone is a
    # safe shrink bound.
    if minimum_size_bytes is None:
        minimum_size_bytes = _find_first_number(
            show,
            ("minimum_size", "minimum_size_bytes", "min_size", "min_size_bytes"),
        )
    return {
        "root_mount": root_mount,
        "usage": usage,
        "show": show,
        "filesystem_uuid": _text(filesystem_uuid) if filesystem_uuid is not None else None,
        "device_count": device_count,
        "devices": devices,
        "device_size_bytes": device_size_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "minimum_size_bytes": minimum_size_bytes,
    }


def _safe_disk_usage(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as error:
        return None, {
            "code": "staging_unavailable",
            "path": str(path),
            "message": f"Cannot measure staging capacity at {path}",
            "detail": _short_text(error),
        }
    return {
        "path": str(path),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "available_bytes": int(usage.free),
        "free_gib": round(usage.free / GIB, 6),
    }, None


def _optional_text_command(command: Sequence[str], runner: Runner | None) -> tuple[str | None, dict[str, Any] | None]:
    """Read a diagnostic command without making it a storage-safety gate."""

    returncode, stdout, stderr, immediate_error = _run_read_only(command, runner)
    if immediate_error is not None:
        return None, immediate_error
    if returncode != 0:
        return None, _command_error(command, returncode, stderr)
    return stdout, None


_DMI_VIRTUALIZATION_MARKERS = (
    ("qemu", "qemu"),
    ("kvm", "kvm"),
    ("virtualbox", "virtualbox"),
    ("vmware", "vmware"),
    ("hyper-v", "hyper-v"),
    ("microsoft corporation virtual", "hyper-v"),
    ("xen", "xen"),
    ("parallels", "parallels"),
    ("bhyve", "bhyve"),
    ("amazon ec2", "amazon-ec2"),
    ("google compute engine", "google-compute-engine"),
    ("openstack", "openstack"),
)


def _dmi_virtualization(host: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recognize strong, root-collected DMI VM markers as a fallback."""

    fields = " ".join(
        _text(host.get(name)).strip().lower()
        for name in ("product_name", "product_version", "sys_vendor", "board_name")
        if host.get(name) not in (None, "")
    )
    for marker, technology in _DMI_VIRTUALIZATION_MARKERS:
        if marker in fields:
            return {
                "is_virtual": True,
                "technology": technology,
                "source": "dmi",
            }
    return None


def _collect_virtualization(
    *, runner: Runner | None, host: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Determine whether the root-collected host is a VM.

    ``systemd-detect-virt --vm`` is a fixed, read-only probe.  Exit status 1
    means a physical host; execution failures fall back to strong DMI markers
    without turning an otherwise usable AC-powered host into an inventory
    error.  The distinction matters only when no AC/battery device is exposed:
    a proven VM can safely treat that absence as expected, while a physical
    host must remain blocked.
    """

    command = [_VIRTUALIZATION_COMMAND, "--vm"]
    returncode, stdout, stderr, immediate_error = _run_read_only(command, runner)
    dmi = _dmi_virtualization(host)
    if immediate_error is not None:
        return dmi or {
            "is_virtual": None,
            "technology": None,
            "source": "unknown",
        }, immediate_error
    if returncode == 0:
        return {
            "is_virtual": True,
            "technology": _short_text(stdout, limit=80) or None,
            "source": "systemd-detect-virt",
        }, None
    if returncode == 1:
        return {
            "is_virtual": False,
            "technology": None,
            "source": "systemd-detect-virt",
        }, None
    error = _command_error(command, returncode, stderr)
    return dmi or {
        "is_virtual": None,
        "technology": None,
        "source": "unknown",
    }, error


def _parse_uname(text: str | None) -> dict[str, Any]:
    if not text:
        return {"raw": None, "kernel": None, "machine": None}
    raw = " ".join(text.split())
    fields = raw.split()
    machine = next(
        (
            field
            for field in fields
            if field.lower()
            in {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "i686", "riscv64", "ppc64le"}
        ),
        None,
    )
    return {
        "raw": raw,
        "kernel": fields[0] if fields else None,
        "hostname": fields[1] if len(fields) > 1 else None,
        "release": fields[2] if len(fields) > 2 else None,
        "version": " ".join(fields[3 : fields.index(machine)]).strip() if machine in fields and fields.index(machine) > 3 else (fields[3] if len(fields) > 3 else None),
        "machine": machine or (fields[4] if len(fields) > 4 else None),
    }


def collect(
    *,
    runner: Runner | None = None,
    sysfs_root: str | os.PathLike[str] = "/sys",
    proc_root: str | os.PathLike[str] = "/proc",
    etc_root: str | os.PathLike[str] = "/etc",
    staging_paths: Sequence[str | os.PathLike[str]] | None = None,
    root_mount: str = "/",
) -> dict[str, Any]:
    """Collect a read-only inventory of the currently running Fedora host.

    ``runner`` receives a list of command arguments and may return a
    ``subprocess.CompletedProcess``, a ``(returncode, stdout, stderr)`` tuple,
    or a mapping with those keys.  This is the only command-execution seam;
    every production command below is an inspection command and no shell is
    used.
    """

    sysfs = Path(sysfs_root)
    proc = Path(proc_root)
    etc = Path(etc_root)
    errors: list[dict[str, Any]] = []
    try:
        euid = int(os.geteuid())
    except (AttributeError, OSError, TypeError, ValueError):
        euid = None
    is_root = euid == 0
    if not is_root:
        errors.append({
            "code": "root_required",
            "message": "The preflight must run as root to inspect and revalidate the target safely",
            "euid": euid,
        })

    lsblk_command = [_LSBLK_COMMAND, "--bytes", "--json", "--output", _LSBLK_COLUMNS]
    findmnt_command = [
        _FINDMNT_COMMAND,
        "--json",
        "--output",
        "TARGET,SOURCE,FSTYPE,OPTIONS,FSROOT,UUID,PARTUUID",
    ]
    lsblk = _parse_json_command(lsblk_command, runner, errors)
    findmnt = _parse_json_command(findmnt_command, runner, errors)
    efi_text = _parse_text_command([_EFIBOOTMGR_COMMAND, "-v"], runner, errors)
    uname_text, uname_error = _optional_text_command([_UNAME_COMMAND, "-a"], runner)

    devices = _flatten_devices(lsblk)
    mounts = _parse_mounts(findmnt)
    root_path = root_mount or "/"
    btrfs = _collect_btrfs(root_path, runner=runner, errors=errors)

    # sfdisk's JSON dump is the authoritative sector geometry used by the
    # planner.  It is read only; no partition mutation command is exposed by
    # this module.  Resolve the disk from the mounted root before querying it.
    root_mount_record = _mount_for(mounts, ("/",))
    root_device = _find_source_device(_mount_source(root_mount_record), devices)
    parent_disk = _parent_disk(root_device, devices) if root_device else None
    partition_table: Mapping[str, Any] | None = None
    sfdisk = None
    parent_path = _device_path(parent_disk or {})
    if parent_path:
        sfdisk = _parse_json_command([_SFDISK_COMMAND, "--json", parent_path], runner, errors)
        if isinstance(sfdisk, Mapping):
            candidate = _mapping_value(sfdisk, "partitiontable", "partition_table")
            if isinstance(candidate, Mapping):
                partition_table = dict(candidate)

    os_release = _read_os_release(etc / "os-release", errors)
    meminfo = _parse_meminfo(proc / "meminfo", errors)
    total_bytes = meminfo.get("memtotal")
    available_bytes = meminfo.get("memavailable")
    firmware_path = sysfs / "firmware" / "efi"
    try:
        uefi = firmware_path.is_dir()
    except OSError as error:
        uefi = None
        errors.append({
            "code": "firmware_unreadable",
            "path": str(firmware_path),
            "message": "Cannot inspect the UEFI sysfs directory",
            "detail": _short_text(error),
        })
    secure_boot = _read_secure_boot(firmware_path, errors)

    host: dict[str, Any] = {}
    for key in (
        "product_name",
        "product_version",
        "sys_vendor",
        "board_name",
        "product_serial",
        "chassis_serial",
    ):
        value = _read_sysfs_value(sysfs / "class" / "dmi" / "id" / key, errors=errors)
        if value is not None:
            host[key] = value.strip()

    power = _collect_power(sysfs, errors)
    virtualization, virtualization_error = _collect_virtualization(runner=runner, host=host)
    paths = tuple(staging_paths or _DEFAULT_STAGING_PATHS)
    staging: dict[str, Any] = {"path": None, "free_bytes": None, "available_bytes": None, "free_gib": None}
    staging_error: dict[str, Any] | None = None
    for candidate in paths:
        staging_result, candidate_error = _safe_disk_usage(Path(candidate))
        if staging_result is not None:
            staging = staging_result
            staging_error = None
            break
        staging_error = candidate_error
    if staging["path"] is None and staging_error is not None:
        errors.append(staging_error)

    diagnostics: dict[str, Any] = {}
    if uname_error:
        diagnostics["uname_error"] = uname_error
    if virtualization_error:
        diagnostics["virtualization_error"] = virtualization_error

    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": _utc_now(),
        "root": {"euid": euid, "is_root": is_root},
        "host": host,
        "kernel": _parse_uname(uname_text),
        "os": {
            "id": os_release.get("id"),
            "id_like": os_release.get("id_like"),
            "name": os_release.get("name"),
            "pretty_name": os_release.get("pretty_name"),
            "version_id": os_release.get("version_id"),
            "raw": os_release,
        },
        "firmware": {
            "uefi": uefi,
            "efi_sysfs_path": str(firmware_path),
            "secure_boot": secure_boot,
        },
        "secure_boot": secure_boot,
        "memory": {
            "total_bytes": total_bytes,
            "available_bytes": available_bytes,
            "total_gib": round(total_bytes / GIB, 6) if total_bytes is not None else None,
            "available_gib": round(available_bytes / GIB, 6) if available_bytes is not None else None,
        },
        "power": power,
        "virtualization": virtualization,
        "block_devices": devices,
        "lsblk": lsblk,
        "mounts": mounts,
        "findmnt": findmnt,
        "btrfs": btrfs,
        "sfdisk": sfdisk,
        "partition_table": partition_table,
        "efi": _parse_efi(efi_text),
        "staging": staging,
        "diagnostics": diagnostics,
        "errors": errors,
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(candidate) for key, candidate in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(candidate) for candidate in value]
    return _text(value)


def _devices_from_inventory(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: Any = _mapping_value(inventory, "block_devices", "devices", "lsblk")
    devices = _flatten_devices(candidates)
    if devices:
        return devices
    # Fixture inventories often provide a single disk object and a separate
    # partition list instead of lsblk's nested shape.
    storage = _mapping_value(inventory, "storage")
    if isinstance(storage, Mapping):
        devices = _flatten_devices(_mapping_value(storage, "block_devices", "devices", "lsblk"))
    if devices:
        return devices
    disk = _mapping_value(inventory, "disk")
    if isinstance(disk, Mapping):
        result = [dict(disk)]
        for partition in _as_list(_mapping_value(disk, "partitions")):
            if isinstance(partition, Mapping):
                child = dict(partition)
                child.setdefault("parent_name", _mapping_value(disk, "name", "kname"))
                child.setdefault("parent_path", _mapping_value(disk, "path"))
                result.append(child)
        return result
    return []


def _mounts_from_inventory(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("mounts", "findmnt"):
        candidate = _mapping_value(inventory, key)
        if candidate is not None:
            mounts = _parse_mounts(candidate)
            if mounts:
                return mounts
    storage = _mapping_value(inventory, "storage")
    if isinstance(storage, Mapping):
        return _parse_mounts(_mapping_value(storage, "mounts", "findmnt"))
    return []


def _device_field(device: Mapping[str, Any], *names: str) -> Any:
    return _mapping_value(device, *names)


def _device_path(device: Mapping[str, Any]) -> str | None:
    path = _device_field(device, "path", "devpath", "device_path")
    if path:
        path_text = _text(path)
        return path_text if path_text.startswith("/") else "/dev/" + path_text
    name = _device_field(device, "name", "kname", "device")
    if name:
        name_text = _text(name)
        return name_text if name_text.startswith("/") else "/dev/" + name_text
    return None


def _device_type(device: Mapping[str, Any]) -> str:
    return _text(_device_field(device, "type")).strip().lower()


def _is_disk(device: Mapping[str, Any]) -> bool:
    return _device_type(device) in {"disk", "nvme", "mmc", "loop"} or (
        _device_type(device) == "" and _mapping_has(device, "model", "serial", "wwn", "ptuuid")
    )


def _is_partition(device: Mapping[str, Any]) -> bool:
    return _device_type(device) in {"part", "partition"} or _mapping_has(
        device, "partuuid", "parttype", "parttypename", "partition_number"
    )


def _source_matches(source: Any, device: Mapping[str, Any]) -> bool:
    if source is None:
        return False
    source_text = _text(source).strip()
    # findmnt annotates a mounted Btrfs subvolume as
    # ``/dev/nvme0n1p3[/root]``.  The suffix identifies the subvolume and is
    # handled separately by :func:`_subvolume`; matching here must use the
    # underlying block-device path.
    source_text = re.sub(r"\[[^]]*\]$", "", source_text)
    if source_text.startswith("UUID="):
        source_text = source_text[5:]
        return source_text and source_text == _text(_device_field(device, "uuid", "filesystem_uuid"))
    if source_text.startswith("PARTUUID="):
        source_text = source_text[9:]
        return source_text and source_text == _text(_device_field(device, "partuuid"))
    if source_text.startswith("/dev/disk/by-partuuid/"):
        source_id = source_text.rsplit("/", 1)[-1]
        return source_id == _text(_device_field(device, "partuuid"))
    if source_text.startswith("/dev/disk/by-uuid/"):
        source_id = source_text.rsplit("/", 1)[-1]
        return source_id == _text(_device_field(device, "uuid", "filesystem_uuid"))
    path = _device_path(device)
    if path and source_text == path:
        return True
    return source_text.rsplit("/", 1)[-1] == (_text(_device_field(device, "name", "kname")))


def _mount_field(mount: Mapping[str, Any], *names: str) -> Any:
    return _mapping_value(mount, *names)


def _mount_for(mounts: Sequence[Mapping[str, Any]], targets: Iterable[str]) -> Mapping[str, Any] | None:
    wanted = set(targets)
    for mount in mounts:
        if _text(_mount_field(mount, "target", "mountpoint")).strip() in wanted:
            return mount
    return None


def _mount_fstype(mount: Mapping[str, Any] | None) -> str:
    return _text(_mount_field(mount or {}, "fstype", "filesystem", "fs_type")).strip().lower()


def _mount_source(mount: Mapping[str, Any] | None) -> str | None:
    value = _mount_field(mount or {}, "source", "src", "device")
    return _text(value).strip() if value is not None else None


def _mount_options(mount: Mapping[str, Any] | None) -> str:
    options = _mount_field(mount or {}, "options", "opts")
    if isinstance(options, (list, tuple, set)):
        return ",".join(_text(item) for item in options)
    return _text(options)


def _subvolume(mount: Mapping[str, Any] | None) -> str | None:
    options = _mount_options(mount)
    match = re.search(r"(?:^|,)subvol(?:id)?=([^,]+)", options)
    if match:
        return match.group(1)
    fsroot = _mount_field(mount or {}, "fsroot", "fs_root", "root")
    if fsroot is not None and _text(fsroot).strip() not in {"", "/"}:
        return _text(fsroot).strip()
    return None


def _find_source_device(source: str | None, devices: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if source is None:
        return None
    for device in devices:
        if _source_matches(source, device):
            return device
    return None


def _parent_disk(
    partition: Mapping[str, Any], devices: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    parent_name = _device_field(partition, "pkname", "parent_name", "disk_name")
    parent_path = _device_field(partition, "parent_path", "disk_path")
    for device in devices:
        if not _is_disk(device):
            continue
        if parent_path and _device_path(device) == _text(parent_path):
            return device
        if parent_name and _text(_device_field(device, "name", "kname")) == _text(parent_name):
            return device
    # A nested fixture may omit PKNAME but still have a single disk.
    disks = [device for device in devices if _is_disk(device)]
    return disks[0] if len(disks) == 1 else None


def _boundary_bytes(
    device: Mapping[str, Any], logical_sector_size: int | None
) -> tuple[int | None, int | None, str | None]:
    start = _device_field(device, "start_bytes", "start_byte", "offset_bytes")
    size = _device_field(device, "size_bytes", "length_bytes", "partition_size_bytes")
    if start is not None:
        start_bytes = _bytes(start)
    else:
        start_sectors = _device_field(device, "start_sectors", "start_sector")
        if start_sectors is not None and logical_sector_size is not None:
            parsed = _parse_int(start_sectors, minimum=0)
            start_bytes = parsed * logical_sector_size if parsed is not None else None
        else:
            raw_start = _device_field(device, "start")
            if raw_start is None and _device_type(device) in {"disk", "nvme", "mmc"}:
                # lsblk omits START for whole disks; its boundary begins at
                # byte zero for the purposes of an end-of-disk check.
                start_bytes = 0
            else:
                # lsblk documents START as a count of 512-byte sectors even
                # when LOG-SEC is 4096.  Do not interpret it as a byte count.
                parsed = _parse_int(raw_start, minimum=0)
                start_bytes = parsed * 512 if parsed is not None else None
    if size is not None:
        size_bytes = _bytes(size)
    else:
        sectors = _device_field(device, "sectors", "size_sectors", "partition_sectors")
        if sectors is not None and logical_sector_size is not None:
            parsed = _parse_int(sectors, minimum=0)
            size_bytes = parsed * logical_sector_size if parsed is not None else None
        else:
            raw_size = _device_field(device, "size")
            size_bytes = _bytes(raw_size)
    end_bytes = start_bytes + size_bytes if start_bytes is not None and size_bytes is not None else None
    return start_bytes, size_bytes, end_bytes


def _stable_identifier(mapping: Mapping[str, Any], *names: str) -> tuple[str, str] | None:
    for name in names:
        value = _mapping_value(mapping, name)
        if value is None:
            continue
        text_value = _text(value).strip()
        if not text_value or text_value.lower() in {"none", "null", "unknown", "-", "n/a"}:
            continue
        # Device paths are useful context but do not identify a disk across
        # boot/re-enumeration and therefore cannot satisfy this check alone.
        if text_value.startswith("/dev/"):
            continue
        return name, text_value
    return None


def _disk_identity(disk: Mapping[str, Any]) -> tuple[str, str] | None:
    return _stable_identifier(
        disk,
        "stable_id",
        "stableid",
        "serial",
        "wwn",
        "eui",
        "disk_uuid",
        "disk_id",
        "by_id",
        "id",
    )


def _partition_table(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the normalized sfdisk partition-table object, if present."""

    for name in ("partition_table", "sfdisk"):
        candidate = _mapping_value(inventory, name)
        if not isinstance(candidate, Mapping):
            continue
        nested = _mapping_value(candidate, "partitiontable", "partition_table")
        if isinstance(nested, Mapping):
            return nested
        # A fixture may provide the table object directly.
        if _mapping_has(candidate, "label", "id", "partitions"):
            return candidate
    return {}


def _uuid_text(value: Any) -> str | None:
    """Normalize a UUID-like identity while rejecting changed/malformed IDs."""

    value_text = _text(value).strip()
    if not value_text:
        return None
    try:
        import uuid

        return str(uuid.UUID(value_text))
    except (ValueError, AttributeError, TypeError):
        return None


def _table_uuid(disk: Mapping[str, Any], inventory: Mapping[str, Any]) -> str | None:
    value = _mapping_value(disk, "ptuuid", "partition_table_uuid", "gpt_uuid", "table_uuid")
    if value is None:
        value = _mapping_value(inventory, "partition_table_uuid", "gpt_uuid", "table_uuid", "ptuuid")
    if value is None:
        value = _mapping_value(_partition_table(inventory), "id", "ptuuid", "uuid")
    text_value = _text(value).strip() if value is not None else ""
    return text_value or None


def _logical_sector_size(disk: Mapping[str, Any], inventory: Mapping[str, Any]) -> int | None:
    value = _mapping_value(
        disk,
        "logical_sector_size",
        "logical_sector",
        "log_sec",
        "log-sec",
        "sector_size",
    )
    if value is None:
        value = _mapping_value(inventory, "logical_sector_size", "sector_size")
    if value is None:
        value = _mapping_value(_partition_table(inventory), "sectorsize", "sector_size", "logical_sector_size")
    return _parse_int(value, minimum=1)


def _btrfs_inventory(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "btrfs", "filesystem")
    return value if isinstance(value, Mapping) else {}


def _btrfs_value(btrfs: Mapping[str, Any], *names: str) -> Any:
    value = _mapping_value(btrfs, *names)
    if value is not None:
        return value
    for nested_name in ("usage", "show", "filesystem", "device"):
        nested = _mapping_value(btrfs, nested_name)
        value = _find_first_value(nested, names)
        if value is not None:
            return value
    return None


def _btrfs_number(btrfs: Mapping[str, Any], *names: str) -> int | None:
    value = _btrfs_value(btrfs, *names)
    if value is None:
        return None
    parsed = _bytes(value)
    if parsed is not None:
        return parsed
    return _find_first_number(value, ("bytes", "value", "size"))


def _number_from(mapping: Mapping[str, Any], *names: str) -> int | None:
    value = _mapping_value(mapping, *names)
    return _bytes(value) if value is not None else None


def _contains_encryption(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, candidate in value.items():
            normal = _normal_key(key)
            if normal in {"encrypted", "isencrypted", "encryption"}:
                parsed = _parse_bool(candidate)
                if parsed is True or (_text(candidate).strip().lower() not in {"", "none", "false", "0", "no"} and parsed is None):
                    return True
            if _contains_encryption(candidate):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_encryption(item) for item in value)
    elif isinstance(value, str):
        lowered = value.lower()
        return "crypto_luks" in lowered or "cryptsetup" in lowered or "luks" in lowered
    return False


def _normalize_os(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "os", "edition", "distribution")
    if isinstance(value, Mapping):
        return value
    if value is not None:
        return {"name": value}
    return {}


def _normalize_firmware(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "firmware", "uefi")
    if isinstance(value, Mapping):
        return value
    return {"uefi": value}


def _normalize_secure_boot(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "secure_boot")
    if isinstance(value, Mapping):
        return value
    firmware = _normalize_firmware(inventory)
    nested = _mapping_value(firmware, "secure_boot", "secureboot")
    if isinstance(nested, Mapping):
        return nested
    if nested is not None:
        return {"enabled": nested}
    if _mapping_has(firmware, "secure_boot_enabled", "secure_boot_state"):
        return firmware
    if value is not None:
        return {"enabled": value}
    return {}


def _normalize_memory(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "memory", "ram")
    return value if isinstance(value, Mapping) else {}


def _normalize_power(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "power", "battery")
    return value if isinstance(value, Mapping) else {}


def _normalize_virtualization(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "virtualization", "virt", "vm")
    if isinstance(value, Mapping):
        return value
    if value is not None:
        return {"is_virtual": value}
    return {}


def _normalize_staging(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "staging", "storage_capacity")
    return value if isinstance(value, Mapping) else {}


def _normalize_efi(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _mapping_value(inventory, "efi", "efibootmgr", "bootloader")
    return value if isinstance(value, Mapping) else {}


def _inventory_error_blockers(inventory: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    blockers: list[tuple[str, str, dict[str, Any]]] = []
    errors = _mapping_value(inventory, "errors", "error")
    for error in _as_list(errors):
        if isinstance(error, Mapping):
            code = _text(_mapping_value(error, "code", "error")).strip() or "inventory_error"
            message = _text(_mapping_value(error, "message", "detail")).strip() or "Inventory collection reported an error"
            details = dict(error)
            # ``add`` receives the normalized blocker code and message as
            # positional arguments.  Keep the original values as details
            # under distinct keys so a malformed command cannot trigger a
            # secondary ``multiple values for keyword`` exception.
            details.pop("code", None)
            details.pop("message", None)
            details.pop("error", None)
            if code not in {"inventory_error", ""}:
                details.setdefault("source_code", code)
        else:
            code = "inventory_error"
            message = _text(error) or "Inventory collection reported an error"
            details = {"error": error}
        if code == "root_required":
            blocker_code = "root_required"
        elif code in {"malformed_json", "malformed_os_release", "malformed_meminfo"}:
            blocker_code = "malformed_inventory"
        elif code in {"file_unavailable", "file_unreadable", "firmware_unreadable"}:
            blocker_code = "inventory_file_unavailable"
        else:
            blocker_code = "inventory_unavailable"
        blockers.append((blocker_code, message, details))
    return blockers


def _partition_table_rows(table: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = _mapping_value(table, "partitions", "partition")
    return [item for item in _as_list(value) if isinstance(item, Mapping)]


def _partition_table_blockers(
    table: Mapping[str, Any],
    *,
    devices: Sequence[Mapping[str, Any]],
    parent_disk: Mapping[str, Any] | None,
    root_partition: Mapping[str, Any] | None,
    logical_sector_size: int | None,
    allocation_gib: int,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Validate the exact sfdisk table and cross-check it with lsblk."""

    blockers: list[tuple[str, str, dict[str, Any]]] = []

    def reject(code: str, message: str, **details: Any) -> None:
        blockers.append((code, message, details))

    if not table:
        reject("partition_table_unavailable", "The read-only GPT partition table could not be collected")
        return blockers
    label = _text(_mapping_value(table, "label")).strip().lower()
    if label != "gpt":
        reject("unsupported_partition_table", "The supported layout requires a GPT partition table", label=label or None)

    table_id = _uuid_text(_mapping_value(table, "id", "ptuuid", "uuid"))
    if table_id is None:
        reject("malformed_partition_table_id", "The GPT partition-table ID is missing or malformed")
    disk_id = _uuid_text(_mapping_value(parent_disk or {}, "ptuuid", "partition_table_uuid", "gpt_uuid"))
    if table_id is not None and disk_id is not None and table_id != disk_id:
        reject(
            "partition_table_id_changed",
            "The sfdisk table ID differs from the block-device table ID",
            table_id=table_id,
            block_device_id=disk_id,
        )

    table_device = _text(_mapping_value(table, "device", "path")).strip()
    parent_path = _device_path(parent_disk or {})
    if table_device and parent_path and table_device != parent_path:
        reject(
            "partition_table_device_changed",
            "The read-only partition table names a different target disk",
            table_device=table_device,
            mounted_device=parent_path,
        )
    table_sector = _parse_int(_mapping_value(table, "sectorsize", "sector_size"), minimum=1)
    if table_sector is None:
        reject("partition_table_sector_size_missing", "The GPT table logical sector size is missing")
    elif logical_sector_size is not None and table_sector != logical_sector_size:
        reject(
            "logical_sector_size_changed",
            "The partition table sector size differs from lsblk",
            table_sector_size=table_sector,
            block_device_sector_size=logical_sector_size,
        )

    rows = _partition_table_rows(table)
    if len(rows) != 3:
        reject(
            "partition_table_topology_unsupported",
            "The supported Fedora source layout has exactly ESP, /boot, and Btrfs root partitions",
            partition_count=len(rows),
        )
    for index, row in enumerate(rows, 1):
        start = _parse_int(_mapping_value(row, "start"), minimum=0)
        size = _parse_int(_mapping_value(row, "size", "sectors"), minimum=1)
        row_uuid = _uuid_text(_mapping_value(row, "uuid", "partuuid"))
        node = _text(_mapping_value(row, "node", "device", "path")).strip()
        if start is None or size is None:
            reject("malformed_partition_geometry", "A GPT partition has missing or malformed sector geometry", partition=index)
        if row_uuid is None:
            reject("malformed_partition_id", "A GPT partition ID is missing or malformed", partition=index)
        expected_node = None
        if parent_path:
            expected_node = f"{parent_path}{'p' if parent_path[-1].isdigit() else ''}{index}"
        if expected_node and node and node != expected_node:
            reject(
                "partition_device_changed",
                "The sfdisk partition node differs from the mounted target",
                partition=index,
                table_node=node,
                expected_node=expected_node,
            )

        # Cross-check every source partition ID and boundary against lsblk.
        # A changed ESP or /boot ID is just as unsafe as a changed root ID,
        # even though only the root is mounted as Btrfs.
        if node:
            matching_device = next(
                (device for device in devices if _device_path(device) == node),
                None,
            )
            if matching_device is not None:
                table_part_uuid = _uuid_text(_mapping_value(row, "uuid", "partuuid"))
                block_part_uuid = _uuid_text(_mapping_value(matching_device, "partuuid"))
                if table_part_uuid is not None and block_part_uuid is not None and table_part_uuid != block_part_uuid:
                    reject(
                        "partition_id_changed",
                        "A partition ID differs between sfdisk and lsblk",
                        partition=index,
                        table_id=table_part_uuid,
                        block_device_id=block_part_uuid,
                    )
                sector = table_sector or logical_sector_size
                block_start, block_size, _ = _boundary_bytes(matching_device, logical_sector_size)
                if sector and start is not None and block_start is not None and start * sector != block_start:
                    reject("partition_boundary_changed", "A partition start differs between sfdisk and lsblk", partition=index)
                if sector and size is not None and block_size is not None and size * sector != block_size:
                    reject("partition_boundary_changed", "A partition size differs between sfdisk and lsblk", partition=index)

    # Cross-check Fedora root's UUID and byte boundary against the table row.
    root_row: Mapping[str, Any] | None = None
    root_number = _parse_int(_mapping_value(root_partition or {}, "partn", "partition_number", "number"), minimum=1)
    if root_number is not None and root_number <= len(rows):
        root_row = rows[root_number - 1]
    elif len(rows) >= 3:
        root_row = rows[2]
    if root_row is not None and root_partition is not None:
        table_uuid = _uuid_text(_mapping_value(root_row, "uuid", "partuuid"))
        lsblk_uuid = _uuid_text(_mapping_value(root_partition, "partuuid"))
        if table_uuid is not None and lsblk_uuid is not None and table_uuid != lsblk_uuid:
            reject(
                "partition_id_changed",
                "The Fedora root partition ID differs between sfdisk and lsblk",
                table_id=table_uuid,
                block_device_id=lsblk_uuid,
            )
        sector = table_sector or logical_sector_size
        table_start = _parse_int(_mapping_value(root_row, "start"), minimum=0)
        table_size = _parse_int(_mapping_value(root_row, "size", "sectors"), minimum=1)
        root_start, root_size, _ = _boundary_bytes(root_partition, logical_sector_size)
        if sector and table_start is not None and root_start is not None and table_start * sector != root_start:
            reject("partition_boundary_changed", "The Fedora root partition start differs between sfdisk and lsblk")
        if sector and table_size is not None and root_size is not None and table_size * sector != root_size:
            reject("partition_boundary_changed", "The Fedora root partition size differs between sfdisk and lsblk")

    # Use the exact-sector planner as a second, shared validation layer.  This
    # also checks ordering, overlap, GPT types, identities, and the minimum
    # allocation.  Only call it with a valid allocation so its diagnostics do
    # not mask the clearer allocation blocker above.
    if allocation_gib >= 64:
        try:
            from . import storage

            storage.layout(dict(table), allocation_gib=allocation_gib)
        except (ImportError, ValueError, TypeError) as error:
            reject("partition_table_invalid", "The exact-sector storage planner rejected the source table", detail=_short_text(error))
    return blockers


def _ceil_sector(value: int, sector: int) -> int:
    return ((value + sector - 1) // sector) * sector


def _floor_sector(value: int, sector: int) -> int:
    return (value // sector) * sector


def _layout_identity(
    *,
    disk: Mapping[str, Any] | None,
    partition: Mapping[str, Any] | None,
    parent_disk: Mapping[str, Any] | None,
    mounts: Sequence[Mapping[str, Any]],
    btrfs: Mapping[str, Any],
    logical_sector_size: int | None,
    partition_table: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    disk = disk or {}
    partition = partition or {}
    parent_disk = parent_disk or disk
    disk_id = _disk_identity(parent_disk)
    start_bytes, size_bytes, end_bytes = _boundary_bytes(partition, logical_sector_size)
    return {
        "disk_path": _device_path(parent_disk),
        "disk_name": _text(_device_field(parent_disk, "name", "kname")) or None,
        "disk_identity": {"field": disk_id[0], "value": disk_id[1]} if disk_id else None,
        "partition_table_type": _text(_device_field(parent_disk, "pttype", "partition_table_type")).lower() or None,
        # A hand-authored inventory may carry the GPT ID only in its sfdisk
        # table; include it in the identity just as collect() does on a real
        # host.
        "partition_table_uuid": _table_uuid(
            parent_disk,
            {"partition_table": partition_table} if partition_table else {},
        ),
        "logical_sector_size": logical_sector_size,
        "source_partition": {
            "path": _device_path(partition),
            "name": _text(_device_field(partition, "name", "kname")) or None,
            "number": _parse_int(_device_field(partition, "partn", "partition_number", "number"), minimum=0),
            "uuid": _text(_device_field(partition, "uuid", "filesystem_uuid")) or None,
            "partuuid": _text(_device_field(partition, "partuuid")) or None,
            "start_bytes": start_bytes,
            "size_bytes": size_bytes,
            "end_bytes": end_bytes,
        },
        "btrfs": {
            "filesystem_uuid": _text(_btrfs_value(btrfs, "filesystem_uuid", "uuid", "fsid")) or None,
            "device_size_bytes": _btrfs_number(btrfs, "device_size_bytes", "device_size", "total_bytes", "filesystem_size"),
            "device_count": _parse_int(_btrfs_value(btrfs, "device_count", "total_devices", "num_devices"), minimum=0),
        },
        # Include the complete immutable GPT geometry and IDs when available.
        # This catches a changed non-root partition even if its mount is not
        # currently visible.  The table is omitted only for legacy fixture
        # inventories that predate the sfdisk collection field.
        "partition_table": _json_safe(partition_table) if partition_table else None,
        "mounts": [
            {
                "target": _text(_mount_field(mount, "target", "mountpoint")),
                "source": _mount_source(mount),
                "fstype": _mount_fstype(mount),
                "subvolume": _subvolume(mount),
            }
            for mount in mounts
            if _text(_mount_field(mount, "target", "mountpoint")) in {"/", "/home", "/var/home", "/boot", "/boot/efi", "/efi"}
        ],
    }


def fingerprint(inventory: Mapping[str, Any]) -> str:
    """Return the stable target fingerprint used for pre-write revalidation.

    Volatile values such as free space, battery state, collection time, and
    memory pressure are deliberately excluded.  Identity and topology fields
    are included even when incomplete, so a changed or newly discovered
    device cannot accidentally retain an old fingerprint.
    """

    devices = _devices_from_inventory(inventory)
    mounts = _mounts_from_inventory(inventory)
    root_mount = _mount_for(mounts, ("/",))
    root_partition = _find_source_device(_mount_source(root_mount), devices)
    parent_disk = _parent_disk(root_partition, devices) if root_partition else None
    if parent_disk is None:
        disks = [device for device in devices if _is_disk(device)]
        parent_disk = disks[0] if len(disks) == 1 else None
    logical = _logical_sector_size(parent_disk or {}, inventory)
    identity = _layout_identity(
        disk=parent_disk,
        partition=root_partition,
        parent_disk=parent_disk,
        mounts=mounts,
        btrfs=_btrfs_inventory(inventory),
        logical_sector_size=logical,
        partition_table=_partition_table(inventory),
    )
    payload = json.dumps(_json_safe(identity), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_layout(
    allocation_gib: int,
    *,
    inventory: Mapping[str, Any],
    parent_disk: Mapping[str, Any] | None,
    source_partition: Mapping[str, Any] | None,
    btrfs: Mapping[str, Any],
    logical_sector_size: int | None,
) -> dict[str, Any]:
    """Build the review target using the shared exact-sector planner.

    A byte-oriented fallback remains useful for hand-authored diagnostics, but
    collected inventories always carry the sfdisk table and therefore use the
    same three-partition geometry as the privileged storage worker.
    """

    table = _partition_table(inventory)
    if table:
        try:
            from . import storage

            target = storage.layout(dict(table), allocation_gib=allocation_gib)
            target = dict(target)
            target["allocation_gib"] = allocation_gib
            target["allocation_bytes"] = allocation_gib * GIB
            target["boot_gib"] = MIN_ZEUS_BOOT_GIB
            target["root_minimum_gib"] = MIN_ZEUS_ROOT_GIB
            target["home_location"] = "inside_zeus_root:/var/home"
            target["new_partitions"] = list(target.get("partitions", []))
            target["operations"] = [
                "revalidate_target_fingerprint",
                "shrink_btrfs_filesystem_live",
                "verify_btrfs_device_boundary",
                "reduce_source_partition_end_only",
                "reboot_into_fedora",
                "verify_kernel_partition_geometry",
                "create_new_esp_boot_root_partitions",
                "install_zeus_only_into_new_root",
                "add_installer_owned_boot_entry_after_success",
            ]
            target["btrfs"] = {
                "device_size_bytes": _btrfs_number(
                    btrfs,
                    "device_size_bytes",
                    "device_size",
                    "total_bytes",
                    "filesystem_size",
                ),
                "minimum_size_bytes": _btrfs_number(
                    btrfs,
                    "minimum_size_bytes",
                    "minimum_size",
                    "min_size_bytes",
                    "min_size",
                ),
            }
            return target
        except (ImportError, ValueError, TypeError):
            # The validation path emits the actionable blocker.  Continue to
            # produce a serializable review object for the UI.
            pass

    allocation_bytes = allocation_gib * GIB
    esp_bytes = MIN_ZEUS_ESP_GIB * GIB
    boot_bytes = MIN_ZEUS_BOOT_GIB * GIB
    root_bytes = MIN_ZEUS_ROOT_GIB * GIB
    source_partition = source_partition or {}
    parent_disk = parent_disk or {}
    start_bytes, size_bytes, end_bytes = _boundary_bytes(source_partition, logical_sector_size)
    actual_btrfs_size = _btrfs_number(
        btrfs,
        "device_size_bytes",
        "device_size",
        "total_bytes",
        "filesystem_size",
    )
    if actual_btrfs_size is None:
        actual_btrfs_size = size_bytes
    source_new_size = actual_btrfs_size - allocation_bytes if actual_btrfs_size is not None else None
    source_new_end = start_bytes + source_new_size if start_bytes is not None and source_new_size is not None else None
    if source_new_end is not None and logical_sector_size:
        source_new_end = _floor_sector(source_new_end, logical_sector_size)
        source_new_size = source_new_end - start_bytes if start_bytes is not None else source_new_size
    target_start = source_new_end
    partitions: list[dict[str, Any]] = []
    cursor = target_start
    for role, size, filesystem, mountpoint in (
        ("esp", esp_bytes, "vfat", "/boot/efi"),
        ("boot", boot_bytes, "ext4", "/boot"),
        ("root", max(allocation_bytes - esp_bytes - boot_bytes, root_bytes), "btrfs", "/"),
    ):
        partition = {
            "role": role,
            "filesystem": filesystem,
            "mountpoint": mountpoint,
            "size_bytes": size,
            "size_gib": round(size / GIB, 6),
            "start_bytes": cursor,
            "end_bytes": cursor + size if cursor is not None else None,
        }
        partitions.append(partition)
        cursor = cursor + size if cursor is not None else None
    source_old_size = size_bytes
    reclaimed = source_old_size - source_new_size if source_old_size is not None and source_new_size is not None else None
    free_tail = reclaimed - allocation_bytes if reclaimed is not None else None
    return {
        "allocation_gib": allocation_gib,
        "allocation_bytes": allocation_bytes,
        "source_disk": {
            "path": _device_path(parent_disk),
            "name": _text(_device_field(parent_disk, "name", "kname")) or None,
            "identity": _disk_identity(parent_disk)[1] if _disk_identity(parent_disk) else None,
            "identity_field": _disk_identity(parent_disk)[0] if _disk_identity(parent_disk) else None,
            "partition_table_type": _text(_device_field(parent_disk, "pttype", "partition_table_type")).lower() or None,
            "partition_table_uuid": _table_uuid(parent_disk, {}),
            "logical_sector_size": logical_sector_size,
        },
        "source_partition": {
            "path": _device_path(source_partition),
            "name": _text(_device_field(source_partition, "name", "kname")) or None,
            "partuuid": _text(_device_field(source_partition, "partuuid")) or None,
            "uuid": _text(_device_field(source_partition, "uuid", "filesystem_uuid")) or None,
            "start_bytes": start_bytes,
            "size_bytes": source_old_size,
            "end_bytes": end_bytes,
            "new_size_bytes": source_new_size,
            "new_end_bytes": source_new_end,
            "partition_shrink_bytes": reclaimed,
            "btrfs_device_size_bytes": actual_btrfs_size,
            "btrfs_new_device_size_bytes": source_new_size,
        },
        "new_partitions": partitions,
        "unallocated_tail_bytes": free_tail,
        "operations": [
            "revalidate_target_fingerprint",
            "shrink_btrfs_filesystem_live",
            "verify_btrfs_device_boundary",
            "reduce_source_partition_end_only",
            "reboot_into_fedora",
            "verify_kernel_partition_geometry",
            "create_new_esp_boot_root_partitions",
            "install_zeus_only_into_new_root",
            "add_installer_owned_boot_entry_after_success",
        ],
    }


def plan(inventory: Mapping[str, Any], allocation_gib: int | float = 128) -> dict[str, Any]:
    """Validate an inventory and return a fail-closed, reviewable plan.

    ``inventory`` may be the result of :func:`collect` or an equivalent JSON
    fixture using the same semantic fields.  This function only computes and
    validates values; it never invokes a command or writes a file.
    """

    if not isinstance(inventory, Mapping):
        inventory = {"errors": [{"code": "invalid_inventory", "message": "Inventory must be a mapping"}]}
    try:
        safe_inventory = _json_safe(copy.deepcopy(dict(inventory)))
    except (TypeError, ValueError, OverflowError, RecursionError, MemoryError):
        safe_inventory = {"errors": [{"code": "invalid_inventory", "message": "Inventory is not JSON-compatible"}]}
        inventory = {"errors": [{"code": "invalid_inventory", "message": "Inventory is not JSON-compatible"}]}
    blockers: list[str] = []
    details: list[dict[str, Any]] = []

    def add(code: str, message: str, **extra: Any) -> None:
        if code in blockers:
            return
        blockers.append(code)
        item: dict[str, Any] = {"code": code, "message": message}
        if extra:
            item["details"] = _json_safe(extra)
        details.append(item)

    for code, message, extra in _inventory_error_blockers(inventory):
        add(code, message, **extra)
    root_info = _mapping_value(inventory, "root", "privilege")
    if isinstance(root_info, Mapping) and _mapping_has(root_info, "is_root", "root"):
        if _parse_bool(_mapping_value(root_info, "is_root", "root")) is not True:
            add("root_required", "The preflight inventory was collected without required root privileges")
    elif root_info is not None and _parse_bool(root_info) is not True:
        add("root_required", "The preflight inventory was collected without required root privileges")

    allocation_valid = False
    allocation_value: int
    try:
        if isinstance(allocation_gib, bool):
            raise ValueError("boolean allocation")
        numeric_allocation = float(allocation_gib)
        allocation_valid = math.isfinite(numeric_allocation) and numeric_allocation.is_integer()
    except (TypeError, ValueError, OverflowError):
        numeric_allocation = 0.0
    if not allocation_valid:
        allocation_value = 0
        add("invalid_allocation", "The Zeus allocation must be a finite whole number of GiB")
    else:
        allocation_value = int(numeric_allocation)
        if allocation_value < MIN_ALLOCATION_GIB:
            add(
                "allocation_too_small",
                f"The Zeus allocation must be at least {MIN_ALLOCATION_GIB} GiB",
                minimum_gib=MIN_ALLOCATION_GIB,
            )
        if allocation_value > 2048:
            add("allocation_too_large", "The requested Zeus allocation exceeds the supported preflight limit")

    devices = _devices_from_inventory(inventory)
    mounts = _mounts_from_inventory(inventory)
    root_mount = _mount_for(mounts, ("/",))
    home_mount = _mount_for(mounts, ("/home", "/var/home"))
    boot_mount = _mount_for(mounts, ("/boot",))
    esp_mount = _mount_for(mounts, ("/boot/efi", "/efi"))
    root_partition = _find_source_device(_mount_source(root_mount), devices)
    parent_disk = _parent_disk(root_partition, devices) if root_partition else None
    disks = [device for device in devices if _is_disk(device)]
    if parent_disk is None and len(disks) == 1:
        parent_disk = disks[0]

    os_info = _normalize_os(inventory)
    os_id = _text(_mapping_value(os_info, "id", "distribution_id")).strip().lower()
    os_name = _text(_mapping_value(os_info, "name", "pretty_name", "distribution")).strip().lower()
    os_like = _text(_mapping_value(os_info, "id_like", "idlike")).strip().lower().split()
    if not os_id and not os_name:
        add("fedora_identity_unknown", "The running operating-system identity is unavailable")
    elif os_id != "fedora" and "fedora" not in os_like and not os_name.startswith("fedora"):
        add("non_fedora", "The running operating system is not identified as Fedora")
    elif os_id == "fedora" or "fedora" in os_like or os_name.startswith("fedora"):
        version_text = _text(_mapping_value(os_info, "version_id", "version", "release")).strip()
        if not version_text:
            add("fedora_version_unknown", "The supported Fedora release could not be established")
        elif not re.match(r"^43(?:[.]|$)", version_text):
            add(
                "unsupported_fedora_version",
                "This preflight supports Fedora 43 only",
                version=version_text,
            )

    firmware = _normalize_firmware(inventory)
    uefi = _parse_bool(_mapping_value(firmware, "uefi", "is_uefi"))
    if uefi is False:
        add("non_uefi", "UEFI firmware is required for the supported dual-boot path")
    elif uefi is None:
        add("uefi_state_unknown", "UEFI firmware state could not be established")

    secure_boot = _normalize_secure_boot(inventory)
    secure_boot_enabled = _parse_bool(
        _mapping_value(secure_boot, "enabled", "active", "secure_boot_enabled")
    )
    secure_boot_state = _text(
        _mapping_value(secure_boot, "state", "status", "secure_boot_state")
    ).strip().lower()
    state_enabled = True if secure_boot_state in {"enabled", "on", "1", "true"} else (
        False if secure_boot_state in {"disabled", "off", "0", "false"} else None
    )
    if (
        secure_boot_enabled is not None
        and state_enabled is not None
        and secure_boot_enabled != state_enabled
    ):
        add("secure_boot_unknown", "The UEFI SecureBoot state fields disagree")
    elif secure_boot_enabled is True or state_enabled is True:
        add(
            "secure_boot_enabled",
            "Secure Boot must be disabled for the supported Zeus boot path",
        )
    elif secure_boot_enabled is not False and state_enabled is not False:
        add("secure_boot_unknown", "The UEFI SecureBoot state could not be established")

    memory = _normalize_memory(inventory)
    total_bytes = _bytes(_mapping_value(memory, "total_bytes"))
    if total_bytes is None:
        total_gib = _gib(_mapping_value(memory, "total_gib", "total"))
        total_bytes = int(total_gib * GIB) if total_gib is not None else None
    if total_bytes is None:
        add("ram_measurement_missing", "Total and available RAM could not be measured")
    elif total_bytes < int(MIN_TOTAL_RAM_MEASURED_GIB * GIB):
        add(
            "insufficient_ram",
            (
                f"At least {MIN_TOTAL_RAM_GIB} GiB total RAM is required "
                f"({MIN_TOTAL_RAM_MEASURED_GIB:g} GiB measured after tolerance)"
            ),
            total_gib=round(total_bytes / GIB, 6),
            required_gib=MIN_TOTAL_RAM_GIB,
            measured_floor_gib=MIN_TOTAL_RAM_MEASURED_GIB,
        )
    available_bytes = _bytes(_mapping_value(memory, "available_bytes"))
    if available_bytes is None:
        available_gib = _gib(_mapping_value(memory, "available_gib", "available"))
        available_bytes = int(available_gib * GIB) if available_gib is not None else None
    if available_bytes is None:
        add("ram_measurement_missing", "Available RAM could not be measured")
    elif available_bytes < MIN_AVAILABLE_RAM_GIB * GIB:
        add(
            "insufficient_ram",
            f"At least {MIN_AVAILABLE_RAM_GIB} GiB of available RAM is required while preparing the install",
            available_gib=round(available_bytes / GIB, 6),
            required_gib=MIN_AVAILABLE_RAM_GIB,
        )
    if (
        total_bytes is not None
        and available_bytes is not None
        and available_bytes > total_bytes
    ):
        add(
            "ram_measurement_invalid",
            "Measured available RAM exceeds measured total RAM",
            total_gib=round(total_bytes / GIB, 6),
            available_gib=round(available_bytes / GIB, 6),
        )

    power = _normalize_power(inventory)
    virtualization = _normalize_virtualization(inventory)
    is_virtual = _parse_bool(_mapping_value(virtualization, "is_virtual", "virtual", "is_vm", "vm"))
    ac_online = _parse_bool(_mapping_value(power, "ac_online", "on_ac", "mains_online"))
    if ac_online is False:
        add("ac_required", "Connect AC power before staging a partition operation")
    elif ac_online is None:
        if is_virtual is not True:
            add("power_state_unknown", "AC power state could not be measured")

    staging = _normalize_staging(inventory)
    staging_free = _bytes(_mapping_value(staging, "free_bytes", "available_bytes"))
    if staging_free is None:
        staging_gib = _gib(_mapping_value(staging, "free_gib", "available_gib", "free"))
        staging_free = int(staging_gib * GIB) if staging_gib is not None else None
    if staging_free is None:
        add("staging_measurement_missing", "Free staging capacity could not be measured")
    elif staging_free < MIN_STAGING_GIB * GIB:
        add(
            "insufficient_staging_space",
            f"At least {MIN_STAGING_GIB} GiB of staging capacity is required for download and rollback",
            available_gib=round(staging_free / GIB, 6),
            required_gib=MIN_STAGING_GIB,
        )

    # Every identity component used by the write path is required.  Device
    # paths are retained as context but cannot substitute for IDs.
    identity = _disk_identity(parent_disk or {})
    table = _partition_table(inventory)
    pttype = _text(_device_field(parent_disk or {}, "pttype", "partition_table_type")).strip().lower()
    if not pttype:
        pttype = _text(_mapping_value(table, "label")).strip().lower()
    ptuuid_raw = _table_uuid(parent_disk or {}, inventory)
    ptuuid = _uuid_text(ptuuid_raw)
    logical_sector_size = _logical_sector_size(parent_disk or {}, inventory)
    if identity is None:
        add("missing_disk_identity", "The target disk has no stable serial, WWN, EUI, or equivalent identity")
    if pttype != "gpt":
        if pttype:
            add("unsupported_partition_table", "The supported layout requires a GPT partition table", partition_table_type=pttype)
        else:
            add("partition_table_identity_missing", "The target partition-table type could not be established")
    if ptuuid is None:
        if ptuuid_raw:
            add("malformed_partition_table_id", "The GPT partition-table UUID is malformed")
        else:
            add("missing_partition_table_identity", "The GPT partition-table UUID is required for revalidation")
    if logical_sector_size is None:
        add("missing_logical_sector_size", "The target disk logical sector size is required for aligned boundaries")
    elif logical_sector_size not in {512, 4096}:
        add("unsupported_logical_sector_size", "The target disk uses an unsupported logical sector size", sector_size=logical_sector_size)

    for code, message, extra in _partition_table_blockers(
        table,
        devices=devices,
        parent_disk=parent_disk,
        root_partition=root_partition,
        logical_sector_size=logical_sector_size,
        allocation_gib=allocation_value,
    ):
        add(code, message, **extra)

    if parent_disk is None or root_partition is None:
        add("partition_topology_unknown", "The Fedora root partition and its parent disk could not be identified")
    else:
        partition_id = _stable_identifier(root_partition, "partuuid", "partition_uuid", "uuid", "filesystem_uuid")
        if partition_id is None:
            add("missing_partition_identity", "The Fedora root partition has no stable PARTUUID or filesystem UUID")
        else:
            raw_partuuid = _text(_device_field(root_partition, "partuuid")).strip()
            if raw_partuuid and _uuid_text(raw_partuuid) is None:
                add("malformed_partition_id", "The Fedora root partition PARTUUID is malformed")
        start_bytes, size_bytes, end_bytes = _boundary_bytes(root_partition, logical_sector_size)
        disk_start, disk_size, disk_end = _boundary_bytes(parent_disk, logical_sector_size)
        if start_bytes is None or size_bytes is None or end_bytes is None:
            add("partition_boundary_missing", "The Fedora root partition start and size are required")
        if logical_sector_size is not None and start_bytes is not None and start_bytes % logical_sector_size:
            add("partition_boundary_unaligned", "The Fedora root partition start is not sector aligned")
        if disk_end is None:
            add("disk_boundary_missing", "The target disk boundary is required to prove the source partition is safe to shrink")
        elif end_bytes is None or end_bytes > disk_end:
            add("partition_boundary_invalid", "The Fedora root partition extends beyond the target disk")
        elif any(
            _is_partition(device)
            and device is not root_partition
            and _parent_disk(device, devices) is parent_disk
            and _boundary_bytes(device, logical_sector_size)[0] is not None
            and _boundary_bytes(device, logical_sector_size)[0] >= end_bytes
            for device in devices
        ):
            add("partition_topology_unsupported", "A partition follows Fedora's Btrfs root; end-only shrink cannot place Zeus safely")

    if root_mount is None or _mount_fstype(root_mount) != "btrfs":
        add("fedora_root_not_btrfs", "Fedora root must be a Btrfs filesystem for the supported shrink path")
    if home_mount is None or _mount_fstype(home_mount) != "btrfs":
        add("fedora_home_not_btrfs", "Fedora home must remain an identified Btrfs subvolume")
    if boot_mount is None or _mount_fstype(boot_mount) not in {"ext4", "xfs"}:
        add("fedora_boot_topology_unsupported", "Fedora must have a dedicated ext4 or XFS /boot mount")
    if esp_mount is None or _mount_fstype(esp_mount) not in {"vfat", "fat", "fat32"}:
        add("fedora_esp_topology_unsupported", "Fedora must have a dedicated vfat EFI system partition")

    boot_partition = _find_source_device(_mount_source(boot_mount), devices)
    esp_partition = _find_source_device(_mount_source(esp_mount), devices)
    if boot_mount is not None and boot_partition is None:
        add("fedora_boot_identity_missing", "The Fedora /boot mount is not tied to an identified partition")
    if esp_mount is not None and esp_partition is None:
        add("fedora_esp_identity_missing", "The Fedora EFI mount is not tied to an identified partition")
    if parent_disk is not None:
        for role, device in (("boot", boot_partition), ("esp", esp_partition)):
            if device is None:
                continue
            owner = _parent_disk(device, devices)
            if owner is not parent_disk:
                add(
                    "fedora_boot_topology_unsupported",
                    f"Fedora {role} must be on the same disk as its Btrfs root",
                    partition=role,
                )
        if boot_partition is root_partition or esp_partition is root_partition:
            add("fedora_boot_topology_unsupported", "Fedora boot partitions must be separate from its Btrfs root")
    if root_mount is not None and home_mount is not None:
        root_source = _mount_source(root_mount)
        home_source = _mount_source(home_mount)
        same_source = root_source == home_source or (
            _find_source_device(root_source, devices) is not None
            and _find_source_device(root_source, devices) is _find_source_device(home_source, devices)
        )
        if not same_source:
            add("fedora_root_home_separate_devices", "Fedora root and home must be subvolumes on the same Btrfs device")
        if _subvolume(root_mount) is None or _subvolume(home_mount) is None:
            add("subvolume_identity_missing", "Fedora root and home Btrfs subvolumes must be explicitly identified")

    btrfs = _btrfs_inventory(inventory)
    if _contains_encryption(inventory):
        add("unsupported_encryption", "Encrypted or dm-crypt storage is outside the supported preflight path")
    device_count = _parse_int(_btrfs_value(btrfs, "device_count", "total_devices", "num_devices"), minimum=0)
    btrfs_devices = _mapping_value(btrfs, "devices")
    if device_count is None and btrfs_devices is not None:
        records = [item for item in _as_list(btrfs_devices) if isinstance(item, Mapping)]
        device_count = len(records)
    if device_count is None:
        add("btrfs_topology_unknown", "Btrfs device count could not be established")
    elif device_count != 1:
        add("multi_device_btrfs", "Multi-device Btrfs layouts are outside the supported shrink path", device_count=device_count)

    actual_btrfs_size = _btrfs_number(
        btrfs,
        "device_size_bytes",
        "device_size",
        "total_bytes",
        "filesystem_size",
    )
    minimum_btrfs_size = _btrfs_number(
        btrfs,
        "minimum_size_bytes",
        "minimum_size",
        "min_size_bytes",
        "min_size",
    )
    partition_start, partition_size, partition_end = _boundary_bytes(root_partition or {}, logical_sector_size)
    if actual_btrfs_size is None or minimum_btrfs_size is None:
        add("shrink_bounds_unknown", "Measured Btrfs device and conservative minimum sizes are required before shrinking")
    else:
        if minimum_btrfs_size <= 0 or actual_btrfs_size <= 0 or minimum_btrfs_size >= actual_btrfs_size:
            add("shrink_bounds_invalid", "The measured Btrfs minimum size is not below its current device size")
        if partition_size is not None and actual_btrfs_size > partition_size:
            add("btrfs_exceeds_partition", "The measured Btrfs device boundary exceeds Fedora's partition boundary")
        if allocation_valid and allocation_value >= MIN_ALLOCATION_GIB:
            required_new_size = actual_btrfs_size - allocation_value * GIB
            if required_new_size < minimum_btrfs_size:
                add(
                    "insufficient_shrinkable_space",
                    "The measured Btrfs minimum size leaves insufficient space for the requested Zeus allocation",
                    device_size_bytes=actual_btrfs_size,
                    minimum_size_bytes=minimum_btrfs_size,
                    requested_bytes=allocation_value * GIB,
                )

    efi = _normalize_efi(inventory)
    efi_supported = _parse_bool(_mapping_value(efi, "variables_supported", "supported"))
    entries = _as_list(_mapping_value(efi, "entries", "boot_entries"))
    if efi_supported is not True or not entries:
        add("efi_state_unavailable", "UEFI boot variables and existing boot entries could not be verified")
    else:
        fedora_entry = False
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            label = _text(_mapping_value(entry, "description", "label", "name", "path")).lower()
            if "fedora" in label or "\\efi\\fedora" in label or "/efi/fedora" in label:
                fedora_entry = True
                break
        if not fedora_entry:
            add("fedora_boot_entry_missing", "An existing Fedora EFI boot entry is required to preserve Fedora's boot path")
        order = _mapping_value(efi, "boot_order", "order")
        if not order:
            add("efi_boot_order_missing", "The existing EFI boot order could not be verified")
        elif isinstance(order, str):
            order_values = {item.upper() for item in re.findall(r"[0-9A-Fa-f]{4}", order)}
        else:
            order_values = {_text(item).upper() for item in _as_list(order)}
        if order and not any(
            isinstance(entry, Mapping)
            and _text(_mapping_value(entry, "id", "number")).upper() in order_values
            and "fedora" in _text(_mapping_value(entry, "description", "label", "name", "path")).lower()
            for entry in entries
        ):
            add("fedora_boot_entry_not_ordered", "The existing Fedora EFI entry is not present in BootOrder")

    layout = _target_layout(
        allocation_value,
        inventory=inventory,
        parent_disk=parent_disk,
        source_partition=root_partition,
        btrfs=btrfs,
        logical_sector_size=logical_sector_size,
    )
    target_fingerprint = fingerprint(inventory)
    # The storage planner also reports its GPT-table digest under
    # ``table_fingerprint``.  Expose the preflight target fingerprint under
    # the unambiguous ``fingerprint`` key so the backend can validate both
    # layers without treating the table digest as a conflicting plan target.
    layout["fingerprint"] = target_fingerprint
    return {
        "schema_version": SCHEMA_VERSION,
        "supported": not blockers,
        "blockers": blockers,
        "blocker_details": details,
        "inventory": safe_inventory,
        "allocation_gib": allocation_value,
        "target": layout,
        "proposed_target_layout": layout,
        "fingerprint": target_fingerprint,
    }


__all__ = [
    "SCHEMA_VERSION",
    "MIN_TOTAL_RAM_GIB",
    "MIN_TOTAL_RAM_TOLERANCE_GIB",
    "MIN_TOTAL_RAM_MEASURED_GIB",
    "MIN_AVAILABLE_RAM_GIB",
    "MIN_STAGING_GIB",
    "MIN_ALLOCATION_GIB",
    "collect",
    "fingerprint",
    "plan",
]
