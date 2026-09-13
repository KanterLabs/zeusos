"""Safe, one-shot Fedora boot chooser shutdown support.

This module intentionally owns a very small privileged surface.  The
installer writes one immutable provenance record into ``/etc/zeus``.  The
runtime checks that record against the current UEFI variables and the
generated Fedora GRUB menu before it sets the Fedora EFI entry as ``BootNext``
and asks systemd to power off.

The public command accepts only ``status --json`` and ``poweroff --json``.
All paths and executable names below are constants; callers cannot select a
disk, mountpoint, command, or EFI entry.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1

MARKER_PATH = Path("/etc/zeus/boot-chooser.json")
LOCK_PATH = Path("/run/zeus/boot-chooser.lock")
PRIVATE_MOUNT_PATH = Path("/run/zeus/boot-chooser-fedora-boot")
FEDORA_GRUB_RELATIVE_PATH = Path("grub2/grub.cfg")
PARTUUID_LINK_DIR = Path("/dev/disk/by-partuuid")

EFIBOOTMGR_COMMAND = "/usr/sbin/efibootmgr"
FINDMNT_COMMAND = "/usr/bin/findmnt"
LSBLK_COMMAND = "/usr/bin/lsblk"
MOUNT_COMMAND = "/usr/bin/mount"
UMOUNT_COMMAND = "/usr/bin/umount"
SYSTEMCTL_COMMAND = "/usr/bin/systemctl"

MANAGED_GRUB_ENTRY_ID = "zeusos-dualboot"
FEDORA_SHIM_PATH = r"\EFI\fedora\shimx64.efi"

# Keep every subprocess bounded.  EFI output is normally only a few KiB and
# a generated GRUB config is normally well below one MiB; these limits leave
# room for a real machine without accepting unbounded attacker-controlled
# output.
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
MAX_GRUB_CONFIG_BYTES = 8 * 1024 * 1024
MARKER_MAX_BYTES = 4 * 1024
COMMAND_TIMEOUT_SECONDS = 8
POWER_TIMEOUT_SECONDS = 20

_UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})$")
_BOOT_LINE_RE = re.compile(r"^(Boot[0-9A-Fa-f]{4})(\*)?\s+(.+)$")
_HD_RE = re.compile(
    r"HD\(\s*[0-9]+\s*,\s*GPT\s*,\s*"
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
    r"\s*,"
)
_FILE_RE = re.compile(r"(?:^|/)File\(([^)]*)\)")
_BOOT_ORDER_RE = re.compile(r"^BootOrder:\s*([0-9A-Fa-f]{4}(?:\s*,\s*[0-9A-Fa-f]{4})*)\s*$")
_BOOT_NEXT_RE = re.compile(r"^BootNext:\s*(?:(?:Boot)?([0-9A-Fa-f]{4})|<none>)\s*$", re.IGNORECASE)
_BOOT_CURRENT_RE = re.compile(r"^BootCurrent:\s*[0-9A-Fa-f]{4}\s*$")
_TIMEOUT_LINE_RE = re.compile(r"^Timeout:\s*[0-9]+\s+seconds\s*$", re.IGNORECASE)
_MENUENTRY_RE = re.compile(
    r"^\s*menuentry\s+(['\"])(?P<title>.*?)\1\s+"
    r"(?:--id\s+(['\"])(?P<id>[^'\"]+)\3)?"
)
_ID_ANYWHERE_RE = re.compile(
    r"--id\s+(['\"])(?P<id>[^'\"]+)\1"
)
_SET_TIMEOUT_RE = re.compile(r"\bset\s+timeout\s*=\s*(-?[0-9]+)\b")
_SET_TIMEOUT_STYLE_RE = re.compile(r"\bset\s+timeout_style\s*=\s*menu\b", re.IGNORECASE)

MARKER_KEYS = frozenset(
    {
        "schema_version",
        "fedora_boot_entry",
        "fedora_boot_path",
        "fedora_esp_partuuid",
        "fedora_boot_partuuid",
        "grub_entry_id",
        "grub_timeout_style",
        "grub_timeout",
        "default_preserved",
    }
)


class BootChooserError(RuntimeError):
    """Stable, user-safe error for a refused or failed operation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class BootMarker:
    """The installer-owned, immutable boot chooser provenance record."""

    fedora_boot_entry: str
    fedora_boot_path: str
    fedora_esp_partuuid: str
    fedora_boot_partuuid: str
    grub_entry_id: str
    grub_timeout_style: str
    grub_timeout: int
    default_preserved: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "fedora_boot_entry": self.fedora_boot_entry,
            "fedora_boot_path": self.fedora_boot_path,
            "fedora_esp_partuuid": self.fedora_esp_partuuid,
            "fedora_boot_partuuid": self.fedora_boot_partuuid,
            "grub_entry_id": self.grub_entry_id,
            "grub_timeout_style": self.grub_timeout_style,
            "grub_timeout": self.grub_timeout,
            "default_preserved": self.default_preserved,
        }


@dataclass(frozen=True)
class EfiEntry:
    identifier: str
    active: bool
    description: str
    esp_partuuid: str | None
    path: str | None

    def signature(self) -> tuple[Any, ...]:
        return (
            self.identifier,
            self.active,
            self.description,
            self.esp_partuuid,
            self.path,
        )


@dataclass(frozen=True)
class EfiSnapshot:
    boot_order: tuple[str, ...]
    boot_next: str | None
    entries: Mapping[str, EfiEntry]

    def preservation_signature(self) -> tuple[Any, ...]:
        return (
            self.boot_order,
            tuple((key, self.entries[key].signature()) for key in sorted(self.entries)),
        )


@dataclass(frozen=True)
class MountInfo:
    target: str
    source: str
    fstype: str
    options: frozenset[str]
    partuuid: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., Any]


def _normalize_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _UUID_RE.fullmatch(value):
        return None
    return value.lower()


def _normalize_entry(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = _ENTRY_RE.fullmatch(value)
    if not match:
        return None
    return f"Boot{match.group(1).upper()}"


def _normalize_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # The installer records one known Fedora shim path.  Converting slash
    # spelling here lets the schema reject path injection while tolerating
    # firmware tools that print forward slashes.
    normalized = value.strip().replace("/", "\\")
    if normalized.casefold() != FEDORA_SHIM_PATH.casefold():
        return None
    return FEDORA_SHIM_PATH


def _normalize_listed_path(value: Any) -> str | None:
    """Normalize a non-target firmware path for preservation comparisons."""

    if not isinstance(value, str) or not value or len(value) > 1024:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in value):
        return None
    return value


def _efi_path_key(value: str | None) -> str:
    return value.replace("/", "\\").casefold() if isinstance(value, str) else ""


def _short_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _result_from_value(value: Any) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    if isinstance(value, (bytes, str)):
        output = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return CommandResult(0, stdout=output)
    if isinstance(value, (tuple, list)):
        return CommandResult(
            int(value[0]) if value else 0,
            str(value[1] if len(value) > 1 and value[1] is not None else ""),
            str(value[2] if len(value) > 2 and value[2] is not None else ""),
        )
    if isinstance(value, Mapping):
        return CommandResult(
            int(value.get("returncode", value.get("code", 0))),
            str(value.get("stdout", value.get("output", "")) or ""),
            str(value.get("stderr", value.get("error", "")) or ""),
        )
    return CommandResult(
        int(getattr(value, "returncode", 0)),
        str(getattr(value, "stdout", "") or ""),
        str(getattr(value, "stderr", "") or ""),
    )


def _bounded_result(result: CommandResult) -> CommandResult:
    if len(result.stdout.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
        raise BootChooserError("output_too_large", "A required system query returned too much data")
    if len(result.stderr.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
        raise BootChooserError("output_too_large", "A required system query returned too much data")
    return result


def _run(arguments: Sequence[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    """Run a fixed argv vector with bounded capture and no shell."""

    try:
        result = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise BootChooserError("command_timeout", "A required system query timed out") from error
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise BootChooserError("command_unavailable", "A required system command could not run") from error
    return _bounded_result(_result_from_value(result))


def _execute(arguments: Sequence[str], timeout: int, runner: Runner | None) -> CommandResult:
    """Execute a command, accepting compact fixture runners in tests."""

    if runner is None:
        return _run(arguments, timeout)
    args = list(arguments)
    try:
        try:
            value = runner(args, timeout)
        except TypeError:
            value = runner(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
    except subprocess.TimeoutExpired as error:
        raise BootChooserError("command_timeout", "A required system query timed out") from error
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise BootChooserError("command_unavailable", "A required system command could not run") from error
    try:
        return _bounded_result(_result_from_value(value))
    except (TypeError, ValueError) as error:
        raise BootChooserError("invalid_command_result", "A required system command returned invalid data") from error


def _duplicate_pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or non-string JSON key")
        result[key] = value
    return result


def _safe_parent_chain(path: Path, *, require_root: bool, label: str) -> None:
    """Require a directory chain that cannot be replaced by an untrusted user."""

    if not path.is_absolute():
        raise BootChooserError("unsafe_path", f"The {label} path must be absolute")
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise BootChooserError("unsafe_path", f"The {label} directory is missing") from error
        except OSError as error:
            raise BootChooserError("unsafe_path", f"The {label} directory cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BootChooserError("unsafe_path", f"The {label} parent is not a directory")
        if require_root and metadata.st_uid != 0:
            raise BootChooserError("unsafe_path_owner", f"The {label} parent is not root-owned")
        # A world/group writable parent is unsafe.  For fixture paths under a
        # shared temporary directory, only the direct parent is checked when
        # root ownership checks are disabled.
        if metadata.st_mode & 0o022 and (require_root or current == path):
            raise BootChooserError("unsafe_path_mode", f"The {label} parent is writable")
        if current == current.parent:
            return
        current = current.parent


def _safe_marker_file(path: Path, *, require_root: bool) -> None:
    _safe_parent_chain(path.parent, require_root=require_root, label="marker")
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise BootChooserError("marker_missing", "The boot chooser record is missing") from error
    except OSError as error:
        raise BootChooserError("marker_unreadable", "The boot chooser record cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BootChooserError("marker_unsafe", "The boot chooser record is not a regular file")
    if require_root and metadata.st_uid != 0:
        raise BootChooserError("marker_owner", "The boot chooser record is not root-owned")
    if metadata.st_mode & 0o022 or metadata.st_mode & 0o111:
        raise BootChooserError("marker_mode", "The boot chooser record has unsafe permissions")


def _read_marker_bytes(path: Path) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as error:
        raise BootChooserError("marker_unreadable", "The boot chooser record cannot be read") from error
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BootChooserError("marker_unsafe", "The boot chooser record is not a regular file")
        content = bytearray()
        while len(content) <= MARKER_MAX_BYTES:
            chunk = os.read(descriptor, min(4096, MARKER_MAX_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MARKER_MAX_BYTES:
            raise BootChooserError("marker_too_large", "The boot chooser record is too large")
        return bytes(content)
    except BootChooserError:
        raise
    except OSError as error:
        raise BootChooserError("marker_unreadable", "The boot chooser record cannot be read") from error
    finally:
        os.close(descriptor)


def _marker_from_payload(payload: Any) -> BootMarker:
    if not isinstance(payload, dict) or set(payload) != MARKER_KEYS:
        raise BootChooserError("marker_schema", "The boot chooser record has an unsupported schema")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise BootChooserError("marker_schema", "The boot chooser record has an unsupported schema")
    raw_entry = payload.get("fedora_boot_entry")
    raw_path = payload.get("fedora_boot_path")
    raw_esp = payload.get("fedora_esp_partuuid")
    raw_boot = payload.get("fedora_boot_partuuid")
    entry = _normalize_entry(raw_entry)
    path = _normalize_path(raw_path)
    esp = _normalize_uuid(raw_esp)
    boot = _normalize_uuid(raw_boot)
    if (
        entry is None
        or path is None
        or esp is None
        or boot is None
        or raw_entry != entry
        or raw_path != FEDORA_SHIM_PATH
        or raw_esp != esp
        or raw_boot != boot
        or esp == boot
    ):
        raise BootChooserError("marker_identity", "The boot chooser record has an invalid identity")
    if payload.get("grub_entry_id") != MANAGED_GRUB_ENTRY_ID:
        raise BootChooserError("marker_identity", "The boot chooser record has an invalid menu entry")
    if payload.get("grub_timeout_style") != "menu":
        raise BootChooserError("marker_menu", "The boot chooser record does not require a visible menu")
    timeout = payload.get("grub_timeout")
    if type(timeout) is not int or not 0 < timeout <= 600:
        raise BootChooserError("marker_menu", "The boot chooser record has an invalid menu timeout")
    if payload.get("default_preserved") is not True:
        raise BootChooserError("marker_default", "The boot chooser record does not preserve the default")
    return BootMarker(entry, path, esp, boot, MANAGED_GRUB_ENTRY_ID, "menu", timeout, True)


def load_marker(path: Path | None = None, *, require_root: bool = True) -> BootMarker:
    """Load and strictly validate the immutable installer record."""

    path = MARKER_PATH if path is None else Path(path)
    _safe_marker_file(path, require_root=require_root)
    raw = _read_marker_bytes(path)
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise BootChooserError("marker_json", "The boot chooser record is invalid JSON") from error
    return _marker_from_payload(payload)


def build_marker(
    *,
    fedora_boot_entry: str,
    fedora_boot_partuuid: str,
    fedora_esp_partuuid: str,
    grub_timeout: int,
    fedora_boot_path: str = FEDORA_SHIM_PATH,
) -> dict[str, Any]:
    """Build the exact marker shape from already-qualified installer evidence."""

    marker = _marker_from_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "fedora_boot_entry": fedora_boot_entry,
            "fedora_boot_path": fedora_boot_path,
            "fedora_esp_partuuid": fedora_esp_partuuid,
            "fedora_boot_partuuid": fedora_boot_partuuid,
            "grub_entry_id": MANAGED_GRUB_ENTRY_ID,
            "grub_timeout_style": "menu",
            "grub_timeout": grub_timeout,
            "default_preserved": True,
        }
    )
    return marker.as_dict()


def _parse_efi_entry(line: str) -> EfiEntry:
    match = _BOOT_LINE_RE.fullmatch(line)
    if not match:
        raise BootChooserError("efi_parse", "The EFI entry listing is malformed")
    identifier = _normalize_entry(match.group(1))
    if identifier is None:
        raise BootChooserError("efi_parse", "The EFI entry identifier is malformed")
    rest = match.group(3)
    hd_matches = list(_HD_RE.finditer(rest))
    file_matches = list(_FILE_RE.finditer(rest))
    if len(hd_matches) > 1 or len(file_matches) > 1:
        raise BootChooserError("efi_identity", "The EFI entry identity is ambiguous")
    esp_partuuid: str | None = None
    path: str | None = None
    starts = [match.start() for match in (hd_matches + file_matches)]
    identity_start = min(starts) if starts else len(rest)
    if hd_matches:
        esp_partuuid = _normalize_uuid(hd_matches[0].group(1))
        if esp_partuuid is None:
            raise BootChooserError("efi_identity", "The EFI entry identity is unsupported")
    if file_matches:
        path = _normalize_listed_path(file_matches[0].group(1))
        if path is None:
            raise BootChooserError("efi_identity", "The EFI entry identity is unsupported")
    description = rest[:identity_start].strip()
    if not description or any(ord(character) < 0x20 for character in description):
        raise BootChooserError("efi_parse", "The EFI entry description is malformed")
    return EfiEntry(identifier, bool(match.group(2)), description, esp_partuuid, path)


def parse_efibootmgr(text: str) -> EfiSnapshot:
    """Parse only the bounded, verbose fields needed for identity checks."""

    if not isinstance(text, str) or len(text.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
        raise BootChooserError("efi_output", "The EFI status output is too large")
    boot_order: tuple[str, ...] | None = None
    boot_next: str | None = None
    saw_boot_next = False
    entries: dict[str, EfiEntry] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("BootOrder:"):
            if boot_order is not None:
                raise BootChooserError("efi_parse", "The EFI boot order was repeated")
            order_match = _BOOT_ORDER_RE.fullmatch(line)
            if order_match is None:
                raise BootChooserError("efi_parse", "The EFI boot order is malformed")
            order = tuple(f"Boot{item.upper()}" for item in re.findall(r"[0-9A-Fa-f]{4}", order_match.group(1)))
            if len(set(order)) != len(order):
                raise BootChooserError("efi_duplicate", "The EFI boot order contains a duplicate entry")
            boot_order = order
            continue
        if line.startswith("BootNext:"):
            if saw_boot_next:
                raise BootChooserError("efi_parse", "The EFI next boot value was repeated")
            next_match = _BOOT_NEXT_RE.fullmatch(line)
            if next_match is None:
                raise BootChooserError("efi_parse", "The EFI next boot value is malformed")
            saw_boot_next = True
            boot_next = f"Boot{next_match.group(1).upper()}" if next_match.group(1) else None
            continue
        if line.startswith("BootCurrent:"):
            if _BOOT_CURRENT_RE.fullmatch(line) is None:
                raise BootChooserError("efi_parse", "The EFI current boot value is malformed")
            continue
        if line.startswith("Timeout:"):
            if _TIMEOUT_LINE_RE.fullmatch(line) is None:
                raise BootChooserError("efi_parse", "The EFI timeout value is malformed")
            continue
        if line.startswith("Boot"):
            entry = _parse_efi_entry(line)
            if entry.identifier in entries:
                raise BootChooserError("efi_duplicate", "The EFI entry identifier was repeated")
            entries[entry.identifier] = entry
            continue
        # efibootmgr can print informational diagnostics on stderr, but any
        # unrecognised stdout line makes identity matching unsafe.
        raise BootChooserError("efi_parse", "The EFI status output is malformed")
    if boot_order is None or not boot_order or not entries:
        raise BootChooserError("efi_parse", "The EFI status output is incomplete")
    if any(identifier not in entries for identifier in boot_order):
        raise BootChooserError("efi_identity", "The EFI boot order references a missing entry")
    if boot_next is not None and boot_next not in entries:
        raise BootChooserError("efi_identity", "The EFI next boot value references a missing entry")
    return EfiSnapshot(boot_order, boot_next, dict(entries))


def query_efi(*, runner: Runner | None = None) -> EfiSnapshot:
    result = _execute((EFIBOOTMGR_COMMAND, "-v"), COMMAND_TIMEOUT_SECONDS, runner)
    if result.returncode != 0:
        raise BootChooserError("efi_unavailable", "UEFI boot variables are unavailable")
    return parse_efibootmgr(result.stdout)


def _validate_live_identity(marker: BootMarker, snapshot: EfiSnapshot) -> EfiEntry:
    target = snapshot.entries.get(marker.fedora_boot_entry)
    if target is None:
        raise BootChooserError("efi_entry_missing", "The recorded Fedora EFI entry is missing")
    if not target.active:
        raise BootChooserError("efi_entry_inactive", "The recorded Fedora EFI entry is inactive")
    if target.esp_partuuid != marker.fedora_esp_partuuid:
        raise BootChooserError("efi_esp_changed", "The recorded Fedora EFI partition changed")
    if target.path is None or _efi_path_key(target.path) != _efi_path_key(marker.fedora_boot_path):
        raise BootChooserError("efi_path_changed", "The recorded Fedora EFI path changed")
    path_matches = [
        entry
        for entry in snapshot.entries.values()
        if entry.path is not None and _efi_path_key(entry.path) == _efi_path_key(marker.fedora_boot_path)
    ]
    if len(path_matches) != 1:
        raise BootChooserError("efi_duplicate_path", "The Fedora EFI path is not unique")
    identity_matches = [
        entry
        for entry in snapshot.entries.values()
        if entry.path is not None and _efi_path_key(entry.path) == _efi_path_key(marker.fedora_boot_path)
        and entry.esp_partuuid == marker.fedora_esp_partuuid
    ]
    if len(identity_matches) != 1:
        raise BootChooserError("efi_duplicate_identity", "The Fedora EFI identity is not unique")
    if marker.fedora_boot_entry not in snapshot.boot_order:
        raise BootChooserError("efi_entry_not_ordered", "The recorded Fedora EFI entry is not in BootOrder")
    return target


def _json_object(text: str, *, error_code: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BootChooserError(error_code, "A required system query returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise BootChooserError(error_code, "A required system query returned invalid JSON")
    return payload


def _mapping_value(mapping: Mapping[str, Any], name: str) -> Any:
    if name in mapping:
        return mapping[name]
    lower = name.lower()
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() == lower:
            return value
    return None


def _ensure_directory(
    path: Path,
    *,
    require_root: bool,
    label: str,
    _direct: bool = True,
) -> None:
    """Create one fixed runtime directory without following symlinks."""

    if not path.is_absolute():
        raise BootChooserError("unsafe_path", f"The {label} path must be absolute")
    if path == Path("/"):
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _ensure_directory(path.parent, require_root=require_root, label=label, _direct=False)
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            pass
        except OSError as error:
            raise BootChooserError("unsafe_path", f"The {label} directory could not be created") from error
        try:
            metadata = path.lstat()
        except OSError as error:
            raise BootChooserError("unsafe_path", f"The {label} directory cannot be inspected") from error
    except OSError as error:
        raise BootChooserError("unsafe_path", f"The {label} directory cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BootChooserError("unsafe_path", f"The {label} parent is not a directory")
    if require_root and metadata.st_uid != 0:
        raise BootChooserError("unsafe_path_owner", f"The {label} parent is not root-owned")
    if metadata.st_mode & 0o022 and (_direct or require_root):
        raise BootChooserError("unsafe_path_mode", f"The {label} parent is writable")
    if _direct or not (metadata.st_mode & 0o755):
        try:
            os.chmod(path, 0o755)
        except OSError as error:
            raise BootChooserError("unsafe_path", f"The {label} directory permissions could not be verified") from error


def _find_mount(
    path: Path,
    *,
    runner: Runner | None,
    allow_missing: bool,
) -> MountInfo | None:
    result = _execute(
        (
            FINDMNT_COMMAND,
            "--json",
            "--mountpoint",
            str(path),
            "--output",
            "TARGET,SOURCE,FSTYPE,OPTIONS,PARTUUID",
        ),
        COMMAND_TIMEOUT_SECONDS,
        runner,
    )
    if result.returncode != 0:
        if allow_missing and not result.stdout.strip():
            return None
        raise BootChooserError("mount_probe", "The Fedora /boot mount could not be inspected")
    payload = _json_object(result.stdout, error_code="mount_probe")
    records = payload.get("filesystems")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping):
        raise BootChooserError("mount_probe", "The Fedora /boot mount identity is ambiguous")
    record = records[0]
    target = _short_text(_mapping_value(record, "target"), 256)
    source = _short_text(_mapping_value(record, "source"), 512)
    fstype = _short_text(_mapping_value(record, "fstype"), 64).lower()
    option_text = _mapping_value(record, "options")
    partuuid = _normalize_uuid(_mapping_value(record, "partuuid"))
    if (
        not target
        or target != str(path)
        or not source
        or not fstype
        or not isinstance(option_text, str)
        or partuuid is None
    ):
        raise BootChooserError("mount_probe", "The Fedora /boot mount identity is incomplete")
    options = frozenset(item.strip().lower() for item in option_text.split(",") if item.strip())
    return MountInfo(target, source, fstype, options, partuuid)


def _ensure_mountpoint(path: Path, *, require_root: bool) -> None:
    _ensure_directory(path.parent, require_root=require_root, label="private mount")
    _safe_parent_chain(path.parent, require_root=require_root, label="private mount")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o755)
        except OSError as error:
            raise BootChooserError("mountpoint", "The private mountpoint could not be created") from error
        metadata = path.lstat()
    except OSError as error:
        raise BootChooserError("mountpoint", "The private mountpoint cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BootChooserError("mountpoint", "The private mountpoint is unsafe")
    if require_root and metadata.st_uid != 0:
        raise BootChooserError("mountpoint_owner", "The private mountpoint is not root-owned")
    if metadata.st_mode & 0o022:
        raise BootChooserError("mountpoint_mode", "The private mountpoint is writable")


def _validate_partuuid_source(
    partuuid: str,
    *,
    runner: Runner | None,
    require_root: bool,
    link_dir: Path | None = None,
) -> Path:
    if _normalize_uuid(partuuid) != partuuid:
        raise BootChooserError("block_identity", "The Fedora /boot PARTUUID is invalid")
    link_dir = PARTUUID_LINK_DIR if link_dir is None else Path(link_dir)
    _safe_parent_chain(link_dir.parent, require_root=require_root, label="PARTUUID")
    source = link_dir / partuuid
    try:
        parent_metadata = link_dir.lstat()
        metadata = source.lstat()
    except (FileNotFoundError, OSError) as error:
        raise BootChooserError("block_identity", "The Fedora /boot block identity is unavailable") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise BootChooserError("block_identity", "The PARTUUID directory is unsafe")
    if parent_metadata.st_mode & 0o022:
        raise BootChooserError("block_identity", "The PARTUUID directory is writable")
    if require_root and (parent_metadata.st_uid != 0 or metadata.st_uid != 0):
        raise BootChooserError("block_identity", "The Fedora /boot block identity is not root-owned")
    if not stat.S_ISLNK(metadata.st_mode):
        raise BootChooserError("block_identity", "The PARTUUID identity is not a symlink")
    try:
        resolved = Path(os.path.realpath(source))
        resolved_metadata = resolved.stat()
    except OSError as error:
        raise BootChooserError("block_identity", "The Fedora /boot block identity is unavailable") from error
    if (
        not resolved.is_absolute()
        or not str(resolved).startswith("/dev/")
        or not stat.S_ISBLK(resolved_metadata.st_mode)
    ):
        raise BootChooserError("block_identity", "The Fedora /boot identity is not a block partition")

    result = _execute(
        (
            LSBLK_COMMAND,
            "--json",
            "--bytes",
            "--output",
            "PATH,TYPE,PARTUUID,MOUNTPOINTS",
            str(source),
        ),
        COMMAND_TIMEOUT_SECONDS,
        runner,
    )
    if result.returncode != 0:
        raise BootChooserError("block_identity", "The Fedora /boot partition could not be queried")
    payload = _json_object(result.stdout, error_code="block_identity")
    records: list[Mapping[str, Any]] = []

    def collect(value: Any) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, Mapping):
                continue
            records.append(item)
            collect(item.get("children"))

    collect(payload.get("blockdevices"))
    matches = [
        item
        for item in records
        if str(_mapping_value(item, "type") or "").lower() == "part"
        and _normalize_uuid(_mapping_value(item, "partuuid")) == partuuid
    ]
    if len(matches) != 1:
        raise BootChooserError("block_identity", "The Fedora /boot partition identity is ambiguous")
    mountpoints = _mapping_value(matches[0], "mountpoints")
    _validate_unmounted_mountpoints(mountpoints)
    observed_path = _mapping_value(matches[0], "path")
    if isinstance(observed_path, str) and os.path.realpath(observed_path) != str(resolved):
        raise BootChooserError("block_identity", "The Fedora /boot partition path changed")
    return source


def _validate_unmounted_mountpoints(mountpoints: Any) -> None:
    """Reject a Fedora /boot partition already exposed at another path."""

    if (
        not isinstance(mountpoints, list)
        or any(isinstance(item, str) and item.strip() for item in mountpoints)
        or any(item is not None and not isinstance(item, str) for item in mountpoints)
    ):
        raise BootChooserError("block_mounted", "The Fedora /boot partition is already mounted")


def _validate_mount(info: MountInfo, marker: BootMarker) -> None:
    if info.partuuid != marker.fedora_boot_partuuid:
        raise BootChooserError("mount_identity", "The mounted Fedora /boot partition changed")
    if info.fstype not in {"ext4", "xfs"}:
        raise BootChooserError("mount_fstype", "The mounted Fedora /boot filesystem is unsupported")
    required = {"ro", "nosuid", "nodev", "noexec"}
    if not required.issubset(info.options) or "rw" in info.options:
        raise BootChooserError("mount_options", "The Fedora /boot mount is not safely read-only")


def _safe_tree_file(path: Path, *, require_root: bool, max_bytes: int) -> str:
    _safe_parent_chain(path.parent, require_root=require_root, label="GRUB config")
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError) as error:
        raise BootChooserError("grub_missing", "The generated Fedora GRUB config is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BootChooserError("grub_unsafe", "The generated Fedora GRUB config is not a regular file")
    if require_root and metadata.st_uid != 0:
        raise BootChooserError("grub_owner", "The generated Fedora GRUB config is not root-owned")
    if metadata.st_mode & 0o022 or metadata.st_mode & 0o111:
        raise BootChooserError("grub_mode", "The generated Fedora GRUB config has unsafe permissions")
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootChooserError("grub_unreadable", "The generated Fedora GRUB config cannot be read") from error
    try:
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise BootChooserError("grub_too_large", "The generated Fedora GRUB config is too large")
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError as error:
            raise BootChooserError("grub_invalid", "The generated Fedora GRUB config is invalid text") from error
    except BootChooserError:
        raise
    except OSError as error:
        raise BootChooserError("grub_unreadable", "The generated Fedora GRUB config cannot be read") from error
    finally:
        os.close(descriptor)


def validate_grub_config(text: str, marker: BootMarker) -> None:
    """Prove that the mounted generated menu is visible and non-empty."""

    if len(text.encode("utf-8", errors="replace")) > MAX_GRUB_CONFIG_BYTES:
        raise BootChooserError("grub_too_large", "The generated Fedora GRUB config is too large")
    active_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    active_text = "\n".join(active_lines)
    menu_entries = [line for line in active_lines if re.match(r"^\s*menuentry\s+", line)]
    if len(menu_entries) < 2:
        raise BootChooserError("grub_menu", "The generated Fedora menu does not offer a chooser")
    managed_ids = [match.group("id") for match in (_ID_ANYWHERE_RE.search(line) for line in menu_entries) if match]
    if managed_ids.count(marker.grub_entry_id) != 1:
        raise BootChooserError("grub_entry", "The managed Zeus menu entry is missing or duplicated")
    if not _SET_TIMEOUT_STYLE_RE.search(active_text):
        raise BootChooserError("grub_menu", "The generated Fedora menu is not visibly selectable")
    timeout_values = []
    for match in _SET_TIMEOUT_RE.finditer(active_text):
        try:
            timeout_values.append(int(match.group(1)))
        except ValueError:
            continue
    if marker.grub_timeout not in timeout_values or marker.grub_timeout <= 0:
        raise BootChooserError("grub_timeout", "The generated Fedora menu timeout is not the recorded non-zero value")


@contextmanager
def _mounted_fedora_boot(
    marker: BootMarker,
    *,
    runner: Runner | None,
    mount_path: Path,
    require_root: bool,
    link_dir: Path,
) -> Iterator[None]:
    _ensure_mountpoint(mount_path, require_root=require_root)
    existing = _find_mount(mount_path, runner=runner, allow_missing=True)
    if existing is not None:
        raise BootChooserError("mountpoint_busy", "The private Fedora /boot mountpoint is already in use")
    source = _validate_partuuid_source(
        marker.fedora_boot_partuuid,
        runner=runner,
        require_root=require_root,
        link_dir=link_dir,
    )
    mounted = False
    try:
        result = _execute(
            (
                MOUNT_COMMAND,
                "--read-only",
                "--options",
                "nosuid,nodev,noexec",
                str(source),
                str(mount_path),
            ),
            COMMAND_TIMEOUT_SECONDS,
            runner,
        )
        if result.returncode != 0:
            raise BootChooserError("mount_failed", "The Fedora /boot partition could not be mounted safely")
        mounted = True
        info = _find_mount(mount_path, runner=runner, allow_missing=False)
        if info is None:
            raise BootChooserError("mount_probe", "The Fedora /boot mount disappeared")
        _validate_mount(info, marker)
        config = mount_path / FEDORA_GRUB_RELATIVE_PATH
        text = _safe_tree_file(config, require_root=require_root, max_bytes=MAX_GRUB_CONFIG_BYTES)
        validate_grub_config(text, marker)
        yield
    finally:
        if mounted:
            result = _execute((UMOUNT_COMMAND, str(mount_path)), COMMAND_TIMEOUT_SECONDS, runner)
            if result.returncode != 0:
                raise BootChooserError("unmount_failed", "The Fedora /boot partition could not be unmounted")
            if _find_mount(mount_path, runner=runner, allow_missing=True) is not None:
                raise BootChooserError("unmount_failed", "The Fedora /boot partition remained mounted")


@contextmanager
def _exclusive_lock(path: Path | None = None, *, require_root: bool = True) -> Iterator[None]:
    path = LOCK_PATH if path is None else Path(path)
    _ensure_directory(path.parent, require_root=require_root, label="operation lock")
    _safe_parent_chain(path.parent, require_root=require_root, label="operation lock")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise BootChooserError("lock", "The boot chooser lock cannot be inspected") from error
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise BootChooserError("lock", "The boot chooser lock is unsafe")
        if require_root and metadata.st_uid != 0:
            raise BootChooserError("lock", "The boot chooser lock is not root-owned")
        if metadata.st_mode & 0o022:
            raise BootChooserError("lock", "The boot chooser lock is writable")
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise BootChooserError("lock", "The boot chooser lock cannot be opened") from error
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BootChooserError("busy", "Another boot chooser operation is already running") from error
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BootChooserError("busy", "Another boot chooser operation is already running") from error
            raise BootChooserError("lock", "The boot chooser lock could not be acquired") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _base_result(
    *,
    ok: bool,
    available: bool,
    state: str,
    message: str,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "available": available,
        "state": state,
        "message": message,
    }
    if error is not None:
        result["error"] = error
    return result


def status(
    *,
    runner: Runner | None = None,
    marker_path: Path | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """Return a small read-only capability result."""

    try:
        marker = load_marker(marker_path, require_root=require_root)
        snapshot = query_efi(runner=runner)
        _validate_live_identity(marker, snapshot)
        if snapshot.boot_next is not None and snapshot.boot_next != marker.fedora_boot_entry:
            raise BootChooserError("bootnext_conflict", "Another one-shot boot request is pending")
    except BootChooserError as error:
        return _base_result(
            ok=False,
            available=False,
            state="unavailable",
            message="The boot chooser is unavailable on this installation.",
            error=error.code,
        )
    return _base_result(
        ok=True,
        available=True,
        state="available",
        message="The boot chooser is available.",
    )


def _preserved(before: EfiSnapshot, after: EfiSnapshot) -> bool:
    return before.preservation_signature() == after.preservation_signature()


def _set_boot_next(target: EfiEntry, *, runner: Runner | None) -> CommandResult:
    return _execute(
        (EFIBOOTMGR_COMMAND, "-n", target.identifier[4:]),
        COMMAND_TIMEOUT_SECONDS,
        runner,
    )


def _clear_boot_next(*, runner: Runner | None) -> CommandResult:
    return _execute((EFIBOOTMGR_COMMAND, "-N"), COMMAND_TIMEOUT_SECONDS, runner)


def _rollback_boot_next(
    before: EfiSnapshot,
    *,
    runner: Runner | None,
) -> None:
    clear_result = _clear_boot_next(runner=runner)
    if clear_result.returncode != 0:
        raise BootChooserError("rollback_failed", "The one-shot boot request could not be rolled back")
    try:
        after = query_efi(runner=runner)
    except BootChooserError as error:
        raise BootChooserError("rollback_failed", "The one-shot boot rollback could not be verified") from error
    if after.boot_next is not None or not _preserved(before, after):
        raise BootChooserError("rollback_failed", "The one-shot boot rollback changed protected EFI state")


def poweroff(
    *,
    runner: Runner | None = None,
    effective_uid: int | None = None,
    marker_path: Path | None = None,
    lock_path: Path | None = None,
    mount_path: Path | None = None,
    link_dir: Path | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """Prepare one-shot Fedora boot and request a normal system poweroff."""

    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid != 0:
        return _base_result(
            ok=False,
            available=False,
            state="error",
            message="Administrator authentication is required.",
            error="authorization_required",
        )
    marker_path = MARKER_PATH if marker_path is None else Path(marker_path)
    lock_path = LOCK_PATH if lock_path is None else Path(lock_path)
    mount_path = PRIVATE_MOUNT_PATH if mount_path is None else Path(mount_path)
    link_dir = PARTUUID_LINK_DIR if link_dir is None else Path(link_dir)
    helper_set_boot_next = False
    before: EfiSnapshot | None = None
    try:
        with _exclusive_lock(lock_path, require_root=require_root):
            marker = load_marker(marker_path, require_root=require_root)
            before = query_efi(runner=runner)
            target = _validate_live_identity(marker, before)
            if before.boot_next is not None and before.boot_next != target.identifier:
                raise BootChooserError("bootnext_conflict", "Another one-shot boot request is pending")
            with _mounted_fedora_boot(
                marker,
                runner=runner,
                mount_path=mount_path,
                require_root=require_root,
                link_dir=link_dir,
            ):
                # The GRUB file is checked inside the context.  No EFI write
                # can occur until the context has successfully unmounted it.
                pass

            refreshed_marker = load_marker(marker_path, require_root=require_root)
            if refreshed_marker != marker:
                raise BootChooserError("marker_changed", "The boot chooser record changed during validation")
            current = query_efi(runner=runner)
            current_target = _validate_live_identity(refreshed_marker, current)
            if before is None or not _preserved(before, current) or current.boot_next != before.boot_next:
                raise BootChooserError("efi_changed", "UEFI state changed during validation")
            if current.boot_next is not None and current.boot_next != current_target.identifier:
                raise BootChooserError("bootnext_conflict", "Another one-shot boot request is pending")

            # Close the marker race immediately at the write boundary.  The
            # final read is intentionally after the mount has gone away and
            # before any firmware variable mutation.
            final_marker = load_marker(marker_path, require_root=require_root)
            if final_marker != refreshed_marker:
                raise BootChooserError("marker_changed", "The boot chooser record changed during validation")
            current_target = _validate_live_identity(final_marker, current)

            set_result = _set_boot_next(current_target, runner=runner)
            set_attempted = before.boot_next != current_target.identifier
            after_set = query_efi(runner=runner)
            # Claim ownership of a value only after a readback proves that
            # this invocation actually set our target.  In particular, an
            # unexpected different BootNext must never be cleared as part of
            # a rollback.
            helper_set_boot_next = set_attempted and after_set.boot_next == current_target.identifier
            if not _preserved(before, after_set):
                raise BootChooserError("efi_preservation", "The EFI boot order or entry data changed")
            if after_set.boot_next != current_target.identifier:
                raise BootChooserError("efi_readback", "The one-shot boot request could not be verified")
            if set_result.returncode != 0:
                if helper_set_boot_next:
                    helper_set_boot_next = False
                    _rollback_boot_next(before, runner=runner)
                raise BootChooserError("efi_write", "The one-shot boot request was rejected")

            power_result = _execute(
                (SYSTEMCTL_COMMAND, "poweroff"),
                POWER_TIMEOUT_SECONDS,
                runner,
            )
            if power_result.returncode != 0:
                if helper_set_boot_next:
                    helper_set_boot_next = False
                    _rollback_boot_next(before, runner=runner)
                raise BootChooserError("poweroff_failed", "The system poweroff request failed")
            return _base_result(
                ok=True,
                available=True,
                state="poweroff_requested",
                message="Poweroff requested; the Fedora boot chooser will appear next.",
            )
    except BootChooserError as error:
        # A failure after a successful set but before the explicit rollback
        # path (for example a malformed readback) must still make a best effort
        # to clear only the value this invocation created.  If rollback itself
        # fails, expose only a stable error category.
        if helper_set_boot_next and before is not None:
            try:
                _rollback_boot_next(before, runner=runner)
            except BootChooserError:
                return _base_result(
                    ok=False,
                    available=False,
                    state="error",
                    message="The boot chooser failed and could not verify rollback.",
                    error="rollback_failed",
                )
        return _base_result(
            ok=False,
            available=False,
            state="error",
            message=error.message,
            error=error.code,
        )
    except (OSError, ValueError, TypeError) as error:
        if helper_set_boot_next and before is not None:
            try:
                _rollback_boot_next(before, runner=runner)
            except BootChooserError:
                return _base_result(
                    ok=False,
                    available=False,
                    state="error",
                    message="The boot chooser failed and could not verify rollback.",
                    error="rollback_failed",
                )
        return _base_result(
            ok=False,
            available=False,
            state="error",
            message="The boot chooser action failed safely.",
            error="operation_failed",
        )


query_status = status
poweroff_action = poweroff


VALID_OPERATIONS = frozenset({"status", "poweroff"})
VALID_ACTIONS = VALID_OPERATIONS


def admin_action(action: str = "poweroff", **kwargs: Any) -> dict[str, Any]:
    """Compatibility facade for callers that name the privileged operation."""

    if action != "poweroff":
        raise BootChooserError("invalid_request", "Use the poweroff operation.")
    return poweroff(**kwargs)


def _public_error(message: str, *, error: str = "invalid_request") -> dict[str, Any]:
    return _base_result(ok=False, available=False, state="error", message=message, error=error)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    effective_uid: int | None = None,
) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 2 or arguments[1] != "--json" or arguments[0] not in VALID_OPERATIONS:
        print(json.dumps(_public_error("Use status --json or poweroff --json."), sort_keys=True, separators=(",", ":")))
        return 2
    if arguments[0] == "status":
        result = status(runner=runner)
    else:
        result = poweroff(runner=runner, effective_uid=effective_uid)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") else 1


admin_main = main


__all__ = [
    "BootChooserError",
    "BootMarker",
    "EfiEntry",
    "EfiSnapshot",
    "EFIBOOTMGR_COMMAND",
    "FEDORA_GRUB_RELATIVE_PATH",
    "FEDORA_SHIM_PATH",
    "FINDMNT_COMMAND",
    "LOCK_PATH",
    "LSBLK_COMMAND",
    "MANAGED_GRUB_ENTRY_ID",
    "MARKER_KEYS",
    "MARKER_PATH",
    "MOUNT_COMMAND",
    "PARTUUID_LINK_DIR",
    "PRIVATE_MOUNT_PATH",
    "SCHEMA_VERSION",
    "SYSTEMCTL_COMMAND",
    "UMOUNT_COMMAND",
    "VALID_ACTIONS",
    "admin_action",
    "admin_main",
    "build_marker",
    "load_marker",
    "main",
    "parse_efibootmgr",
    "poweroff",
    "poweroff_action",
    "query_status",
    "status",
    "validate_grub_config",
]
