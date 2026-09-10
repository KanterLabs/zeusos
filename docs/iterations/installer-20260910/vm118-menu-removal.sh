#!/usr/bin/env bash
# Re-runnable VM118-only menu-removal rehearsal.
#
# The script deliberately uploads only the independently captured managed
# script, invokes only the installed removal module, and uses the backend's
# fixed command runner.  It never selects destructive removal or accepts a
# caller-provided privileged command.
set -euo pipefail

VM_HOST="${VM_HOST:-10.0.0.118}"
VM_USER="${VM_USER:-root}"
SSH_KEY="${SSH_KEY:-/home/shane/.ssh/homelab_mesh}"
MENU_COPY="${MENU_COPY:-$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)/out/installer/tmp/vm118-menu-before.sh}"
REMOTE_COPY="/run/zeus-vm118-menu-before.sh"

if [[ ! -f "$MENU_COPY" ]]; then
    echo "missing independent menu copy: $MENU_COPY" >&2
    exit 2
fi

SSH_OPTIONS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=5
)

scp -q "${SSH_OPTIONS[@]}" "$MENU_COPY" "$VM_USER@$VM_HOST:$REMOTE_COPY"
ssh "${SSH_OPTIONS[@]}" "$VM_USER@$VM_HOST" python3 - "$REMOTE_COPY" <<'PY'
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, "/usr/lib/zeus-installer")

from zeus_installer import preflight, removal
from zeus_installer.backend import CommandRunner, Journal


EXPECTED_DMI = "29f40b00-bc18-4ff3-82da-1d9006d7db4c"
EXPECTED_MARKER = {"vmid": 118, "purpose": "zeus-dualboot-rehearsal"}
JOURNAL_ROOT = "/var/lib/zeus/installer"
SCRIPT = Path("/etc/grub.d/42_zeus_dualboot")
DEFAULTS = Path("/etc/default/grub")
CONFIG = Path("/boot/grub2/grub.cfg")
GRUBENV = Path("/boot/grub2/grubenv")
ZEUS_COPY = Path("/run/zeus-vm118-menu-before.sh")
FIXED_GRUB = [
    "/usr/sbin/grub2-mkconfig",
    "--no-grubenv-update",
    "-o",
    "/boot/grub2/grub.cfg",
]
MANIFEST = Path("/var/lib/zeus-fixture/baseline-files.json")
FEDORA_AND_SENTINEL_DATA = [
    "/home/zeus-fixture-data.bin",
    "/home/zeus-fixture-home-sentinel.txt",
    "/var/lib/zeus-fixture/root-data.bin",
    "/var/lib/zeus-fixture/root-sentinel.txt",
    "/var/lib/zeus-sentinel/data.bin",
    "/var/lib/zeus-sentinel/independent-disk-sentinel.txt",
]
ZEUS_BLOCK = re.compile(
    r"### BEGIN /etc/grub\.d/42_zeus_dualboot ###\n.*?"
    r"### END /etc/grub\.d/42_zeus_dualboot ###\n?",
    re.DOTALL,
)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(path: Path) -> dict:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"present": False}
    if not stat.S_ISREG(metadata.st_mode):
        return {
            "present": True,
            "regular": False,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }
    return {
        "present": True,
        "regular": True,
        "sha256": digest_file(path),
        "bytes": metadata.st_size,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def manifest_paths() -> list[str]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("fixture manifest is not an object")
    return sorted(str(path) for path in value)


def grub_script_paths() -> list[str]:
    result = []
    for path in sorted(Path("/etc/grub.d").iterdir()):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            result.append(str(path))
    return result


def table_from(inventory: dict) -> dict:
    sfdisk = inventory.get("sfdisk")
    if not isinstance(sfdisk, dict):
        raise RuntimeError("preflight did not return sfdisk data")
    table = sfdisk.get("partitiontable")
    if not isinstance(table, dict):
        raise RuntimeError("preflight did not return a GPT table")
    return table


def table_summary(table: dict) -> dict:
    partitions = table.get("partitions")
    if not isinstance(partitions, list):
        raise RuntimeError("preflight GPT table has no partitions")
    by_number = {
        index + 1: item
        for index, item in enumerate(partitions)
        if isinstance(item, dict)
    }
    return {
        "sha256": removal.canonical_hash(table),
        "device": table.get("device"),
        "disk_guid": str(table.get("id", "")).lower(),
        "partition_count": len(partitions),
        "partitions": {
            str(number): {
                "node": item.get("node"),
                "uuid": str(item.get("uuid", "")).lower(),
                "start": item.get("start"),
                "size": item.get("size"),
            }
            for number, item in sorted(by_number.items())
        },
    }


def inventory_snapshot(inventory: dict, paths: list[str], grub_paths: list[str]) -> dict:
    table = table_from(inventory)
    return {
        "table": table_summary(table),
        "manifest": {path: file_info(Path(path)) for path in paths},
        "fixed_files": {
            str(path): file_info(path)
            for path in (SCRIPT, DEFAULTS, CONFIG, GRUBENV)
        },
        "grub_scripts": {path: file_info(Path(path)) for path in grub_paths},
        "fedora_and_sentinel_data": {
            path: file_info(Path(path)) for path in FEDORA_AND_SENTINEL_DATA
        },
        "mount_count": len(inventory.get("mounts", []))
        if isinstance(inventory.get("mounts"), list)
        else None,
    }


def require_fixture() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("VM118 rehearsal requires root")
    marker = Path("/etc/zeus-dualboot-fixture.json")
    metadata = marker.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError("VM118 fixture marker is not private root-owned data")
    if json.loads(marker.read_text(encoding="utf-8")) != EXPECTED_MARKER:
        raise RuntimeError("VM118 fixture marker mismatch")
    dmi = Path("/sys/class/dmi/id/product_uuid").read_text(encoding="ascii").strip().lower()
    if dmi != EXPECTED_DMI:
        raise RuntimeError("VM118 DMI identity mismatch")


def safe_read(path: Path) -> bytes | None:
    if path != SCRIPT:
        raise RuntimeError("reader received an unexpected path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise RuntimeError("managed menu script metadata changed")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def safe_remove(path: Path) -> None:
    if path != SCRIPT:
        raise RuntimeError("remover received an unexpected path")
    directory = path.parent
    directory_fd = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise RuntimeError("managed menu script metadata changed")
        os.unlink(path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def restore_exact(copy_path: Path, expected_hash: str) -> None:
    payload = copy_path.read_bytes()
    if removal.content_hash(payload) != expected_hash:
        raise RuntimeError("independent menu copy hash changed")
    directory = SCRIPT.parent
    directory_fd = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    temporary_name = f".42_zeus_dualboot.vm118.{os.getpid()}"
    descriptor = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o700,
            dir_fd=directory_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, 0o755)
        os.fchown(descriptor, 0, 0)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            SCRIPT.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def fixed_grub(runner: CommandRunner) -> None:
    result = runner.run(list(FIXED_GRUB))
    if getattr(result, "returncode", None) != 0:
        raise RuntimeError("fixed grub regeneration failed")


def without_zeus_block(text: str) -> str:
    # grub2-mkconfig emits one separator newline after the final generated
    # section. Removing the last script also removes that section's leading
    # separator, so normalize only the trailing separator for comparison.
    result = ZEUS_BLOCK.sub("", text)
    return result.rstrip("\n") + ("\n" if text.endswith("\n") else "")


def changed_values(before: dict, after: dict, *, omit: set[str] = set()) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if key not in omit and before.get(key) != after.get(key))


def main() -> dict:
    require_fixture()
    copy_path = Path(sys.argv[1]) if len(sys.argv) == 2 else ZEUS_COPY
    if copy_path != ZEUS_COPY:
        raise RuntimeError("the menu copy path is fixed for VM118")
    copy_info = file_info(copy_path)
    if not copy_info.get("present") or not copy_info.get("regular"):
        raise RuntimeError("independent menu copy is unavailable")

    journal = Journal(JOURNAL_ROOT, require_root=True)
    before_record = copy.deepcopy(journal.load())
    if not isinstance(before_record, dict) or before_record.get("phase") != "installed":
        raise RuntimeError("the VM118 journal is not at the installed boundary")
    if before_record.get("removal") is not None:
        raise RuntimeError("the VM118 journal already contains removal state")

    manifest = manifest_paths()
    grub_paths = grub_script_paths()
    before_inventory = preflight.collect()
    if before_inventory.get("errors"):
        raise RuntimeError("Fedora preflight returned diagnostics")
    if before_inventory.get("os", {}).get("id") != "fedora":
        raise RuntimeError("the current root is not Fedora")
    if not SCRIPT.exists():
        raise RuntimeError("the managed menu script is missing before rehearsal")
    menu_hash = digest_file(SCRIPT)
    if menu_hash != copy_info.get("sha256"):
        raise RuntimeError("the independent menu copy does not match the live script")

    # The removal executor refreshes the plan from its supplied inventory. It
    # receives the same exact menu identity the planner used, while still
    # using preflight for every disk and root identity.
    before_inventory["bootmenu_hash"] = menu_hash
    before_inventory["bootmenu_present"] = True
    plan = removal.build_plan(
        journal,
        before_inventory,
        mode=removal.MENU_ONLY,
        current_bootmenu_hash=menu_hash,
        bootmenu_present=True,
    )
    expected_operations = [
        {
            "kind": "remove_file",
            "path": str(SCRIPT),
            "expected_sha256": menu_hash,
        },
        {"kind": "regenerate_grub", "argv": list(FIXED_GRUB)},
    ]
    if plan.get("operations") != expected_operations:
        raise RuntimeError("menu-only plan emitted an unexpected operation")
    if plan.get("mode") != removal.MENU_ONLY or plan.get("backup") is not None:
        raise RuntimeError("menu-only plan crossed a destructive boundary")
    if any(
        operation.get("kind") in {"delete_partitions", "resize", "reallocate"}
        for operation in plan.get("operations", [])
    ):
        raise RuntimeError("menu-only plan contains a partition operation")

    cancelled = removal.build_plan(journal, before_inventory, cancelled=True)
    if cancelled.get("operations") or not cancelled.get("cancelled") or cancelled.get("mutated"):
        raise RuntimeError("cancellation was not a no-op")

    before_snapshot = inventory_snapshot(before_inventory, manifest, grub_paths)
    before_config = CONFIG.read_text(encoding="utf-8")
    before_grubenv = GRUBENV.read_bytes()
    before_record_id = before_record.get("operation_id")
    runner = CommandRunner()
    executor = removal.RemovalExecutor(
        qualified=True,
        reader=safe_read,
        remove_file=safe_remove,
        runner=runner,
        require_root=True,
    )
    # Keep the restoration boundary outside the rehearsal checks. If a
    # command, readback, or assertion fails after unlinking the script, the
    # exact independent copy is still restored before the failure is returned.
    execution = None
    during_record = None
    during_removal = None
    after_inventory = None
    after_snapshot = None
    after_config = None
    after_grubenv = None
    repeat_plan = None
    repeat_execution = None
    removed_menu_absent = False
    rehearsal_error = None
    try:
        execution = executor.execute(
            plan,
            journal=journal,
            inventory=before_inventory,
            dry_run=False,
        )
        if execution.get("state") != "menu_disabled" or not execution.get("mutated"):
            raise RuntimeError("menu-only executor did not report a mutation")
        if runner.commands != [FIXED_GRUB]:
            raise RuntimeError("executor ran an unexpected privileged command")
        if SCRIPT.exists():
            raise RuntimeError("managed menu script remained after removal")
        removed_menu_absent = not SCRIPT.exists()

        during_record = journal.load()
        during_removal = during_record.get("removal") if isinstance(during_record, dict) else None
        if not isinstance(during_removal, dict) or during_removal.get("phase") != "menu_disabled":
            raise RuntimeError("menu removal state was not durably recorded")

        after_inventory = preflight.collect()
        if after_inventory.get("errors"):
            raise RuntimeError("post-removal Fedora preflight returned diagnostics")
        after_inventory["bootmenu_present"] = False
        after_snapshot = inventory_snapshot(after_inventory, manifest, grub_paths)
        after_config = CONFIG.read_text(encoding="utf-8")
        after_grubenv = GRUBENV.read_bytes()
        if "zeusos-dualboot" in after_config or "Zeus OS" in after_config:
            raise RuntimeError("the generated Fedora menu still contains Zeus")
        if without_zeus_block(before_config) != after_config:
            raise RuntimeError("GRUB regeneration changed another Fedora menu block")
        if before_grubenv != after_grubenv:
            raise RuntimeError("GRUB environment changed despite --no-grubenv-update")

        repeat_plan = removal.build_plan(
            journal,
            after_inventory,
            mode=removal.MENU_ONLY,
            bootmenu_present=False,
        )
        if repeat_plan.get("menu", {}).get("action") != "already_disabled" or repeat_plan.get("operations"):
            raise RuntimeError("repeat menu-only planning was not a no-op")
        repeat_execution = removal.RemovalExecutor(qualified=True).execute(
            plan,
            journal=journal,
            inventory=after_inventory,
            dry_run=False,
        )
        if not repeat_execution.get("idempotent") or repeat_execution.get("mutated"):
            raise RuntimeError("repeat menu-only execution was not harmless")
    except Exception as error:
        rehearsal_error = error

    # Restore only the exact script captured before this test, then use the
    # same fixed Fedora command. This leaves both boot choices available for
    # the VM owner while preserving grubenv and every other generated block.
    restore_runner = CommandRunner()
    restored_inventory = None
    restored_snapshot = None
    restored_config = None
    restored_grubenv = None
    final_record = None
    restoration_error = None
    restoration_performed = False
    try:
        if not SCRIPT.exists():
            restore_exact(copy_path, menu_hash)
            fixed_grub(restore_runner)
            restoration_performed = True
        elif rehearsal_error is None:
            raise RuntimeError("managed menu script remained after removal")
        if restoration_performed:
            if digest_file(SCRIPT) != menu_hash:
                raise RuntimeError("restored menu script differs from the independent copy")
            restored_config = CONFIG.read_text(encoding="utf-8")
            restored_grubenv = GRUBENV.read_bytes()
            if restored_config != before_config:
                raise RuntimeError("restored GRUB config differs from its pre-test bytes")
            if restored_grubenv != before_grubenv:
                raise RuntimeError("GRUB environment changed during restoration")
            if restore_runner.commands != [FIXED_GRUB]:
                raise RuntimeError("restoration ran an unexpected privileged command")

            restored_inventory = preflight.collect()
            if restored_inventory.get("errors"):
                raise RuntimeError("post-restore Fedora preflight returned diagnostics")
            restored_snapshot = inventory_snapshot(restored_inventory, manifest, grub_paths)
            final_record_before_clear = journal.load()
            if not isinstance(final_record_before_clear, dict):
                raise RuntimeError("journal disappeared during restoration")
            cleared = copy.deepcopy(final_record_before_clear)
            cleared.pop("removal", None)
            journal.write(cleared)
            final_record = journal.load()
            if final_record != before_record:
                raise RuntimeError("restored journal does not match its installed pre-test state")
    except Exception as error:
        restoration_error = error

    if restoration_error is not None:
        if rehearsal_error is not None:
            raise RuntimeError(
                f"menu rehearsal failed ({rehearsal_error}); exact restoration failed ({restoration_error})"
            )
        raise restoration_error
    if rehearsal_error is not None:
        raise rehearsal_error

    # Fedora fixture and independent sentinel files must be byte-for-byte
    # stable throughout the menu rehearsal. Zeus filesystems remain unmounted,
    # so their contents are deliberately not read by this harness. GPT
    # identity is compared as a complete table so no partition can be silently
    # resized or reallocated.
    manifest_after_removal_changes = changed_values(
        before_snapshot["manifest"], after_snapshot["manifest"]
    )
    data_changes = changed_values(
        before_snapshot["fedora_and_sentinel_data"],
        restored_snapshot["fedora_and_sentinel_data"],
    )
    table_unchanged = (
        before_snapshot["table"] == after_snapshot["table"] == restored_snapshot["table"]
    )
    other_grub_changes = changed_values(
        before_snapshot["grub_scripts"], after_snapshot["grub_scripts"], omit={str(SCRIPT)}
    )
    final_grub_changes = changed_values(
        before_snapshot["grub_scripts"], restored_snapshot["grub_scripts"]
    )
    if manifest_after_removal_changes or data_changes or not table_unchanged:
        raise RuntimeError("data or GPT identity changed during menu rehearsal")
    if other_grub_changes or final_grub_changes:
        raise RuntimeError("an unrelated Fedora GRUB script changed")

    return {
        "ok": True,
        "vmid": 118,
        "dmi_uuid": EXPECTED_DMI,
        "root_os": before_inventory["os"].get("id"),
        "copy": copy_info,
        "journal": {
            "operation_id": before_record_id,
            "phase_before": before_record.get("phase"),
            "phase_during": during_record.get("phase"),
            "removal_phase_during": during_removal.get("phase"),
            "removal_cleared_after_restore": "removal" not in final_record,
            "restored_record_matches_before": final_record == before_record,
        },
        "plan": {
            "plan_id": plan.get("plan_id"),
            "plan_digest": plan.get("plan_digest"),
            "mode": plan.get("mode"),
            "menu_action": plan.get("menu", {}).get("action"),
            "recorded_menu_sha256": plan.get("menu", {}).get("recorded_sha256"),
            "disk": plan.get("disk"),
            "zeus_partitions": plan.get("zeus_partitions"),
            "operations": plan.get("operations"),
            "backup": plan.get("backup"),
        },
        "cancellation": {
            "cancelled": cancelled.get("cancelled"),
            "mutated": cancelled.get("mutated"),
            "operations": cancelled.get("operations"),
        },
        "executor": execution,
        "repeat": {
            "plan_action": repeat_plan.get("menu", {}).get("action"),
            "plan_operations": repeat_plan.get("operations"),
            "execution": repeat_execution,
        },
        "commands": {
            "removal": runner.commands,
            "restoration": restore_runner.commands,
            "destructive_partition_commands": [],
        },
        "checks": {
            "menu_absent_during_check": removed_menu_absent,
            "generated_menu_removed_zeus": "zeusos-dualboot" not in after_config
            and "Zeus OS" not in after_config,
            "grubenv_unchanged": before_grubenv == after_grubenv == restored_grubenv,
            "fedora_grub_blocks_unchanged_after_removal": without_zeus_block(before_config)
            == after_config,
            "fedora_grub_config_restored": restored_config == before_config,
            "manifest_unchanged": not manifest_after_removal_changes,
            "fedora_and_sentinel_data_unchanged": not data_changes,
            "gpt_table_unchanged": table_unchanged,
            "other_grub_scripts_unchanged": not other_grub_changes and not final_grub_changes,
            "no_partition_operation": runner.commands == [FIXED_GRUB]
            and not any(command and command[0] == "/usr/bin/sgdisk" for command in runner.commands),
            "independent_copy_restored": digest_file(SCRIPT) == menu_hash,
        },
        "before": before_snapshot,
        "after_menu_only": after_snapshot,
        "after_restore": restored_snapshot,
    }


try:
    print(json.dumps(main(), sort_keys=True, indent=2))
except Exception as error:
    print(
        json.dumps(
            {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error),
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1)
finally:
    try:
        ZEUS_COPY.unlink()
    except FileNotFoundError:
        pass
PY
