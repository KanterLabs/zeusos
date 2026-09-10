"""Focused tests for the journaled two-phase dual-boot executor."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = str(ROOT / "installer")
if INSTALLER not in sys.path:
    sys.path.insert(0, INSTALLER)

from zeus_installer import backend, storage  # noqa: E402
from zeus_installer import executor as installer_executor  # noqa: E402


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _table() -> dict[str, object]:
    disk = "/dev/nvme0n1"
    ids = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )
    first = 2048
    p1_size = 600 * 1024 * 1024 // 512
    p2_start = first + p1_size
    p2_size = 2 * 1024 * 1024 * 1024 // 512
    p3_start = ((p2_start + p2_size + 2047) // 2048) * 2048
    p3_size = 1_800_000_000
    return {
        "label": "gpt",
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "device": disk,
        "unit": "sectors",
        "firstlba": first,
        "lastlba": p3_start + p3_size - 1,
        "sectorsize": 512,
        "partitions": [
            {
                "node": f"{disk}p1",
                "start": first,
                "size": p1_size,
                "type": storage.EFI,
                "uuid": ids[0],
                "name": "Fedora EFI",
            },
            {
                "node": f"{disk}p2",
                "start": p2_start,
                "size": p2_size,
                "type": storage.LINUX,
                "uuid": ids[1],
                "name": "Fedora boot",
            },
            {
                "node": f"{disk}p3",
                "start": p3_start,
                "size": p3_size,
                "type": storage.LINUX,
                "uuid": ids[2],
                "name": "Fedora root",
            },
        ],
    }


def _plan_and_proposal() -> tuple[dict[str, object], dict[str, object]]:
    table = _table()
    proposal = storage.layout(table, 128)
    p3 = table["partitions"][2]
    assert isinstance(p3, dict)
    inventory = {
        "btrfs": {
            "device_count": 1,
            "device_size_bytes": int(p3["size"]) * 512 - 16 * storage.MIB,
            "minimum_size_bytes": int(p3["size"]) * 512 - 256 * storage.GIB,
        },
        "block_devices": [
            {"path": "/dev/nvme0n1", "type": "disk", "serial": "ZEUS-TEST-DISK"},
            {"path": "/dev/nvme0n1p1", "type": "part", "partuuid": p3["uuid"]},
            {"path": "/dev/nvme0n1p2", "type": "part", "partuuid": "44444444-4444-4444-8444-444444444444"},
            {"path": "/dev/nvme0n1p3", "type": "part", "partuuid": p3["uuid"], "partn": 3},
        ],
    }
    target = {
        "sfdisk_table": copy.deepcopy(table),
        "allocation_gib": 128,
        "source_partition": {
            "path": "/dev/nvme0n1p3",
            "partuuid": p3["uuid"],
            "size_bytes": int(p3["size"]) * 512,
            "new_size_bytes": int(proposal["fedora_new_size"]) * 512,
        },
    }
    plan = {
        "schema_version": 1,
        "supported": True,
        "blockers": [],
        "fingerprint": "sha256:" + "a" * 64,
        "allocation_gib": 128,
        "target": target,
        "inventory": inventory,
    }
    return plan, proposal


class FixtureRunner:
    """A command runner that models only the fixed executor argv surface."""

    def __init__(self, table: dict[str, object], proposal: dict[str, object]) -> None:
        self.table = copy.deepcopy(table)
        self.proposal = proposal
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.formatted: dict[str, tuple[str, str]] = {}
        self.mounts: dict[str, tuple[str, str]] = {}
        self.btrfs_bytes = int(table["partitions"][2]["size"]) * 512 - 16 * storage.MIB
        self.bootupctl_unmounts_esp = True

    @staticmethod
    def _node(disk: str, number: int) -> str:
        return f"{disk}p{number}"

    def _findmnt(self, target: str) -> Result:
        if target == "/":
            item = {
                "source": "/dev/nvme0n1p3",
                "fstype": "btrfs",
                "uuid": "fedora-fs",
                "partuuid": self.table["partitions"][2]["uuid"],
            }
        else:
            source, fstype = self.mounts.get(target, ("", ""))
            item = {"source": source, "fstype": fstype}
        return Result(stdout=json.dumps({"filesystems": [item] if item["source"] else []}))

    def run(self, argv: list[str], **kwargs: object) -> Result:
        self.calls.append((list(argv), dict(kwargs)))
        program = argv[0]
        if argv[:2] == [installer_executor.SFDISK, "--json"]:
            return Result(stdout=json.dumps({"partitiontable": self.table}))
        if program == installer_executor.LSBLK:
            return Result(
                stdout=json.dumps(
                    {
                        "blockdevices": [
                            {"path": "/dev/nvme0n1", "type": "disk", "serial": "ZEUS-TEST-DISK"}
                        ]
                    }
                )
            )
        if program == installer_executor.FINDMNT:
            option = "--mountpoint" if "--mountpoint" in argv else "--target"
            target = argv[argv.index(option) + 1]
            if option == "--mountpoint" and target not in self.mounts:
                return Result(returncode=1)
            return self._findmnt(target)
        if argv[:3] == [installer_executor.BTRFS, "filesystem", "resize"]:
            self.btrfs_bytes = int(argv[3])
            return Result()
        if argv[:3] == [installer_executor.BTRFS, "filesystem", "sync"]:
            return Result()
        if argv[:3] == [installer_executor.BTRFS, "inspect-internal", "dump-super"]:
            return Result(stdout=f"dev_item.total_bytes {self.btrfs_bytes}\n")
        if program == installer_executor.SFDISK and "-N" in argv:
            number = int(argv[argv.index("-N") + 1])
            input_text = str(kwargs.get("input", ""))
            size_match = re.search(r"(?:^|\n)\s*size=(\d+)(?:\s|$)", input_text)
            if size_match is None:
                raise AssertionError(f"missing sfdisk partition size input: {input_text!r}")
            size = int(size_match.group(1))
            self.table["partitions"][number - 1]["size"] = size
            return Result()
        if program == installer_executor.SFDISK and "--append" in argv:
            input_text = str(kwargs.get("input", ""))
            additions = []
            for line in input_text.splitlines():
                start = int(re.search(r"start=(\d+)", line).group(1))
                size = int(re.search(r"size=(\d+)", line).group(1))
                kind = re.search(r"type=([^,]+)", line).group(1)
                guid = re.search(r"uuid=([^,]+)", line).group(1)
                name = re.search(r'name="([^"]+)"', line).group(1)
                additions.append(
                    {
                        "node": self._node(self.table["device"], len(self.table["partitions"]) + 1),
                        "start": start,
                        "size": size,
                        "type": kind,
                        "uuid": guid,
                        "name": name,
                    }
                )
            self.table["partitions"].extend(additions)
            return Result()
        if program == installer_executor.PARTX or program == installer_executor.UDEVADM:
            return Result()
        if program == installer_executor.BLOCKDEV:
            return Result(stdout=str(int(self.table["partitions"][2]["size"]) * 512))
        if program == installer_executor.BLKID:
            node = argv[-1]
            if node not in self.formatted:
                return Result(returncode=2)
            fs_uuid, fs_type = self.formatted[node]
            return Result(stdout=f"UUID={fs_uuid}\nTYPE={fs_type}\n")
        if program in {installer_executor.MKFS_FAT, installer_executor.MKFS_EXT4}:
            node = argv[-1]
            number = int(re.search(r"(?:p|)([456])$", node).group(1))
            if number == 4:
                self.formatted[node] = ("A1B2-C3D4", "vfat")
            elif number == 5:
                self.formatted[node] = ("55555555-5555-4555-8555-555555555555", "ext4")
            else:
                self.formatted[node] = ("66666666-6666-4666-8666-666666666666", "ext4")
            return Result()
        if program == installer_executor.MOUNT:
            node, target = argv[-2], argv[-1]
            fstype = "vfat" if node.endswith("p4") else "ext4"
            self.mounts[target] = (node, fstype)
            return Result()
        if program == installer_executor.PODMAN:
            if "--entrypoint" in argv:
                entrypoint = argv[argv.index("--entrypoint") + 1]
                if entrypoint == installer_executor.BOOTUPCTL:
                    self.mounts.pop(str(installer_executor.TARGET_ESP), None)
            return Result()
        if program == installer_executor.GRUB2_MKCONFIG:
            return Result()
        raise AssertionError(f"unexpected fixed command: {argv}")


class ExecutorFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_parent = ROOT / "out" / "installer" / "tmp"
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_parent)
        self.journal = backend.Journal(Path(self.temp.name) / "state", require_root=False, boot_id=lambda: "deadbeef")
        self.plan, self.proposal = _plan_and_proposal()
        self.journal.begin(self.plan)
        self.artifact_path = self.journal.artifacts_path / "zeus.oci"
        self.artifact_path.write_bytes(b"verified archive")
        self.artifact_path.chmod(0o600)
        self.receipt = {"verified": True, "backup_target": "/var/backups/zeus-test"}
        self.files: dict[Path, bytes] = {}
        self.boot = {"id": "deadbeef"}
        self.events: list[str] = []

        @contextmanager
        def inhibitor():
            self.events.append("acquire")
            try:
                yield
            finally:
                self.events.append("release")

        self.inhibitor = inhibitor
        self.runner = FixtureRunner(self.plan["target"]["sfdisk_table"], self.proposal)
        self.executor = installer_executor.DualBootExecutor(
            qualified=True,
            efi_update=lambda *args, **kwargs: None,
            qualification_receipt={
                "qualified": True,
                "build_id": installer_executor.QUALIFIED_BUILD_ID,
                "manifest_digest": installer_executor.QUALIFIED_MANIFEST_DIGEST,
                "bootc_version": installer_executor.QUALIFIED_BOOTC_VERSION,
                "bootupd_version": installer_executor.QUALIFIED_BOOTUPD_VERSION,
            },
            backup_receipt_path="/etc/zeus-dualboot-backup.json",
            boot_id=lambda: self.boot["id"],
            uuid_factory=iter(
                [
                    uuid.UUID("77777777-7777-4777-8777-777777777777"),
                    uuid.UUID("88888888-8888-4888-8888-888888888888"),
                    uuid.UUID("99999999-9999-4999-8999-999999999999"),
                ]
            ).__next__,
            reader=lambda path: json.dumps(self.receipt) if path == Path("/etc/zeus-dualboot-backup.json") else None,
            writer=lambda path, data, mode: self.files.__setitem__(path, bytes(data)),
            inhibitor=inhibitor,
            require_root=False,
        )
        self.executor._resolve_deployment_root = lambda: Path("/target/ostree/deploy/default/deploy/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.0")
        self.artifact = {
            "path": str(self.artifact_path),
            "build_id": installer_executor.QUALIFIED_BUILD_ID,
            "manifest_digest": installer_executor.QUALIFIED_MANIFEST_DIGEST,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute_stage1(self) -> dict[str, object]:
        result = self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
        )
        self.assertEqual(result["phase"], "reboot_required")
        return result

    def _set_fresh_resources(self, *, ac_online: bool) -> None:
        inventory = copy.deepcopy(self.plan["inventory"])
        inventory.update(
            {
                "memory": {"available_bytes": 4 * 1024**3},
                "power": {"ac_online": ac_online},
                "staging": {"free_bytes": 16 * 1024**3},
            }
        )
        self.executor.inventory_provider = lambda: {
            "supported": True,
            "blockers": [],
            "fingerprint": self.plan["fingerprint"],
            "inventory": inventory,
        }

    def _replace_executor_state(self, state: dict[str, object]) -> None:
        record = self.journal.load()
        self.assertIsInstance(record, dict)
        record["executor_state"] = copy.deepcopy(state)
        self.journal.write(record)

    @staticmethod
    def _is_mutating_command(argv: list[str]) -> bool:
        return argv[0] in {
            installer_executor.BTRFS,
            installer_executor.MKFS_FAT,
            installer_executor.MKFS_EXT4,
            installer_executor.MOUNT,
            installer_executor.PARTX,
            installer_executor.PODMAN,
            installer_executor.GRUB2_MKCONFIG,
        } or "-N" in argv or "--append" in argv

    def test_missing_backup_receipt_default_fails_before_mutating_command(self) -> None:
        self.executor.backup_receipt_path = Path(self.temp.name) / "missing-backup.json"
        self.executor.reader = None
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(
                plan=self.plan,
                artifact=self.artifact,
                runner=self.runner,
                journal=self.journal,
            )
        self.assertEqual(context.exception.code, "backup_receipt_missing")
        self.assertFalse(any(self._is_mutating_command(argv) for argv, _kwargs in self.runner.calls))
        self.assertEqual(self.events, [])
        self.assertNotIn("executor_state", self.journal.load())

    def test_without_backup_stage1_keeps_geometry_guards_and_truthful_decision(self) -> None:
        self._set_fresh_resources(ac_online=True)
        receipt_reader = mock.Mock(side_effect=AssertionError("without-backup stage1 read a receipt"))
        self.executor.reader = receipt_reader
        result = self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=True,
        )
        self.assertEqual(result["phase"], "reboot_required")
        commands = [argv for argv, _kwargs in self.runner.calls]
        resize = commands.index(
            [
                installer_executor.BTRFS,
                "filesystem",
                "resize",
                str(self.proposal["fedora_filesystem_limit_bytes"]),
                "/",
            ]
        )
        sync = commands.index([installer_executor.BTRFS, "filesystem", "sync", "/"])
        end_change = next(index for index, argv in enumerate(commands) if "-N" in argv)
        self.assertLess(resize, sync)
        self.assertLess(sync, end_change)
        state = self.journal.load()["executor_state"]
        self.assertEqual(
            state["backup_decision"],
            {
                "without_backup": True,
                "verified": False,
                "fingerprint": self.plan["fingerprint"],
            },
        )
        self.assertNotIn("backup", state)
        self.assertEqual(receipt_reader.call_count, 0)
        self.assertNotIn(self.executor.backup_receipt_path, self.files)

    def test_without_backup_still_requires_ac_before_storage_mutation(self) -> None:
        self._set_fresh_resources(ac_online=False)
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(
                plan=self.plan,
                artifact=self.artifact,
                runner=self.runner,
                journal=self.journal,
                without_backup=True,
            )
        self.assertEqual(context.exception.code, "ac_required")
        self.assertFalse(any(self._is_mutating_command(argv) for argv, _kwargs in self.runner.calls))
        self.assertEqual(self.events, [])
        self.assertNotIn("executor_state", self.journal.load())

    def test_stage2_false_continuation_preserves_persisted_without_backup_choice(self) -> None:
        self._set_fresh_resources(ac_online=True)
        receipt_path = self.executor.backup_receipt_path

        def reader(path: Path) -> str | None:
            if path == receipt_path:
                raise AssertionError("stage2 continuation read a backup receipt")
            return None

        self.executor.reader = reader
        self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=True,
        )
        self.boot["id"] = "feedface"
        result = self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=False,
        )
        self.assertEqual(result["phase"], "installed")
        state = self.journal.load()["executor_state"]
        self.assertEqual(
            state["backup_decision"],
            {
                "without_backup": True,
                "verified": False,
                "fingerprint": self.plan["fingerprint"],
            },
        )
        self.assertNotIn("backup", state)
        self.assertNotIn(receipt_path, self.files)

    def test_mismatched_persisted_backup_decision_is_refused(self) -> None:
        self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=True,
        )
        state = copy.deepcopy(self.journal.load()["executor_state"])
        state["backup_decision"]["fingerprint"] = "sha256:" + "b" * 64
        self._replace_executor_state(state)
        calls_before = len(self.runner.calls)
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(
                plan=self.plan,
                artifact=self.artifact,
                runner=self.runner,
                journal=self.journal,
            )
        self.assertEqual(context.exception.code, "target_mismatch")
        self.assertEqual(len(self.runner.calls), calls_before)

    def test_malformed_persisted_backup_decision_is_refused(self) -> None:
        self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=True,
        )
        state = copy.deepcopy(self.journal.load()["executor_state"])
        state["backup_decision"] = {"without_backup": True, "verified": False}
        self._replace_executor_state(state)
        calls_before = len(self.runner.calls)
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(
                plan=self.plan,
                artifact=self.artifact,
                runner=self.runner,
                journal=self.journal,
            )
        self.assertEqual(context.exception.code, "invalid_state")
        self.assertEqual(len(self.runner.calls), calls_before)

    def test_null_persisted_backup_decision_is_refused(self) -> None:
        self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
            without_backup=True,
        )
        state = copy.deepcopy(self.journal.load()["executor_state"])
        state["backup_decision"] = None
        self._replace_executor_state(state)
        calls_before = len(self.runner.calls)
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(
                plan=self.plan,
                artifact=self.artifact,
                runner=self.runner,
                journal=self.journal,
            )
        self.assertEqual(context.exception.code, "invalid_state")
        self.assertEqual(len(self.runner.calls), calls_before)

    def test_verified_receipt_route_remains_the_default_stage1_path(self) -> None:
        result = self.execute_stage1()
        self.assertEqual(result["phase"], "reboot_required")
        state = self.journal.load()["executor_state"]
        self.assertEqual(
            state["backup"],
            {"verified": True, "backup_target": "/var/backups/zeus-test"},
        )
        self.assertNotIn("backup_decision", state)

    def test_disabled_by_default_and_pinned_qualification_is_explicit(self) -> None:
        self.assertFalse(installer_executor.DualBootExecutor().qualified)
        self.assertTrue(self.executor.qualified)

    def test_stage1_shrinks_before_end_only_partition_change_and_records_reboot(self) -> None:
        result = self.execute_stage1()
        commands = [argv for argv, _kwargs in self.runner.calls]
        resize = commands.index([installer_executor.BTRFS, "filesystem", "resize", str(self.proposal["fedora_filesystem_limit_bytes"]), "/"])
        sync = commands.index([installer_executor.BTRFS, "filesystem", "sync", "/"])
        end_change = next(index for index, argv in enumerate(commands) if "-N" in argv)
        self.assertLess(resize, sync)
        self.assertLess(sync, end_change)
        self.assertEqual(
            commands[end_change],
            [
                installer_executor.SFDISK,
                "--no-reread",
                "--no-tell-kernel",
                "--lock",
                "--wipe",
                "never",
                "--wipe-partitions",
                "never",
                "-N",
                "3",
                "/dev/nvme0n1",
            ],
        )
        end_kwargs = self.runner.calls[end_change][1]
        self.assertEqual(end_kwargs.get("input"), f"size={self.proposal['fedora_new_size']}\n")
        self.assertEqual(self.events, ["acquire", "release"])
        state = self.journal.load()["executor_state"]
        self.assertEqual(state["phase"], "reboot_required")
        self.assertEqual(state["expected_table"]["partitions"][2]["size"], self.proposal["fedora_new_size"])
        self.assertEqual(result["old_boot_id"], "deadbeef")

    def test_stage2_requires_changed_boot_then_uses_owned_partitions_and_scoped_efi(self) -> None:
        self.execute_stage1()
        self.boot["id"] = "feedface"
        result = self.executor.execute(
            plan=self.plan,
            artifact=self.artifact,
            runner=self.runner,
            journal=self.journal,
        )
        self.assertEqual(result["phase"], "installed")
        commands = [argv for argv, _kwargs in self.runner.calls]
        append_index = next(i for i, argv in enumerate(commands) if "--append" in argv)
        mkfs_index = next(i for i, argv in enumerate(commands) if argv[0] in {installer_executor.MKFS_FAT, installer_executor.MKFS_EXT4})
        self.assertLess(append_index, mkfs_index)
        bootup = next(
            argv
            for argv in commands
            if argv[0] == installer_executor.PODMAN
            and "--entrypoint" in argv
            and argv[argv.index("--entrypoint") + 1] == installer_executor.BOOTUPCTL
        )
        bootup_tail = bootup[bootup.index(installer_executor.BOOTUPCTL) :]
        bootup_tail.pop(1)  # the pinned OCI image reference precedes inner argv
        self.assertEqual(bootup_tail, [installer_executor.BOOTUPCTL, "backend", "install", "--component", "EFI", "--write-uuid", "/target"])
        self.assertNotIn("--device", bootup)
        self.assertNotIn("--filesystem", bootup)
        self.assertIn([installer_executor.GRUB2_MKCONFIG, "--no-grubenv-update", "-o", str(installer_executor.FEDORA_GRUB_CONFIG)], commands)
        podman = next(argv for argv in commands if argv[0] == installer_executor.PODMAN)
        self.assertIn("to-filesystem", podman)
        self.assertIn("--bootloader", podman)
        self.assertIn("none", podman)
        self.assertIn("--skip-finalize", podman)
        self.assertIn("--skip-fetch-check", podman)
        self.assertNotIn("to-disk", podman)
        self.assertNotIn("to-existing-root", podman)
        self.assertEqual(self.events, ["acquire", "release", "acquire", "release"])
        seed = self.files[installer_executor.TARGET_SEED / "user-data"].decode()
        self.assertIn("name: shane", seed)
        self.assertIn("root", seed)
        self.assertIn("disable_root: true", seed)
        self.assertIn("preserve_hostname: true", seed)
        self.assertIn("resize_rootfs: false", seed)
        self.assertEqual(self.files[self.executor.deployment_root / "etc/hostname"], b"zeus\n")
        self.assertNotIn('"root"', json.dumps(commands))
        state_text = json.dumps(self.journal.load(), sort_keys=True)
        self.assertNotIn('"password"', state_text)
        written_paths = {str(path) for path in self.files}
        self.assertIn("/target/ostree/deploy/default/deploy/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.0/etc/zeus/efi-update.json", written_paths)
        self.assertIn("/target/ostree/deploy/default/deploy/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.0/etc/zeus/efi_update.py", written_paths)
        self.assertIn("def run_update", self.files[Path("/target/ostree/deploy/default/deploy/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.0/etc/zeus/efi_update.py")].decode())
        dropin = self.files[
            Path("/target/ostree/deploy/default/deploy/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.0/etc/systemd/system/bootloader-update.service.d/zeus-efi.conf")
        ].decode()
        self.assertIn("MountFlags=slave", dropin)
        self.assertIn("RequiresMountsFor=/boot/efi", dropin)

    def _assert_failed_command_is_not_replayed(self, *, stage: int, match) -> None:
        if stage == 2:
            self.execute_stage1()
            self.boot["id"] = "feedface"
        run = self.runner.run
        hit = []
        def fail_after_command(argv, **kwargs):
            result = run(argv, **kwargs)
            if match(argv):
                hit.append(list(argv))
                return Result(returncode=1)  # The command may already have written.
            return result
        self.runner.run = fail_after_command
        with self.assertRaises(installer_executor.ExecutorError):
            self.executor.execute(plan=self.plan, artifact=self.artifact, runner=self.runner, journal=self.journal)
        self.assertEqual(len(hit), 1)
        before = len(self.runner.calls)
        state = self.journal.load()["executor_state"]
        self.assertEqual(state["status"], "in_progress")
        with self.assertRaises(installer_executor.ExecutorError) as error:
            self.executor.execute(plan=self.plan, artifact=self.artifact, runner=self.runner, journal=self.journal)
        self.assertEqual(error.exception.code, "interrupted")
        self.assertEqual(len(self.runner.calls), before)

    def test_failed_filesystem_resize_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=1, match=lambda a: a[:3] == [installer_executor.BTRFS, "filesystem", "resize"])

    def test_failed_partition_end_write_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=1, match=lambda a: "-N" in a)

    def test_failed_partition_append_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=2, match=lambda a: "--append" in a)

    def test_failed_format_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=2, match=lambda a: a[0] == installer_executor.MKFS_EXT4)

    def test_failed_payload_install_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=2, match=lambda a: "to-filesystem" in a)

    def test_failed_grub_generation_is_not_replayed(self) -> None:
        self._assert_failed_command_is_not_replayed(stage=2, match=lambda a: a[0] == installer_executor.GRUB2_MKCONFIG)

    def test_boot_mounts_replace_existing_entries_without_changing_ostree_root(self) -> None:
        old = "# original\nUUID=old-root /sysroot ext4 ro 0 1\nUUID=old-boot /boot ext4 defaults 0 2\nUUID=old-efi /boot/efi vfat defaults 0 2\nLABEL=DATA /data ext4 defaults 0 2\n"
        self.executor.deployment_root = Path("/target/ostree/deploy/default/deploy/" + "a" * 64 + ".0")
        self.executor.reader = lambda path: old
        self.executor._write_fstab({"root": "new-root", "boot": "new-boot", "esp": "A1B2-C3D4"})
        result = self.files[self.executor.deployment_root / "etc/fstab"].decode()
        self.assertIn("UUID=old-root /sysroot ext4 ro 0 1", result)
        self.assertIn("LABEL=DATA /data ext4 defaults 0 2", result)
        self.assertEqual(result.count(" /boot "), 1)
        self.assertEqual(result.count(" /boot/efi "), 1)
        self.assertNotIn("old-boot", result)
        self.assertNotIn("old-efi", result)

    def test_real_deployment_discovery_rejects_ambiguity_and_symlinks(self) -> None:
        target = Path(self.temp.name) / "target"
        parent = target / "ostree/deploy/default/deploy"
        parent.mkdir(parents=True, mode=0o755)
        for path in [target, target / "ostree", target / "ostree/deploy", parent.parent, parent]:
            path.chmod(0o755)
        deployment = parent / ("a" * 64 + ".0")
        deployment.mkdir(mode=0o755)
        exe = installer_executor.DualBootExecutor(require_root=False)
        with mock.patch.object(installer_executor, "TARGET_ROOT", target):
            self.assertEqual(exe._resolve_deployment_root(), deployment)
            other = parent / ("b" * 64 + ".0")
            other.mkdir(mode=0o755)
            with self.assertRaises(installer_executor.ExecutorError):
                exe._resolve_deployment_root()
            other.rmdir()
            other.symlink_to(deployment, target_is_directory=True)
            with self.assertRaises(installer_executor.ExecutorError):
                exe._resolve_deployment_root()

    def test_inhibitor_failure_attempts_no_storage_mutation(self) -> None:
        def fail_inhibitor():
            raise RuntimeError("no inhibitor")

        self.executor.inhibitor = fail_inhibitor
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(plan=self.plan, artifact=self.artifact, runner=self.runner, journal=self.journal)
        self.assertEqual(context.exception.code, "inhibitor_unavailable")
        mutating = {
            installer_executor.BTRFS,
            installer_executor.MKFS_FAT,
            installer_executor.MKFS_EXT4,
            installer_executor.PARTX,
            installer_executor.PODMAN,
            installer_executor.BOOTUPCTL,
        }
        self.assertFalse(any(argv[0] in mutating or "-N" in argv or "--append" in argv for argv, _ in self.runner.calls))

    def test_fresh_resource_guard_rejects_ac_loss_before_any_storage_command(self) -> None:
        self.executor.inventory_provider = lambda: {
            "supported": True,
            "blockers": [],
            "fingerprint": self.plan["fingerprint"],
            "inventory": {
                "memory": {"available_bytes": 4 * 1024**3},
                "power": {"ac_online": False},
                "staging": {"free_bytes": 16 * 1024**3},
            },
        }
        with self.assertRaises(installer_executor.ExecutorError) as context:
            self.executor.execute(plan=self.plan, artifact=self.artifact, runner=self.runner, journal=self.journal)
        self.assertEqual(context.exception.code, "ac_required")
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
