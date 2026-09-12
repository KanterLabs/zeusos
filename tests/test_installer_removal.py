"""Populated, read-only safety tests for Zeus dual-boot removal planning."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

from zeus_installer import removal  # noqa: E402


DISK_GUID = "a21c680f-f699-4bdf-95eb-e9ca012f8170"
FEDORA_GUID = "b1c6131b-68dc-4c19-9745-5778c358a541"
FEDORA_ESP_GUID = "9f143da6-ff85-45c3-ac68-51be50c23622"
FEDORA_BOOT_GUID = "2d0c8330-ad37-443c-91f0-a49843f0bcdf"
GRUB_BYTES = b"#!/bin/sh\n# Managed by Zeus dual-boot installer.\n"


def fixture() -> tuple[dict[str, object], dict[str, object]]:
    guids = [str(uuid.UUID(int=index)) for index in (4, 5, 6)]
    table = {
        "label": "gpt",
        "id": DISK_GUID,
        "device": "/dev/nvme0n1",
        "partitions": [
            {"number": 1, "node": "/dev/nvme0n1p1", "uuid": FEDORA_ESP_GUID},
            {"number": 2, "node": "/dev/nvme0n1p2", "uuid": FEDORA_BOOT_GUID},
            {"number": 3, "node": "/dev/nvme0n1p3", "uuid": FEDORA_GUID},
            {"number": 4, "node": "/dev/nvme0n1p4", "uuid": guids[0]},
            {"number": 5, "node": "/dev/nvme0n1p5", "uuid": guids[1]},
            {"number": 6, "node": "/dev/nvme0n1p6", "uuid": guids[2]},
        ],
    }
    record: dict[str, object] = {
        "schema_version": 1,
        "operation_id": "install-uuid-1234",
        "phase": "installed",
        "fingerprint": "target-fingerprint",
        "executor_state": {
            "executor": "zeus-dualboot",
            "stage": 2,
            "phase": "installed",
            "status": "complete",
            "disk": "/dev/nvme0n1",
            "original_table": copy.deepcopy(table),
            "allocated_table": copy.deepcopy(table),
            "new_guids": guids,
            "bootmenu_hash": removal.content_hash(GRUB_BYTES),
            "backup": {
                "verified": True,
                "root_trusted": True,
                "backup_target": "/var/lib/zeus/recovery",
                "fingerprint": "target-fingerprint",
                "disk_guid": DISK_GUID,
            },
        },
    }
    inventory: dict[str, object] = {
        "os": {"id": "fedora", "version_id": "43"},
        "partition_table": copy.deepcopy(table),
        "mounts": [
            {"target": "/", "source": "/dev/nvme0n1p3", "fstype": "btrfs"},
            {"target": "/boot", "source": "/dev/nvme0n1p2", "fstype": "ext4"},
            {"target": "/boot/efi", "source": "/dev/nvme0n1p1", "fstype": "vfat"},
        ],
        "block_devices": [
            {"path": "/dev/nvme0n1p3", "partuuid": FEDORA_GUID},
        ],
        "bootmenu": {"present": True, "sha256": removal.content_hash(GRUB_BYTES)},
        "zeus_mounted": False,
    }
    return record, inventory


class RemovalPlanTests(unittest.TestCase):
    def test_populated_fixture_creates_menu_only_plan_and_keeps_zeus(self) -> None:
        record, inventory = fixture()
        result = removal.build_plan(record, inventory)

        self.assertEqual(result["mode"], removal.MENU_ONLY)
        self.assertTrue(result["retains_zeus_files"])
        self.assertTrue(result["retains_zeus_partitions"])
        self.assertFalse(result["reallocate_fedora"])
        self.assertEqual(
            [operation["kind"] for operation in result["operations"]],
            ["remove_file", "regenerate_grub"],
        )
        self.assertEqual(result["disk"]["guid"], DISK_GUID)
        self.assertEqual(
            [part["guid"] for part in result["zeus_partitions"]],
            [str(uuid.UUID(int=index)) for index in (4, 5, 6)],
        )
        self.assertTrue(result["menu"]["preserve_owner_settings"])
        self.assertTrue(result["menu"]["preserve_other_entries"])
        self.assertEqual(result["data_policy"], removal.DATA_RETAIN)
        self.assertEqual(result["data"]["action"], "preserve")
        self.assertTrue(result["data"]["explicit_confirmation"])
        self.assertEqual(
            [item["number"] for item in result["data"]["resources"]],
            [4, 5, 6],
        )
        self.assertEqual(
            [(item["role"], item["number"]) for item in result["fedora_preservation"]["partitions"]],
            [("esp", 1), ("boot", 2), ("root", 3)],
        )
        self.assertFalse(result["space"]["automatic_reclaim"])

    def test_unknown_disk_identity_refuses_before_any_operation(self) -> None:
        record, inventory = fixture()
        del inventory["partition_table"]
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(record, inventory)
        self.assertEqual(context.exception.code, "disk_identity_unknown")

    def test_wrong_os_refuses_even_when_partition_id_matches(self) -> None:
        record, inventory = fixture()
        inventory["os"] = {"id": "zeus"}
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(record, inventory)
        self.assertEqual(context.exception.code, "wrong_os")

    def test_mounted_zeus_partition_refuses_destructive_mode(self) -> None:
        record, inventory = fixture()
        inventory["mounts"] = [
            *inventory["mounts"],
            {"target": "/target", "source": "/dev/nvme0n1p6", "fstype": "ext4"},
        ]
        record["executor_state"]["vm_tested"] = True
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(
                record,
                inventory,
                mode=removal.DESTRUCTIVE,
                confirm_plan_id=record["operation_id"],
            )
        self.assertEqual(context.exception.code, "zeus_mounted")

    def test_changed_managed_script_refuses_but_owner_menu_settings_are_not_restored(self) -> None:
        record, inventory = fixture()
        inventory["bootmenu"]["sha256"] = removal.content_hash(b"owner changed")
        inventory["grub_defaults_changed"] = True
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(record, inventory)
        self.assertEqual(context.exception.code, "menu_owner_changed")

    def test_owner_changed_menu_settings_are_preserved_when_script_is_unchanged(self) -> None:
        record, inventory = fixture()
        inventory["grub_defaults_changed"] = True
        result = removal.build_plan(record, inventory)
        self.assertEqual(result["menu"]["preserve_paths"], ["/etc/default/grub", "/boot/grub2/grub.cfg"])
        self.assertEqual([operation["kind"] for operation in result["operations"]], ["remove_file", "regenerate_grub"])

    def test_destructive_mode_requires_exact_confirmation_backup_and_vm_gate(self) -> None:
        record, inventory = fixture()
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(record, inventory, mode=removal.DESTRUCTIVE, confirm_plan_id="wrong")
        self.assertEqual(context.exception.code, "confirmation_required")

        record["executor_state"]["backup"] = {"verified": True, "root_trusted": True, "backup_target": "/var/lib/zeus/recovery"}
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(
                record,
                inventory,
                mode=removal.DESTRUCTIVE,
                confirm_plan_id=record["operation_id"],
            )
        self.assertEqual(context.exception.code, "removal_not_qualified")

        record["executor_state"]["vm_tested"] = True
        result = removal.build_plan(
            record,
            inventory,
            mode=removal.DESTRUCTIVE,
            confirm_plan_id=record["operation_id"],
        )
        self.assertEqual(result["mode"], removal.DESTRUCTIVE)
        self.assertEqual(result["operations"][-1]["kind"], "delete_partitions")
        self.assertEqual(
            result["operations"][-1]["partition_guids"],
            [str(uuid.UUID(int=index)) for index in (4, 5, 6)],
        )
        self.assertEqual(result["data_policy"], removal.DATA_DELETE)
        self.assertEqual(result["data"]["action"], "delete_with_partitions")
        self.assertEqual(result["operations"][-1]["owner"], "journal")
        self.assertEqual(result["operations"][-1]["data_policy"], removal.DATA_DELETE)

    def test_data_policy_is_explicit_and_cannot_conflict_with_mode(self) -> None:
        record, inventory = fixture()
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(record, inventory, data_policy=removal.DATA_DELETE)
        self.assertEqual(context.exception.code, "data_policy_conflict")

        record["executor_state"]["vm_tested"] = True
        with self.assertRaises(removal.RemovalError) as context:
            removal.build_plan(
                record,
                inventory,
                mode=removal.DESTRUCTIVE,
                data_policy=removal.DATA_RETAIN,
                confirm_plan_id=record["operation_id"],
            )
        self.assertEqual(context.exception.code, "data_policy_conflict")

    def test_plan_validation_rejects_space_reclaim_or_fedora_boot_mutation(self) -> None:
        record, inventory = fixture()
        result = removal.build_plan(record, inventory)
        result["space"]["automatic_reclaim"] = True
        with self.assertRaises(removal.RemovalError) as context:
            removal.validate_plan(result)
        self.assertEqual(context.exception.code, "invalid_plan")

        result = removal.build_plan(record, inventory)
        result["fedora_preservation"]["retain_esp"] = False
        with self.assertRaises(removal.RemovalError) as context:
            removal.validate_plan(result)
        self.assertEqual(context.exception.code, "invalid_plan")

    def test_cancellation_is_a_noop_and_repeat_menu_removal_is_safe(self) -> None:
        record, inventory = fixture()
        executor = removal.RemovalExecutor()
        cancelled = executor.execute(removal.build_plan(record, inventory), cancelled=True)
        self.assertTrue(cancelled["cancelled"])
        self.assertFalse(cancelled["mutated"])

        inventory["bootmenu"] = {"present": False}
        repeated = removal.build_plan(record, inventory)
        self.assertEqual(repeated["menu"]["action"], "already_disabled")
        self.assertEqual(repeated["operations"], [])

    def test_plan_rejects_arbitrary_command_and_executor_is_dry_run_by_default(self) -> None:
        record, inventory = fixture()
        result = removal.build_plan(record, inventory)
        result["operations"][0]["path"] = "/home/shane"
        with self.assertRaises(removal.RemovalError):
            removal.validate_plan(result)

        result = removal.build_plan(record, inventory)
        preview = removal.RemovalExecutor().execute(result)
        self.assertTrue(preview["ok"])
        self.assertFalse(preview["mutated"])

    def test_qualified_hook_only_runs_fixed_menu_operations_and_records_boundary(self) -> None:
        record, inventory = fixture()
        files = {removal.FEDORA_GRUB_SCRIPT: GRUB_BYTES}
        commands: list[list[str]] = []

        class MemoryJournal:
            require_root = False

            def __init__(self, value: dict[str, object]) -> None:
                self.value = value

            def load(self) -> dict[str, object]:
                return self.value

            def write(self, value: dict[str, object]) -> None:
                self.value = copy.deepcopy(value)

        class Runner:
            def run(self, argv: list[str]) -> object:
                commands.append(argv)
                return type("Result", (), {"returncode": 0})()

        journal = MemoryJournal(record)
        executor = removal.RemovalExecutor(
            qualified=True,
            reader=lambda path: files.get(path),
            remove_file=lambda path: files.pop(path, None),
            runner=Runner(),
            require_root=False,
        )
        result = removal.remove(journal, inventory, executor=executor, apply=True, require_root=False)
        self.assertEqual(result["state"], "menu_disabled")
        self.assertEqual(commands, [[removal.GRUB2_MKCONFIG, removal.GRUB_NO_GRUBENV_UPDATE, "-o", str(removal.FEDORA_GRUB_CONFIG)]])
        self.assertNotIn(removal.FEDORA_GRUB_SCRIPT, files)
        self.assertEqual(journal.value["removal"]["phase"], "menu_disabled")

        repeated = removal.remove(journal, {**inventory, "bootmenu": {"present": False}}, executor=executor, apply=True, require_root=False)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(commands, [[removal.GRUB2_MKCONFIG, removal.GRUB_NO_GRUBENV_UPDATE, "-o", str(removal.FEDORA_GRUB_CONFIG)]])

    def test_qualified_destructive_hook_deletes_only_journal_owned_partitions(self) -> None:
        record, inventory = fixture()
        record["executor_state"]["vm_tested"] = True
        commands: list[list[str]] = []

        class MemoryJournal:
            require_root = False

            def __init__(self, value: dict[str, object]) -> None:
                self.value = value

            def load(self) -> dict[str, object]:
                return self.value

            def write(self, value: dict[str, object]) -> None:
                self.value = copy.deepcopy(value)

        class Runner:
            def run(self, argv: list[str]) -> object:
                commands.append(argv)
                return type("Result", (), {"returncode": 0})()

        journal = MemoryJournal(record)
        plan = removal.build_plan(
            record,
            inventory,
            mode=removal.DESTRUCTIVE,
            data_policy=removal.DATA_DELETE,
            confirm_plan_id=record["operation_id"],
            require_root=False,
        )
        executor = removal.RemovalExecutor(
            qualified=True,
            reader=lambda _path: None,
            remove_file=lambda _path: None,
            runner=Runner(),
            require_root=False,
        )
        result = removal.remove(
            journal,
            inventory,
            mode=removal.DESTRUCTIVE,
            data_policy=removal.DATA_DELETE,
            confirm_plan_id=record["operation_id"],
            executor=executor,
            apply=True,
            require_root=False,
        )
        self.assertEqual(result["state"], "partitions_removed")
        self.assertEqual(
            commands,
            [
                [removal.GRUB2_MKCONFIG, removal.GRUB_NO_GRUBENV_UPDATE, "-o", str(removal.FEDORA_GRUB_CONFIG)],
                [removal.SGDISK, "--delete", "4", "--delete", "5", "--delete", "6", "/dev/nvme0n1"],
            ],
        )
        self.assertEqual(journal.value["removal"]["data_policy"], removal.DATA_DELETE)
        self.assertTrue(journal.value["removal"]["fedora_preserved"])
        self.assertFalse(journal.value["removal"]["automatic_reclaim"])
        self.assertEqual(plan["data"]["resources"], plan["zeus_resources"])


if __name__ == "__main__":
    unittest.main()
