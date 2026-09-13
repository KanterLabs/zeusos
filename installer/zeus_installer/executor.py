"""Journaled two-phase Fedora/Zeus dual-boot maintenance executor.

This module is deliberately a small consumer of the preflight and backend
contracts.  It does not discover a target from a client supplied command, and
it does not make itself production qualified.  The default executor is
disabled (``qualified`` is false); a VM qualification may opt in explicitly
after the scoped EFI update worker has been supplied.

The first phase runs from Fedora.  It verifies a root-owned backup receipt,
captures the exact GPT table and stable identities, shrinks the mounted
Fedora Btrfs filesystem, proves the resulting on-disk device boundary, and
changes only partition 3's end.  It then records the expected table and boot
identity before returning ``reboot_required``.

The second phase runs after Fedora has booted again.  It proves the recorded
table and changed boot identity, appends only partitions 4--6 with recorded
UUIDs, formats those new empty partitions, installs the already verified OCI
archive into the mounted Zeus filesystem, installs EFI files only into the
mounted Zeus ESP, and writes Fedora's small chainloader entry last.

Every mutating command is preceded by an atomic journal write containing an
``in_progress`` marker.  A process interruption therefore leaves an
ambiguous boundary that the next invocation refuses to replay.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

try:  # ``crypt`` was removed from some newer Python builds.
    import crypt as _crypt
except ImportError:  # pragma: no cover - depends on the host Python build
    _crypt = None

from . import bootmenu, storage
from .backend import InstallError


SCHEMA_VERSION = 1

# The maintenance executable paths are fixed.  Values supplied by a preflight
# plan can select a disk and UUID, but cannot select a program or destination.
SFDISK = "/usr/sbin/sfdisk"
BTRFS = "/usr/sbin/btrfs"
BLKID = "/usr/sbin/blkid"
BLOCKDEV = "/usr/sbin/blockdev"
PARTX = "/usr/sbin/partx"
UDEVADM = "/usr/sbin/udevadm"
MKFS_FAT = "/usr/sbin/mkfs.fat"
MKFS_EXT4 = "/usr/sbin/mkfs.ext4"
LSBLK = "/usr/bin/lsblk"
FINDMNT = "/usr/bin/findmnt"
MOUNT = "/usr/bin/mount"
PODMAN = "/usr/bin/podman"
BOOTUPCTL = "/usr/bin/bootupctl"
GRUB2_MKCONFIG = "/usr/sbin/grub2-mkconfig"
SYSTEMD_INHIBIT = "/usr/bin/systemd-inhibit"
CAT = "/usr/bin/cat"

BACKUP_RECEIPT_PATH = Path("/etc/zeus-dualboot-backup.json")
TARGET_ROOT = Path("/target")
TARGET_BOOT = TARGET_ROOT / "boot"
TARGET_ESP = TARGET_BOOT / "efi"
TARGET_FSTAB = TARGET_ROOT / "etc/fstab"
TARGET_VAR = TARGET_ROOT / "ostree/deploy/default/var"
TARGET_SEED = TARGET_VAR / "lib/cloud/seed/nocloud-net"
TARGET_EFI_CONFIG = TARGET_ROOT / "etc/zeus/efi-update.json"
TARGET_IMAGE_REF = "/var/lib/zeus/updater/downloads/zeusos-0.1.0-preview.2-git-ad091ecc2978.oci"
TARGET_PAYLOAD = TARGET_VAR / TARGET_IMAGE_REF.removeprefix("/var/")
EFI_WRAPPER_NAME = "efi_update.py"
EFI_DROPIN_NAME = "systemd/system/bootloader-update.service.d/zeus-efi.conf"
FEDORA_GRUB_SCRIPT = Path(bootmenu.SCRIPT_PATH)
FEDORA_GRUB_DEFAULTS = Path("/etc/default/grub")
FEDORA_GRUB_CONFIG = Path("/boot/grub2/grub.cfg")

# The EFI update path is coupled to these versions until a later rehearsal
# proves that a new bootc/bootupd pair preserves the same scoped semantics.
# Keep this allowlist in code so a fresh signed feed cannot silently widen the
# qualified maintenance surface.
QUALIFIED_BUILD_ID = "git-ad091ecc2978"
QUALIFIED_MANIFEST_DIGEST = "sha256:9b7ea1104c3398007600a502d4d73d6588b87735b85cad810a952aa73c7048e0"
# Retain the original qualified payload for already journaled installations.
QUALIFIED_ARTIFACTS = frozenset({
    (QUALIFIED_BUILD_ID, QUALIFIED_MANIFEST_DIGEST),
    ("git-f080c2d9bc53", "sha256:8797860dc4c27c7e8e3f0034bfcf71f9876401df809509c6b49588752c9c1c18"),
})
QUALIFIED_BOOTC_VERSION = "1.16.10"
QUALIFIED_BOOTUPD_VERSION = "0.2.35"

OWNER_USER = "shane"
# The password is intentionally consumed only while creating a private
# cloud-init seed.  It is never put in an argv vector or journal record.
_OWNER_PASSWORD = "root"

_DISK_RE = re.compile(r"\A/dev/(?:nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+)\Z")
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_FAT_UUID_RE = re.compile(r"\A[0-9a-fA-F]{4}-[0-9a-fA-F]{4}\Z")
_BOOT_ID_RE = re.compile(r"\A[0-9a-fA-F-]{8,128}\Z")
_FINGERPRINT_RE = re.compile(r"\A(?:sha256:)?[0-9a-fA-F]{64}\Z")
_MAX_JSON = 512 * 1024
_MAX_OUTPUT = 8 * 1024 * 1024

# The only post-stage-2 state that can be retried is the final Fedora menu
# boundary.  Keep this allowlist explicit: every earlier stage can have
# modified a disk, filesystem, mount, deployment, or EFI tree and therefore
# remains permanently ambiguous after an interruption.
# These errors can occur during read-only finalization preconditions. The
# full saved boundary and fresh target proof are still mandatory on retry.
_FINALIZATION_ERRORS = frozenset({
    "grub_invalid", "grub_conflict", "target_mismatch", "ac_required",
    "resource_unverified", "insufficient_ram", "insufficient_staging_space",
    "boot_chooser_write_failed",
})
_FINALIZATION_ACTIONS = frozenset({
    "write_grub_entry", "grub_regenerate", "write_boot_chooser_marker",
})
_FINALIZATION_COMPLETED_PREFIX = (
    "btrfs_resize",
    "btrfs_sync",
    "partition_append",
    "partition_reread",
    "udev_settle",
    "mkfs_esp",
    "mkfs_boot",
    "mkfs_root",
    "mkdir_target",
    "mount_zeus_root",
    "mkdir_target_boot",
    "mount_zeus_boot",
    "mkdir_target_esp",
    "mount_zeus_esp",
    "bootc_install",
    "copy_verified_payload",
    "cloud_init_seed",
    "efi_scope_config",
    "efi_runtime_integration",
    "efi_install",
    "remount_zeus_esp",
    "write_fstab",
)


def _finalization_saved_state(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Return an executor state from all journal layouts we have supported."""

    if not isinstance(record, Mapping):
        return None
    for key in ("executor_state", "dualboot_state"):
        value = record.get(key)
        if isinstance(value, Mapping):
            return value
    result = record.get("executor_result")
    if isinstance(result, Mapping):
        for key in ("executor_state", "dualboot_state"):
            value = result.get(key)
            if isinstance(value, Mapping):
                return value
        if result.get("executor") == "zeus-dualboot" and isinstance(result.get("state"), Mapping):
            return result["state"]
    return None


def _valid_finalization_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def _valid_finalization_fat_uuid(value: Any) -> bool:
    return isinstance(value, str) and _FAT_UUID_RE.fullmatch(value) is not None


def _valid_finalization_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _valid_finalization_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and _FINGERPRINT_RE.fullmatch(value) is not None


def _valid_finalization_disk(value: Any) -> bool:
    return isinstance(value, str) and _DISK_RE.fullmatch(value) is not None


def _valid_finalization_boot_id(value: Any) -> bool:
    return isinstance(value, str) and _BOOT_ID_RE.fullmatch(value) is not None


def _finalization_partition_node(disk: str, number: int) -> str:
    return disk + ("p" if disk[-1].isdigit() else "") + str(number)


def _valid_finalization_table(value: Any, partition_count: int) -> bool:
    if not isinstance(value, Mapping):
        return False
    disk = value.get("device")
    partitions = value.get("partitions")
    if (
        value.get("label") != "gpt"
        or not _valid_finalization_disk(disk)
        or not _valid_finalization_uuid(value.get("id"))
        or value.get("unit") != "sectors"
        or type(value.get("sectorsize")) is not int
        or value.get("sectorsize") <= 0
        or type(value.get("firstlba")) is not int
        or value.get("firstlba") < 0
        or type(value.get("lastlba")) is not int
        or value.get("lastlba") < value.get("firstlba")
        or not isinstance(partitions, list)
        or len(partitions) != partition_count
    ):
        return False
    for index, part in enumerate(partitions, start=1):
        if not isinstance(part, Mapping):
            return False
        if part.get("node") != _finalization_partition_node(str(disk), index):
            return False
        if type(part.get("start")) is not int or part.get("start") < 0:
            return False
        if type(part.get("size")) is not int or part.get("size") <= 0:
            return False
        if not isinstance(part.get("type"), str) or not part.get("type"):
            return False
        if not _valid_finalization_uuid(part.get("uuid")):
            return False
    return True


def _finalization_normalized_table(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize UUID casing while retaining every other GPT field exactly."""

    normalized = copy.deepcopy(dict(value))
    normalized["id"] = str(normalized.get("id", "")).lower()
    partitions = normalized.get("partitions")
    if isinstance(partitions, list):
        for part in partitions:
            if isinstance(part, dict):
                for key in ("uuid", "type"):
                    if isinstance(part.get(key), str):
                        part[key] = part[key].lower()
    return normalized


def _finalization_tables_equal(left: Any, right: Any) -> bool:
    return (
        isinstance(left, Mapping)
        and isinstance(right, Mapping)
        and _finalization_normalized_table(left) == _finalization_normalized_table(right)
    )


def _finalization_backup_choice_shape(state: Mapping[str, Any]) -> bool:
    """Validate the persisted owner choice without inspecting the target."""

    choices: list[bool] = []
    if "backup_decision" in state:
        candidate = state.get("backup_decision")
        if (
            not isinstance(candidate, Mapping)
            or set(candidate) != {"without_backup", "verified", "fingerprint"}
            or candidate.get("without_backup") is not True
            or candidate.get("verified") is not False
            or not _valid_finalization_fingerprint(candidate.get("fingerprint"))
        ):
            return False
        choices.append(True)
    if state.get("backup") not in (None, {}, []):
        receipt = state.get("backup")
        if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
            return False
        choices.append(False)
    return len(choices) == 1


def _valid_finalization_proposal(
    proposal: Any,
    original: Mapping[str, Any],
    expected: Mapping[str, Any],
    new_guids: Sequence[str],
) -> bool:
    if not isinstance(proposal, Mapping):
        return False
    disk = original.get("device")
    if (
        proposal.get("schema_version") != SCHEMA_VERSION
        or proposal.get("disk") != disk
        or str(proposal.get("disk_guid", "")).lower() != str(original.get("id", "")).lower()
        or proposal.get("sector_size") != original.get("sectorsize")
        or proposal.get("fedora_partition") != _finalization_partition_node(str(disk), 3)
        or proposal.get("fedora_start") != original["partitions"][2].get("start")
        or proposal.get("fedora_original_size") != original["partitions"][2].get("size")
        or proposal.get("fedora_new_size") != expected["partitions"][2].get("size")
        or type(proposal.get("fedora_filesystem_limit_bytes")) is not int
        or proposal.get("fedora_filesystem_limit_bytes") <= 0
        or not _valid_finalization_digest(proposal.get("table_fingerprint"))
    ):
        return False
    partitions = proposal.get("partitions")
    if not isinstance(partitions, list) or len(partitions) != 3:
        return False
    for index, (part, guid) in enumerate(zip(partitions, new_guids), start=4):
        if not isinstance(part, Mapping):
            return False
        if (
            part.get("number") != index
            or part.get("node") != _finalization_partition_node(str(disk), index)
            or type(part.get("start")) is not int
            or part.get("start") < 0
            or type(part.get("size")) is not int
            or part.get("size") <= 0
            or not isinstance(part.get("type"), str)
            or not part.get("type")
            or str(part.get("uuid", guid)).lower() != str(guid).lower()
        ):
            return False
    return True


def _finalization_expected_table_relation(
    original: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    if not _valid_finalization_table(original, 3) or not _valid_finalization_table(expected, 3):
        return False
    left = copy.deepcopy(dict(original))
    right = copy.deepcopy(dict(expected))
    left_parts = left["partitions"]
    right_parts = right["partitions"]
    if type(left_parts[2].get("size")) is not int or type(right_parts[2].get("size")) is not int:
        return False
    if left_parts[2]["size"] <= right_parts[2]["size"]:
        return False
    left_parts[2]["size"] = right_parts[2]["size"]
    return _finalization_tables_equal(left, right)


def _finalization_state_shape(record: Mapping[str, Any]) -> bool:
    state = _finalization_saved_state(record)
    if not isinstance(state, Mapping):
        return False
    phase = record.get("phase")
    error = record.get("error")
    if phase == "error":
        if error not in _FINALIZATION_ERRORS:
            return False
    elif phase == "installing":
        if error not in (None, ""):
            return False
    else:
        return False
    if (
        state.get("schema_version") != SCHEMA_VERSION
        or state.get("executor") != "zeus-dualboot"
        or state.get("stage") != 2
        or state.get("phase") != "mounted"
        or state.get("status") != "in_progress"
        or state.get("action") not in _FINALIZATION_ACTIONS
        or state.get("reboot_required") is not True
        or not _finalization_backup_choice_shape(state)
    ):
        return False
    completed = state.get("completed_actions")
    expected_completed = list(_FINALIZATION_COMPLETED_PREFIX)
    if state.get("action") in {"grub_regenerate", "write_boot_chooser_marker"}:
        expected_completed.append("write_grub_entry")
    if state.get("action") == "write_boot_chooser_marker":
        expected_completed.append("grub_regenerate")
    if completed != expected_completed:
        return False
    marker = state.get("boot_chooser_marker")
    if marker is not None and not bootmenu.validate_boot_chooser_marker(marker):
        return False
    if not _valid_finalization_disk(state.get("disk")):
        return False
    original = state.get("original_table")
    expected = state.get("expected_table")
    allocated = state.get("allocated_table")
    proposal = state.get("proposal")
    if not _valid_finalization_table(original, 3) or not _valid_finalization_table(expected, 3):
        return False
    if not _valid_finalization_table(allocated, 6):
        return False
    if original.get("device") != state.get("disk") or expected.get("device") != state.get("disk") or allocated.get("device") != state.get("disk"):
        return False
    if not _finalization_expected_table_relation(original, expected):
        return False
    guids = state.get("new_guids")
    if (
        not isinstance(guids, list)
        or len(guids) != 3
        or len(set(str(item).lower() for item in guids)) != 3
        or not all(_valid_finalization_uuid(item) for item in guids)
    ):
        return False
    allocated_parts = allocated.get("partitions")
    expected_parts = expected.get("partitions")
    if not isinstance(allocated_parts, list) or not isinstance(expected_parts, list):
        return False
    if allocated_parts[:3] != expected_parts:
        return False
    for index, (part, guid) in enumerate(zip(allocated_parts[3:], guids), start=4):
        proposal_parts = proposal.get("partitions") if isinstance(proposal, Mapping) else None
        proposal_part = proposal_parts[index - 4] if isinstance(proposal_parts, list) and len(proposal_parts) == 3 else None
        if (
            not isinstance(part, Mapping)
            or not isinstance(proposal_part, Mapping)
            or part.get("node") != _finalization_partition_node(str(state["disk"]), index)
            or part.get("start") != proposal_part.get("start")
            or part.get("size") != proposal_part.get("size")
            or str(part.get("type", "")).lower() != str(proposal_part.get("type", "")).lower()
            or str(part.get("uuid", "")).lower() != str(guid).lower()
        ):
            return False
    if not _valid_finalization_proposal(proposal, original, expected, guids):
        return False
    if not _finalization_backup_choice_shape(state):
        return False
    filesystem_uuids = state.get("filesystem_uuids")
    if (
        not isinstance(filesystem_uuids, Mapping)
        or set(filesystem_uuids) != {"root", "boot", "esp"}
        or not _valid_finalization_uuid(filesystem_uuids.get("root"))
        or not _valid_finalization_uuid(filesystem_uuids.get("boot"))
        or not _valid_finalization_fat_uuid(filesystem_uuids.get("esp"))
        or str(state.get("efi_scope_uuid", "")).upper() != str(filesystem_uuids.get("esp", "")).upper()
    ):
        return False
    if (
        not _valid_finalization_digest(state.get("bootmenu_hash"))
        or not _valid_finalization_digest(state.get("efi_wrapper_sha256"))
        or not isinstance(state.get("deployment_root"), str)
        or not state.get("deployment_root", "").startswith("/target/ostree/deploy/default/deploy/")
        or re.fullmatch(r"[0-9a-f]{64}\.[0-9]+", Path(state["deployment_root"]).name) is None
    ):
        return False
    old_boot = state.get("old_boot_id")
    current_boot = state.get("current_boot_id")
    if (
        not _valid_finalization_boot_id(old_boot)
        or not _valid_finalization_boot_id(current_boot)
        or old_boot == current_boot
    ):
        return False
    for key in (
        "actual_filesystem_bytes_before",
        "actual_filesystem_bytes",
        "minimum_filesystem_bytes",
        "filesystem_limit_bytes",
        "kernel_partition_bytes",
    ):
        if type(state.get(key)) is not int or state.get(key) <= 0:
            return False
    if (
        state["actual_filesystem_bytes"] < state["minimum_filesystem_bytes"]
        or state["actual_filesystem_bytes"] > state["filesystem_limit_bytes"]
        or state["kernel_partition_bytes"] != proposal["fedora_new_size"] * proposal["sector_size"]
    ):
        return False
    top_boot = record.get("boot_id")
    if top_boot is not None and (not _valid_finalization_boot_id(top_boot) or top_boot != current_boot):
        return False
    fingerprint = record.get("fingerprint")
    for choice_key in ("backup_decision",):
        choice = state.get(choice_key)
        if isinstance(choice, Mapping) and isinstance(fingerprint, str) and choice.get("fingerprint") != fingerprint:
            return False
    return True


def can_resume_finalization(record: Mapping[str, Any] | None) -> bool:
    """Return whether a journal is exactly at the retryable GRUB boundary.

    This is deliberately a pure structural predicate.  It never reads the
    target, artifact, or host and therefore is safe for status/UI callers.
    The companion :func:`verify_finalization_resume` proves current target
    evidence before an installation phase transition.
    """

    try:
        return isinstance(record, Mapping) and _finalization_state_shape(record)
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return False


def _finalization_inventory_table(value: Any) -> Mapping[str, Any] | None:
    """Find a complete six-partition GPT table in current inventory evidence."""

    seen: set[int] = set()
    pending: list[Any] = [value]
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, Mapping):
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            if _valid_finalization_table(candidate, 6):
                return candidate
            for key in (
                "partition_table",
                "sfdisk_table",
                "partitiontable",
                "sfdisk",
                "storage",
                "target",
                "inventory",
                "current_inventory",
                "current",
                "table",
                "blockdevices",
                "block_devices",
            ):
                nested = candidate.get(key)
                if nested is not None:
                    pending.append(nested)
        elif isinstance(candidate, (list, tuple)):
            pending.extend(candidate)
    return None


def _finalization_inventory_mounts(value: Any) -> list[Mapping[str, Any]]:
    """Extract flat mount records from preflight's mounts/findmnt shapes."""

    if not isinstance(value, Mapping):
        return []

    def collect(candidates: Sequence[Any]) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        pending = list(candidates)
        seen: set[int] = set()
        while pending:
            candidate = pending.pop(0)
            if isinstance(candidate, Mapping):
                marker = id(candidate)
                if marker in seen:
                    continue
                seen.add(marker)
                target = candidate.get("target", candidate.get("mountpoint"))
                if isinstance(target, str):
                    result.append(candidate)
                for key in ("filesystems", "mounts", "children"):
                    nested = candidate.get(key)
                    if nested is not None:
                        pending.append(nested)
            elif isinstance(candidate, (list, tuple)):
                pending.extend(candidate)
        return result

    # ``preflight.collect`` intentionally exposes both a canonical flattened
    # ``mounts`` list and the raw ``findmnt`` tree.  Prefer the canonical list
    # so each target is represented once; otherwise the two aliases would
    # look like duplicate mounts and make a valid resume fail closed.
    canonical = value.get("mounts")
    if canonical is not None:
        result = collect([canonical])
        if result:
            return result
    for key in ("inventory", "current_inventory", "current"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            result = _finalization_inventory_mounts(nested)
            if result:
                return result
    candidates = [value[key] for key in ("findmnt", "filesystems") if value.get(key) is not None]
    result = collect(candidates)
    # Keep a deterministic de-duplicated fallback for inventories assembled
    # from more than one raw findmnt alias.
    unique: list[Mapping[str, Any]] = []
    keys: set[tuple[Any, ...]] = set()
    for item in result:
        marker = tuple(item.get(key) for key in ("target", "mountpoint", "source", "fstype", "uuid", "partuuid"))
        if marker in keys:
            continue
        keys.add(marker)
        unique.append(item)
    return unique


def _finalization_inventory_devices(value: Any) -> list[Mapping[str, Any]]:
    """Flatten only block-device records used for stable-ID evidence."""

    if not isinstance(value, Mapping):
        return []

    def collect(candidates: Sequence[Any]) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        pending: list[Any] = list(candidates)
        seen: set[int] = set()
        while pending:
            candidate = pending.pop(0)
            if isinstance(candidate, Mapping):
                marker = id(candidate)
                if marker in seen:
                    continue
                seen.add(marker)
                if any(key in candidate for key in ("path", "name", "kname", "type", "serial", "wwn")):
                    result.append(candidate)
                for key in ("block_devices", "blockdevices", "devices", "children", "lsblk"):
                    nested = candidate.get(key)
                    if nested is not None:
                        pending.append(nested)
            elif isinstance(candidate, (list, tuple)):
                pending.extend(candidate)
        return result

    # As with mounts, preflight retains both the flattened block_devices list
    # and raw lsblk output.  The flattened list is canonical and avoids
    # treating one disk record as two conflicting stable-ID observations.
    canonical = value.get("block_devices")
    if canonical is not None:
        result = collect([canonical])
        if result:
            return result
    for key in ("inventory", "current_inventory", "current"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            result = _finalization_inventory_devices(nested)
            if result:
                return result
    return collect([value[key] for key in ("lsblk", "blockdevices", "devices") if value.get(key) is not None])


def _finalization_device_path(value: Mapping[str, Any]) -> str | None:
    candidate = value.get("path", value.get("device_path", value.get("devpath")))
    if candidate is None:
        candidate = value.get("name", value.get("kname", value.get("device")))
    if candidate is None:
        return None
    text = str(candidate)
    return text if text.startswith("/") else "/dev/" + text


def _finalization_device_identity(value: Mapping[str, Any]) -> str | None:
    for key in ("stable_id", "stableid", "serial", "wwn", "eui", "disk_id", "by_id", "identity", "id"):
        candidate = value.get(key)
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text and not text.startswith("/dev/") and text.lower() not in {"unknown", "none", "null", "-"}:
            return text
    return None


def _finalization_inventory_boot_id(inventory: Mapping[str, Any]) -> str | None:
    for owner in (inventory, inventory.get("kernel"), inventory.get("host"), inventory.get("boot")):
        if not isinstance(owner, Mapping):
            continue
        for key in ("boot_id", "bootid", "current_boot_id"):
            value = owner.get(key)
            if _valid_finalization_boot_id(value):
                return value
    return None


def verify_finalization_resume(
    *,
    plan: Mapping[str, Any],
    record: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> bool:
    """Prove current six-partition and mount evidence for GRUB-only retry.

    The function is intentionally read-only.  It consumes the fresh
    inventory supplied by the backend and checks every durable identity needed
    by the finalizer.  A runner-backed instance check repeats these proofs
    immediately before the two final menu writes.
    """

    try:
        if not can_resume_finalization(record) or not isinstance(plan, Mapping) or not isinstance(inventory, Mapping):
            return False
        state = _finalization_saved_state(record)
        if not isinstance(state, Mapping):
            return False
        fingerprint = plan.get("fingerprint")
        if not _valid_finalization_fingerprint(fingerprint):
            return False
        backup = state.get("backup_decision")
        if isinstance(backup, Mapping):
            if backup.get("fingerprint") != fingerprint:
                return False
        checker = DualBootExecutor(require_root=False)
        original = state.get("original_table")
        expected = state.get("expected_table")
        allocated = state.get("allocated_table")
        proposal = state.get("proposal")
        guids = state.get("new_guids")
        if not isinstance(original, Mapping) or not isinstance(expected, Mapping) or not isinstance(allocated, Mapping) or not isinstance(proposal, Mapping) or not isinstance(guids, list):
            return False
        plan_table = checker._plan_table(plan)
        if plan_table is not None and not _finalization_tables_equal(plan_table, original):
            return False
        plan_proposal = checker._find_proposal(plan)
        if isinstance(plan_proposal, Mapping):
            if not _valid_finalization_proposal(plan_proposal, original, expected, guids):
                return False
            if not _valid_finalization_proposal(proposal, original, expected, guids):
                return False
            # Compare the durable proposal to all geometry fields exposed by
            # preflight.  The state itself remains the source of the full
            # append identities.
            checker._compare_proposal(proposal, plan_proposal)
        table = _finalization_inventory_table(inventory)
        if table is None or not _finalization_tables_equal(table, allocated):
            return False

        # Stable disk and Fedora source identities are checked from the fresh
        # block-device inventory where those records are available.  The GPT
        # and partition UUIDs above are mandatory; a missing serial is not
        # silently substituted for a different serial.
        expected_disk = checker._expected_disk_identity(plan, plan.get("inventory"))
        devices = _finalization_inventory_devices(inventory)
        disk_devices = [
            item for item in devices
            if _finalization_device_path(item) == state.get("disk")
            and str(item.get("type", "")).lower() in {"disk", "nvme", "mmc"}
        ]
        if expected_disk is not None:
            if len(disk_devices) != 1 or _finalization_device_identity(disk_devices[0]) != str(expected_disk):
                return False
        expected_source = checker._expected_source_identity(plan, plan.get("inventory"))
        allocated_parts = allocated.get("partitions")
        if not isinstance(allocated_parts, list) or len(allocated_parts) != 6:
            return False
        source_uuid = str(allocated_parts[2].get("uuid", "")).lower() if isinstance(allocated_parts[2], Mapping) else ""
        if expected_source is not None and source_uuid != str(expected_source).lower():
            return False

        filesystems = state.get("filesystem_uuids")
        if not isinstance(filesystems, Mapping):
            return False
        expected_mounts = {
            "/target": (allocated_parts[5], filesystems.get("root"), {"ext4"}),
            "/target/boot": (allocated_parts[4], filesystems.get("boot"), {"ext4"}),
            "/target/boot/efi": (allocated_parts[3], filesystems.get("esp"), {"vfat", "fat", "fat32"}),
        }
        mounts = _finalization_inventory_mounts(inventory)
        for target, (part, fs_uuid, types) in expected_mounts.items():
            matches = [
                item for item in mounts
                if item.get("target", item.get("mountpoint")) == target
            ]
            if len(matches) != 1 or not isinstance(part, Mapping) or not isinstance(fs_uuid, str):
                return False
            item = matches[0]
            source = str(item.get("source", item.get("device", ""))).split("[", 1)[0]
            fstype = str(item.get("fstype", item.get("FSTYPE", ""))).lower()
            observed_uuid = item.get("uuid", item.get("UUID"))
            observed_partuuid = item.get("partuuid", item.get("PARTUUID"))
            if (
                source != part.get("node")
                or fstype not in types
                or not isinstance(observed_uuid, str)
                or observed_uuid.lower() != fs_uuid.lower()
                or not isinstance(observed_partuuid, str)
                or observed_partuuid.lower() != str(part.get("uuid", "")).lower()
            ):
                return False

        observed_boot = _finalization_inventory_boot_id(inventory)
        if observed_boot is not None and observed_boot != state.get("current_boot_id"):
            return False
        # Power/RAM/staging are volatile write preconditions, not target
        # identity. The executor checks them immediately before finalization
        # and returns their specific errors instead of a false disk mismatch.
        return True
    except (AttributeError, ExecutorError, OSError, TypeError, ValueError, KeyError, RuntimeError):
        return False


class ExecutorError(InstallError):
    """Stable, user-safe failure raised by the executor."""


class DualBootExecutor:
    """The two-phase maintenance executor consumed by ``InstallerBackend``.

    ``qualified`` defaults to false on purpose.  The caller that qualifies a
    disposable VM may construct this object with ``qualified=True`` only when
    it also supplies the scoped ``efi_update`` integration.  The constructor
    never infers qualification from a fixture marker or from the host's DMI
    strings, which prevents a physical host from being enabled accidentally.

    ``inventory_provider`` is an optional fixture/read-only seam.  The backend
    already performs its own fingerprint check; this provider is used here
    only when a plan does not carry enough Btrfs measurement data.  ``reader``
    and ``writer`` are similarly narrow file seams for non-production tests.
    They receive fixed paths and never receive a command from the client.
    """

    qualified = False

    def __init__(
        self,
        *,
        qualified: bool = False,
        efi_update: Callable[..., Any] | None = None,
        inventory_provider: Callable[[], Mapping[str, Any]] | None = None,
        backup_receipt_path: str | os.PathLike[str] = BACKUP_RECEIPT_PATH,
        boot_id: Callable[[], str] | None = None,
        uuid_factory: Callable[[], uuid.UUID | str] | None = None,
        reader: Callable[[Path], str | bytes | None] | None = None,
        writer: Callable[[Path, bytes, int], Any] | None = None,
        qualification_receipt: Mapping[str, Any] | None = None,
        inhibitor: Callable[[], Any] | Any | None = None,
        require_root: bool = True,
    ):
        self.efi_update = efi_update
        self.qualification_receipt = copy.deepcopy(dict(qualification_receipt)) if isinstance(qualification_receipt, Mapping) else None
        # Requiring the servicing integration is intentional: a successful
        # initial EFI install is not evidence that future bootupd servicing is
        # safely scoped to the Zeus ESP.
        self.qualified = bool(
            qualified
            and callable(efi_update)
            and self._valid_qualification_receipt(self.qualification_receipt)
        )
        self.inventory_provider = inventory_provider
        self.backup_receipt_path = self._absolute_path(
            backup_receipt_path, code="invalid_receipt", message="The backup receipt path is invalid."
        )
        self.boot_id = boot_id or self._read_boot_id
        self.uuid_factory = uuid_factory or uuid.uuid4
        self.reader = reader
        self.writer = writer
        # The production default is an actual sleep/shutdown inhibitor.  A
        # caller running a non-production fixture can inject a context
        # manager (or a factory returning one) so tests never need a systemd
        # session.  The seam is intentionally separate from the privileged
        # command runner: acquiring the inhibitor must happen before the first
        # mutating command, and it is held across the whole write boundary.
        self.inhibitor = inhibitor
        self.require_root = bool(require_root)
        # ``bootc install to-filesystem`` creates an OSTree deployment below
        # the physical target root.  Mutable machine files such as fstab and
        # the EFI scope config must be injected into that deployment, while
        # cloud-init's first-boot seed remains in the physical /var tree.
        self.deployment_root: Path | None = None

    # ------------------------------------------------------------------
    # Public protocol and qualification boundary
    # ------------------------------------------------------------------

    def qualify_for_fixture(
        self,
        efi_update: Callable[..., Any],
        qualification_receipt: Mapping[str, Any] | None = None,
    ) -> None:
        """Explicitly enable this object after a disposable VM qualification.

        Production callers should leave the object disabled until the VM
        rehearsal, including scoped EFI servicing, has passed.
        """

        if not callable(efi_update):
            raise ExecutorError("executor_unavailable", "Scoped EFI servicing is not available.")
        receipt = qualification_receipt or self.qualification_receipt
        if not self._valid_qualification_receipt(receipt):
            raise ExecutorError(
                "executor_unavailable",
                "The pinned VM qualification receipt is unavailable or unsupported.",
            )
        self.efi_update = efi_update
        self.qualification_receipt = copy.deepcopy(dict(receipt))
        self.qualified = True

    def execute(
        self,
        *,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        without_backup: bool = False,
    ) -> Mapping[str, Any]:
        """Execute one safe boundary of the installation.

        Backend normally calls this while its journal is in ``installing``.
        The durable executor result from the previous call determines whether
        this is the pre-reboot phase or the post-reboot phase.
        """

        if type(without_backup) is not bool:
            raise ExecutorError(
                "invalid_action", "The backup choice must be an explicit boolean."
            )

        if self.qualified is not True:
            raise ExecutorError(
                "executor_unavailable",
                "The dual-boot executor is not qualified; no disk change was attempted.",
            )
        if not isinstance(plan, Mapping) or not isinstance(artifact, Mapping):
            raise ExecutorError("invalid_plan", "The dual-boot installation inputs are invalid.")
        self._validate_execution_plan(plan)
        self._verify_qualified_artifact(artifact)
        if journal is None or not callable(getattr(journal, "load", None)) or not callable(
            getattr(journal, "write", None)
        ):
            raise ExecutorError("invalid_state", "The installer journal is unavailable.")

        state_record = self._load_record(journal)
        saved = self._saved_state(state_record)
        effective_without_backup = bool(without_backup)
        if saved is not None:
            persisted_choice = self._saved_backup_choice(saved, plan)
            if persisted_choice is not None:
                if persisted_choice is False and without_backup:
                    raise ExecutorError(
                        "backup_choice_mismatch",
                        "The recorded installation already selected a verified backup.",
                    )
                # A reopened GUI may clear its checkbox after the Fedora
                # reboot.  The root-owned stage-one decision remains
                # authoritative for stage two and must not be replaced by a
                # fresh default.
                effective_without_backup = persisted_choice
            # A stage-two command may have completed every operation through
            # fstab and stopped only while writing Fedora's final menu entry
            # (or regenerating its menu).  This is the one narrowly bounded
            # retry that is safe after an ambiguous marker.  Detect it before
            # the generic ambiguity rejection; all other in-progress state
            # remains fail-closed below.
            if state_record is not None and self.can_resume_finalization(state_record):
                return self._resume_finalization(
                    plan,
                    artifact,
                    runner,
                    journal,
                    state_record,
                    saved,
                )
            self._reject_ambiguous(saved)
            stage = saved.get("stage")
            if stage == 1 and saved.get("phase") == "reboot_required":
                return self._stage2(
                    plan,
                    artifact,
                    runner,
                    journal,
                    state_record,
                    saved,
                    without_backup=effective_without_backup,
                )
            if stage == 2 and saved.get("phase") == "installed":
                return copy.deepcopy(saved.get("result") or self._installed_result(saved))
            if stage == 2:
                # A completed stage-2 command may already have changed a
                # partition, filesystem, mount, EFI tree, or Fedora GRUB.
                # Without a dedicated proof for that exact boundary, replay
                # would be unsafe even when the last marker says complete.
                raise ExecutorError(
                    "interrupted",
                    "The second installation phase stopped at an ambiguous boundary; replay is disabled.",
                )
            if stage == 1 and saved.get("phase") == "stage1_complete":
                # A process can stop after the durable boundary and before the
                # backend's final phase transition.  Returning the already
                # complete safe-boundary result is idempotent and does not
                # repeat a destructive command.
                return self._reboot_result(saved)
            if stage == 1 and saved.get("phase") not in {None, "prepared"}:
                raise ExecutorError(
                    "interrupted",
                    "The first installation phase stopped at an ambiguous boundary; replay is disabled.",
                )
            if stage not in (None, 1, 2):
                raise ExecutorError("invalid_state", "The dual-boot journal state is unsupported.")

        return self._stage1(
            plan,
            artifact,
            runner,
            journal,
            state_record,
            without_backup=effective_without_backup,
        )

    @staticmethod
    def can_resume_finalization(record: Mapping[str, Any] | None) -> bool:
        """Expose the pure structural GRUB-finalization resume predicate."""

        return can_resume_finalization(record)

    @staticmethod
    def _validate_execution_plan(plan: Mapping[str, Any]) -> None:
        """Reject an unvalidated or already blocked preflight plan.

        The backend normally calls ``validate_plan`` before it reaches this
        object.  Keeping this small guard at the privileged boundary makes a
        direct integration (and a fixture runner) fail closed as well.
        """

        if type(plan.get("schema_version")) is not int or plan.get("schema_version") != SCHEMA_VERSION:
            raise ExecutorError("invalid_plan", "The installer plan schema is unsupported.")
        if plan.get("supported") is not True:
            raise ExecutorError("unsupported_plan", "The preflight plan is not supported for installation.")
        blockers = plan.get("blockers", [])
        if not isinstance(blockers, list) or blockers:
            raise ExecutorError("unsupported_plan", "The preflight plan contains blockers.")
        fingerprint = plan.get("fingerprint")
        if not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise ExecutorError("invalid_plan", "The installer plan has no valid target fingerprint.")

    def verify_finalization_resume(
        self,
        *,
        plan: Mapping[str, Any],
        record: Mapping[str, Any],
        inventory: Mapping[str, Any],
    ) -> bool:
        """Prove a current target before allowing GRUB-only finalization."""

        if self.qualified is not True or not self._valid_qualification_receipt(self.qualification_receipt):
            return False
        if not verify_finalization_resume(plan=plan, record=record, inventory=inventory):
            return False
        try:
            state = self._saved_state(record)
            if not isinstance(state, Mapping):
                return False
            old_boot = self._safe_boot_id(state.get("old_boot_id"))
            current_boot = self._safe_boot_id(self.boot_id())
            saved_boot = self._safe_boot_id(state.get("current_boot_id"))
            return (
                old_boot is not None
                and current_boot is not None
                and saved_boot is not None
                and old_boot != current_boot
                and current_boot == saved_boot
            )
        except (ExecutorError, OSError, TypeError, ValueError, AttributeError):
            return False

    # Backend's optional resume hook.  It is deliberately read-only.
    def verify_resume(
        self,
        *,
        plan: Mapping[str, Any],
        record: Mapping[str, Any],
        inventory: Mapping[str, Any],
    ) -> bool:
        try:
            saved = self._saved_state(record)
            if not isinstance(saved, Mapping) or saved.get("stage") != 1:
                return False
            if saved.get("phase") != "reboot_required" or saved.get("status") != "complete":
                return False
            old_boot = saved.get("old_boot_id")
            now = self._safe_boot_id(self.boot_id())
            if old_boot is None or now is None or old_boot == now:
                return False
            expected = saved.get("expected_table")
            if not isinstance(expected, Mapping):
                return False
            current = self._table_from_inventory(inventory)
            if current is not None:
                if dict(current) != dict(expected):
                    return False
            else:
                expected_source = expected.get("partitions", [None, None, {}])[2]
                observed_size = self._inventory_source_size(inventory)
                if not isinstance(expected_source, Mapping) or observed_size is None:
                    return False
                if observed_size != int(expected_source.get("size", -1)):
                    return False
            return True
        except (ExecutorError, OSError, ValueError, TypeError, KeyError, AttributeError):
            return False

    # Older backend revisions look for this name.
    verify_after_reboot = verify_resume

    # ------------------------------------------------------------------
    # Stage 1: mounted Fedora shrink and end-only GPT change
    # ------------------------------------------------------------------

    @classmethod
    def _saved_backup_choice(
        cls,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> bool | None:
        """Read and authenticate a persisted owner backup decision.

        A stage-one state is written before the first mutating command. Once
        that state exists it is authoritative across the reboot, including
        when the reopened GUI's checkbox has returned to its default. Older
        states carry only a public verified receipt and therefore resolve to
        the normal backup path.
        """

        if not isinstance(state, Mapping):
            return None
        expected_fp = cls._fingerprint(plan.get("fingerprint"))
        choices: list[bool] = []
        if "backup_decision" in state:
            candidate = state.get("backup_decision")
            if not isinstance(candidate, Mapping):
                raise ExecutorError("invalid_state", "The persisted backup decision is invalid.")
            # This is the sole schema written for an owner-declined choice.
            # Requiring all three fields prevents a hand-edited or truncated
            # state from turning a continuation into an unverified write.
            if (
                type(candidate.get("without_backup")) is not bool
                or candidate.get("without_backup") is not True
                or type(candidate.get("verified")) is not bool
                or candidate.get("verified") is not False
                or set(candidate) != {"without_backup", "verified", "fingerprint"}
            ):
                raise ExecutorError("invalid_state", "The persisted backup decision is invalid.")
            recorded = cls._fingerprint(candidate.get("fingerprint"))
            if recorded is None or expected_fp is None or recorded != expected_fp:
                raise ExecutorError(
                    "target_mismatch",
                    "The persisted backup decision belongs to another target.",
                )
            choices.append(True)

        # Older stage-one state has a public receipt under ``backup`` and no
        # decision marker. Keep that verified path for fixture compatibility.
        legacy_receipt = state.get("backup")
        if legacy_receipt not in (None, {}, []):
            if not isinstance(legacy_receipt, Mapping) or legacy_receipt.get("verified") is not True:
                raise ExecutorError("invalid_state", "The persisted backup decision is invalid.")
            choices.append(False)

        if not choices:
            return None
        if any(choice != choices[0] for choice in choices[1:]):
            raise ExecutorError("invalid_state", "The persisted backup decision is conflicting.")
        return choices[0]

    @staticmethod
    def _valid_qualification_receipt(receipt: Mapping[str, Any] | None) -> bool:
        if not isinstance(receipt, Mapping):
            return False
        if receipt.get("qualified") is False or receipt.get("physical") is True:
            return False
        nested = receipt.get("artifact")
        artifact = nested if isinstance(nested, Mapping) else receipt
        build = artifact.get("build_id", receipt.get("build_id"))
        digest = artifact.get("manifest_digest", artifact.get("image_manifest_digest", receipt.get("manifest_digest")))
        tools = receipt.get("tools")
        tools = tools if isinstance(tools, Mapping) else {}
        bootc = receipt.get("bootc_version", receipt.get("bootc", tools.get("bootc_version", tools.get("bootc"))))
        bootupd = receipt.get(
            "bootupd_version",
            receipt.get("bootupd", tools.get("bootupd_version", tools.get("bootupd"))),
        )
        return (
            isinstance(build, str) and isinstance(digest, str)
            and (build, digest) in QUALIFIED_ARTIFACTS
            and bootc == QUALIFIED_BOOTC_VERSION
            and bootupd == QUALIFIED_BOOTUPD_VERSION
        )

    def _verify_qualified_artifact(self, artifact: Mapping[str, Any]) -> None:
        if not self.qualified:
            return
        receipt = self.qualification_receipt
        if not self._valid_qualification_receipt(receipt):
            raise ExecutorError(
                "executor_unavailable",
                "The pinned VM qualification receipt is unavailable or unsupported.",
            )
        build, digest = artifact.get("build_id"), artifact.get("manifest_digest")
        if not isinstance(build, str) or not isinstance(digest, str) or (build, digest) not in QUALIFIED_ARTIFACTS:
            raise ExecutorError(
                "artifact_invalid",
                "The staged image is outside the pinned VM qualification build.",
            )

    def _refresh_prewrite_guard(
        self,
        plan: Mapping[str, Any],
        runner: Any,
        *,
        require_supported: bool,
    ) -> None:
        """Refresh volatile preconditions before a mutating boundary.

        ``backend.validate_target`` checks the stable target fingerprint, but
        that fingerprint intentionally excludes AC state, available RAM, and
        free staging space.  The supplied inventory provider is therefore a
        read-only, narrow seam for a fresh ``preflight.collect`` result (or a
        pre-computed current plan).  A production executor cannot silently
        fall back to stale values in the original plan when this refresh is
        unavailable.  Fixture callers may omit the provider by setting
        ``require_root=False``.
        """

        provider = self.inventory_provider
        if provider is None:
            if self.require_root:
                raise ExecutorError(
                    "preflight_unavailable",
                    "A fresh preflight inventory is required before disk mutation.",
                )
            return
        try:
            if callable(provider):
                try:
                    current = provider()
                except TypeError as first:
                    # A fixture may want the same injected runner used by the
                    # executor.  Production providers use the zero-argument
                    # form; this fallback never accepts client commands.
                    try:
                        current = provider(runner)
                    except TypeError:
                        raise first
            else:
                current = provider
        except ExecutorError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ExecutorError(
                "preflight_unavailable",
                "A fresh preflight inventory could not be collected safely.",
            ) from error
        if not isinstance(current, Mapping):
            raise ExecutorError("preflight_unavailable", "The fresh preflight inventory is invalid.")

        current_plan: Mapping[str, Any] | None = None
        inventory: Mapping[str, Any] = current
        candidate = current.get("current_plan")
        if isinstance(candidate, Mapping):
            current_plan = candidate
            nested_inventory = candidate.get("inventory")
            if isinstance(nested_inventory, Mapping):
                inventory = nested_inventory
        elif isinstance(current.get("plan"), Mapping):
            current_plan = current["plan"]
            nested_inventory = current_plan.get("inventory")
            if isinstance(nested_inventory, Mapping):
                inventory = nested_inventory
        elif "supported" in current or "blockers" in current:
            # A pre-computed plan is useful for a caller that already owns
            # the read-only preflight lifecycle.
            current_plan = current
            nested_inventory = current.get("inventory")
            if isinstance(nested_inventory, Mapping):
                inventory = nested_inventory
        elif require_supported:
            try:
                module_name = f"{__package__}.preflight" if __package__ else "preflight"
                preflight = importlib.import_module(module_name)
                plan_function = getattr(preflight, "plan", None)
                if not callable(plan_function):
                    raise AttributeError("preflight.plan is unavailable")
                allocation = self._allocation(plan)
                current_plan = plan_function(current, allocation_gib=allocation)
            except ExecutorError:
                raise
            except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                raise ExecutorError(
                    "preflight_unavailable",
                    "The fresh preflight plan could not be validated safely.",
                ) from error
        else:
            # Stage 2 only needs volatile values.  A plain inventory is still
            # checked below, while avoiding a second full layout proposal over
            # the intentionally changed post-shrink table.
            current_plan = None

        if require_supported:
            if not isinstance(current_plan, Mapping):
                raise ExecutorError("preflight_unavailable", "The current preflight plan is unavailable.")
            if current_plan.get("supported") is not True:
                raise ExecutorError("unsupported_plan", "The current preflight plan is no longer supported.")
            blockers = current_plan.get("blockers", [])
            if not isinstance(blockers, list) or blockers:
                raise ExecutorError("unsupported_plan", "The current preflight plan contains blockers.")
            expected_fingerprint = self._fingerprint(plan.get("fingerprint"))
            observed_fingerprint = self._fingerprint(current_plan.get("fingerprint"))
            if expected_fingerprint is not None and observed_fingerprint is not None:
                if expected_fingerprint != observed_fingerprint:
                    raise ExecutorError(
                        "target_mismatch",
                        "The target changed while the installer was preparing to write.",
                    )
            elif self.require_root:
                raise ExecutorError(
                    "target_unverified",
                    "The refreshed preflight plan has no stable target fingerprint.",
                )

        self._check_dynamic_resources(inventory)

    @staticmethod
    def _mapping_value(owner: Mapping[str, Any], keys: Sequence[str]) -> Any:
        for key in keys:
            if key in owner:
                return owner.get(key)
        return None

    @classmethod
    def _bytes_value(cls, owner: Mapping[str, Any], keys: Sequence[str]) -> int | None:
        value = cls._mapping_value(owner, keys)
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str):
            try:
                parsed = int(value)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        return None

    @classmethod
    def _resource_inventory(cls, inventory: Mapping[str, Any]) -> Mapping[str, Any]:
        nested = inventory.get("inventory")
        return nested if isinstance(nested, Mapping) else inventory

    def _check_dynamic_resources(self, inventory: Mapping[str, Any]) -> None:
        """Require fresh AC, RAM, and staging measurements.

        These checks mirror the preflight policy constants.  They are kept at
        the executor boundary because those values are deliberately omitted
        from the stable target fingerprint and can change between prepare and
        the first or second write phase.
        """

        value = self._resource_inventory(inventory)
        memory = value.get("memory", value.get("ram"))
        power = value.get("power", value.get("battery"))
        staging = value.get("staging", value.get("storage_capacity"))
        virtualization = value.get("virtualization", value.get("virt", value.get("vm")))
        memory = memory if isinstance(memory, Mapping) else {}
        power = power if isinstance(power, Mapping) else {}
        staging = staging if isinstance(staging, Mapping) else {}
        virtualization = virtualization if isinstance(virtualization, Mapping) else {}

        available = self._bytes_value(memory, ("available_bytes",))
        if available is None:
            available_gib = self._mapping_value(memory, ("available_gib", "available"))
            try:
                if available_gib is not None:
                    available = int(float(available_gib) * (1024**3))
            except (TypeError, ValueError, OverflowError):
                available = None
        if available is None:
            raise ExecutorError("resource_unverified", "Available RAM could not be refreshed before mutation.")
        if available < 2 * 1024**3:
            raise ExecutorError("insufficient_ram", "Available RAM is below the maintenance minimum.")

        ac = power.get("ac_online", power.get("on_ac", power.get("mains_online")))
        if isinstance(ac, str):
            normalized_ac = ac.strip().lower()
            ac = True if normalized_ac in {"1", "true", "yes", "on", "online"} else (
                False if normalized_ac in {"0", "false", "no", "off", "offline", "disconnected"} else None
            )
        is_virtual = virtualization.get("is_virtual", virtualization.get("virtual", virtualization.get("is_vm")))
        if isinstance(is_virtual, str):
            is_virtual = is_virtual.strip().lower() in {"1", "true", "yes", "on"}
        # A root-collected systemd-detect-virt --vm result is the explicit
        # VM-only exception for guests that expose no AC/battery sysfs.  A
        # reported offline battery remains a hard failure everywhere.
        if ac is False or (ac is None and is_virtual is not True):
            raise ExecutorError("ac_required", "AC power must remain connected during disk mutation.")

        free = self._bytes_value(staging, ("free_bytes", "available_bytes"))
        if free is None:
            free_gib = self._mapping_value(staging, ("free_gib", "available_gib", "free"))
            try:
                if free_gib is not None:
                    free = int(float(free_gib) * (1024**3))
            except (TypeError, ValueError, OverflowError):
                free = None
        if free is None:
            raise ExecutorError("resource_unverified", "Free staging capacity could not be refreshed before mutation.")
        if free < 8 * 1024**3:
            raise ExecutorError("insufficient_staging_space", "Free staging capacity is below the maintenance minimum.")

    def _stage1(
        self,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        record: Mapping[str, Any] | None,
        *,
        without_backup: bool = False,
    ) -> Mapping[str, Any]:
        del artifact  # Artifact verification is owned by InstallerBackend.
        if type(without_backup) is not bool:
            raise ExecutorError(
                "invalid_action", "The backup choice must be an explicit boolean."
            )
        # The backend's target fingerprint intentionally omits volatile AC,
        # RAM, and staging values.  Refresh those values immediately before
        # the first shrink and require the current plan to remain supported.
        self._refresh_prewrite_guard(plan, runner, require_supported=True)
        original, proposal = self._plan_storage(plan, runner)
        disk = self._disk(proposal.get("disk"))

        if without_backup:
            # This is an owner-declined safety copy, not a synthetic receipt.
            # Keep the decision bound to the exact journal plan so a later
            # continuation cannot silently apply it to another target.
            receipt = None
            backup_record = self._owner_declined_backup(plan)
        else:
            receipt = self._read_backup_receipt()
            self._verify_receipt(receipt, plan, proposal)
            backup_record = self._public_receipt(receipt)

        # Capture the exact table from the privileged read-only command.  If
        # preflight supplied one, equality is required; otherwise this exact
        # snapshot is the baseline bound to the backend fingerprint and is
        # retained in the journal for the reboot proof.
        observed_original = self._sfdisk_table(runner, disk)
        expected_original = self._plan_table(plan)
        if expected_original is not None and observed_original != expected_original:
            raise ExecutorError(
                "target_mismatch", "The selected GPT table changed after preflight."
            )
        if original is not None and observed_original != original:
            raise ExecutorError(
                "target_mismatch", "The selected GPT table changed after preflight."
            )
        original = observed_original
        try:
            proposal = storage.layout(original, int(proposal.get("allocation_gib", self._allocation(plan))))
        except (storage.StorageError, TypeError, ValueError, KeyError) as error:
            raise ExecutorError("invalid_plan", "The selected GPT layout is unsupported.") from error
        self._check_plan_geometry(plan, proposal)
        self._verify_stable_ids(plan, original, runner)

        root = self._findmnt_root(runner)
        self._verify_mounted_fedora(root, proposal)
        actual_before, minimum = self._btrfs_bounds(plan, runner)
        if actual_before < minimum:
            raise ExecutorError(
                "shrink_bounds_invalid",
                "The measured Btrfs device size is below its conservative minimum.",
            )
        partition_bytes = int(proposal["fedora_original_size"]) * int(proposal["sector_size"])
        if actual_before > partition_bytes:
            raise ExecutorError(
                "btrfs_exceeds_partition",
                "The measured Btrfs device boundary exceeds Fedora's partition.",
            )
        limit_bytes = int(proposal["fedora_filesystem_limit_bytes"])
        if limit_bytes < minimum:
            raise ExecutorError(
                "insufficient_shrinkable_space",
                "The measured minimum Btrfs size leaves insufficient Fedora capacity.",
            )

        old_boot_id = self._require_boot_id(self.boot_id(), "The current boot identity is unavailable.")
        expected_table = self._expected_table(original, proposal)
        new_guids = self._new_guids()
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "executor": "zeus-dualboot",
            "stage": 1,
            "phase": "prepared",
            "status": "ready",
            "disk": disk,
            "original_table": copy.deepcopy(original),
            "expected_table": copy.deepcopy(expected_table),
            "proposal": copy.deepcopy(proposal),
            "old_boot_id": old_boot_id,
            "new_guids": new_guids,
            "actual_filesystem_bytes_before": actual_before,
            "minimum_filesystem_bytes": minimum,
            "filesystem_limit_bytes": limit_bytes,
        }
        if without_backup:
            state["backup_decision"] = copy.deepcopy(backup_record)
        else:
            state["backup"] = copy.deepcopy(backup_record)
        self._persist_state(journal, state)

        return self._stage1_write_boundary(
            state=state,
            proposal=proposal,
            expected_table=expected_table,
            runner=runner,
            journal=journal,
        )

    def _stage1_write_boundary(
        self,
        *,
        state: dict[str, Any],
        proposal: Mapping[str, Any],
        expected_table: Mapping[str, Any],
        runner: Any,
        journal: Any,
    ) -> Mapping[str, Any]:
        """Perform the live shrink and end-only partition mutation as one hold.

        The inhibitor is acquired before the first durable ``in_progress``
        marker.  If acquisition fails, no mutating storage command is even
        attempted.  It is released only after the reboot-required record has
        been written successfully.
        """

        with self._write_inhibitor():
            self._before_mutation(journal, state, "btrfs_resize")
            self._run_checked(
                runner,
                [BTRFS, "filesystem", "resize", str(state["filesystem_limit_bytes"]), "/"],
                action="Btrfs filesystem shrink",
            )
            self._after_mutation(journal, state, "btrfs_resize")

            self._before_mutation(journal, state, "btrfs_sync")
            self._run_checked(runner, [BTRFS, "filesystem", "sync", "/"], action="Btrfs filesystem sync")
            self._after_mutation(journal, state, "btrfs_sync")

            actual_after = self._btrfs_superblock_size(runner, proposal["fedora_partition"])
            minimum = int(state["minimum_filesystem_bytes"])
            limit_bytes = int(state["filesystem_limit_bytes"])
            if actual_after < minimum or actual_after > limit_bytes:
                raise ExecutorError(
                    "btrfs_boundary_invalid",
                    "The shrunk Btrfs device boundary is outside the recorded safe limit.",
                )
            state["actual_filesystem_bytes"] = actual_after
            state["phase"] = "ready_for_partition_end"
            state["status"] = "complete"
            self._persist_state(journal, state)

            self._before_mutation(journal, state, "partition_end_change")
            try:
                command = storage.shrink_partition_command(proposal, actual_after)
                partition_input = storage.shrink_partition_input(proposal, actual_after)
            except storage.StorageError as error:
                raise ExecutorError("btrfs_boundary_invalid", str(error)) from error
            self._run_checked(
                runner,
                command,
                action="Fedora partition end change",
                input_text=partition_input,
            )

            # Do not create the reboot boundary from the command's exit code
            # alone.  Read the table back and require the exact planned
            # end-only result before recording ``reboot_required``.  A thin
            # runner or a tool that accepted input without changing the disk
            # must leave the journal ambiguous rather than permitting stage2.
            observed_after = self._sfdisk_table(runner, proposal["disk"])
            if observed_after != expected_table:
                raise ExecutorError(
                    "partition_table_unverified",
                    "The Fedora partition end change could not be proved from the GPT table.",
                )

            # This is the reboot boundary.  The expected table and old boot ID
            # are written while the in-progress marker remains in the journal;
            # a crash before this write is intentionally ambiguous and is
            # refused on a later invocation.
            state["phase"] = "reboot_required"
            state["status"] = "complete"
            state["expected_table"] = copy.deepcopy(dict(expected_table))
            state["reboot_required"] = True
            self._persist_state(journal, state)
        return self._reboot_result(state)

    # ------------------------------------------------------------------
    # Stage 2: append, format, install, EFI, and Fedora menu entry
    # ------------------------------------------------------------------

    def _resume_finalization(
        self,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        record: Mapping[str, Any],
        saved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Retry only the two Fedora GRUB operations at the saved boundary."""

        state = copy.deepcopy(dict(saved))
        rendered = self._verify_finalization_runtime(
            plan=plan,
            artifact=artifact,
            runner=runner,
            journal=journal,
            record=record,
            state=state,
        )
        marker = self._verified_boot_chooser_marker(plan, state)
        with self._write_inhibitor():
            # Repeat the read-only target proof after acquiring the inhibitor
            # and immediately before the first write.  This closes the gap
            # between backend preflight and the final Fedora file mutation.
            rendered = self._verify_finalization_runtime(
                plan=plan,
                artifact=artifact,
                runner=runner,
                journal=journal,
                record=record,
                state=state,
            )
            marker = self._verified_boot_chooser_marker(plan, state)
            # A marker write can fail after Fedora's GRUB menu has already
            # been regenerated.  In that bounded case retry only the marker;
            # replaying the two parent-menu mutations would unnecessarily
            # widen the finalization boundary.  Older journals have no marker
            # and retain the original two-action retry behavior.
            marker_only_retry = state.get("action") == "write_boot_chooser_marker"
            if not marker_only_retry:
                self._before_mutation(journal, state, "write_grub_entry")
                self._write_fedora_grub(rendered)
                self._after_mutation(journal, state, "write_grub_entry")
                self._before_mutation(journal, state, "grub_regenerate")
                self._run_checked(
                    runner,
                    [GRUB2_MKCONFIG, "--no-grubenv-update", "-o", str(FEDORA_GRUB_CONFIG)],
                    action="Regenerate Fedora GRUB menu",
                )
                self._after_mutation(journal, state, "grub_regenerate")
            if marker is not None and "write_boot_chooser_marker" not in state.get("completed_actions", []):
                self._before_mutation(journal, state, "write_boot_chooser_marker")
                self._write_boot_chooser_marker(marker)
                self._after_mutation(journal, state, "write_boot_chooser_marker")
            state["phase"] = "installed"
            state["status"] = "complete"
            state["action"] = None
            state["reboot_required"] = False
            state["zeus_esp_uuid"] = state["filesystem_uuids"]["esp"]
            if marker is not None:
                state["boot_chooser_marker"] = copy.deepcopy(marker)
            state["result"] = self._installed_result(state)
            self._persist_state(journal, state)
        return self._installed_result(state)

    def _verify_finalization_runtime(
        self,
        *,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        record: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> str:
        """Run all finalization checks without changing host or journal state."""

        candidate = dict(record)
        candidate["executor_state"] = dict(state)
        if not can_resume_finalization(candidate):
            raise ExecutorError(
                "interrupted",
                "The saved state is not the qualified Fedora GRUB finalization boundary.",
            )
        if self._saved_backup_choice(state, plan) is None:
            raise ExecutorError("invalid_state", "The saved backup choice is incomplete.")
        self._verify_qualified_artifact(artifact)
        saved_artifact = record.get("artifact")
        if not isinstance(saved_artifact, Mapping):
            raise ExecutorError("artifact_invalid", "The saved qualified OCI archive is unavailable.")
        for key in ("path", "build_id", "manifest_digest"):
            if key in saved_artifact and saved_artifact.get(key) != artifact.get(key):
                raise ExecutorError("artifact_invalid", "The saved qualified OCI archive changed.")
        # This is a read-only ownership and mode check.  The artifact is not
        # used by the GRUB retry, but retaining the original verified source
        # as a prerequisite prevents a fabricated finalization request.
        self._artifact_source(artifact, journal)
        self._refresh_prewrite_guard(plan, runner, require_supported=False)
        old_boot = self._require_boot_id(
            state.get("old_boot_id"), "The recorded boot identity is invalid."
        )
        current_boot = self._require_boot_id(
            self.boot_id(), "The current boot identity is unavailable."
        )
        saved_boot = self._require_boot_id(
            state.get("current_boot_id"), "The recorded current boot identity is invalid."
        )
        if old_boot == current_boot or current_boot != saved_boot:
            raise ExecutorError(
                "target_mismatch",
                "The current boot identity does not match the saved finalization boundary.",
            )
        self._verify_finalization_target(plan, runner, state)
        # Validate the preflight-bound Fedora boot identity before any retry
        # write.  The marker itself is written only after GRUB regeneration;
        # this check merely ensures a retry cannot select a different entry or
        # partition from an ambiguous inventory.
        self._verified_boot_chooser_marker(plan, state)
        return self._finalization_rendered(state)

    def _verify_finalization_target(
        self,
        plan: Mapping[str, Any],
        runner: Any,
        state: Mapping[str, Any],
    ) -> None:
        original = state.get("original_table")
        expected = state.get("expected_table")
        allocated = state.get("allocated_table")
        proposal = state.get("proposal")
        guids = state.get("new_guids")
        if (
            not isinstance(original, Mapping)
            or not isinstance(expected, Mapping)
            or not isinstance(allocated, Mapping)
            or not isinstance(proposal, Mapping)
            or not isinstance(guids, list)
        ):
            raise ExecutorError("invalid_state", "The saved finalization layout is incomplete.")
        plan_table = self._plan_table(plan)
        if plan_table is not None and not _finalization_tables_equal(plan_table, original):
            raise ExecutorError("target_mismatch", "The saved Fedora GPT table belongs to another target.")
        plan_proposal = self._find_proposal(plan)
        if isinstance(plan_proposal, Mapping):
            if not _valid_finalization_proposal(plan_proposal, original, expected, guids):
                raise ExecutorError("target_mismatch", "The saved GPT proposal is no longer valid.")
            try:
                self._compare_proposal(proposal, plan_proposal)
            except ExecutorError:
                raise
            except (TypeError, ValueError, KeyError) as error:
                raise ExecutorError("target_mismatch", "The saved GPT proposal is no longer valid.") from error

        disk = self._disk(state.get("disk"))
        current = self._sfdisk_table(runner, disk)
        if not _valid_finalization_table(current, 6) or not _finalization_tables_equal(current, allocated):
            raise ExecutorError("target_mismatch", "The current GPT table no longer matches the saved allocation.")
        try:
            self._verify_allocated_table(current, expected, proposal, guids)
        except ExecutorError:
            raise
        except (TypeError, ValueError, KeyError) as error:
            raise ExecutorError("target_mismatch", "The current GPT allocation is invalid.") from error
        self._verify_finalization_stable_ids(plan, current, runner)
        self._verify_finalization_mounts(runner, state, current)
        self.deployment_root = self._verify_finalization_deployment(state)
        self._verify_finalization_efi_files(state)

    def _verify_finalization_stable_ids(
        self,
        plan: Mapping[str, Any],
        table: Mapping[str, Any],
        runner: Any,
    ) -> None:
        """Recheck the disk and Fedora partition identities on the full GPT."""

        inventory = plan.get("inventory") if isinstance(plan, Mapping) else None
        expected_ptuuid = self._expected_value(
            plan, inventory, ("partition_table_uuid", "gpt_uuid", "ptuuid", "table_uuid")
        )
        if expected_ptuuid is not None and str(table.get("id", "")).lower() != str(expected_ptuuid).lower():
            raise ExecutorError("target_mismatch", "The GPT table identity changed before finalization.")
        expected_partuuid = self._expected_source_identity(plan, inventory)
        parts = table.get("partitions")
        if expected_partuuid is not None and (
            not isinstance(parts, list)
            or len(parts) < 3
            or not isinstance(parts[2], Mapping)
            or str(parts[2].get("uuid", "")).lower() != str(expected_partuuid).lower()
        ):
            raise ExecutorError("target_mismatch", "The Fedora partition identity changed before finalization.")
        expected_disk_id = self._expected_disk_identity(plan, inventory)
        if expected_disk_id is not None:
            current = self._lsblk_disk(runner, str(table.get("device", "")))
            observed = self._disk_identity(current)
            if observed is None or str(observed) != str(expected_disk_id):
                raise ExecutorError("target_mismatch", "The target disk stable identity changed before finalization.")

    def _verify_finalization_mounts(
        self,
        runner: Any,
        state: Mapping[str, Any],
        table: Mapping[str, Any],
    ) -> None:
        filesystem_uuids = state.get("filesystem_uuids")
        parts = table.get("partitions")
        if not isinstance(filesystem_uuids, Mapping) or not isinstance(parts, list) or len(parts) != 6:
            raise ExecutorError("invalid_state", "The saved filesystem identities are incomplete.")
        expected = (
            (TARGET_ROOT, parts[5], filesystem_uuids.get("root"), {"ext4"}),
            (TARGET_BOOT, parts[4], filesystem_uuids.get("boot"), {"ext4"}),
            (TARGET_ESP, parts[3], filesystem_uuids.get("esp"), {"vfat", "fat", "fat32"}),
        )
        for target, part, fs_uuid, types in expected:
            if not isinstance(part, Mapping) or not isinstance(fs_uuid, str):
                raise ExecutorError("invalid_state", "The saved filesystem identities are invalid.")
            result = self._run_checked(
                runner,
                [FINDMNT, "--json", "--target", str(target), "--output", "SOURCE,FSTYPE,UUID,PARTUUID"],
                action="Verify Zeus finalization mount",
            )
            value = self._json_payload(result)
            item: Mapping[str, Any] | None = None
            if isinstance(value, Mapping):
                filesystems = value.get("filesystems")
                if isinstance(filesystems, list) and len(filesystems) == 1 and isinstance(filesystems[0], Mapping):
                    item = filesystems[0]
                elif "source" in value or "SOURCE" in value:
                    item = value
            if item is None:
                raise ExecutorError("mount_invalid", "A Zeus finalization mount could not be verified.")
            source = str(item.get("source", item.get("SOURCE", ""))).split("[", 1)[0]
            fstype = str(item.get("fstype", item.get("FSTYPE", ""))).lower()
            observed_uuid = item.get("uuid", item.get("UUID"))
            observed_partuuid = item.get("partuuid", item.get("PARTUUID"))
            if (
                source != part.get("node")
                or fstype not in types
                or not isinstance(observed_uuid, str)
                or observed_uuid.lower() != fs_uuid.lower()
                or not isinstance(observed_partuuid, str)
                or observed_partuuid.lower() != str(part.get("uuid", "")).lower()
            ):
                raise ExecutorError("mount_invalid", "A Zeus finalization mount identity changed.")

    def _verify_finalization_deployment(self, state: Mapping[str, Any]) -> Path:
        value = state.get("deployment_root")
        if not isinstance(value, str):
            raise ExecutorError("target_unverified", "The saved deployment identity is unavailable.")
        expected = Path(value)
        if (
            not expected.is_absolute()
            or expected.name == ""
            or re.fullmatch(r"[0-9a-f]{64}\.[0-9]+", expected.name) is None
            or expected.parent != TARGET_ROOT / "ostree/deploy/default/deploy"
        ):
            raise ExecutorError("target_unverified", "The saved deployment identity is invalid.")
        resolved = self._resolve_deployment_root()
        if resolved != expected:
            raise ExecutorError("target_mismatch", "The installed deployment identity changed before finalization.")
        return resolved

    @staticmethod
    def _efi_dropin_bytes() -> bytes:
        return (
            "[Unit]\n"
            "RequiresMountsFor=/boot/efi\n"
            "After=boot-efi.mount\n\n"
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=/usr/bin/python3 -I /etc/zeus/efi_update.py --config /etc/zeus/efi-update.json\n"
            "PrivateNetwork=yes\n"
            "ProtectHome=yes\n"
            "KillMode=mixed\n"
            "MountFlags=slave\n"
        ).encode("utf-8")

    def _verify_finalization_efi_files(self, state: Mapping[str, Any]) -> None:
        """Verify the already-installed scoped EFI integration read-only."""

        source = Path(__file__).with_name("efi_update.py").resolve()
        try:
            metadata = os.lstat(source)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (self.require_root and (metadata.st_uid != 0 or metadata.st_mode & 0o022))
                or metadata.st_size > 512 * 1024
            ):
                raise OSError("EFI worker source is not protected")
            source_data = source.read_bytes()
        except OSError as error:
            raise ExecutorError("efi_scope_failed", "The scoped EFI worker source is unavailable.") from error
        wrapper_path = self._target_etc_path(f"zeus/{EFI_WRAPPER_NAME}")
        config_path = self._target_etc_path("zeus/efi-update.json")
        dropin_path = self._target_etc_path(EFI_DROPIN_NAME)
        wrapper = self._read_file(
            wrapper_path,
            limit=512 * 1024,
            code="efi_scope_failed",
            missing_code="file_missing",
        )
        if hashlib.sha256(source_data).hexdigest() != str(state.get("efi_wrapper_sha256", "")).lower():
            raise ExecutorError("efi_scope_failed", "The saved EFI worker identity is invalid.")
        if hashlib.sha256(wrapper.encode("utf-8")).hexdigest() != str(state.get("efi_wrapper_sha256", "")).lower():
            raise ExecutorError("efi_scope_failed", "The installed EFI worker changed before finalization.")
        config_raw = self._read_file(
            config_path,
            limit=64 * 1024,
            code="efi_scope_failed",
            missing_code="file_missing",
        )
        try:
            config = json.loads(config_raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExecutorError("efi_scope_failed", "The installed EFI scope configuration is invalid.") from error
        filesystem_uuids = state.get("filesystem_uuids")
        if (
            not isinstance(config, Mapping)
            or config.get("schema_version") != SCHEMA_VERSION
            or str(config.get("esp_uuid", "")).upper() != str(filesystem_uuids.get("esp", "")).upper()
            or config.get("mountpoint") != "/boot/efi"
        ):
            raise ExecutorError("efi_scope_failed", "The installed EFI scope configuration changed before finalization.")
        dropin = self._read_file(
            dropin_path,
            limit=64 * 1024,
            code="efi_scope_failed",
            missing_code="file_missing",
        )
        if dropin.encode("utf-8") != self._efi_dropin_bytes():
            raise ExecutorError("efi_scope_failed", "The installed EFI service scope changed before finalization.")

    def _finalization_rendered(self, state: Mapping[str, Any]) -> str:
        filesystem_uuids = state.get("filesystem_uuids")
        if not isinstance(filesystem_uuids, Mapping):
            raise ExecutorError("invalid_state", "The saved filesystem identities are incomplete.")
        esp_uuid = filesystem_uuids.get("esp")
        try:
            rendered = bootmenu.render(esp_uuid)
        except (TypeError, ValueError) as error:
            raise ExecutorError("invalid_state", "The saved Zeus ESP identity is invalid.") from error
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if digest.lower() != str(state.get("bootmenu_hash", "")).lower():
            raise ExecutorError("target_mismatch", "The saved Fedora GRUB entry identity changed before finalization.")
        return rendered

    def _derive_boot_chooser_marker(
        self,
        plan: Mapping[str, Any],
        table: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build the installed boot marker from the validated plan only.

        Current preflight plans always carry an ``efi`` inventory object.  A
        handful of older non-root fixture plans predate that field; retaining
        their legacy path is safe because those fixtures do not represent a
        production-qualified installation and cannot write to a host target.
        """

        inventory = plan.get("inventory") if isinstance(plan, Mapping) else None
        if not isinstance(inventory, Mapping):
            if self.require_root:
                raise ExecutorError("target_unverified", "The Fedora preflight inventory is unavailable.")
            return None
        if "efi" not in inventory and not self.require_root:
            return None
        try:
            return bootmenu.derive_boot_chooser_marker(plan, table)
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise ExecutorError(
                "target_mismatch",
                "The validated Fedora boot identities are missing, ambiguous, or inconsistent.",
            ) from error

    def _verified_boot_chooser_marker(
        self,
        plan: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Verify the durable marker inputs and saved marker agree."""

        table = state.get("original_table")
        selected = table if isinstance(table, Mapping) else None
        derived = self._derive_boot_chooser_marker(plan, selected)
        saved = state.get("boot_chooser_marker")
        if saved is not None:
            if not bootmenu.validate_boot_chooser_marker(saved):
                raise ExecutorError("invalid_state", "The saved Fedora boot chooser marker is invalid.")
            if derived is not None and dict(saved) != dict(derived):
                raise ExecutorError(
                    "target_mismatch",
                    "The saved Fedora boot chooser marker no longer matches preflight inventory.",
                )
            return copy.deepcopy(dict(saved))
        return copy.deepcopy(derived) if derived is not None else None

    def _write_boot_chooser_marker(self, marker: Mapping[str, Any]) -> None:
        """Write the root-owned runtime marker through the protected seam."""

        try:
            data = bootmenu.boot_chooser_marker_bytes(marker)
            path = self._target_etc_path("zeus/boot-chooser.json")
            self._write_file(path, data, 0o644)
        except ExecutorError as error:
            raise ExecutorError(
                "boot_chooser_write_failed",
                "The installed Fedora boot chooser marker could not be saved durably.",
            ) from error
        except (TypeError, ValueError, OSError) as error:
            raise ExecutorError(
                "boot_chooser_write_failed",
                "The installed Fedora boot chooser marker could not be saved durably.",
            ) from error

    def _stage2(
        self,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        record: Mapping[str, Any] | None,
        saved: Mapping[str, Any],
        *,
        without_backup: bool = False,
    ) -> Mapping[str, Any]:
        del record
        # The stage-one decision was authenticated before any storage write;
        # validate it again at continuation so a malformed journal cannot
        # turn stage two into an unbound operation.  ``without_backup`` is
        # otherwise intentionally unused here because no receipt is read in
        # stage two.
        persisted_choice = self._saved_backup_choice(saved, plan)
        if persisted_choice is None:
            # Legacy stage-one journals predate the explicit choice and carry
            # no backup record in some fixtures. They already crossed the
            # verified receipt check, so preserve their continuation path.
            return self._stage2_after_choice(
                plan,
                artifact,
                runner,
                journal,
                saved,
                without_backup=without_backup,
            )
        if persisted_choice is False and without_backup:
            raise ExecutorError(
                "backup_choice_mismatch",
                "The recorded installation already selected a verified backup.",
            )
        return self._stage2_after_choice(
            plan,
            artifact,
            runner,
            journal,
            saved,
            without_backup=persisted_choice,
        )

    def _stage2_after_choice(
        self,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
        saved: Mapping[str, Any],
        *,
        without_backup: bool = False,
    ) -> Mapping[str, Any]:
        del without_backup
        # A reboot can leave the machine on battery or with less staging/RAM
        # than preflight observed.  Refresh the volatile write preconditions
        # before taking the second mutation boundary as well.  The exact
        # changed GPT identity is checked below from the durable stage-1
        # record, so this call deliberately does not require a fresh full
        # preflight plan.
        self._refresh_prewrite_guard(plan, runner, require_supported=False)
        state = copy.deepcopy(dict(saved))
        original = state.get("original_table")
        expected = state.get("expected_table")
        proposal = state.get("proposal")
        if not isinstance(original, Mapping) or not isinstance(expected, Mapping) or not isinstance(proposal, Mapping):
            raise ExecutorError("invalid_state", "The reboot boundary record is incomplete.")
        self._check_plan_geometry(plan, proposal)
        disk = self._disk(proposal.get("disk"))
        old_boot = self._require_boot_id(state.get("old_boot_id"), "The recorded boot identity is invalid.")
        current_boot = self._require_boot_id(self.boot_id(), "The current boot identity is unavailable.")
        if current_boot == old_boot:
            raise ExecutorError("reboot_required", "Reboot into Fedora is required before allocating Zeus space.")

        current_table = self._sfdisk_table(runner, disk)
        kernel_size = self._blockdev_size(runner, proposal["fedora_partition"])
        try:
            storage.verify_after_reboot(
                original,
                current_table,
                proposal,
                old_boot,
                current_boot,
                kernel_size,
            )
        except (storage.StorageError, TypeError, ValueError, KeyError) as error:
            raise ExecutorError(
                "target_mismatch",
                "The recorded Fedora partition end or kernel size did not survive reboot.",
            ) from error
        self._verify_stable_ids(plan, current_table, runner)
        self._verify_target_mountpoint_unused(runner)
        # Resolve all Fedora boot identities before appending or formatting a
        # Zeus partition.  The marker is not written yet; it is persisted as
        # immutable source data and emitted only after Fedora GRUB succeeds.
        boot_chooser_marker = self._derive_boot_chooser_marker(plan, original)

        state["stage"] = 2
        state["phase"] = "ready_for_partition_append"
        state["status"] = "ready"
        state["current_boot_id"] = current_boot
        state["kernel_partition_bytes"] = kernel_size
        if boot_chooser_marker is not None:
            state["boot_chooser_marker"] = copy.deepcopy(boot_chooser_marker)
        self._persist_state(journal, state)

        return self._stage2_write_boundary(
            state=state,
            expected=expected,
            proposal=proposal,
            disk=disk,
            artifact=artifact,
            runner=runner,
            journal=journal,
        )

    def _stage2_write_boundary(
        self,
        *,
        state: dict[str, Any],
        expected: Mapping[str, Any],
        proposal: Mapping[str, Any],
        disk: str,
        artifact: Mapping[str, Any],
        runner: Any,
        journal: Any,
    ) -> Mapping[str, Any]:
        """Append and install Zeus while holding one sleep inhibitor."""

        with self._write_inhibitor():
            guids = state.get("new_guids")
            if not self._valid_guid_list(guids):
                raise ExecutorError("invalid_state", "The partition UUID record is invalid.")
            append_text = storage.append_input(proposal, list(guids))
            self._before_mutation(journal, state, "partition_append")
            self._run_checked(
                runner,
                [SFDISK, "--append", "--no-reread", "--no-tell-kernel", "--lock",
                 "--wipe", "never", "--wipe-partitions", "never", disk],
                action="Zeus partition append",
                input_text=append_text,
            )
            self._after_mutation(journal, state, "partition_append")

            self._before_mutation(journal, state, "partition_reread")
            self._run_checked(runner, [PARTX, "--add", "--nr", "4:6", disk], action="Partition table reread")
            self._after_mutation(journal, state, "partition_reread")

            self._before_mutation(journal, state, "udev_settle")
            self._run_checked(runner, [UDEVADM, "settle"], action="Device node settle")
            self._after_mutation(journal, state, "udev_settle")

            allocated = self._sfdisk_table(runner, disk)
            self._verify_allocated_table(allocated, expected, proposal, guids)
            state["phase"] = "allocated_unformatted"
            state["status"] = "complete"
            state["allocated_table"] = copy.deepcopy(allocated)
            self._persist_state(journal, state)

            nodes = [self._partition_node(disk, number) for number in (4, 5, 6)]
            formats = (
                (MKFS_FAT, ["-F", "32", "-n", "ZEUS-ESP"], "esp"),
                (MKFS_EXT4, ["-F", "-L", "ZEUS-BOOT"], "boot"),
                (MKFS_EXT4, ["-F", "-L", "ZEUS-ROOT"], "root"),
            )
            filesystem_uuids: dict[str, str] = {}
            for node, (program, options, role) in zip(nodes, formats):
                self._verify_new_partition_empty(runner, node)
                self._before_mutation(journal, state, f"mkfs_{role}")
                self._run_checked(runner, [program, *options, node], action=f"Format Zeus {role}")
                self._after_mutation(journal, state, f"mkfs_{role}")
                fs_uuid, fs_type = self._blkid_identity(runner, node, expect_formatted=True)
                expected_type = "vfat" if role == "esp" else "ext4"
                if fs_type.lower() not in {expected_type, "fat32" if role == "esp" else expected_type}:
                    raise ExecutorError("filesystem_invalid", f"Zeus {role} filesystem type is invalid.")
                filesystem_uuids[role] = fs_uuid
            state["phase"] = "formatted"
            state["status"] = "complete"
            state["filesystem_uuids"] = copy.deepcopy(filesystem_uuids)
            self._persist_state(journal, state)

            # Only the fixed /target mountpoint may be prepared before the
            # new root is mounted.  Creating /target/boot or /target/boot/efi
            # at this point would modify Fedora's existing root through an
            # ordinary directory and could hide that mistake later.
            self._before_mutation(journal, state, "mkdir_target")
            self._make_target_directories(include_root=True, include_boot=False, include_esp=False)
            self._after_mutation(journal, state, "mkdir_target")
            self._before_mutation(journal, state, "mount_zeus_root")
            self._run_checked(runner, [MOUNT, nodes[2], str(TARGET_ROOT)], action="Mount Zeus root")
            self._after_mutation(journal, state, "mount_zeus_root")
            self._before_mutation(journal, state, "mkdir_target_boot")
            self._make_target_directories(include_root=False, include_boot=True, include_esp=False)
            self._after_mutation(journal, state, "mkdir_target_boot")
            self._before_mutation(journal, state, "mount_zeus_boot")
            self._run_checked(runner, [MOUNT, nodes[1], str(TARGET_BOOT)], action="Mount Zeus boot")
            self._after_mutation(journal, state, "mount_zeus_boot")
            self._before_mutation(journal, state, "mkdir_target_esp")
            self._make_target_directories(include_root=False, include_boot=False, include_esp=True)
            self._after_mutation(journal, state, "mkdir_target_esp")
            self._before_mutation(journal, state, "mount_zeus_esp")
            self._run_checked(runner, [MOUNT, nodes[0], str(TARGET_ESP)], action="Mount Zeus ESP")
            self._after_mutation(journal, state, "mount_zeus_esp")
            self._verify_target_mounts(runner, nodes)
            state["phase"] = "mounted"
            state["status"] = "complete"
            self._persist_state(journal, state)

            source = self._artifact_source(artifact, journal)
            self._before_mutation(journal, state, "bootc_install")
            self._run_checked(
                runner,
                self._bootc_podman_command(source),
                action="Install the verified Zeus OCI into the mounted target",
                timeout=1800,
            )
            self._after_mutation(journal, state, "bootc_install")
            self.deployment_root = self._resolve_deployment_root()
            state["deployment_root"] = str(self.deployment_root)
            self._persist_state(journal, state)

            self._before_mutation(journal, state, "copy_verified_payload")
            self._copy_verified_payload(Path(source))
            self._after_mutation(journal, state, "copy_verified_payload")

            self._before_mutation(journal, state, "cloud_init_seed")
            self._write_cloud_init_seed()
            self._after_mutation(journal, state, "cloud_init_seed")

            # Persist the UUID consumed by the scoped ongoing EFI worker.  A
            # worker supplied by the qualification harness may provide its
            # own atomic writer; otherwise this fixed JSON shape is written
            # through the executor's protected target-file seam.
            self._before_mutation(journal, state, "efi_scope_config")
            self._harden_target_etc()
            self._write_efi_scope_config(filesystem_uuids["esp"])
            self._after_mutation(journal, state, "efi_scope_config")

            self._before_mutation(journal, state, "efi_runtime_integration")
            wrapper_hash = self._install_efi_runtime()
            state["efi_wrapper_sha256"] = wrapper_hash
            state["efi_scope_uuid"] = filesystem_uuids["esp"]
            self._after_mutation(journal, state, "efi_runtime_integration")

            # bootupctl intentionally owns only the already-mounted Zeus ESP.
            # It may unmount that ESP on return, so remount and verify it
            # before writing fstab or performing the final mount checks.
            self._before_mutation(journal, state, "efi_install")
            self._run_checked(
                runner,
                self._bootupctl_podman_command(source),
                action="Install Zeus EFI component",
            )
            self._after_mutation(journal, state, "efi_install")

            self._before_mutation(journal, state, "remount_zeus_esp")
            self._run_checked(runner, [MOUNT, nodes[0], str(TARGET_ESP)], action="Remount Zeus ESP")
            self._after_mutation(journal, state, "remount_zeus_esp")
            self._verify_target_mounts(runner, nodes)

            self._before_mutation(journal, state, "write_fstab")
            self._write_fstab(filesystem_uuids)
            self._after_mutation(journal, state, "write_fstab")
            self._verify_target_mounts(runner, nodes)

            # Fedora's GRUB is updated only after all Zeus resources are
            # complete.  Fedora's existing default remains untouched while
            # the menu is made visible for five seconds.
            esp_uuid = filesystem_uuids["esp"]
            rendered = bootmenu.render(esp_uuid)
            # Bind the removal/rollback record to the exact bytes written to
            # Fedora's managed entry.  The hash is recorded before the write
            # marker and is included in the final result only after the entry
            # and regenerated menu have completed.
            state["bootmenu_hash"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            self._before_mutation(journal, state, "write_grub_entry")
            self._write_fedora_grub(rendered)
            self._after_mutation(journal, state, "write_grub_entry")
            self._before_mutation(journal, state, "grub_regenerate")
            self._run_checked(
                runner,
                [GRUB2_MKCONFIG, "--no-grubenv-update", "-o", str(FEDORA_GRUB_CONFIG)],
                action="Regenerate Fedora GRUB menu",
            )
            self._after_mutation(journal, state, "grub_regenerate")

            # The marker is the final installer mutation.  It records the
            # already validated Fedora entry/partition identities for the
            # installed runtime, and is intentionally created only after the
            # parent Fedora GRUB menu has regenerated successfully.
            boot_chooser_marker = state.get("boot_chooser_marker")
            if boot_chooser_marker is not None:
                self._before_mutation(journal, state, "write_boot_chooser_marker")
                self._write_boot_chooser_marker(boot_chooser_marker)
                self._after_mutation(journal, state, "write_boot_chooser_marker")

            state["phase"] = "installed"
            state["status"] = "complete"
            state["zeus_esp_uuid"] = esp_uuid
            state["result"] = self._installed_result(state)
            self._persist_state(journal, state)
        return self._installed_result(state)

    # ------------------------------------------------------------------
    # Plan, receipt, table, identity, and Btrfs validation
    # ------------------------------------------------------------------

    def _plan_storage(self, plan: Mapping[str, Any], runner: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        candidate_table = self._plan_table(plan)
        proposal_candidate = self._find_proposal(plan)
        allocation = self._allocation(plan)
        if candidate_table is None:
            # Current preflight plans carry the exact target fingerprint and
            # geometry but older fixtures may omit the sfdisk JSON.  Capture
            # the exact table before deriving the storage proposal; the
            # fingerprint and geometry checks still bind it to preflight.
            disk_hint = self._find_disk_hint(plan, proposal_candidate)
            disk = self._disk(disk_hint)
            candidate_table = self._sfdisk_table(runner, disk)
        try:
            proposal = storage.layout(candidate_table, allocation)
        except (storage.StorageError, TypeError, ValueError, KeyError) as error:
            raise ExecutorError("invalid_plan", "The selected GPT layout is unsupported.") from error
        if isinstance(proposal_candidate, Mapping) and self._looks_like_storage_proposal(proposal_candidate):
            self._compare_proposal(proposal, proposal_candidate)
        return candidate_table, proposal

    @staticmethod
    def _looks_like_storage_proposal(value: Mapping[str, Any]) -> bool:
        return isinstance(value.get("partitions"), list) and "fedora_new_size" in value

    def _find_proposal(self, plan: Mapping[str, Any]) -> Mapping[str, Any] | None:
        owners: list[Any] = [plan]
        for key in ("target", "storage", "storage_plan", "proposed_target_layout", "inventory"):
            value = plan.get(key)
            if isinstance(value, Mapping):
                owners.append(value)
        for owner in owners:
            for key in ("storage_proposal", "proposal", "storage_plan", "layout"):
                candidate = owner.get(key) if isinstance(owner, Mapping) else None
                if isinstance(candidate, Mapping):
                    return candidate
            if self._looks_like_storage_proposal(owner):
                return owner
        return None

    def _plan_table(self, plan: Mapping[str, Any]) -> dict[str, Any] | None:
        owners: list[Any] = [plan]
        for key in ("target", "storage", "storage_plan", "proposed_target_layout", "inventory"):
            value = plan.get(key)
            if isinstance(value, Mapping):
                owners.append(value)
        keys = ("sfdisk_table", "partitiontable", "original_table", "original", "table")
        for owner in owners:
            if not isinstance(owner, Mapping):
                continue
            for key in keys:
                value = owner.get(key)
                if isinstance(value, Mapping):
                    nested = value.get("partitiontable")
                    if isinstance(nested, Mapping):
                        value = nested
                    if self._table_shape(value):
                        return copy.deepcopy(dict(value))
            for key in ("sfdisk", "fdisk", "partition_table"):
                nested_owner = owner.get(key)
                if isinstance(nested_owner, Mapping):
                    nested = nested_owner.get("partitiontable", nested_owner.get("table"))
                    if isinstance(nested, Mapping) and self._table_shape(nested):
                        return copy.deepcopy(dict(nested))
        return None

    @staticmethod
    def _table_shape(value: Mapping[str, Any]) -> bool:
        return (
            value.get("label") == "gpt"
            and isinstance(value.get("partitions"), list)
            and len(value.get("partitions", [])) == 3
        )

    def _find_disk_hint(self, plan: Mapping[str, Any], proposal: Mapping[str, Any] | None) -> str:
        if isinstance(proposal, Mapping):
            for key in ("disk", "source_disk"):
                value = proposal.get(key)
                if isinstance(value, Mapping):
                    value = value.get("path")
                if value:
                    return self._disk(value)
        owners: list[Any] = [plan]
        for key in ("target", "storage", "proposed_target_layout", "inventory"):
            value = plan.get(key)
            if isinstance(value, Mapping):
                owners.append(value)
        for owner in owners:
            for key in ("disk", "disk_path", "source_disk", "device"):
                value = owner.get(key)
                if isinstance(value, Mapping):
                    value = value.get("path", value.get("device"))
                if isinstance(value, str) and _DISK_RE.fullmatch(value):
                    return value
        raise ExecutorError("invalid_plan", "The preflight plan has no fixed target disk.")

    @staticmethod
    def _allocation(plan: Mapping[str, Any]) -> int:
        for owner in (plan, plan.get("target", {}), plan.get("proposed_target_layout", {})):
            if isinstance(owner, Mapping) and owner.get("allocation_gib") is not None:
                value = owner.get("allocation_gib")
                if type(value) is int and value >= 64:
                    return value
        raise ExecutorError("invalid_plan", "The Zeus allocation is invalid.")

    def _compare_proposal(self, actual: Mapping[str, Any], supplied: Mapping[str, Any]) -> None:
        fields = (
            "disk",
            "disk_guid",
            "sector_size",
            "fedora_partition",
            "fedora_start",
            "fedora_original_size",
            "fedora_new_size",
            "fedora_filesystem_limit_bytes",
        )
        for field in fields:
            if field in supplied and supplied.get(field) != actual.get(field):
                raise ExecutorError("target_mismatch", "The planned GPT geometry changed.")
        supplied_parts = supplied.get("partitions")
        actual_parts = actual.get("partitions")
        if isinstance(supplied_parts, list):
            if len(supplied_parts) != 3:
                raise ExecutorError("invalid_plan", "Exactly three Zeus partitions are required.")
            for left, right in zip(supplied_parts, actual_parts or []):
                if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                    raise ExecutorError("invalid_plan", "The planned Zeus partition identities are invalid.")
                for field in ("number", "node", "start", "size", "type", "name"):
                    if field in left and left.get(field) != right.get(field):
                        raise ExecutorError("target_mismatch", "The planned Zeus partition geometry changed.")

    def _check_plan_geometry(self, plan: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        target = plan.get("target")
        if not isinstance(target, Mapping):
            target = plan.get("proposed_target_layout")
        if not isinstance(target, Mapping):
            return
        source = target.get("source_partition")
        if isinstance(source, Mapping):
            expected_disk = source.get("path")
            if expected_disk and expected_disk != proposal.get("fedora_partition"):
                raise ExecutorError("target_mismatch", "The Fedora root partition changed after preflight.")
            expected_new = source.get("new_size_bytes")
            if expected_new is not None and int(expected_new) != int(proposal["fedora_new_size"]) * int(proposal["sector_size"]):
                raise ExecutorError("target_mismatch", "The planned Fedora shrink boundary changed.")
            expected_old = source.get("size_bytes")
            if expected_old is not None and int(expected_old) != int(proposal["fedora_original_size"]) * int(proposal["sector_size"]):
                raise ExecutorError("target_mismatch", "The planned Fedora partition size changed.")
        source_disk = target.get("source_disk")
        if isinstance(source_disk, Mapping):
            path = source_disk.get("path")
            if path and path != proposal.get("disk"):
                raise ExecutorError("target_mismatch", "The target disk changed after preflight.")

    def _owner_declined_backup(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Return the truthful journal record for an owner-declined backup."""

        fingerprint = self._fingerprint(plan.get("fingerprint"))
        if fingerprint is None:
            raise ExecutorError(
                "invalid_plan", "The installer plan has no valid target fingerprint."
            )
        return {
            "without_backup": True,
            "verified": False,
            "fingerprint": fingerprint,
        }

    def _read_backup_receipt(self) -> dict[str, Any]:
        try:
            value = self._read_json(
                self.backup_receipt_path,
                code="backup_unverified",
                limit=128 * 1024,
            )
        except ExecutorError as error:
            if error.code == "file_missing":
                # Keep the legacy machine-readable code only in journals
                # created by older helpers. New failures identify the missing
                # backup receipt directly for the owner and GUI.
                raise ExecutorError(
                    "backup_receipt_missing",
                    "A verified backup receipt is required before shrinking; no receipt was found.",
                ) from error
            raise
        if not isinstance(value, Mapping):
            raise ExecutorError("backup_unverified", "A verified backup receipt is required.")
        return dict(value)

    def _verify_receipt(self, receipt: Mapping[str, Any], plan: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        if receipt.get("verified") is not True:
            raise ExecutorError("backup_unverified", "A verified backup receipt is required before shrinking.")
        target = receipt.get("backup_target")
        if not isinstance(target, str) or not target.strip() or "\x00" in target or len(target) > 4096:
            raise ExecutorError("backup_unverified", "The backup receipt has no valid backup target.")
        plan_fp = self._fingerprint(plan.get("fingerprint"))
        for key in ("fingerprint", "target_fingerprint", "inventory_fingerprint"):
            receipt_fp = self._fingerprint(receipt.get(key))
            if receipt_fp is not None and plan_fp is not None and receipt_fp != plan_fp:
                raise ExecutorError("backup_target_mismatch", "The verified backup belongs to another target.")
        table_fp = receipt.get("table_fingerprint")
        if table_fp is not None and str(table_fp).lower() != str(proposal.get("table_fingerprint", "")).lower():
            raise ExecutorError("backup_target_mismatch", "The verified backup table identity does not match.")

    @staticmethod
    def _fingerprint(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if _FINGERPRINT_RE.fullmatch(value) is None:
            return value if value else None
        return value.lower() if value.lower().startswith("sha256:") else "sha256:" + value.lower()

    @staticmethod
    def _public_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"verified": True}
        target = receipt.get("backup_target")
        if isinstance(target, str):
            result["backup_target"] = target[:4096]
        for key in ("fingerprint", "target_fingerprint", "table_fingerprint", "verified_at"):
            value = receipt.get(key)
            if isinstance(value, (str, int, float, bool)):
                result[key] = value
        return result

    def _verify_stable_ids(self, plan: Mapping[str, Any], table: Mapping[str, Any], runner: Any) -> None:
        if not self._table_shape(table):
            raise ExecutorError("target_mismatch", "The exact GPT table could not be verified.")
        inventory = plan.get("inventory") if isinstance(plan, Mapping) else None
        expected_disk_id = self._expected_disk_identity(plan, inventory)
        expected_ptuuid = self._expected_value(plan, inventory, ("partition_table_uuid", "gpt_uuid", "ptuuid", "table_uuid"))
        if expected_ptuuid is not None and str(table.get("id", "")).lower() != str(expected_ptuuid).lower():
            raise ExecutorError("target_mismatch", "The GPT table identity changed after preflight.")
        expected_partuuid = self._expected_source_identity(plan, inventory)
        if expected_partuuid is not None:
            source = table.get("partitions", [None, None, None])[2]
            if not isinstance(source, Mapping) or str(source.get("uuid", "")).lower() != str(expected_partuuid).lower():
                raise ExecutorError("target_mismatch", "The Fedora partition identity changed after preflight.")
        if expected_disk_id is None:
            return
        current = self._lsblk_disk(runner, str(table.get("device", "")))
        observed_id = self._disk_identity(current)
        if observed_id is None or str(observed_id) != str(expected_disk_id):
            raise ExecutorError("target_mismatch", "The target disk stable identity changed after preflight.")

    def _expected_disk_identity(self, plan: Mapping[str, Any], inventory: Any) -> str | None:
        owners = [plan, inventory, plan.get("target") if isinstance(plan, Mapping) else None]
        for owner in owners:
            if not isinstance(owner, Mapping):
                continue
            identity = owner.get("disk_identity")
            if isinstance(identity, Mapping):
                value = identity.get("value")
                if value:
                    return str(value)
            if isinstance(identity, str) and identity:
                return identity
            for key in ("stable_id", "serial", "wwn", "eui", "disk_id"):
                value = owner.get(key)
                if value:
                    return str(value)
            for key in ("disk", "target_disk", "source_disk"):
                nested = owner.get(key)
                if isinstance(nested, Mapping):
                    value = self._disk_identity(nested)
                    if value:
                        return value
        # Collected preflight inventories put the stable value on the
        # ``block_devices`` disk record.  Walk that shape explicitly rather
        # than trusting whichever top-level metadata a fixture happened to
        # expose.
        if isinstance(inventory, Mapping):
            records = self._flatten_devices(
                inventory.get("block_devices", inventory.get("devices", inventory.get("lsblk")))
            )
            disks = [item for item in records if str(item.get("type", "")).lower() in {"disk", "nvme", "mmc"}]
            if len(disks) == 1:
                return self._disk_identity(disks[0])
            for item in disks:
                if self._disk_identity(item):
                    return self._disk_identity(item)
        return None

    @staticmethod
    def _disk_identity(value: Mapping[str, Any] | None) -> str | None:
        if not isinstance(value, Mapping):
            return None
        for key in ("stable_id", "stableid", "serial", "wwn", "eui", "disk_id", "by_id", "identity", "id"):
            candidate = value.get(key)
            if candidate is None:
                continue
            text = str(candidate).strip()
            if text and not text.startswith("/dev/") and text.lower() not in {"unknown", "none", "null", "-"}:
                return text
        return None

    def _expected_value(self, plan: Mapping[str, Any], inventory: Any, keys: Sequence[str]) -> Any:
        for owner in (plan, inventory, plan.get("target") if isinstance(plan, Mapping) else None):
            if not isinstance(owner, Mapping):
                continue
            for key in keys:
                value = owner.get(key)
                if value is not None:
                    return value
            for key in ("disk", "target_disk", "source_disk"):
                nested = owner.get(key)
                if isinstance(nested, Mapping):
                    for candidate in keys:
                        value = nested.get(candidate)
                        if value is not None:
                            return value
        return None

    def _expected_source_identity(self, plan: Mapping[str, Any], inventory: Any) -> str | None:
        source_paths: set[str] = set()
        for owner in (plan, inventory, plan.get("target") if isinstance(plan, Mapping) else None):
            if not isinstance(owner, Mapping):
                continue
            source = owner.get("source_partition")
            if isinstance(source, Mapping):
                for key in ("partuuid", "partition_uuid", "uuid", "filesystem_uuid"):
                    value = source.get(key)
                    if value:
                        path = source.get("path", source.get("device"))
                        if isinstance(path, str):
                            source_paths.add(path)
                        return str(value)
            for key in ("fedora_partition_uuid", "fedora_partuuid", "source_partuuid"):
                value = owner.get(key)
                if value:
                    return str(value)
            for key in ("fedora_partition", "source_partition_path"):
                value = owner.get(key)
                if isinstance(value, str):
                    source_paths.add(value)
        if isinstance(inventory, Mapping):
            records = self._flatten_devices(
                inventory.get("block_devices", inventory.get("devices", inventory.get("lsblk")))
            )
            for item in records:
                path = self._device_path(item)
                partn = item.get("partn", item.get("partition_number", item.get("number")))
                if source_paths and path not in source_paths:
                    continue
                if not source_paths and str(partn) != "3":
                    continue
                value = item.get("partuuid", item.get("partition_uuid", item.get("uuid")))
                if value:
                    text = str(value).strip()
                    if text and not text.startswith("/dev/"):
                        return text
        return None

    def _lsblk_disk(self, runner: Any, disk: str) -> Mapping[str, Any]:
        result = self._run_checked(
            runner,
            [LSBLK, "--bytes", "--json", "--output", "NAME,KNAME,PATH,TYPE,SIZE,UUID,PARTUUID,PARTTYPE,PTTYPE,PTUUID,START,PARTN,LOG-SEC,SERIAL,WWN"],
            action="Read stable disk identity",
        )
        payload = self._stdout(result)
        if isinstance(payload, Mapping):
            value: Any = payload
        else:
            try:
                value = json.loads(payload or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ExecutorError("target_unverified", "The disk stable identity could not be read.") from error
        records = self._flatten_devices(value)
        for item in records:
            if self._device_path(item) == disk:
                return item
        if self._device_path(value) == disk:
            return value
        raise ExecutorError("target_unverified", "The target disk stable identity could not be read.")

    def _btrfs_bounds(self, plan: Mapping[str, Any], runner: Any) -> tuple[int, int]:
        candidates: list[Any] = []
        inventory = plan.get("inventory")
        target = plan.get("target")
        for owner in (inventory, target, plan.get("proposed_target_layout")):
            if isinstance(owner, Mapping):
                value = owner.get("btrfs")
                if isinstance(value, Mapping):
                    candidates.append(value)
                source = owner.get("source_partition")
                if isinstance(source, Mapping):
                    candidates.append(source)
        actual = self._first_bytes(candidates, ("device_size_bytes", "btrfs_device_size_bytes", "device_size", "total_bytes", "filesystem_size"))
        minimum = self._first_bytes(candidates, ("minimum_size_bytes", "minimum_size", "min_size_bytes", "min_size"))
        count = self._first_int(candidates, ("device_count", "total_devices", "num_devices"))
        if count is not None and count != 1:
            raise ExecutorError("multi_device_btrfs", "Only a single-device Fedora Btrfs filesystem is supported.")
        if actual is None or minimum is None or count is None:
            measured = self._measure_btrfs(runner)
            actual = actual if actual is not None else measured[0]
            minimum = minimum if minimum is not None else measured[1]
            count = count if count is not None else measured[2]
        if actual is None or minimum is None or count != 1:
            raise ExecutorError("shrink_bounds_unknown", "Measured single-device Btrfs bounds are required.")
        return int(actual), int(minimum)

    def _measure_btrfs(self, runner: Any) -> tuple[int | None, int | None, int | None]:
        usage = self._run_checked(
            runner,
            [BTRFS, "filesystem", "usage", "--raw", "--json", "/"],
            action="Read Btrfs usage",
        )
        show = self._run_checked(
            runner,
            [BTRFS, "filesystem", "show", "--raw", "--json", "/"],
            action="Read Btrfs devices",
        )
        usage_value = self._json_payload(usage)
        show_value = self._json_payload(show)
        actual = self._recursive_number(usage_value, ("device_size", "device_size_bytes", "total_bytes", "filesystem_size"))
        minimum = self._recursive_number(usage_value, ("minimum_size", "minimum_size_bytes", "min_size", "min_size_bytes"))
        count = self._recursive_int(show_value, ("device_count", "total_devices", "num_devices"))
        if count is None:
            devices = self._recursive_list(show_value, "devices")
            count = len(devices) if devices else None
        if actual is None:
            devices = self._recursive_list(show_value, "devices")
            sizes = [self._recursive_number(item, ("size", "device_size", "bytes")) for item in devices]
            if sizes and all(value is not None for value in sizes):
                actual = sum(int(value) for value in sizes if value is not None)
        return actual, minimum, count

    def _findmnt_root(self, runner: Any) -> Mapping[str, Any]:
        result = self._run_checked(
            runner,
            [FINDMNT, "--json", "--target", "/", "--output", "SOURCE,FSTYPE,UUID,PARTUUID"],
            action="Verify mounted Fedora root",
        )
        value = self._json_payload(result)
        if isinstance(value, Mapping):
            filesystems = value.get("filesystems")
            if isinstance(filesystems, list) and filesystems and isinstance(filesystems[0], Mapping):
                return filesystems[0]
            if "fstype" in value or "FSTYPE" in value:
                return value
        raise ExecutorError("target_unverified", "The mounted Fedora root could not be verified.")

    def _verify_mounted_fedora(self, mounted: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        fstype = str(mounted.get("fstype", mounted.get("FSTYPE", ""))).lower()
        source = str(mounted.get("source", mounted.get("SOURCE", ""))).split("[", 1)[0]
        if fstype != "btrfs" or source != proposal.get("fedora_partition"):
            raise ExecutorError(
                "target_unverified",
                "Fedora root must be the recorded mounted single-device Btrfs partition.",
            )

    def _btrfs_superblock_size(self, runner: Any, partition: str) -> int:
        result = self._run_checked(
            runner,
            [BTRFS, "inspect-internal", "dump-super", partition],
            action="Verify Btrfs device boundary",
        )
        text = self._stdout(result)
        matches = re.findall(r"^\s*dev_item\.total_bytes\s*(?:=|:)??\s*([0-9]+)\s*$", text, re.MULTILINE)
        if len(matches) != 1:
            # dump-super versions differ in whether an equals sign is shown.
            matches = re.findall(r"dev_item\.total_bytes\s*(?:=|:)\s*([0-9]+)", text)
        if len(matches) != 1:
            raise ExecutorError("btrfs_boundary_invalid", "The Btrfs device boundary could not be proved.")
        return int(matches[0])

    # ------------------------------------------------------------------
    # Stage 2 partition and filesystem helpers
    # ------------------------------------------------------------------

    def _verify_allocated_table(
        self,
        current: Mapping[str, Any],
        expected: Mapping[str, Any],
        proposal: Mapping[str, Any],
        guids: Sequence[str],
    ) -> None:
        expected_existing = list(expected.get("partitions", []))
        current_parts = current.get("partitions")
        if not isinstance(current_parts, list) or len(current_parts) != 6:
            raise ExecutorError("target_mismatch", "The GPT append did not create exactly three Zeus partitions.")
        if current.get("label") != expected.get("label") or current.get("id") != expected.get("id"):
            raise ExecutorError("target_mismatch", "The existing GPT identity changed during append.")
        if current_parts[:3] != expected_existing[:3]:
            raise ExecutorError("target_mismatch", "Fedora partition geometry changed during append.")
        for part, expected_part, guid in zip(current_parts[3:], proposal.get("partitions", []), guids):
            if not isinstance(part, Mapping) or not isinstance(expected_part, Mapping):
                raise ExecutorError("target_mismatch", "The new partition record is invalid.")
            if (
                int(part.get("start", -1)),
                int(part.get("size", -1)),
                str(part.get("type", "")).upper(),
                str(part.get("uuid", "")).lower(),
            ) != (
                int(expected_part.get("start", -2)),
                int(expected_part.get("size", -2)),
                str(expected_part.get("type", "")).upper(),
                str(guid).lower(),
            ):
                raise ExecutorError("target_mismatch", "The new partition identity or geometry is not owned by Zeus.")

    def _verify_new_partition_empty(self, runner: Any, node: str) -> None:
        result = self._run_checked(
            runner,
            [BLKID, "--probe", "--output", "export", node],
            action="Verify new Zeus partition is empty",
            accepted_returncodes=(0, 2),
        )
        text = self._stdout(result)
        if isinstance(result, Mapping):
            for key in ("TYPE", "FSTYPE", "UUID", "LABEL", "type", "fstype", "uuid", "label"):
                if result.get(key):
                    raise ExecutorError("target_not_empty", "A new Zeus partition already contains a filesystem.")
        # A GPT partition may produce PART_ENTRY_* metadata.  A filesystem
        # TYPE/UUID/LABEL means formatting could destroy pre-existing data.
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key in {"TYPE", "UUID", "LABEL", "FSTYPE"} and value:
                raise ExecutorError("target_not_empty", "A new Zeus partition already contains a filesystem.")

    def _blkid_identity(self, runner: Any, node: str, *, expect_formatted: bool) -> tuple[str, str]:
        result = self._run_checked(
            runner,
            [BLKID, "--probe", "--output", "export", node],
            action="Verify Zeus filesystem identity",
        )
        values: dict[str, str] = {}
        if isinstance(result, Mapping):
            for key in ("UUID", "uuid", "TYPE", "type", "FSTYPE", "fstype"):
                value = result.get(key)
                if value is not None:
                    values[key.upper()] = str(value)
        for line in self._stdout(result).splitlines():
            key, separator, value = line.partition("=")
            if separator and key and value:
                values[key] = value
        fs_uuid = values.get("UUID")
        fs_type = values.get("TYPE", values.get("FSTYPE", ""))
        if not isinstance(fs_uuid, str) or not fs_uuid or not isinstance(fs_type, str) or not fs_type:
            raise ExecutorError("filesystem_invalid", "The Zeus filesystem identity could not be verified.")
        if expect_formatted and not (_UUID_RE.fullmatch(fs_uuid) or _FAT_UUID_RE.fullmatch(fs_uuid)):
            raise ExecutorError("filesystem_invalid", "The Zeus filesystem UUID is invalid.")
        return (fs_uuid.upper() if _FAT_UUID_RE.fullmatch(fs_uuid) else str(uuid.UUID(fs_uuid))), fs_type

    def _make_target_directories(
        self,
        *,
        include_root: bool = True,
        include_boot: bool = True,
        include_esp: bool = True,
    ) -> None:
        paths: list[Path] = []
        if include_root:
            paths.append(TARGET_ROOT)
        if include_boot:
            paths.append(TARGET_BOOT)
        if include_esp:
            paths.append(TARGET_ESP)
        for path in paths:
            if self.writer is not None:
                continue
            self._mkdir_fixed(path)

    def _verify_target_mountpoint_unused(self, runner: Any) -> None:
        """Refuse to mount over an unrelated pre-existing /target mount."""

        result = self._run_checked(
            runner,
            [FINDMNT, "--json", "--mountpoint", str(TARGET_ROOT), "--output", "SOURCE,FSTYPE,UUID,PARTUUID"],
            action="Verify the Zeus target mountpoint is unused",
            # util-linux findmnt returns 1 when the target has no mounted
            # filesystem.  That documented read-only result is the expected
            # empty state; every other non-zero result remains fatal.
            accepted_returncodes=(0, 1),
        )
        raw = self._stdout(result).strip()
        # findmnt emits no JSON at all for an unmounted target and exits 1
        # on util-linux versions used by Fedora.  Treat that exact empty
        # result as the proven empty state; malformed non-empty output still
        # fails closed through _json_payload below.
        value = {"filesystems": []} if self._returncode(result) == 1 and not raw else self._json_payload(result)
        if (
            not isinstance(value, Mapping)
            or "filesystems" not in value
            or not isinstance(value.get("filesystems"), list)
        ):
            raise ExecutorError("mount_invalid", "The fixed Zeus target mountpoint could not be verified.")
        filesystems = value["filesystems"]
        if filesystems:
            raise ExecutorError("mount_invalid", "The fixed Zeus target mountpoint is already mounted.")
        if self.writer is None and TARGET_ROOT.exists():
            if TARGET_ROOT.is_symlink() or not TARGET_ROOT.is_dir() or any(TARGET_ROOT.iterdir()):
                raise ExecutorError("mount_invalid", "The fixed Zeus target directory is not empty.")

    def _verify_target_mounts(self, runner: Any, nodes: Sequence[str]) -> None:
        expected = (
            (TARGET_ROOT, nodes[2], {"ext4"}),
            (TARGET_BOOT, nodes[1], {"ext4"}),
            (TARGET_ESP, nodes[0], {"vfat", "fat", "fat32"}),
        )
        for target, node, types in expected:
            result = self._run_checked(
                runner,
                [FINDMNT, "--json", "--target", str(target), "--output", "SOURCE,FSTYPE,UUID,PARTUUID"],
                action="Verify Zeus mount",
            )
            value = self._json_payload(result)
            item: Mapping[str, Any] | None = None
            if isinstance(value, Mapping):
                filesystems = value.get("filesystems")
                if isinstance(filesystems, list) and filesystems and isinstance(filesystems[0], Mapping):
                    item = filesystems[0]
                elif "source" in value or "SOURCE" in value:
                    item = value
            if item is None:
                raise ExecutorError("mount_invalid", "A Zeus mount could not be verified.")
            source = str(item.get("source", item.get("SOURCE", ""))).split("[", 1)[0]
            fstype = str(item.get("fstype", item.get("FSTYPE", ""))).lower()
            if source != node or fstype not in types:
                raise ExecutorError("mount_invalid", "A Zeus mount points at an unexpected filesystem.")

    def _artifact_source(self, artifact: Mapping[str, Any], journal: Any) -> str:
        value = artifact.get("path")
        if not isinstance(value, str) or not value:
            raise ExecutorError("artifact_invalid", "The verified OCI archive path is missing.")
        path = self._absolute_path(value, code="artifact_invalid", message="The verified OCI archive path is invalid.")
        expected_parent = getattr(journal, "artifacts_path", None)
        if expected_parent is not None and path.parent != Path(expected_parent):
            raise ExecutorError("artifact_invalid", "The OCI archive is outside installer-owned storage.")
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise ExecutorError("artifact_invalid", "The verified OCI archive is unavailable.") from error
        expected_owner = 0 if self.require_root else os.geteuid()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != expected_owner or metadata.st_mode & 0o077:
            raise ExecutorError("artifact_invalid", "The verified OCI archive is not protected.")
        return str(path)

    @staticmethod
    def _bootc_podman_command(source: str) -> list[str]:
        # The target image and archive are both handled through Podman.  The
        # bootc command is intentionally restricted to to-filesystem, with a
        # local verified source and no fetch.  The target mount is shared so
        # bootc's private mount namespace can complete its setup.
        return [
            PODMAN,
            "run",
            "--rm",
            "--privileged",
            "--pid=host",
            "--network=none",
            "--security-opt",
            "label=disable",
            "--mount",
            "type=bind,src=/target,dst=/target,rw,bind-propagation=rshared",
            "--mount",
            f"type=bind,src={source},dst=/run/zeus/source.oci,ro",
            "--pull=never",
            "--entrypoint",
            "/usr/bin/bootc",
            f"oci-archive:{source}",
            "install",
            "to-filesystem",
            "--bootloader",
            "none",
            "--skip-finalize",
            "--skip-fetch-check",
            "--stateroot",
            "default",
            "--target-transport",
            "oci-archive",
            "--target-imgref",
            TARGET_IMAGE_REF,
            "--source-imgref",
            "oci-archive:/run/zeus/source.oci",
            "/target",
        ]

    @staticmethod
    def _bootupctl_podman_command(source: str) -> list[str]:
        """Run initial EFI installation from the pinned Zeus image.

        Fedora's host image is not required to carry the matching bootupd
        binary.  The already verified Zeus archive is therefore used as the
        privileged Podman image, with the mounted target recursively shared
        into its namespace.  The inner command has no disk/filesystem selector
        and can only reuse the premounted Zeus ESP at ``/target/boot/efi``.
        """

        return [
            PODMAN,
            "run",
            "--rm",
            "--privileged",
            "--pid=host",
            "--network=none",
            "--security-opt",
            "label=disable",
            "--mount",
            "type=bind,src=/target,dst=/target,rw,bind-propagation=rshared",
            "--mount",
            f"type=bind,src={source},dst=/run/zeus/source.oci,ro",
            "--pull=never",
            "--entrypoint",
            BOOTUPCTL,
            f"oci-archive:{source}",
            "backend",
            "install",
            "--component",
            "EFI",
            "--write-uuid",
            str(TARGET_ROOT),
        ]

    def _resolve_deployment_root(self) -> Path:
        """Select the one new deployment in the explicitly named stateroot.

        This target was freshly formatted. Multiple deployments, symlinks, or
        an unexpected directory are therefore an installation error.
        """
        parent = TARGET_ROOT / "ostree/deploy/default/deploy"
        expected_owner = 0 if self.require_root else os.geteuid()
        try:
            current = TARGET_ROOT
            for part in ("", *parent.relative_to(TARGET_ROOT).parts):
                current = current / part
                metadata = os.lstat(current)
                if (not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_uid != expected_owner
                        or metadata.st_mode & 0o022):
                    raise OSError("unsafe deployment parent")
            deployments = []
            for entry in parent.iterdir():
                metadata = os.lstat(entry)
                if stat.S_ISREG(metadata.st_mode):
                    continue  # OSTree origin records live alongside deployments.
                if (not stat.S_ISDIR(metadata.st_mode)
                        or re.fullmatch(r"[0-9a-f]{64}\.[0-9]+", entry.name) is None
                        or metadata.st_uid != expected_owner
                        or metadata.st_mode & 0o022):
                    raise OSError("unsafe deployment directory")
                deployments.append(entry)
            if len(deployments) != 1:
                raise OSError("ambiguous fresh deployment")
            return deployments[0]
        except OSError as error:
            raise ExecutorError("target_unverified", "The installed OSTree deployment could not be uniquely verified.") from error

    def _target_etc_path(self, name: str) -> Path:
        if not isinstance(name, str):
            raise ExecutorError("unsafe_storage", "The target configuration filename is invalid.")
        relative = Path(name)
        if (
            relative.is_absolute()
            or not relative.parts
            or len(relative.parts) > 4
            or any(
                part in {"", ".", ".."} or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", part) is None
                for part in relative.parts
            )
        ):
            raise ExecutorError("unsafe_storage", "The target configuration filename is invalid.")
        if self.deployment_root is None:
            raise ExecutorError("invalid_state", "The installed OSTree deployment has not been resolved.")
        return self.deployment_root / "etc" / relative

    def _copy_verified_payload(self, source: Path) -> None:
        """Keep the exact verified archive for the installed OS origin."""

        target = TARGET_PAYLOAD
        if self.writer is not None:
            try:
                self.writer(target, source.read_bytes(), 0o600)
            except (OSError, ValueError, TypeError) as error:
                raise ExecutorError("state_write_failed", "The verified OCI archive could not be copied into Zeus.") from error
            return
        self._mkdir_fixed(target.parent)
        try:
            downloads_metadata = target.parent.lstat()
            if downloads_metadata.st_uid != 0 or downloads_metadata.st_mode & 0o077:
                raise OSError("updater downloads directory is not private")
            # Only these shared status directories are public. The archive and
            # downloads directory retain their private permissions.
            for public_directory in (target.parent.parent.parent, target.parent.parent):
                metadata = public_directory.lstat()
                if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                    raise OSError("updater status directory is not protected")
                public_directory.chmod(0o755)
            source_metadata = os.lstat(source)
            if (
                stat.S_ISLNK(source_metadata.st_mode)
                or not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_mode & 0o077
            ):
                raise OSError("source archive is not protected")
            try:
                existing = os.lstat(target)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                raise OSError("target archive already exists")
            temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
            descriptor = None
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.fchmod(descriptor, 0o600)
                with open(source, "rb", buffering=0) as source_stream:
                    while True:
                        chunk = source_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short archive write")
                            view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(temporary, target)
                directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except OSError as error:
            raise ExecutorError("state_write_failed", "The verified OCI archive could not be copied into Zeus.") from error

    # ------------------------------------------------------------------
    # Journal state and command-runner seams
    # ------------------------------------------------------------------

    @contextmanager
    def _write_inhibitor(self):
        """Hold a fixed sleep/shutdown inhibitor over each write phase.

        Closing a laptop lid or an unattended shutdown during a live Btrfs
        resize or partition-table change would leave an ambiguous boundary.
        The helper uses ``systemd-inhibit`` with a private ``cat`` process and
        performs a one-byte readiness round trip before yielding.  Tests and
        disposable fixtures inject a context manager through ``inhibitor``;
        production cannot disable the hold by changing a plan field.
        """

        supplied = self.inhibitor
        if supplied is not None:
            try:
                token = supplied if hasattr(supplied, "__enter__") else supplied()
                if hasattr(token, "__enter__") and hasattr(token, "__exit__"):
                    with token:
                        yield
                else:
                    # A fixture factory may return a plain release callback;
                    # the factory invocation itself is the acquisition point.
                    try:
                        yield
                    finally:
                        self._release_injected_inhibitor(token)
            except ExecutorError:
                raise
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                raise ExecutorError(
                    "inhibitor_unavailable",
                    "The system sleep/shutdown inhibitor could not be acquired.",
                ) from error
            return

        if not self.require_root:
            # ``require_root=False`` is an explicit non-production test seam.
            yield
            return

        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [
                    SYSTEMD_INHIBIT,
                    "--what=sleep:shutdown",
                    "--mode=block",
                    CAT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if process.stdin is None or process.stdout is None:
                raise OSError("inhibitor pipes unavailable")
            process.stdin.write(b"\n")
            process.stdin.flush()
            ready_fds, _, _ = select.select([process.stdout], [], [], 5.0)
            if not ready_fds:
                raise OSError("inhibitor readiness timed out")
            ready = process.stdout.read(1)
            if ready != b"\n" or process.poll() is not None:
                raise OSError("inhibitor readiness failed")
            yield
        except ExecutorError:
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            raise ExecutorError(
                "inhibitor_unavailable",
                "The system sleep/shutdown inhibitor could not be acquired.",
            ) from error
        finally:
            if process is not None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass

    @staticmethod
    def _release_injected_inhibitor(token: Any) -> None:
        if callable(token):
            token()

    def _load_record(self, journal: Any) -> Mapping[str, Any] | None:
        try:
            value = journal.load()
        except InstallError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise ExecutorError("invalid_state", "The installer journal could not be read.") from error
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _saved_state(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if not isinstance(record, Mapping):
            return None
        for key in ("executor_state", "dualboot_state"):
            value = record.get(key)
            if isinstance(value, Mapping):
                return value
        result = record.get("executor_result")
        if isinstance(result, Mapping):
            for key in ("executor_state", "dualboot_state"):
                value = result.get(key)
                if isinstance(value, Mapping):
                    return value
            if result.get("executor") == "zeus-dualboot" and isinstance(result.get("state"), Mapping):
                return result["state"]
        return None

    def _persist_state(self, journal: Any, state: Mapping[str, Any]) -> None:
        try:
            record = journal.load()
        except InstallError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise ExecutorError("invalid_state", "The installer journal could not be read.") from error
        updated = copy.deepcopy(dict(record)) if isinstance(record, Mapping) else {}
        updated["executor_state"] = copy.deepcopy(dict(state))
        updated["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            journal.write(updated)
        except InstallError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise ExecutorError("state_write_failed", "The installer state could not be saved durably.") from error

    def _before_mutation(self, journal: Any, state: dict[str, Any], action: str) -> None:
        state["status"] = "in_progress"
        state["action"] = action
        self._persist_state(journal, state)

    def _after_mutation(self, journal: Any, state: dict[str, Any], action: str) -> None:
        state["status"] = "complete"
        state["action"] = None
        completed = state.setdefault("completed_actions", [])
        if isinstance(completed, list) and action not in completed:
            completed.append(action)
        self._persist_state(journal, state)

    @staticmethod
    def _reject_ambiguous(state: Mapping[str, Any]) -> None:
        if state.get("status") == "in_progress":
            raise ExecutorError(
                "interrupted",
                "An earlier dual-boot command may have run; review the fixture before retrying.",
            )

    def _run_checked(
        self,
        runner: Any,
        argv: Sequence[str],
        *,
        action: str,
        input_text: str | None = None,
        timeout: float = 120.0,
        accepted_returncodes: Sequence[int] = (0,),
    ) -> Any:
        args = self._validate_argv(argv)
        method = getattr(runner, "run", None)
        if not callable(method):
            if callable(runner):
                method = runner
            else:
                raise ExecutorError("invalid_command", "The maintenance command runner is unavailable.")
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "accepted_returncodes": tuple(accepted_returncodes),
        }
        if input_text is not None:
            kwargs["input"] = input_text
        try:
            try:
                result = method(args, **kwargs)
            except TypeError as first:
                # The production CommandRunner implements the typed
                # ``accepted_returncodes`` contract.  Keep a narrow fixture
                # seam for tiny runners that predate that keyword; partition
                # input still requires either ``input=`` or run_with_input.
                fallback = {"timeout": timeout}
                if input_text is not None:
                    fallback["input"] = input_text
                try:
                    result = method(args, **fallback)
                except TypeError:
                    if input_text is None:
                        raise first
                    alternate = getattr(runner, "run_with_input", None)
                    if not callable(alternate):
                        raise ExecutorError(
                            "invalid_command",
                            "The maintenance command runner cannot accept partition input.",
                        ) from first
                    result = alternate(
                        args,
                        input=input_text,
                        timeout=timeout,
                    )
        except ExecutorError:
            raise
        except (subprocess.SubprocessError, OSError, ValueError, TypeError) as error:
            raise ExecutorError("command_failed", f"{action} failed safely.") from error
        code = self._returncode(result)
        if code not in accepted_returncodes:
            raise ExecutorError("command_failed", f"{action} failed safely.")
        return result

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> list[str]:
        if isinstance(argv, (str, bytes, bytearray)) or not isinstance(argv, (list, tuple)) or not argv:
            raise ExecutorError("invalid_command", "Privileged commands require a fixed argument array.")
        result: list[str] = []
        for item in argv:
            if not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096:
                raise ExecutorError("invalid_command", "Privileged command arguments are invalid.")
            result.append(item)
        # CommandRunner performs the authoritative allowlist check.  This
        # local check catches accidental relative paths before that boundary.
        if not result[0].startswith("/"):
            raise ExecutorError("invalid_command", "Privileged commands require absolute executable paths.")
        return result

    @staticmethod
    def _returncode(result: Any) -> int:
        if result is None:
            return 0
        if isinstance(result, Mapping):
            value = result.get("returncode", result.get("code", 0))
        elif isinstance(result, (tuple, list)):
            value = result[0] if result else 0
        else:
            value = getattr(result, "returncode", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _stdout(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, Mapping):
            value = result.get("stdout", result.get("output", ""))
        elif isinstance(result, (tuple, list)):
            value = result[1] if len(result) > 1 else ""
        else:
            value = getattr(result, "stdout", "")
        if isinstance(value, Mapping):
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")[:_MAX_OUTPUT]

    @classmethod
    def _json_payload(cls, result: Any) -> Any:
        raw = cls._stdout(result)
        if isinstance(result, Mapping) and any(key in result for key in ("partitiontable", "filesystems", "blockdevices")):
            return result
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExecutorError("target_unverified", "A maintenance inspection returned invalid JSON.") from error
        return value

    def _sfdisk_table(self, runner: Any, disk: str) -> dict[str, Any]:
        result = self._run_checked(runner, [SFDISK, "--json", disk], action="Read exact GPT table")
        value = self._json_payload(result)
        if isinstance(value, Mapping):
            table = value.get("partitiontable", value.get("table"))
            if isinstance(table, Mapping):
                return copy.deepcopy(dict(table))
            if self._table_shape(value):
                return copy.deepcopy(dict(value))
        raise ExecutorError("target_unverified", "The exact GPT table could not be read.")

    def _blockdev_size(self, runner: Any, partition: str) -> int:
        result = self._run_checked(runner, [BLOCKDEV, "--getsize64", partition], action="Read Fedora kernel partition size")
        raw = self._stdout(result).strip()
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise ExecutorError("target_unverified", "The kernel Fedora partition size is invalid.") from error
        if value <= 0:
            raise ExecutorError("target_unverified", "The kernel Fedora partition size is invalid.")
        return value

    # ------------------------------------------------------------------
    # Fixed-path file handling and owner seed
    # ------------------------------------------------------------------

    def _read_json(self, path: Path, *, code: str, limit: int) -> Any:
        raw = self._read_file(path, limit=limit, code=code, private=True)
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExecutorError(code, "The backup receipt is not valid JSON.") from error

    def _read_file(self, path: Path, *, limit: int, code: str, missing_code: str | None = None, private: bool = False) -> str:
        if self.reader is not None:
            try:
                value = self.reader(path)
            except (OSError, ValueError, TypeError) as error:
                raise ExecutorError(code, "The required installer file is unavailable.") from error
            if value is None:
                raise ExecutorError(missing_code or code, "The required installer file is unavailable.")
            if isinstance(value, bytes):
                raw = value[: limit + 1]
                if len(raw) > limit:
                    raise ExecutorError(code, "The required installer file is too large.")
                return raw.decode("utf-8")
            text = str(value)
            if len(text.encode("utf-8")) > limit:
                raise ExecutorError(code, "The required installer file is too large.")
            return text
        try:
            metadata = os.lstat(path)
        except FileNotFoundError as error:
            raise ExecutorError(missing_code or "file_missing", "The required installer file is unavailable.") from error
        except OSError as error:
            raise ExecutorError(code, "The required installer file is unavailable.") from error
        expected_owner = 0 if self.require_root else os.geteuid()
        forbidden_mode = 0o077 if private else 0o022
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != expected_owner or metadata.st_mode & forbidden_mode:
            raise ExecutorError(code, f"The required installer file is not protected: {path}.")
        if metadata.st_size > limit:
            raise ExecutorError(code, "The required installer file is too large.")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, limit + 1)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ExecutorError(code, "The required installer file could not be read.") from error
        if len(raw) > limit:
            raise ExecutorError(code, "The required installer file is too large.")
        try:
            return raw.decode("utf-8")
        except UnicodeError as error:
            raise ExecutorError(code, "The required installer file is not UTF-8.") from error

    def _write_cloud_init_seed(self) -> None:
        password_hash = None
        if _crypt is not None:
            try:
                password_hash = _crypt.crypt(_OWNER_PASSWORD, _crypt.mksalt(_crypt.METHOD_SHA512))
            except (AttributeError, TypeError, ValueError, OSError):
                password_hash = None
        credential_lines = "    lock_passwd: false\n"
        if isinstance(password_hash, str) and password_hash.startswith("$6$"):
            credential_lines += f"    passwd: {password_hash}\n"
        else:
            # Fedora's Python no longer includes crypt. Cloud-init accepts
            # this field in the same protected 0600 first-boot seed.
            credential_lines += f"    plain_text_passwd: {_OWNER_PASSWORD}\n"
        user_data = (
            "#cloud-config\n"
            "users:\n"
            f"  - name: {OWNER_USER}\n"
            "    gecos: Zeus owner\n"
            "    groups: [wheel]\n"
            f"{credential_lines}"
            "    shell: /bin/bash\n"
            "ssh_pwauth: false\n"
            "disable_root: true\n"
            "preserve_hostname: true\n"
            "resize_rootfs: false\n"
            "growpart: {mode: 'off'}\n"
            "package_update: false\n"
            "package_upgrade: false\n"
            "runcmd:\n"
            "  - [touch, /etc/cloud/cloud-init.disabled]\n"
        ).encode("utf-8")
        meta_data = (
            "instance-id: zeus-dualboot\n"
            "local-hostname: zeus\n"
        ).encode("utf-8")
        # Set the hostname before boot; cloud-init need not contact hostname
        # services during an offline first boot.
        self._write_file(self._target_etc_path("hostname"), b"zeus\n", 0o644)
        self._write_file(TARGET_SEED / "user-data", user_data, 0o600)
        self._write_file(TARGET_SEED / "meta-data", meta_data, 0o600)

    def _write_efi_scope_config(self, esp_uuid: str) -> None:
        if _FAT_UUID_RE.fullmatch(str(esp_uuid)) is None:
            raise ExecutorError("filesystem_invalid", "The Zeus ESP UUID is not a canonical FAT volume ID.")
        config_path = self._target_etc_path("zeus/efi-update.json")
        scope_writer = getattr(self.efi_update, "write_scope_config", None)
        if callable(scope_writer):
            try:
                try:
                    scope_writer(config_path, str(esp_uuid).upper(), require_root=self.require_root)
                except TypeError:
                    # Keep compatibility with a tiny qualification fixture
                    # writer that accepts only path and UUID.
                    scope_writer(config_path, str(esp_uuid).upper())
            except ExecutorError:
                raise
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                raise ExecutorError("efi_scope_failed", "The scoped EFI configuration could not be installed.") from error
            return
        data = (
            json.dumps(
                {
                    "schema_version": 1,
                    "esp_uuid": str(esp_uuid).upper(),
                    "mountpoint": "/boot/efi",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        self._write_file(config_path, data, 0o644)

    def _harden_target_etc(self) -> None:
        """Make the deployment config directories safe for the EFI worker.

        Some bootc payloads currently ship ``/etc`` group-writable.  The
        scoped worker deliberately rejects a writable parent chain, so fix
        the two target directories before asking its atomic config writer to
        create ``efi-update.json``.  The operation is limited to the newly
        installed deployment; Fedora's live ``/etc`` is never touched.
        """

        if self.deployment_root is None:
            raise ExecutorError("invalid_state", "The installed OSTree deployment has not been resolved.")
        if self.writer is not None:
            # The fixture writer records files without materializing a target
            # tree.  Production performs the ownership and mode checks below.
            return
        for path in (self.deployment_root / "etc", self.deployment_root / "etc" / "zeus"):
            if not path.is_absolute():
                raise ExecutorError("unsafe_storage", "The target configuration path is invalid.")
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                self._mkdir_fixed(path)
                try:
                    metadata = os.lstat(path)
                except OSError as error:
                    raise ExecutorError("unsafe_storage", "The target configuration directory is unavailable.") from error
            except OSError as error:
                raise ExecutorError("unsafe_storage", "The target configuration directory could not be checked.") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExecutorError("unsafe_storage", "The target configuration directory is unsafe.")
            if metadata.st_uid != 0:
                raise ExecutorError("unsafe_storage", "The target configuration directory is not root-owned.")
            try:
                os.chmod(path, 0o755)
                directory_fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise ExecutorError("state_write_failed", "The target configuration directory could not be secured.") from error

    def _install_efi_runtime(self) -> str:
        """Install the scoped EFI worker and its bootloader-update drop-in."""

        source = Path(__file__).with_name("efi_update.py").resolve()
        try:
            metadata = os.lstat(source)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (
                    self.require_root
                    and (metadata.st_uid != 0 or metadata.st_mode & 0o022)
                )
            ):
                raise OSError("EFI worker source is not a regular file")
            if metadata.st_size > 512 * 1024:
                raise OSError("EFI worker source is unexpectedly large")
            source_data = source.read_bytes()
        except OSError as error:
            raise ExecutorError("efi_scope_failed", "The scoped EFI worker source is unavailable.") from error
        wrapper_path = self._target_etc_path(f"zeus/{EFI_WRAPPER_NAME}")
        dropin_path = self._target_etc_path(EFI_DROPIN_NAME)
        self._write_file(wrapper_path, source_data, 0o755)
        self._write_file(dropin_path, self._efi_dropin_bytes(), 0o644)
        return hashlib.sha256(source_data).hexdigest()

    def _write_fstab(self, filesystem_uuids: Mapping[str, str]) -> None:
        fstab_path = self._target_etc_path("fstab")
        existing = ""
        try:
            existing = self._read_file(
                fstab_path, limit=64 * 1024, code="fstab_invalid", missing_code="file_missing"
            )
        except ExecutorError as error:
            if error.code != "file_missing":
                raise
        # bootc owns the OSTree root mount and its kernel arguments. Retain
        # that root entry and unrelated mounts, replacing only boot mounts.
        retained = []
        for line in existing.splitlines():
            columns = line.split()
            if line.strip() == "# Zeus dual-boot managed mounts":
                continue
            if columns and not columns[0].startswith("#") and len(columns) >= 2:
                if columns[1] in {"/boot", "/boot/efi"}:
                    continue
            retained.append(line)
        managed = (
            "# Zeus dual-boot managed mounts\n"
            f"UUID={filesystem_uuids['boot']} /boot ext4 defaults 0 2\n"
            f"UUID={filesystem_uuids['esp']} /boot/efi vfat umask=0077 0 2\n"
        )
        prefix = "\n".join(retained).rstrip()
        content = (prefix + "\n" if prefix else "") + managed
        self._write_file(fstab_path, content.encode("utf-8"), 0o644)

    def _write_fedora_grub(self, rendered: str) -> None:
        previous = None
        try:
            previous = self._read_file(
                FEDORA_GRUB_SCRIPT, limit=64 * 1024, code="grub_invalid", missing_code="file_missing"
            )
        except ExecutorError as error:
            if error.code != "file_missing":
                raise
        if previous is not None and previous != rendered:
            raise ExecutorError("grub_conflict", "The Fedora Zeus GRUB entry is owned by another version.")
        self._write_file(FEDORA_GRUB_SCRIPT, rendered.encode("utf-8"), 0o755)
        defaults = ""
        try:
            defaults = self._read_file(
                FEDORA_GRUB_DEFAULTS, limit=64 * 1024, code="grub_invalid", missing_code="file_missing"
            )
        except ExecutorError as error:
            if error.code != "file_missing":
                raise
        self._write_file(FEDORA_GRUB_DEFAULTS, bootmenu.visible_menu_config(defaults).encode("utf-8"), 0o644)

    def _write_file(self, path: Path, data: bytes, mode: int) -> None:
        if self.writer is not None:
            try:
                self.writer(path, bytes(data), int(mode))
            except (OSError, ValueError, TypeError) as error:
                raise ExecutorError("state_write_failed", "The installer file could not be saved durably.") from error
            return
        self._mkdir_fixed(path.parent)
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise ExecutorError("state_write_failed", "The installer file could not be checked.") from error
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
            raise ExecutorError("unsafe_storage", "The installer file is not a regular file.")
        temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                int(mode),
            )
            os.fchmod(descriptor, int(mode))
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short installer file write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise ExecutorError("state_write_failed", "The installer file could not be saved durably.") from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _mkdir_fixed(self, path: Path) -> None:
        if not path.is_absolute():
            raise ExecutorError("unsafe_storage", "Installer paths must be absolute.")
        current = Path(path.anchor)
        for component in path.parts:
            if component == path.anchor:
                continue
            current /= component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current, 0o700)
                except OSError as error:
                    raise ExecutorError("unsafe_storage", "Installer directories could not be created.") from error
                metadata = os.lstat(current)
            except OSError as error:
                raise ExecutorError("unsafe_storage", "Installer directories could not be checked.") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExecutorError("unsafe_storage", "Installer directories contain an unsafe path.")

    # ------------------------------------------------------------------
    # Miscellaneous parsing and identity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _absolute_path(value: str | os.PathLike[str], *, code: str, message: str) -> Path:
        try:
            path = Path(value)
        except (TypeError, ValueError) as error:
            raise ExecutorError(code, message) from error
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ExecutorError(code, message)
        return path

    @staticmethod
    def _disk(value: Any) -> str:
        if not isinstance(value, str) or _DISK_RE.fullmatch(value) is None:
            raise ExecutorError("invalid_plan", "The target disk path is invalid.")
        return value

    @staticmethod
    def _partition_node(disk: str, number: int) -> str:
        disk = DualBootExecutor._disk(disk)
        if type(number) is not int or number not in (1, 2, 3, 4, 5, 6):
            raise ExecutorError("invalid_plan", "The partition number is invalid.")
        return disk + ("p" if disk[-1].isdigit() else "") + str(number)

    @staticmethod
    def _expected_table(original: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
        expected = copy.deepcopy(dict(original))
        parts = expected.get("partitions")
        if not isinstance(parts, list) or len(parts) != 3:
            raise ExecutorError("invalid_plan", "The original GPT table is invalid.")
        parts[2]["size"] = proposal["fedora_new_size"]
        return expected

    def _new_guids(self) -> list[str]:
        values: list[str] = []
        for _ in range(3):
            try:
                candidate = str(self.uuid_factory())
                parsed = uuid.UUID(candidate)
            except (AttributeError, TypeError, ValueError) as error:
                raise ExecutorError("invalid_state", "A Zeus partition identity could not be generated.") from error
            values.append(str(parsed))
        if len(set(values)) != 3:
            raise ExecutorError("invalid_state", "Zeus partition identities must be distinct.")
        return values

    @staticmethod
    def _valid_guid_list(value: Any) -> bool:
        return isinstance(value, list) and len(value) == 3 and len(set(value)) == 3 and all(
            isinstance(item, str) and _UUID_RE.fullmatch(item) for item in value
        )

    def _reboot_result(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "executor": "zeus-dualboot",
            "phase": "reboot_required",
            "stage": 1,
            "reboot_required": True,
            "executor_state": copy.deepcopy(dict(state)),
            "expected_table": copy.deepcopy(state.get("expected_table")),
            "old_boot_id": state.get("old_boot_id"),
            "new_guids": copy.deepcopy(state.get("new_guids")),
        }

    @staticmethod
    def _installed_result(state: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "executor": "zeus-dualboot",
            "phase": "installed",
            "stage": 2,
            "reboot_required": False,
            "executor_state": copy.deepcopy(dict(state)),
            "zeus_esp_uuid": state.get("zeus_esp_uuid"),
            "filesystem_uuids": copy.deepcopy(state.get("filesystem_uuids")),
            # At the installed boundary the six-partition table is the
            # removal identity.  During stage1 ``expected_table`` is the
            # three-partition post-shrink table; prefer the durable append
            # readback once it exists.
            "expected_table": copy.deepcopy(state.get("allocated_table", state.get("expected_table"))),
            "bootmenu_hash": state.get("bootmenu_hash"),
            "boot_chooser_marker": copy.deepcopy(state.get("boot_chooser_marker")),
        }

    @staticmethod
    def _safe_boot_id(value: Any) -> str | None:
        if not isinstance(value, str) or _BOOT_ID_RE.fullmatch(value) is None:
            return None
        return value

    def _require_boot_id(self, value: Any, message: str) -> str:
        checked = self._safe_boot_id(value)
        if checked is None:
            raise ExecutorError("target_unverified", message)
        return checked

    @staticmethod
    def _read_boot_id() -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return "unavailable"

    @staticmethod
    def _device_path(value: Mapping[str, Any] | None) -> str | None:
        if not isinstance(value, Mapping):
            return None
        candidate = value.get("path", value.get("device_path", value.get("devpath")))
        if candidate is None:
            candidate = value.get("name", value.get("kname", value.get("device")))
        if candidate is None:
            return None
        text = str(candidate)
        return text if text.startswith("/") else "/dev/" + text

    @classmethod
    def _flatten_devices(cls, value: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, Mapping):
            if any(key in value for key in ("path", "name", "kname", "type", "serial", "wwn")):
                result.append(dict(value))
            for key in ("blockdevices", "block_devices", "devices", "children", "partitions"):
                nested = value.get(key)
                if nested is not None:
                    result.extend(cls._flatten_devices(nested))
        elif isinstance(value, (list, tuple)):
            for item in value:
                result.extend(cls._flatten_devices(item))
        return result

    @staticmethod
    def _first_bytes(candidates: Sequence[Any], keys: Sequence[str]) -> int | None:
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for key in keys:
                value = candidate.get(key)
                if type(value) is int and value > 0:
                    return value
                if isinstance(value, str):
                    try:
                        parsed = int(value)
                    except ValueError:
                        continue
                    if parsed > 0:
                        return parsed
        return None

    @staticmethod
    def _first_int(candidates: Sequence[Any], keys: Sequence[str]) -> int | None:
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for key in keys:
                value = candidate.get(key)
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed >= 0:
                    return parsed
        return None

    @classmethod
    def _recursive_number(cls, value: Any, keys: Sequence[str]) -> int | None:
        if isinstance(value, Mapping):
            for key in keys:
                candidate = value.get(key)
                if candidate is not None:
                    try:
                        parsed = int(candidate)
                    except (TypeError, ValueError):
                        parsed = None
                    if parsed is not None and parsed > 0:
                        return parsed
            for nested in value.values():
                found = cls._recursive_number(nested, keys)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = cls._recursive_number(nested, keys)
                if found is not None:
                    return found
        return None

    @classmethod
    def _recursive_int(cls, value: Any, keys: Sequence[str]) -> int | None:
        if isinstance(value, Mapping):
            for key in keys:
                candidate = value.get(key)
                try:
                    parsed = int(candidate)
                except (TypeError, ValueError):
                    parsed = None
                if parsed is not None and parsed >= 0:
                    return parsed
            for nested in value.values():
                found = cls._recursive_int(nested, keys)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = cls._recursive_int(nested, keys)
                if found is not None:
                    return found
        return None

    @classmethod
    def _recursive_list(cls, value: Any, key: str) -> list[Any]:
        if isinstance(value, Mapping):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
            for nested in value.values():
                found = cls._recursive_list(nested, key)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = cls._recursive_list(nested, key)
                if found:
                    return found
        return []

    @classmethod
    def _table_from_inventory(cls, inventory: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if not isinstance(inventory, Mapping):
            return None
        for key in ("sfdisk_table", "partitiontable", "original_table", "table"):
            candidate = inventory.get(key)
            if isinstance(candidate, Mapping):
                nested = candidate.get("partitiontable")
                candidate = nested if isinstance(nested, Mapping) else candidate
                if cls._table_shape(candidate):
                    return candidate
        for nested_key in ("sfdisk", "storage", "disk", "target"):
            nested = inventory.get(nested_key)
            if isinstance(nested, Mapping):
                found = cls._table_from_inventory(nested)
                if found is not None:
                    return found
        return None

    @classmethod
    def _inventory_source_size(cls, inventory: Mapping[str, Any]) -> int | None:
        table = cls._table_from_inventory(inventory)
        if isinstance(table, Mapping):
            parts = table.get("partitions")
            if isinstance(parts, list) and len(parts) >= 3 and isinstance(parts[2], Mapping):
                try:
                    return int(parts[2]["size"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None


__all__ = [
    "BACKUP_RECEIPT_PATH",
    "CAT",
    "can_resume_finalization",
    "DualBootExecutor",
    "EFI_DROPIN_NAME",
    "EFI_WRAPPER_NAME",
    "ExecutorError",
    "OWNER_USER",
    "QUALIFIED_BOOTC_VERSION",
    "QUALIFIED_BOOTUPD_VERSION",
    "QUALIFIED_BUILD_ID",
    "QUALIFIED_MANIFEST_DIGEST",
    "SCHEMA_VERSION",
    "SYSTEMD_INHIBIT",
    "TARGET_EFI_CONFIG",
    "TARGET_IMAGE_REF",
    "TARGET_PAYLOAD",
    "TARGET_ROOT",
    "verify_finalization_resume",
]
