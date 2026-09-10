"""Fail-closed removal planning for the Fedora-launched Zeus installation.

Removal is intentionally separate from installation.  The only durable
authority for a removal plan is the root-owned installation journal.  A plan
must contain the GPT disk identity, the three GPT identities allocated to
Zeus (partitions 4--6), and the hash of the installer-owned Fedora GRUB
script.  A fresh Fedora inventory is then required before a plan is returned.

The default operation only removes the installer-owned GRUB script and asks
Fedora to regenerate its menu.  It does not touch Zeus files, filesystems, or
partitions.  Partition deletion is an explicitly confirmed operation and is
kept behind a VM-qualification marker.  This module emits fixed operation
descriptors and offers narrow executor hooks for a later qualified backend;
it never accepts a client-supplied privileged command.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import copy
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Any, Callable
import uuid

from . import bootmenu
from .backend import InstallError, Journal


SCHEMA_VERSION = 1
REMOVAL_SCHEMA_VERSION = 1

MENU_ONLY = "menu_only"
DESTRUCTIVE = "destructive"
SAFE = MENU_ONLY  # Friendly alias used by launcher integrations.

FEDORA_GRUB_SCRIPT = Path(bootmenu.SCRIPT_PATH)
FEDORA_GRUB_DEFAULTS = Path("/etc/default/grub")
FEDORA_GRUB_CONFIG = Path("/boot/grub2/grub.cfg")
GRUB2_MKCONFIG = "/usr/sbin/grub2-mkconfig"
SGDISK = "/usr/bin/sgdisk"
GRUB_NO_GRUBENV_UPDATE = "--no-grubenv-update"

_DISK_RE = re.compile(r"\A/dev/(?:nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+)\Z")
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_HASH_RE = re.compile(r"\A(?:sha256:)?[0-9a-fA-F]{64}\Z")
_PLAN_ID_RE = re.compile(r"\A[^\x00-\x20\x7f]{1,256}\Z")
_MAX_JSON = 512 * 1024


class RemovalError(InstallError):
    """An expected, safe removal failure with a stable code."""


def _error(code: str, message: str) -> RemovalError:
    return RemovalError(code, message)


def _clone(value: Any, *, limit: int = _MAX_JSON, code: str = "invalid_state") -> Any:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError) as exc:
        raise _error(code, "The removal state is not valid JSON.") from exc
    if len(raw) > limit:
        raise _error(code, "The removal state is too large.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError, MemoryError) as exc:
        raise _error(code, "The removal state is not valid JSON.") from exc


def canonical_hash(value: Any) -> str:
    """Hash JSON in the same deterministic form used by the installer."""

    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def content_hash(value: str | bytes | bytearray) -> str:
    """Return a bare SHA-256 digest for a fixed file's exact bytes."""

    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, (bytes, bytearray)):
        raise _error("invalid_file", "The managed Fedora file contents are invalid.")
    return hashlib.sha256(bytes(value)).hexdigest()


def _uuid(value: Any, *, message: str = "The recorded identity is invalid.") -> str:
    if not isinstance(value, str):
        raise _error("identity_invalid", message)
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise _error("identity_invalid", message) from exc


def _hash(value: Any, *, message: str = "The recorded file hash is invalid.") -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise _error("identity_invalid", message)
    return value.removeprefix("sha256:").lower()


def _plan_id(value: Any) -> str:
    if not isinstance(value, str) or _PLAN_ID_RE.fullmatch(value) is None:
        raise _error("journal_invalid", "The installation journal has no valid plan ID.")
    return value


def _path(value: Any, *, disk: bool = False, message: str = "The recorded path is invalid.") -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise _error("identity_invalid", message)
    if disk:
        if _DISK_RE.fullmatch(value) is None:
            raise _error("identity_invalid", message)
    elif not value.startswith("/"):
        raise _error("identity_invalid", message)
    return value


def _maps(value: Any, *, depth: int = 0, limit: int = 8) -> Iterable[Mapping[str, Any]]:
    """Yield bounded nested mappings from fixture and preflight-shaped data."""

    if depth > limit or not isinstance(value, Mapping):
        return
    yield value
    for child in value.values():
        if isinstance(child, Mapping):
            yield from _maps(child, depth=depth + 1, limit=limit)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, Mapping):
                    yield from _maps(item, depth=depth + 1, limit=limit)


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _state_candidates(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return journal, executor result, and executor state in precedence order."""

    result: list[Mapping[str, Any]] = [record]
    for key in ("executor_state", "dualboot_state"):
        value = record.get(key)
        if isinstance(value, Mapping):
            result.append(value)
    executor_result = record.get("executor_result")
    if isinstance(executor_result, Mapping):
        result.append(executor_result)
        for key in ("executor_state", "dualboot_state", "state"):
            value = executor_result.get(key)
            if isinstance(value, Mapping):
                result.append(value)
    # Preserve precedence while avoiding duplicate object references.
    unique: list[Mapping[str, Any]] = []
    for value in result:
        if not any(value is previous for previous in unique):
            unique.append(value)
    return unique


def _load_journal(source: Any, *, require_root: bool = True) -> dict[str, Any]:
    """Load a journal through the secure backend seam or accept a test record.

    A production caller should pass :class:`backend.Journal` (the default
    source does so) so its root ownership, mode, no-follow and atomic-read
    checks run.  A plain mapping is accepted for pure planner tests and for a
    backend that already performed those checks.
    """

    journal = source
    if journal is None:
        journal = Journal(require_root=require_root)
    if isinstance(journal, Mapping):
        value = dict(journal)
    else:
        load = getattr(journal, "load", None)
        if not callable(load):
            raise _error("journal_unavailable", "The root-owned installation journal is unavailable.")
        if require_root and getattr(journal, "require_root", True) is False:
            raise _error("journal_untrusted", "The installation journal is not root-owned and protected.")
        try:
            loaded = load()
        except RemovalError:
            raise
        except InstallError as exc:
            raise _error("journal_unavailable", "The root-owned installation journal is unavailable.") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _error("journal_unavailable", "The root-owned installation journal is unavailable.") from exc
        if not isinstance(loaded, Mapping):
            raise _error("journal_invalid", "The installation journal is invalid.")
        value = dict(loaded)

    if value.get("journal_trusted") is False or value.get("root_owned") is False:
        raise _error("journal_untrusted", "The installation journal is not root-owned and protected.")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        raise _error("journal_invalid", "The installation journal schema is unsupported.")
    return value


def _installed_state(record: Mapping[str, Any]) -> Mapping[str, Any]:
    phase = record.get("phase")
    state = next(
        (
            candidate
            for candidate in _state_candidates(record)[1:]
            if candidate.get("executor") == "zeus-dualboot"
        ),
        None,
    )
    if record.get("executor") == "zeus-dualboot" and isinstance(record, Mapping):
        state = record
    if state is None:
        raise _error("not_installed", "The journal does not describe a completed Zeus installation.")
    state_phase = state.get("phase")
    if phase != "installed" and state_phase != "installed":
        raise _error("not_installed", "The journal does not describe a completed Zeus installation.")
    if state.get("status") in {"in_progress", "interrupted"}:
        raise _error("interrupted", "The earlier Zeus operation requires review before removal.")
    if state.get("stage") not in (None, 2):
        raise _error("not_installed", "The journal is before the completed Zeus installation boundary.")
    return state


def _extract_table(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("label") == "gpt" and isinstance(value.get("partitions"), list):
        return value
    for key in ("partitiontable", "partition_table", "sfdisk", "table", "original_table", "original", "allocated_table"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _extract_table(nested)
            if found is not None:
                return found
    return None


def _record_table(record: Mapping[str, Any]) -> Mapping[str, Any]:
    for state in _state_candidates(record):
        for key in ("allocated_table", "current_table", "original_table", "partition_table", "partitiontable"):
            table = _extract_table(state.get(key))
            if table is not None:
                return table
        table = _extract_table(state)
        if table is not None:
            return table
    raise _error("journal_invalid", "The installation journal has no exact GPT table.")


def _record_disk(record: Mapping[str, Any]) -> tuple[str, str]:
    values: list[str] = []
    paths: list[str] = []
    for state in _state_candidates(record):
        direct_guid = _first(state, ("disk_guid", "gpt_uuid", "partition_table_uuid", "table_uuid"))
        if direct_guid is not None:
            values.append(_uuid(direct_guid, message="The recorded GPT disk identity is invalid."))
        for key in ("allocated_table", "current_table", "original_table", "partition_table", "partitiontable", "table"):
            table = _extract_table(state.get(key))
            if table is not None and table.get("id") is not None:
                values.append(_uuid(table.get("id"), message="The recorded GPT disk identity is invalid."))
        disk = state.get("disk")
        if isinstance(disk, Mapping):
            path_value = _first(disk, ("path", "device", "node"))
            guid_value = _first(disk, ("guid", "disk_guid", "gpt_uuid"))
            if path_value is not None:
                paths.append(_path(path_value, disk=True, message="The recorded target disk path is invalid."))
            if guid_value is not None:
                values.append(_uuid(guid_value, message="The recorded GPT disk identity is invalid."))
        elif disk is not None:
            paths.append(_path(disk, disk=True, message="The recorded target disk path is invalid."))
    if not values:
        try:
            table = _record_table(record)
            values.append(_uuid(table.get("id"), message="The recorded GPT disk identity is invalid."))
        except RemovalError:
            pass
    if not values:
        raise _error("disk_identity_missing", "The journal has no exact GPT disk identity.")
    if len(set(values)) != 1:
        raise _error("journal_invalid", "The recorded GPT disk identities disagree.")
    table = _record_table(record)
    table_device = table.get("device")
    if table_device is not None:
        paths.append(_path(table_device, disk=True, message="The recorded target disk path is invalid."))
    if not paths:
        raise _error("disk_identity_missing", "The journal has no fixed target disk path.")
    if len(set(paths)) != 1:
        raise _error("journal_invalid", "The recorded target disk paths disagree.")
    return paths[0], values[0]


def _partition_number(partition: Mapping[str, Any]) -> int | None:
    value = _first(partition, ("number", "partn", "partition_number"))
    if type(value) is int:
        return value
    node = _first(partition, ("node", "path", "device"))
    if isinstance(node, str):
        match = re.search(r"(?:p|)([4-6])\Z", node)
        if match:
            return int(match.group(1))
    return None


def _partition_uuid(partition: Mapping[str, Any]) -> str | None:
    value = _first(partition, ("uuid", "partuuid", "partition_uuid", "partition_guid", "gpt_uuid"))
    if value is None:
        return None
    try:
        return _uuid(value, message="A recorded Zeus partition identity is invalid.")
    except RemovalError:
        return None


def _record_partition_guids(record: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = []
    for state in _state_candidates(record):
        for key in ("new_guids", "partition_guids", "zeus_partition_guids", "new_partition_guids"):
            if state.get(key) is not None:
                candidates.append(state.get(key))
        zeus_partitions = state.get("zeus_partitions")
        if isinstance(zeus_partitions, list):
            numbered = {
                _partition_number(item): _partition_uuid(item)
                for item in zeus_partitions
                if isinstance(item, Mapping) and _partition_number(item) in (4, 5, 6)
            }
            if all(number in numbered and numbered[number] is not None for number in (4, 5, 6)):
                candidates.append([numbered[number] for number in (4, 5, 6)])
        for key in ("allocated_table", "current_table"):
            table = _extract_table(state.get(key))
            if table is None or not isinstance(table.get("partitions"), list):
                continue
            numbered = {
                _partition_number(item): _partition_uuid(item)
                for item in table["partitions"]
                if isinstance(item, Mapping) and _partition_number(item) in (4, 5, 6)
            }
            if all(number in numbered and numbered[number] is not None for number in (4, 5, 6)):
                candidates.append([numbered[number] for number in (4, 5, 6)])
    selected: list[str] | None = None
    for candidate in candidates:
        values: list[Any]
        if isinstance(candidate, Mapping):
            values = [candidate.get(str(number), candidate.get(number)) for number in (4, 5, 6)]
        elif isinstance(candidate, (list, tuple)):
            values = list(candidate)
        else:
            continue
        if len(values) != 3:
            raise _error("journal_invalid", "The journal must record exactly three Zeus partition identities.")
        try:
            normalized = [_uuid(value, message="A recorded Zeus partition identity is invalid.") for value in values]
        except RemovalError:
            raise
        if len(set(normalized)) != 3:
            raise _error("journal_invalid", "The recorded Zeus partition identities must be distinct.")
        if selected is None:
            selected = normalized
        elif selected != normalized:
            raise _error("journal_invalid", "The recorded Zeus partition identities disagree.")
    if selected is None:
        # The installation executor's durable stage state is expected to carry
        # this field.  Never infer IDs from labels or partition numbers.
        raise _error("partition_identity_missing", "The journal has no exact Zeus partition identities.")
    return selected


def _record_fedora_root(record: Mapping[str, Any], table: Mapping[str, Any]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    parts = table.get("partitions")
    if isinstance(parts, list) and len(parts) >= 3 and isinstance(parts[2], Mapping):
        source = parts[2]
        path_value = _first(source, ("node", "path", "device"))
        guid_value = _first(source, ("uuid", "partuuid", "partition_uuid"))
        if path_value is not None and guid_value is not None:
            candidates.append(
                (
                    _path(path_value, message="The recorded Fedora root path is invalid."),
                    _uuid(guid_value, message="The recorded Fedora root identity is invalid."),
                )
            )
    for state in _state_candidates(record):
        root = _first(state, ("fedora_root", "fedora_root_partition", "source_partition"))
        if isinstance(root, Mapping):
            path_value = _first(root, ("path", "node", "device"))
            guid_value = _first(root, ("partuuid", "partition_uuid", "uuid", "guid"))
            if path_value is not None and guid_value is not None:
                candidates.append(
                    (
                        _path(path_value, message="The recorded Fedora root path is invalid."),
                        _uuid(guid_value, message="The recorded Fedora root identity is invalid."),
                    )
                )
    if not candidates:
        raise _error("fedora_root_identity_missing", "The journal has no exact Fedora root identity.")
    if len(set(candidates)) != 1:
        raise _error("journal_invalid", "The recorded Fedora root identities disagree.")
    return candidates[0]


def _record_menu_hash(record: Mapping[str, Any]) -> str:
    values: list[str] = []
    for state in _state_candidates(record):
        for key in (
            "bootmenu_hash",
            "boot_menu_hash",
            "bootmenu_sha256",
            "grub_script_hash",
            "grub_script_sha256",
            "grub_entry_hash",
            "managed_script_sha256",
            "menu_entry_hash",
            "managed_grub_hash",
        ):
            value = state.get(key)
            if value is not None:
                values.append(_hash(value, message="The journal's managed GRUB hash is invalid."))
        menu = state.get("bootmenu")
        for key in ("bootmenu", "boot_menu", "grub_script", "managed_grub"):
            menu = state.get(key)
            if isinstance(menu, Mapping):
                value = _first(menu, ("hash", "sha256", "script_hash", "grub_script_hash"))
                if value is not None:
                    values.append(_hash(value, message="The journal's managed GRUB hash is invalid."))
    if not values:
        raise _error("menu_identity_missing", "The journal has no recorded Zeus GRUB script hash.")
    if len(set(values)) != 1:
        raise _error("journal_invalid", "The recorded Zeus GRUB script hashes disagree.")
    return values[0]


def _record_backup(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for state in _state_candidates(record):
        value = state.get("backup")
        if isinstance(value, Mapping):
            return value
    return None


def _trusted_backup(record: Mapping[str, Any], *, disk_guid: str, fingerprint: Any) -> dict[str, Any]:
    backup = _record_backup(record)
    if backup is None or backup.get("verified") is not True:
        raise _error("backup_untrusted", "A recorded, verified root-owned backup is required.")
    root_trusted = (
        backup.get("root_trusted") is True
        or backup.get("root_owned") is True
        or backup.get("trusted_root") is True
        or (backup.get("owner_uid") == 0 and backup.get("trusted") is True)
        or (str(backup.get("owner", "")).lower() == "root" and backup.get("trusted") is True)
    )
    if not root_trusted:
        raise _error("backup_untrusted", "The recorded backup is not explicitly trusted as root-owned.")
    target = backup.get("backup_target", backup.get("target"))
    if not isinstance(target, str) or not target.startswith("/") or "\x00" in target or len(target) > 4096:
        raise _error("backup_untrusted", "The recorded backup target is invalid.")
    for key in ("disk_guid", "gpt_uuid", "partition_table_uuid"):
        if backup.get(key) is not None and _uuid(backup[key], message="The backup GPT identity is invalid.") != disk_guid:
            raise _error("backup_target_mismatch", "The recorded backup belongs to another disk.")
    if fingerprint is not None:
        for key in ("fingerprint", "target_fingerprint", "inventory_fingerprint"):
            value = backup.get(key)
            if value is not None and _normalize_fingerprint(value) != _normalize_fingerprint(fingerprint):
                raise _error("backup_target_mismatch", "The recorded backup belongs to another target.")
    result: dict[str, Any] = {"verified": True, "root_trusted": True, "backup_target": target}
    for key in ("verified_at", "fingerprint", "target_fingerprint", "disk_guid"):
        if isinstance(backup.get(key), (str, int, float, bool)):
            result[key] = backup[key]
    return result


def _vm_tested(record: Mapping[str, Any]) -> bool:
    for state in _state_candidates(record):
        for key in (
            "vm_tested",
            "vm_qualified",
            "removal_vm_tested",
            "removal_vm_qualified",
            "destructive_removal_qualified",
        ):
            if state.get(key) is True:
                return True
        for key in ("qualification", "vm_qualification", "removal_qualification"):
            value = state.get(key)
            if isinstance(value, Mapping):
                if value.get("tested") is True or value.get("qualified") is True:
                    return True
                removal = value.get("removal")
                if isinstance(removal, Mapping) and (removal.get("tested") is True or removal.get("qualified") is True):
                    return True
    return False


def _inventory_os(inventory: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("os", "running_os", "distribution", "os_id"):
        value = inventory.get(key)
        if isinstance(value, Mapping):
            for nested in ("id", "distribution_id", "name", "pretty_name", "distribution"):
                if isinstance(value.get(nested), str):
                    values.append(value[nested].lower())
        elif isinstance(value, str):
            values.append(value.lower())
    if not values:
        raise _error("os_identity_unknown", "The running operating-system identity is unavailable.")
    if any("zeus" in value for value in values):
        raise _error("wrong_os", "Removal must be launched from Fedora, not Zeus.")
    if not any(value == "fedora" or value.startswith("fedora ") or "fedora" in value for value in values):
        raise _error("wrong_os", "Removal must be launched from Fedora, not another operating system.")
    return next(value for value in values if "fedora" in value)


def _inventory_table(inventory: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("partition_table", "partitiontable", "sfdisk", "table", "storage"):
        found = _extract_table(inventory.get(key))
        if found is not None:
            return found
    return _extract_table(inventory)


def _inventory_disk_guid(inventory: Mapping[str, Any], table: Mapping[str, Any] | None) -> str:
    values: list[str] = []
    for key in ("disk_guid", "gpt_uuid", "partition_table_uuid", "table_uuid", "ptuuid"):
        if inventory.get(key) is not None:
            values.append(_uuid(inventory[key], message="The current GPT disk identity is invalid."))
    disk = inventory.get("disk")
    if isinstance(disk, Mapping):
        value = _first(disk, ("guid", "disk_guid", "gpt_uuid", "partition_table_uuid", "ptuuid"))
        if value is not None:
            values.append(_uuid(value, message="The current GPT disk identity is invalid."))
    if table is not None and table.get("id") is not None:
        values.append(_uuid(table["id"], message="The current GPT disk identity is invalid."))
    if not values:
        raise _error("disk_identity_unknown", "The current GPT disk identity could not be established.")
    if len(set(values)) != 1:
        raise _error("disk_identity_changed", "The current GPT disk identities disagree.")
    return values[0]


def _inventory_partitions(inventory: Mapping[str, Any], table: Mapping[str, Any] | None) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    candidates: list[Any] = []
    if table is not None:
        candidates.append(table.get("partitions"))
    for key in ("partitions", "partition_list", "zeus_partitions", "new_partitions"):
        candidates.append(inventory.get(key))
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, Mapping):
                number = _partition_number(item)
                if number is not None:
                    result[number] = item
    direct = inventory.get("partition_guids")
    if isinstance(direct, Mapping):
        for number in (4, 5, 6):
            value = direct.get(str(number), direct.get(number))
            if value is not None:
                result[number] = {"number": number, "uuid": value}
    elif isinstance(direct, (list, tuple)) and len(direct) == 3:
        for number, value in zip((4, 5, 6), direct):
            result[number] = {"number": number, "uuid": value}
    return result


def _inventory_root(inventory: Mapping[str, Any]) -> tuple[str, str]:
    root_candidates: list[Mapping[str, Any]] = []
    for key in ("fedora_root", "fedora_root_partition", "root_partition", "root"):
        value = inventory.get(key)
        if isinstance(value, Mapping) and _first(value, ("source", "path", "node", "device", "partuuid", "partition_uuid")) is not None:
            root_candidates.append(value)
    mounts: list[Mapping[str, Any]] = []
    for key in ("mounts", "findmnt"):
        value = inventory.get(key)
        if isinstance(value, list):
            mounts.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            mounts.extend(_maps(value))
    root_mount = next(
        (
            item
            for item in mounts
            if _first(item, ("target", "mountpoint", "path")) == "/"
        ),
        None,
    )
    if root_mount is not None:
        root_candidates.append(root_mount)
    source: str | None = None
    guid: str | None = None
    for candidate in root_candidates:
        source_value = _first(candidate, ("source", "path", "node", "device"))
        if isinstance(source_value, str) and source_value.startswith("/dev/"):
            source = source_value.split("[", 1)[0]
        value = _first(candidate, ("partuuid", "partition_uuid", "partition_guid"))
        if value is not None:
            try:
                guid = _uuid(value, message="The current Fedora root identity is invalid.")
            except RemovalError:
                raise
        if source is not None and guid is not None:
            break
    # Preflight's root mount normally gives only a device path; correlate it
    # with the lsblk-shaped device mapping to recover the PARTUUID.
    if source is not None and guid is None:
        marker = "/dev/disk/by-partuuid/"
        if source.startswith(marker):
            candidate = source[len(marker) :]
            if _UUID_RE.fullmatch(candidate):
                guid = _uuid(candidate, message="The current Fedora root identity is invalid.")
                source = next(
                    (
                        str(candidate_map.get("path"))
                        for candidate_map in _maps(inventory)
                        if candidate_map.get("partuuid") and str(candidate_map.get("partuuid")).lower() == candidate.lower()
                        and isinstance(candidate_map.get("path"), str)
                    ),
                    source,
                )
    if source is not None and guid is None:
        for candidate in _maps(inventory):
            candidate_source = _first(candidate, ("path", "node", "device", "name", "kname"))
            if candidate_source != source:
                continue
            value = _first(candidate, ("partuuid", "partition_uuid"))
            if value is not None:
                guid = _uuid(value, message="The current Fedora root identity is invalid.")
                break
    if source is None or guid is None:
        raise _error("fedora_root_identity_unknown", "The current Fedora root identity could not be established.")
    return source, guid


def _inventory_file_hash(inventory: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Return current managed-script hash and whether the script is present."""

    values: list[Any] = []
    present: bool | None = None
    for key in ("bootmenu_present", "boot_menu_present", "grub_script_present"):
        if isinstance(inventory.get(key), bool):
            present = inventory[key]
    for key in ("bootmenu", "boot_menu", "grub", "grub_script"):
        value = inventory.get(key)
        if isinstance(value, Mapping):
            if value.get("present") is False or value.get("exists") is False:
                present = False
            elif value.get("present") is True or value.get("exists") is True:
                present = True
            candidate = _first(value, ("hash", "sha256", "script_hash", "grub_script_hash", "content"))
            if candidate is not None:
                values.append(candidate)
    for key in ("bootmenu_hash", "boot_menu_hash", "bootmenu_sha256", "grub_script_hash", "grub_script_sha256", "menu_hash"):
        if inventory.get(key) is not None:
            values.append(inventory[key])
    files = inventory.get("files")
    if isinstance(files, Mapping):
        value = files.get(str(FEDORA_GRUB_SCRIPT))
        if value is not None:
            values.append(value)
    for candidate in _maps(inventory):
        path_value = _first(candidate, ("path", "name"))
        if path_value == str(FEDORA_GRUB_SCRIPT):
            if candidate.get("present") is False or candidate.get("exists") is False:
                present = False
            elif candidate.get("present") is True or candidate.get("exists") is True:
                present = True
            value = _first(candidate, ("hash", "sha256", "sha256sum", "content"))
            if value is not None:
                values.append(value)
    if values:
        normalized: list[str] = []
        for value in values:
            if isinstance(value, (bytes, bytearray)):
                normalized.append(content_hash(value))
            elif isinstance(value, str) and _HASH_RE.fullmatch(value):
                normalized.append(_hash(value))
            elif isinstance(value, str):
                normalized.append(content_hash(value))
            else:
                raise _error("menu_state_unknown", "The current Fedora GRUB script identity is invalid.")
        if len(set(normalized)) != 1:
            raise _error("menu_state_changed", "The current Fedora GRUB script identity is ambiguous.")
        return normalized[0], present is not False
    if present is False:
        return None, False
    raise _error("menu_state_unknown", "The current Fedora GRUB script identity could not be established.")


def _inventory_mounts(inventory: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for key in ("mounts", "findmnt", "zeus_mounts"):
        value = inventory.get(key)
        if isinstance(value, list):
            result.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            result.extend(_maps(value))
    return result


def _partition_is_mounted(inventory: Mapping[str, Any], number: int, guid: str, disk: str) -> bool | None:
    direct = _first(inventory, ("zeus_mounted", "mounted_zeus", "zeus_partitions_mounted"))
    if direct is True:
        return True
    if direct is False and not _inventory_mounts(inventory):
        return False
    node = f"{disk}{'p' if disk[-1].isdigit() else ''}{number}"
    current = _inventory_partitions(inventory, _inventory_table(inventory)).get(number)
    if isinstance(current, Mapping) and isinstance(current.get("mounted"), bool):
        return current["mounted"]
    mounts = _inventory_mounts(inventory)
    if not mounts:
        # A plan that deletes partitions cannot claim they are unmounted when
        # no mount inventory was collected.
        return None
    for mount in mounts:
        source = _first(mount, ("source", "device", "node", "path"))
        partuuid = _first(mount, ("partuuid", "partition_uuid", "uuid"))
        if source == node:
            return True
        if isinstance(source, str) and source.rstrip("/") == node:
            return True
        if isinstance(partuuid, str):
            try:
                if _uuid(partuuid) == guid:
                    return True
            except RemovalError:
                continue
    return False


def _fingerprint(record: Mapping[str, Any]) -> str | None:
    value = record.get("fingerprint")
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _error("journal_invalid", "The installation journal fingerprint is invalid.")
    return value


def _normalize_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return "sha256:" + value.lower()
    if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value):
        return value.lower()
    return value


def _find_record_plan_id(record: Mapping[str, Any]) -> str:
    for key in ("operation_id", "plan_id", "installation_plan_id", "installation_id"):
        if record.get(key) is not None:
            return _plan_id(record[key])
    for state in _state_candidates(record):
        for key in ("plan_id", "operation_id", "installation_plan_id", "installation_id"):
            if state.get(key) is not None:
                return _plan_id(state[key])
    raise _error("journal_invalid", "The installation journal has no exact plan ID.")


def _requested_mode(mode: Any, destructive: bool) -> str:
    if mode is None:
        return DESTRUCTIVE if destructive else MENU_ONLY
    if mode in ("safe", "menu", "menu_only", "disable_menu"):
        selected = MENU_ONLY
    elif mode in ("destructive", "delete_partitions", "remove_partitions"):
        selected = DESTRUCTIVE
    else:
        raise _error("invalid_mode", "The removal mode is invalid.")
    if destructive and selected != DESTRUCTIVE:
        raise _error("invalid_mode", "The removal mode is invalid.")
    return selected


def build_plan(
    journal: Any,
    inventory: Mapping[str, Any],
    *,
    mode: str | None = None,
    destructive: bool = False,
    confirm_plan_id: str | None = None,
    confirmed_plan_id: str | None = None,
    plan_id: str | None = None,
    current_bootmenu_hash: str | bytes | None = None,
    bootmenu_present: bool | None = None,
    cancelled: bool = False,
    require_root: bool = True,
) -> dict[str, Any]:
    """Build a bounded removal plan without running a privileged operation.

    ``journal`` may be a secure :class:`backend.Journal` or a mapping already
    loaded by one.  ``inventory`` is the fresh, read-only Fedora inventory.
    The current managed-script hash may be supplied explicitly by a backend;
    otherwise it is read from the fixed-shape inventory fields used by the
    tests and preflight adapters.
    """

    if cancelled:
        return {
            "schema_version": REMOVAL_SCHEMA_VERSION,
            "operation": "zeus_removal",
            "state": "cancelled",
            "cancelled": True,
            "mutated": False,
            "operations": [],
        }
    if not isinstance(inventory, Mapping):
        raise _error("invalid_inventory", "The current Fedora inventory is invalid.")
    if confirm_plan_id is None:
        confirm_plan_id = confirmed_plan_id
    record = _load_journal(journal, require_root=require_root)
    state = _installed_state(record)
    selected_mode = _requested_mode(mode, destructive)
    recorded_plan_id = _find_record_plan_id(record)
    if plan_id is not None and _plan_id(plan_id) != recorded_plan_id:
        raise _error("plan_id_mismatch", "The requested plan ID does not match the installation journal.")
    fingerprint = _fingerprint(record)
    disk, disk_guid = _record_disk(record)
    current_table = _inventory_table(inventory)
    current_disk_guid = _inventory_disk_guid(inventory, current_table)
    if not hmac.compare_digest(current_disk_guid, disk_guid):
        raise _error("disk_identity_changed", "The current GPT disk is not the recorded installation disk.")
    expected_root_path, expected_root_guid = _record_fedora_root(record, _record_table(record))
    _inventory_os(inventory)
    partition_guids = _record_partition_guids(record)
    current_root_path, current_root_guid = _inventory_root(inventory)
    if current_root_guid in partition_guids or current_root_path.endswith(tuple(f"p{number}" for number in (4, 5, 6))):
        raise _error("wrong_os", "The running root resolves to a Zeus partition.")
    if current_root_path != expected_root_path or not hmac.compare_digest(current_root_guid, expected_root_guid):
        raise _error("fedora_root_mismatch", "The running Fedora root is not the recorded Fedora root.")

    expected_table = _record_table(record)
    expected_parts = _inventory_partitions(inventory, current_table)
    if current_table is None and not all(number in expected_parts for number in (4, 5, 6)):
        raise _error("partition_identity_unknown", "The current GPT partition identities could not be established.")
    for number, guid in zip((4, 5, 6), partition_guids):
        current = expected_parts.get(number)
        if current is None or _partition_uuid(current) != guid:
            raise _error("partition_identity_changed", f"The current Zeus partition {number} is not journal-owned.")

    recorded_hash = _record_menu_hash(record)
    if current_bootmenu_hash is not None:
        current_hash = content_hash(current_bootmenu_hash) if isinstance(current_bootmenu_hash, (bytes, bytearray)) else _hash(current_bootmenu_hash)
        script_present = bootmenu_present is not False
    elif bootmenu_present is not None:
        current_hash, script_present = _inventory_file_hash(inventory)
        if bootmenu_present is False:
            current_hash, script_present = None, False
        else:
            script_present = True
    else:
        current_hash, script_present = _inventory_file_hash(inventory)
    if script_present and current_hash is not None and not hmac.compare_digest(current_hash, recorded_hash):
        raise _error("menu_owner_changed", "The installer-owned Fedora GRUB script was changed by its owner.")

    menu_action = "already_disabled" if not script_present else "remove_managed_script"
    operations: list[dict[str, Any]] = []
    if script_present:
        operations.extend(
            [
                {
                    "kind": "remove_file",
                    "path": str(FEDORA_GRUB_SCRIPT),
                    "expected_sha256": recorded_hash,
                },
                {
                    "kind": "regenerate_grub",
                    "argv": [GRUB2_MKCONFIG, GRUB_NO_GRUBENV_UPDATE, "-o", str(FEDORA_GRUB_CONFIG)],
                },
            ]
        )

    mounted: dict[str, bool | None] = {}
    for number, guid in zip((4, 5, 6), partition_guids):
        mounted[str(number)] = _partition_is_mounted(inventory, number, guid, disk)
    backup: dict[str, Any] | None = None
    if selected_mode == DESTRUCTIVE:
        if confirm_plan_id is None or not hmac.compare_digest(_plan_id(confirm_plan_id), recorded_plan_id):
            raise _error("confirmation_required", "Destructive removal requires exact plan ID confirmation.")
        backup = _trusted_backup(record, disk_guid=disk_guid, fingerprint=fingerprint)
        if not _vm_tested(record):
            raise _error("removal_not_qualified", "Destructive removal remains gated until the VM test is recorded.")
        if any(value is None for value in mounted.values()):
            raise _error("mount_state_unknown", "Every Zeus partition must be proven unmounted before deletion.")
        if any(value is True for value in mounted.values()):
            raise _error("zeus_mounted", "A Zeus partition is mounted; unmount it before deletion.")
        # The command deletes only the three recorded numbers after the GUID
        # checks above.  It never grows Fedora or performs an allocation.
        operations.append(
            {
                "kind": "delete_partitions",
                "argv": [
                    SGDISK,
                    "--delete",
                    "4",
                    "--delete",
                    "5",
                    "--delete",
                    "6",
                    disk,
                ],
                "disk_guid": disk_guid,
                "partition_guids": list(partition_guids),
            }
        )

    result: dict[str, Any] = {
        "schema_version": REMOVAL_SCHEMA_VERSION,
        "operation": "zeus_removal",
        "plan_id": recorded_plan_id,
        "source_operation_id": recorded_plan_id,
        "fingerprint": fingerprint,
        "mode": selected_mode,
        "destructive": selected_mode == DESTRUCTIVE,
        "disk": {"path": disk, "guid": disk_guid},
        "fedora_root": {"path": expected_root_path, "partition_guid": expected_root_guid, "os": "fedora"},
        "zeus_partitions": [
            {"number": number, "path": f"{disk}{'p' if disk[-1].isdigit() else ''}{number}", "guid": guid, "mounted": mounted[str(number)]}
            for number, guid in zip((4, 5, 6), partition_guids)
        ],
        "menu": {
            "action": menu_action,
            "script_path": str(FEDORA_GRUB_SCRIPT),
            "recorded_sha256": recorded_hash,
            "current_sha256": current_hash,
            "preserve_paths": [str(FEDORA_GRUB_DEFAULTS), str(FEDORA_GRUB_CONFIG)],
            "preserve_owner_settings": True,
            "preserve_other_entries": True,
        },
        "backup": backup,
        "vm_qualified": _vm_tested(record),
        "operations": operations,
        "reallocate_fedora": False,
        "retains_zeus_files": selected_mode == MENU_ONLY,
        "retains_zeus_partitions": selected_mode == MENU_ONLY,
        "journal_schema_version": SCHEMA_VERSION,
    }
    result["plan_digest"] = canonical_hash(result)
    return result


def validate_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a removal plan and reject injected operations or paths."""

    if not isinstance(value, Mapping):
        raise _error("invalid_plan", "The removal plan is invalid.")
    plan = _clone(dict(value), code="invalid_plan")
    if plan.get("schema_version") != REMOVAL_SCHEMA_VERSION or plan.get("operation") != "zeus_removal":
        raise _error("invalid_plan", "The removal plan schema is unsupported.")
    plan_id = _plan_id(plan.get("plan_id"))
    disk = plan.get("disk")
    if not isinstance(disk, Mapping):
        raise _error("invalid_plan", "The removal plan has no exact disk identity.")
    disk_path = _path(disk.get("path"), disk=True, message="The removal plan disk path is invalid.")
    disk_guid = _uuid(disk.get("guid"), message="The removal plan disk identity is invalid.")
    root = plan.get("fedora_root")
    if not isinstance(root, Mapping) or root.get("os") != "fedora":
        raise _error("invalid_plan", "The removal plan has no Fedora root identity.")
    _path(root.get("path"), message="The removal plan Fedora root path is invalid.")
    _uuid(root.get("partition_guid"), message="The removal plan Fedora root identity is invalid.")
    if plan.get("mode") not in (MENU_ONLY, DESTRUCTIVE) or plan.get("destructive") is not (plan.get("mode") == DESTRUCTIVE):
        raise _error("invalid_plan", "The removal plan mode is invalid.")
    if plan.get("mode") == DESTRUCTIVE:
        if plan.get("vm_qualified") is not True:
            raise _error("removal_not_qualified", "Destructive removal remains gated until the VM test is recorded.")
        backup = plan.get("backup")
        if not isinstance(backup, Mapping) or backup.get("verified") is not True or backup.get("root_trusted") is not True:
            raise _error("backup_untrusted", "A recorded, verified root-owned backup is required.")
    partitions = plan.get("zeus_partitions")
    if not isinstance(partitions, list) or len(partitions) != 3:
        raise _error("invalid_plan", "The removal plan must contain exactly three Zeus partitions.")
    guids: list[str] = []
    for expected_number, item in zip((4, 5, 6), partitions):
        if not isinstance(item, Mapping) or item.get("number") != expected_number:
            raise _error("invalid_plan", "The removal plan partition sequence is invalid.")
        if item.get("path") != f"{disk_path}{'p' if disk_path[-1].isdigit() else ''}{expected_number}":
            raise _error("invalid_plan", "The removal plan partition path is invalid.")
        guids.append(_uuid(item.get("guid"), message="The removal plan partition identity is invalid."))
    if len(set(guids)) != 3:
        raise _error("invalid_plan", "The removal plan partition identities must be distinct.")
    menu = plan.get("menu")
    if not isinstance(menu, Mapping) or menu.get("script_path") != str(FEDORA_GRUB_SCRIPT):
        raise _error("invalid_plan", "The removal plan managed file is invalid.")
    recorded_hash = _hash(menu.get("recorded_sha256"))
    if menu.get("action") not in ("remove_managed_script", "already_disabled"):
        raise _error("invalid_plan", "The removal plan menu action is invalid.")
    operations = plan.get("operations")
    if not isinstance(operations, list) or len(operations) > 3:
        raise _error("invalid_plan", "The removal plan operations are invalid.")
    allowed_kinds = {"remove_file", "regenerate_grub", "delete_partitions"}
    for operation in operations:
        if not isinstance(operation, Mapping) or operation.get("kind") not in allowed_kinds:
            raise _error("invalid_plan", "The removal plan contains an unsupported operation.")
        kind = operation["kind"]
        if kind == "remove_file":
            if operation.get("path") != str(FEDORA_GRUB_SCRIPT) or _hash(operation.get("expected_sha256")) != recorded_hash:
                raise _error("invalid_plan", "The removal plan file operation is not installer-owned.")
        elif kind == "regenerate_grub":
            if operation.get("argv") != [GRUB2_MKCONFIG, GRUB_NO_GRUBENV_UPDATE, "-o", str(FEDORA_GRUB_CONFIG)]:
                raise _error("invalid_plan", "The removal plan GRUB operation is not fixed.")
        else:
            if plan.get("mode") != DESTRUCTIVE:
                raise _error("invalid_plan", "Partition deletion is unavailable in menu-only mode.")
            if operation.get("disk_guid") != disk_guid or operation.get("partition_guids") != guids:
                raise _error("invalid_plan", "The partition deletion identities are not journal-bound.")
            if operation.get("argv") != [SGDISK, "--delete", "4", "--delete", "5", "--delete", "6", disk_path]:
                raise _error("invalid_plan", "The partition deletion operation is not fixed.")
    supplied_digest = plan.pop("plan_digest", None)
    if supplied_digest is not None:
        if not isinstance(supplied_digest, str) or not hmac.compare_digest(supplied_digest, canonical_hash(plan)):
            raise _error("invalid_plan", "The removal plan digest does not match its contents.")
    plan["plan_digest"] = canonical_hash(plan)
    return plan


def fixed_operations(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return detached fixed operations for a validated review or executor."""

    plan = validate_plan(value)
    return copy.deepcopy(plan["operations"])


def remove(
    journal: Any,
    inventory: Mapping[str, Any],
    *,
    mode: str | None = None,
    destructive: bool = False,
    confirm_plan_id: str | None = None,
    confirmed_plan_id: str | None = None,
    executor: "RemovalExecutor" | None = None,
    apply: bool = False,
    cancelled: bool = False,
    require_root: bool = True,
) -> dict[str, Any]:
    """Plan removal and optionally hand it to a qualified fixed executor.

    The default is a review-only plan.  Passing ``apply=True`` is required to
    cross the executor boundary; the executor itself remains unqualified until
    a separately reviewed VM qualification is supplied.
    """

    effective_confirm = confirm_plan_id if confirm_plan_id is not None else confirmed_plan_id
    checked = build_plan(
        journal,
        inventory,
        mode=mode,
        destructive=destructive,
        confirm_plan_id=effective_confirm,
        cancelled=cancelled,
        require_root=require_root,
    )
    if cancelled:
        return checked
    if executor is None or not apply:
        return checked
    return executor.execute(
        checked,
        journal=journal,
        inventory=inventory,
        confirm_plan_id=effective_confirm,
        dry_run=False,
    )


preview = build_plan


class RemovalExecutor:
    """Narrow hook for a future qualified Fedora removal backend.

    Construction is unqualified by default.  ``dry_run=True`` (the default)
    only returns the fixed operations.  A real executor must be explicitly
    qualified and receive a fresh plan with the VM qualification marker; the
    only accepted command arrays are those emitted by :func:`build_plan`.
    """

    qualified = False

    def __init__(
        self,
        *,
        qualified: bool = False,
        reader: Callable[[Path], str | bytes | None] | None = None,
        remove_file: Callable[[Path], Any] | None = None,
        runner: Any | None = None,
        vm_qualified: bool = False,
        require_root: bool = True,
    ):
        self.qualified = bool(qualified)
        self.reader = reader
        self.remove_file = remove_file
        self.runner = runner
        # This flag is only an additional test seam; the journal marker is
        # still mandatory for destructive operations.
        self.vm_qualified = bool(vm_qualified)
        self.require_root = bool(require_root)
        self._completed_plan_ids: set[str] = set()

    def execute(
        self,
        plan: Mapping[str, Any],
        *,
        journal: Any | None = None,
        inventory: Mapping[str, Any] | None = None,
        confirm_plan_id: str | None = None,
        cancelled: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if cancelled:
            return {
                "schema_version": REMOVAL_SCHEMA_VERSION,
                "ok": True,
                "state": "cancelled",
                "cancelled": True,
                "mutated": False,
                "operations": [],
            }
        checked = validate_plan(plan)
        if not dry_run and self.qualified is not True:
            raise _error("removal_unavailable", "The removal executor is not qualified; no change was attempted.")
        if checked["mode"] == DESTRUCTIVE and not self.vm_qualified and not checked.get("vm_qualified"):
            raise _error("removal_not_qualified", "Destructive removal remains gated until the VM test is recorded.")
        operations = fixed_operations(checked)
        if dry_run:
            return {
                "schema_version": REMOVAL_SCHEMA_VERSION,
                "ok": True,
                "state": "planned",
                "mode": checked["mode"],
                "mutated": False,
                "operations": operations,
                "plan_id": checked["plan_id"],
            }
        if journal is None:
            raise _error("journal_unavailable", "The root-owned installation journal is required before mutation.")
        bound_record = _load_journal(journal, require_root=self.require_root)
        _installed_state(bound_record)
        if _find_record_plan_id(bound_record) != checked["plan_id"]:
            raise _error("plan_id_mismatch", "The removal plan is not bound to the installation journal.")
        disk, disk_guid = _record_disk(bound_record)
        if checked["disk"]["path"] != disk or checked["disk"]["guid"] != disk_guid:
            raise _error("target_mismatch", "The removal plan target changed after review.")
        if _record_partition_guids(bound_record) != [part["guid"] for part in checked["zeus_partitions"]]:
            raise _error("target_mismatch", "The removal plan partition identities changed after review.")
        if _record_menu_hash(bound_record) != checked["menu"]["recorded_sha256"]:
            raise _error("target_mismatch", "The removal plan managed-file identity changed after review.")
        previous = bound_record.get("removal")
        if isinstance(previous, Mapping) and previous.get("plan_id") == checked["plan_id"]:
            previous_mode = previous.get("mode")
            previous_phase = previous.get("phase")
            if previous_mode == checked["mode"] and previous_phase in {"menu_disabled", "partitions_removed"}:
                return {
                    "schema_version": REMOVAL_SCHEMA_VERSION,
                    "ok": True,
                    "state": previous_phase,
                    "mode": previous_mode,
                    "mutated": False,
                    "completed": [],
                    "operations": [],
                    "plan_id": checked["plan_id"],
                    "idempotent": True,
                }
            if previous.get("status") == "in_progress":
                raise _error("interrupted", "An earlier removal operation may have run; review it before retrying.")
        if inventory is None:
            raise _error("target_unverified", "A fresh Fedora inventory is required before mutation.")
        live = build_plan(
            bound_record,
            inventory,
            mode=checked["mode"],
            confirm_plan_id=confirm_plan_id,
        )
        if live["plan_digest"] != checked["plan_digest"]:
            raise _error("target_mismatch", "The reviewed removal plan no longer matches the current target.")
        if checked["mode"] == DESTRUCTIVE:
            if confirm_plan_id is None or not hmac.compare_digest(_plan_id(confirm_plan_id), checked["plan_id"]):
                raise _error("confirmation_required", "Destructive removal requires exact plan ID confirmation.")
            _trusted_backup(
                bound_record,
                disk_guid=disk_guid,
                fingerprint=bound_record.get("fingerprint"),
            )
            if not _vm_tested(bound_record):
                raise _error("removal_not_qualified", "Destructive removal remains gated until the VM test is recorded.")
        if checked["plan_id"] in self._completed_plan_ids:
            return {
                "schema_version": REMOVAL_SCHEMA_VERSION,
                "ok": True,
                "state": "partitions_removed" if checked["mode"] == DESTRUCTIVE else "menu_disabled",
                "mode": checked["mode"],
                "mutated": False,
                "completed": [],
                "operations": [],
                "plan_id": checked["plan_id"],
                "idempotent": True,
            }
        if self.reader is None or self.remove_file is None or self.runner is None:
            raise _error("executor_unavailable", "A qualified fixed removal backend is unavailable.")
        if not callable(getattr(journal, "write", None)):
            raise _error("journal_unavailable", "The root-owned installation journal cannot record removal state.")
        self._write_journal_state(journal, bound_record, checked, "in_progress")
        completed: list[str] = []
        for operation in operations:
            kind = operation["kind"]
            if kind == "remove_file":
                path = FEDORA_GRUB_SCRIPT
                try:
                    current = self.reader(path)
                except FileNotFoundError:
                    current = None
                except (OSError, TypeError, ValueError) as exc:
                    raise _error("menu_state_unknown", "The managed Fedora GRUB script could not be read safely.") from exc
                if current is not None and content_hash(current) != operation["expected_sha256"]:
                    raise _error("menu_owner_changed", "The installer-owned Fedora GRUB script was changed by its owner.")
                if current is not None:
                    try:
                        self.remove_file(path)
                    except FileNotFoundError:
                        pass
                    except (OSError, TypeError, ValueError) as exc:
                        raise _error("remove_failed", "The managed Fedora GRUB script could not be removed safely.") from exc
                completed.append(kind)
            elif kind == "regenerate_grub":
                self._run_fixed(operation["argv"])
                completed.append(kind)
            elif kind == "delete_partitions":
                self._run_fixed(operation["argv"])
                completed.append(kind)
        self._completed_plan_ids.add(checked["plan_id"])
        self._write_journal_state(
            journal,
            bound_record,
            checked,
            "partitions_removed" if checked["mode"] == DESTRUCTIVE else "menu_disabled",
        )
        return {
            "schema_version": REMOVAL_SCHEMA_VERSION,
            "ok": True,
            "state": "partitions_removed" if checked["mode"] == DESTRUCTIVE else "menu_disabled",
            "mode": checked["mode"],
            "mutated": bool(completed),
            "completed": completed,
            "operations": operations,
            "plan_id": checked["plan_id"],
        }

    @staticmethod
    def _write_journal_state(
        journal: Any,
        record: Mapping[str, Any],
        plan: Mapping[str, Any],
        phase: str,
    ) -> None:
        updated = copy.deepcopy(dict(record))
        updated["removal"] = {
            "schema_version": REMOVAL_SCHEMA_VERSION,
            "plan_id": plan["plan_id"],
            "mode": plan["mode"],
            "phase": phase,
            "status": "complete" if phase in {"menu_disabled", "partitions_removed"} else "in_progress",
            "disk_guid": plan["disk"]["guid"],
            "partition_guids": [item["guid"] for item in plan["zeus_partitions"]],
            "bootmenu_hash": plan["menu"]["recorded_sha256"],
        }
        try:
            journal.write(updated)
        except InstallError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _error("state_write_failed", "The removal state could not be recorded durably.") from exc

    def _run_fixed(self, argv: Any) -> None:
        if not isinstance(argv, list):
            raise _error("invalid_plan", "The fixed command operation is invalid.")
        if argv[0] == GRUB2_MKCONFIG:
            expected = [GRUB2_MKCONFIG, GRUB_NO_GRUBENV_UPDATE, "-o", str(FEDORA_GRUB_CONFIG)]
        elif argv[0] == SGDISK:
            if len(argv) != 8 or argv[1:7] != ["--delete", "4", "--delete", "5", "--delete", "6"]:
                raise _error("invalid_plan", "The fixed partition operation is invalid.")
            expected = argv
        else:
            raise _error("command_not_allowed", "The removal command is not allowed.")
        if argv != expected:
            raise _error("command_not_allowed", "The removal command is not allowed.")
        run = getattr(self.runner, "run", None)
        if not callable(run):
            raise _error("executor_unavailable", "The fixed removal command backend is unavailable.")
        try:
            result = run(list(expected))
        except RemovalError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _error("command_failed", "The fixed removal command failed safely.") from exc
        returncode = getattr(result, "returncode", 0)
        if returncode not in (None, 0):
            raise _error("command_failed", "The fixed removal command failed safely.")

    def cancel(self, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Cancellation is a durable no-op because planning performs no writes."""

        del plan
        return {
            "schema_version": REMOVAL_SCHEMA_VERSION,
            "ok": True,
            "state": "cancelled",
            "cancelled": True,
            "mutated": False,
            "operations": [],
        }

    run = execute
    install = execute


class RemovalPlanner:
    """Small object wrapper for GUI/backend callers that prefer stateful use."""

    def __init__(
        self,
        journal: Any,
        *,
        inventory_provider: Callable[[], Mapping[str, Any]] | Mapping[str, Any] | None = None,
        require_root: bool = True,
    ):
        self.journal = journal
        self.inventory_provider = inventory_provider
        self.require_root = bool(require_root)

    def plan(self, inventory: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if inventory is None:
            provider = self.inventory_provider
            if callable(provider):
                inventory = provider()
            elif isinstance(provider, Mapping):
                inventory = provider
            else:
                raise _error("invalid_inventory", "The current Fedora inventory is invalid.")
        kwargs.setdefault("require_root", self.require_root)
        return build_plan(self.journal, inventory, **kwargs)

    build = plan


# Public aliases keep the pure planner boundary easy to discover for older
# launcher prototypes while all aliases share the same fail-closed behavior.
plan_removal = build_plan
removal_plan = build_plan
build_removal_plan = build_plan
create_removal_plan = build_plan
plan = build_plan
validate_removal_plan = validate_plan


__all__ = [
    "DESTRUCTIVE",
    "FEDORA_GRUB_CONFIG",
    "FEDORA_GRUB_DEFAULTS",
    "FEDORA_GRUB_SCRIPT",
    "GRUB2_MKCONFIG",
    "GRUB_NO_GRUBENV_UPDATE",
    "MENU_ONLY",
    "REMOVAL_SCHEMA_VERSION",
    "RemovalError",
    "RemovalExecutor",
    "RemovalPlanner",
    "SAFE",
    "SGDISK",
    "build_plan",
    "build_removal_plan",
    "canonical_hash",
    "content_hash",
    "create_removal_plan",
    "fixed_operations",
    "preview",
    "remove",
    "plan",
    "plan_removal",
    "removal_plan",
    "validate_plan",
    "validate_removal_plan",
]
