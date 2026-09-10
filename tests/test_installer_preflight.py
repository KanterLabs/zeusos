from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

from zeus_installer import preflight, storage


GIB = preflight.GIB
SECTOR = 512
DISK_PATH = "/dev/nvme0n1"
DISK_GUID = "a21c680f-f699-4bdf-95eb-e9ca012f8170"
PART_GUIDS = (
    "9f143da6-ff85-45c3-ac68-51be50c23622",
    "2d0c8330-ad37-443c-91f0-a49843f0bcdf",
    "b1c6131b-68dc-4c19-9745-5778c358a541",
)
FS_GUIDS = (
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
)


def _fixture() -> dict[str, object]:
    """Return the reported N154G 600 MiB ESP/1 GiB boot layout."""

    disk_sectors = int(931.5 * GIB // SECTOR)
    esp_sectors = 600 * 1024**2 // SECTOR
    boot_sectors = GIB // SECTOR
    root_start = 2048 + esp_sectors + boot_sectors
    root_sectors = disk_sectors - 34 + 1 - root_start
    table = {
        "label": "gpt",
        "id": DISK_GUID,
        "device": DISK_PATH,
        "unit": "sectors",
        "firstlba": 34,
        "lastlba": disk_sectors - 34,
        "sectorsize": SECTOR,
        "partitions": [
            {
                "node": f"{DISK_PATH}p1",
                "start": 2048,
                "size": esp_sectors,
                "type": storage.EFI,
                "uuid": PART_GUIDS[0],
                "name": "EFI System Partition",
            },
            {
                "node": f"{DISK_PATH}p2",
                "start": 2048 + esp_sectors,
                "size": boot_sectors,
                "type": storage.LINUX,
                "uuid": PART_GUIDS[1],
                "name": "Fedora boot",
            },
            {
                "node": f"{DISK_PATH}p3",
                "start": root_start,
                "size": root_sectors,
                "type": storage.LINUX,
                "uuid": PART_GUIDS[2],
                "name": "Fedora Btrfs",
            },
        ],
    }
    partitions = [
        {
            "name": f"nvme0n1p{number}",
            "kname": f"nvme0n1p{number}",
            "path": f"{DISK_PATH}p{number}",
            "type": "part",
            "size": size * SECTOR,
            "partuuid": partuuid,
            "parttype": parttype,
            "pkname": "nvme0n1",
            "partn": number,
            "start": start,
            "fstype": fstype,
            "uuid": fsuuid,
        }
        for number, size, start, partuuid, parttype, fstype, fsuuid in (
            (1, esp_sectors, 2048, PART_GUIDS[0], storage.EFI, "vfat", FS_GUIDS[0]),
            (2, boot_sectors, 2048 + esp_sectors, PART_GUIDS[1], storage.LINUX, "ext4", FS_GUIDS[1]),
            (3, root_sectors, root_start, PART_GUIDS[2], storage.LINUX, "btrfs", FS_GUIDS[2]),
        )
    ]
    lsblk = {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "kname": "nvme0n1",
                "path": DISK_PATH,
                "type": "disk",
                "size": disk_sectors * SECTOR,
                "model": "N154G NVMe",
                "serial": "N154G-SSD-001",
                "pttype": "gpt",
                "ptuuid": DISK_GUID,
                "log-sec": SECTOR,
                "phy-sec": SECTOR,
                "children": partitions,
            }
        ]
    }
    findmnt = {
        "filesystems": [
            {
                "target": "/",
                "source": f"{DISK_PATH}p3[/root]",
                "fstype": "btrfs",
                "options": "rw,relatime,subvol=root",
                "fsroot": "/root",
                "children": [
                    {
                        "target": "/home",
                        "source": f"{DISK_PATH}p3[/home]",
                        "fstype": "btrfs",
                        "options": "rw,relatime,subvol=home",
                        "fsroot": "/home",
                    },
                    {
                        "target": "/boot",
                        "source": f"{DISK_PATH}p2",
                        "fstype": "ext4",
                        "options": "rw,relatime",
                    },
                    {
                        "target": "/boot/efi",
                        "source": f"{DISK_PATH}p1",
                        "fstype": "vfat",
                        "options": "rw,relatime",
                    },
                ],
            }
        ]
    }
    # These numbers mirror a realistic occupied Fedora root while retaining
    # enough measured slack for the proposed 128 GiB Zeus allocation.
    device_bytes = root_sectors * SECTOR
    usage = "\n".join(
        (
            "Overall:",
            f"    Device size:              {device_bytes}",
            f"    Used:                      {350 * GIB}",
            f"    Free (estimated):         {500 * GIB}",
        )
    )
    show = "\n".join(
        (
            f"Label: none  uuid: {FS_GUIDS[2]}",
            f"\tTotal devices 1 FS bytes used {350 * GIB}",
            f"\tdevid    1 size {device_bytes} used {350 * GIB} path {DISK_PATH}p3",
        )
    )
    efibootmgr = (
        "BootCurrent: 0001\n"
        "Timeout: 1 seconds\n"
        "BootOrder: 0001,0002\n"
        f"Boot0001* Fedora HD(1,GPT,{PART_GUIDS[0]},0x800)/File(\\EFI\\fedora\\shimx64.efi)\n"
        "Boot0002* Linux Firmware\n"
    )
    return {
        "table": table,
        "lsblk": lsblk,
        "findmnt": findmnt,
        "usage": usage,
        "show": show,
        "efibootmgr": efibootmgr,
        "uname": "Linux n154g 6.18.0-43.fc43.x86_64 #1 SMP x86_64 GNU/Linux\n",
    }


class FixtureRunner:
    def __init__(self, fixture: dict[str, object]):
        self.fixture = fixture
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> tuple[int, str, str]:
        self.calls.append((list(command), dict(kwargs)))
        name = Path(command[0]).name
        if name == "lsblk":
            return 0, json.dumps(self.fixture["lsblk"]), ""
        if name == "findmnt":
            return 0, json.dumps(self.fixture["findmnt"]), ""
        if name == "efibootmgr":
            return 0, str(self.fixture["efibootmgr"]), ""
        if name == "sfdisk":
            return 0, json.dumps({"partitiontable": self.fixture["table"]}), ""
        if name == "uname":
            return 0, str(self.fixture["uname"]), ""
        if name == "systemd-detect-virt":
            return 1, "", ""
        if name == "btrfs" and command[2] == "usage":
            return 0, str(self.fixture["usage"]), ""
        if name == "btrfs" and command[2] == "show":
            return 0, str(self.fixture["show"]), ""
        raise AssertionError(f"unexpected read-only command: {command!r}")


def _write_sysfs_fixture(root: Path) -> tuple[Path, Path, Path]:
    sysfs = root / "sys"
    proc = root / "proc"
    etc = root / "etc"
    (sysfs / "firmware" / "efi" / "efivars").mkdir(parents=True)
    (sysfs / "firmware" / "efi" / "efivars" / preflight._SECURE_BOOT_VARIABLE).write_bytes(
        b"\x07\x00\x00\x00\x00"
    )
    (sysfs / "class" / "dmi" / "id").mkdir(parents=True)
    for key, value in {
        "product_name": "N154G",
        "product_version": "1.0",
        "sys_vendor": "Nimo",
        "board_name": "N154G",
    }.items():
        (sysfs / "class" / "dmi" / "id" / key).write_text(value + "\n", encoding="utf-8")
    for name, values in {
        "AC": {"type": "Mains", "online": "1"},
        "BAT0": {"type": "Battery", "capacity": "91", "status": "Charging"},
    }.items():
        source = sysfs / "class" / "power_supply" / name
        source.mkdir(parents=True)
        for key, value in values.items():
            (source / key).write_text(value + "\n", encoding="utf-8")
    proc.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal:       8192000 kB\nMemAvailable:   7920000 kB\n", encoding="utf-8"
    )
    etc.mkdir()
    (etc / "os-release").write_text(
        'NAME="Fedora Linux"\nID=fedora\nVERSION_ID="43"\nPRETTY_NAME="Fedora Linux 43"\n',
        encoding="utf-8",
    )
    return sysfs, proc, etc


class PreflightTests(unittest.TestCase):
    def test_collect_and_plan_report_n154g_without_writes(self) -> None:
        fixture = _fixture()
        runner = FixtureRunner(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, proc, etc = _write_sysfs_fixture(root)
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"Fedora data must remain untouched")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            with mock.patch.object(preflight.os, "geteuid", return_value=0):
                inventory = preflight.collect(
                    runner=runner,
                    sysfs_root=sysfs,
                    proc_root=proc,
                    etc_root=etc,
                    staging_paths=[ROOT],
                )
                result = preflight.plan(inventory, allocation_gib=128)
            self.assertEqual([], inventory["errors"])
            self.assertEqual("N154G", inventory["host"]["product_name"])
            self.assertEqual("x86_64", inventory["kernel"]["machine"])
            self.assertEqual("disabled", inventory["secure_boot"]["state"])
            self.assertFalse(inventory["virtualization"]["is_virtual"])
            self.assertEqual(
                {"/", "/home", "/boot", "/boot/efi"},
                {mount["target"] for mount in inventory["mounts"]},
            )
            self.assertEqual(1, inventory["btrfs"]["device_count"])
            self.assertGreater(inventory["btrfs"]["minimum_size_bytes"], 0)
            self.assertTrue(result["supported"])
            self.assertEqual([], result["blockers"])
            self.assertIs(result["target"], result["proposed_target_layout"])
            self.assertEqual(3, len(result["target"]["partitions"]))
            self.assertEqual(128, result["target"]["allocation_gib"])
            self.assertEqual(
                result["target"]["partitions"][0]["start"],
                result["target"]["fedora_new_size"] + result["target"]["fedora_start"],
            )
            self.assertEqual(2 * GIB // SECTOR, result["target"]["partitions"][1]["size"])
            self.assertEqual("inside_zeus_root:/var/home", result["target"]["home_location"])
            self.assertTrue(any("shrink_btrfs_filesystem_live" == op for op in result["target"]["operations"]))
            self.assertFalse(any("offline" in op for op in result["target"]["operations"]))
            self.assertNotIn("reread_partition_table", result["target"]["operations"])
            self.assertEqual(before, hashlib.sha256(sentinel.read_bytes()).hexdigest())

        self.assertTrue(runner.calls)
        for command, kwargs in runner.calls:
            self.assertFalse(kwargs.get("shell"))
            self.assertNotIn(Path(command[0]).name, {"mkfs", "sfdisk-write", "parted", "wipefs"})
        btrfs_calls = [command for command, _ in runner.calls if Path(command[0]).name == "btrfs"]
        self.assertEqual(
            [["/usr/sbin/btrfs", "filesystem", "usage", "--raw", "/"],
             ["/usr/sbin/btrfs", "filesystem", "show", "--raw", "/"]],
            btrfs_calls,
        )
        virtualization_calls = [
            command for command, _ in runner.calls if Path(command[0]).name == "systemd-detect-virt"
        ]
        self.assertEqual([["/usr/bin/systemd-detect-virt", "--vm"]], virtualization_calls)

    def test_missing_and_malformed_collection_errors_fail_closed(self) -> None:
        def malformed(command: list[str], **kwargs: object) -> tuple[int, str, str]:
            name = Path(command[0]).name
            if name in {"lsblk", "findmnt", "sfdisk"}:
                return 0, "{not-json", ""
            if name == "efibootmgr":
                return 1, "", "EFI variables are not supported"
            if name == "btrfs":
                return 0, "garbled btrfs output", ""
            if name == "uname":
                return 0, "Linux test 1 x86_64", ""
            if name == "systemd-detect-virt":
                return 1, "", ""
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, proc, etc = _write_sysfs_fixture(root)
            with mock.patch.object(preflight.os, "geteuid", return_value=0):
                inventory = preflight.collect(
                    runner=malformed,
                    sysfs_root=sysfs,
                    proc_root=proc,
                    etc_root=etc,
                    staging_paths=[ROOT],
                )
            codes = {error["code"] for error in inventory["errors"]}
            self.assertIn("malformed_json", codes)
            self.assertIn("malformed_btrfs_output", codes)
            result = preflight.plan(inventory)
            self.assertFalse(result["supported"])
        self.assertIn("malformed_inventory", result["blockers"])
        self.assertIn("partition_table_unavailable", result["blockers"])

    def test_vm_power_exception_requires_virtualization_proof_and_secure_boot_disabled(self) -> None:
        fixture = _fixture()
        runner = FixtureRunner(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, proc, etc = _write_sysfs_fixture(root)
            with mock.patch.object(preflight.os, "geteuid", return_value=0):
                inventory = preflight.collect(
                    runner=runner,
                    sysfs_root=sysfs,
                    proc_root=proc,
                    etc_root=etc,
                    staging_paths=[ROOT],
                )

        vm = copy.deepcopy(inventory)
        vm["power"] = {"ac_online": None, "sources": []}
        vm["virtualization"] = {"is_virtual": True, "technology": "kvm"}
        self.assertTrue(preflight.plan(vm)["supported"])

        unproven = copy.deepcopy(vm)
        unproven["virtualization"] = {"is_virtual": False}
        unproven_result = preflight.plan(unproven)
        self.assertFalse(unproven_result["supported"])
        self.assertIn("power_state_unknown", unproven_result["blockers"])

        enabled = copy.deepcopy(vm)
        enabled["secure_boot"] = {"enabled": True, "state": "enabled"}
        enabled_result = preflight.plan(enabled)
        self.assertFalse(enabled_result["supported"])
        self.assertIn("secure_boot_enabled", enabled_result["blockers"])

        unknown = copy.deepcopy(vm)
        unknown["secure_boot"] = {"state": "unknown"}
        unknown_result = preflight.plan(unknown)
        self.assertFalse(unknown_result["supported"])
        self.assertIn("secure_boot_unknown", unknown_result["blockers"])

    def test_changed_partition_ids_change_fingerprint_and_block_plan(self) -> None:
        fixture = _fixture()
        runner = FixtureRunner(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, proc, etc = _write_sysfs_fixture(root)
            with mock.patch.object(preflight.os, "geteuid", return_value=0):
                inventory = preflight.collect(
                    runner=runner,
                    sysfs_root=sysfs,
                    proc_root=proc,
                    etc_root=etc,
                    staging_paths=[ROOT],
                )
            good = preflight.plan(inventory)
            changed = copy.deepcopy(inventory)
            changed["partition_table"]["partitions"][2]["uuid"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            changed_result = preflight.plan(changed)
        self.assertNotEqual(good["fingerprint"], changed_result["fingerprint"])
        self.assertFalse(changed_result["supported"])
        self.assertIn("partition_id_changed", changed_result["blockers"])

        malformed = copy.deepcopy(inventory)
        malformed["partition_table"]["id"] = "changed-disk-id"
        malformed_result = preflight.plan(malformed)
        self.assertFalse(malformed_result["supported"])
        self.assertIn("malformed_partition_table_id", malformed_result["blockers"])

    def test_unsupported_layouts_and_resources_are_refused(self) -> None:
        fixture = _fixture()
        runner = FixtureRunner(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, proc, etc = _write_sysfs_fixture(root)
            with mock.patch.object(preflight.os, "geteuid", return_value=0):
                inventory = preflight.collect(
                    runner=runner,
                    sysfs_root=sysfs,
                    proc_root=proc,
                    etc_root=etc,
                    staging_paths=[ROOT],
                )
        cases = {
            "non_uefi": {"firmware": {"uefi": False}},
            "encrypted": {"block_devices": [{"fstype": "crypto_LUKS"}]},
            "multi_device": {"btrfs": {"device_count": 2}},
            "no_ac": {"power": {"ac_online": False}},
            "low_staging": {"staging": {"free_bytes": 1 * GIB}},
            "low_ram": {"memory": {"available_bytes": int(1.75 * GIB)}},
            "low_total_ram": {
                "memory": {
                    "total_bytes": int(7.25 * GIB),
                    "available_bytes": int(3 * GIB),
                }
            },
            "wrong_release": {"os": {"id": "fedora", "version_id": "42"}},
        }
        expected = {
            "non_uefi": "non_uefi",
            "encrypted": "unsupported_encryption",
            "multi_device": "multi_device_btrfs",
            "no_ac": "ac_required",
            "low_staging": "insufficient_staging_space",
            "low_ram": "insufficient_ram",
            "low_total_ram": "insufficient_ram",
            "wrong_release": "unsupported_fedora_version",
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                candidate = copy.deepcopy(inventory)
                for key, value in changes.items():
                    candidate[key] = value
                result = preflight.plan(candidate)
                self.assertFalse(result["supported"])
                self.assertIn(expected[name], result["blockers"])


if __name__ == "__main__":
    unittest.main()
