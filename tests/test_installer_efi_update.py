"""Safety tests for the UUID-scoped bootupd EFI update wrapper."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = str(ROOT / "installer")
if INSTALLER not in sys.path:
    sys.path.insert(0, INSTALLER)

from zeus_installer import efi_update  # noqa: E402


ZEUS_UUID = "1234-ABCD"
FEDORA_UUID = "A1B2-C3D4"


def mount_output(esp_uuid: str) -> str:
    return json.dumps(
        {
            "filesystems": [
                {
                    "target": "/boot/efi",
                    "source": f"/dev/disk/by-uuid/{esp_uuid}",
                    "fstype": "vfat",
                    "uuid": esp_uuid,
                }
            ]
        }
    )


class MountRunner:
    """Small command fixture with mount state and bootupd hooks."""

    def __init__(self, mounted_uuid: str | None = None) -> None:
        self.mounted_uuid = mounted_uuid
        self.calls: list[list[str]] = []
        self.bootupd_returncode = 0
        self.bootupd_version = efi_update.SUPPORTED_BOOTUPCTL_VERSION
        self.root_count = 1
        self.drift_after_update: str | None = None
        self.disappear_after_update = False

    def __call__(self, command: list[str], **_kwargs: object) -> dict[str, object]:
        self.calls.append(list(command))
        if command[0] == efi_update.BOOTUPCTL_COMMAND and command[1:] == ["--version"]:
            return {
                "returncode": 0,
                "stdout": f"bootupctl {self.bootupd_version}\n",
                "stderr": "",
            }
        if command[0] == efi_update.FINDMNT_COMMAND and "--target" in command:
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {"filesystems": [{"source": "/dev/sda4"}]}
                ),
                "stderr": "",
            }
        if command[0] == efi_update.LSBLK_COMMAND:
            roots = [
                {"name": f"sd{chr(ord('a') + index)}", "type": "disk"}
                for index in range(self.root_count)
            ]
            return {"returncode": 0, "stdout": json.dumps({"blockdevices": roots}), "stderr": ""}
        if command[0] == efi_update.FINDMNT_COMMAND:
            if self.mounted_uuid is None:
                return {"returncode": 1, "stdout": "", "stderr": "not a mountpoint"}
            return {"returncode": 0, "stdout": mount_output(self.mounted_uuid), "stderr": ""}
        if command[0] == efi_update.MOUNT_COMMAND:
            self.assert_args(command, [efi_update.MOUNT_COMMAND, "--uuid", ZEUS_UUID, "/boot/efi"])
            self.mounted_uuid = ZEUS_UUID
            return {"returncode": 0}
        if command[0] == efi_update.UMOUNT_COMMAND:
            self.assert_args(command, [efi_update.UMOUNT_COMMAND, "/boot/efi"])
            self.mounted_uuid = None
            return {"returncode": 0}
        if command[0] == efi_update.BOOTUPCTL_COMMAND:
            self.assert_args(command, [efi_update.BOOTUPCTL_COMMAND, "update"])
            if self.disappear_after_update:
                self.mounted_uuid = None
            elif self.drift_after_update is not None:
                self.mounted_uuid = self.drift_after_update
            return {"returncode": self.bootupd_returncode, "stdout": "updated\n", "stderr": ""}
        raise AssertionError(f"unexpected command: {command!r}")

    @staticmethod
    def assert_args(actual: list[str], expected: list[str]) -> None:
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, got {actual!r}")


class ScopeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_parent = ROOT / "out" / "installer" / "tmp"
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_parent)
        self.path = Path(self.temp.name) / "etc" / "zeus" / "efi-update.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, value: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.write_text(json.dumps(value), encoding="utf-8")
        self.path.chmod(0o644)

    def test_writer_is_atomic_and_normalizes_uuid(self) -> None:
        result = efi_update.write_scope_config(self.path, ZEUS_UUID.lower(), require_root=False)
        self.assertEqual(result, self.path)
        self.assertEqual(
            efi_update.load_scope(self.path, require_root=False),
            efi_update.EfiScope(ZEUS_UUID),
        )
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_symlink_config_fails_closed(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.parent.chmod(0o700)
        target = self.path.parent / "outside.json"
        efi_update.write_scope_config(target, ZEUS_UUID, require_root=False)
        self.path.symlink_to(target)
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.load_scope(self.path, require_root=False)
        self.assertEqual(context.exception.code, "unsafe_config")

    def test_group_writable_config_fails_closed(self) -> None:
        self.write(
            {
                "schema_version": 1,
                "esp_uuid": ZEUS_UUID,
                "mountpoint": "/boot/efi",
            }
        )
        self.path.chmod(0o664)
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.load_scope(self.path, require_root=False)
        self.assertEqual(context.exception.code, "unsafe_config_mode")

    def test_schema_cannot_change_mountpoint_or_uuid(self) -> None:
        for value, code in (
            (
                {"schema_version": 1, "esp_uuid": ZEUS_UUID, "mountpoint": "/efi"},
                "invalid_mountpoint",
            ),
            (
                {"schema_version": 1, "esp_uuid": "not-a-uuid", "mountpoint": "/boot/efi"},
                "invalid_esp_uuid",
            ),
            (
                {
                    "schema_version": 1,
                    "esp_uuid": "11111111-2222-3333-4444-555555555555",
                    "mountpoint": "/boot/efi",
                },
                "invalid_esp_uuid",
            ),
        ):
            self.write(value)
            with self.assertRaises(efi_update.EfiUpdateError) as context:
                efi_update.load_scope(self.path, require_root=False)
            self.assertEqual(context.exception.code, code)


class ScopedUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_parent = ROOT / "out" / "installer" / "tmp"
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_parent)
        self.config = Path(self.temp.name) / "efi-update.json"
        efi_update.write_scope_config(self.config, ZEUS_UUID, require_root=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_existing_zeus_mount_is_used_and_left_mounted(self) -> None:
        runner = MountRunner(ZEUS_UUID)
        result = efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["mounted_for_update"])
        self.assertEqual(runner.mounted_uuid, ZEUS_UUID)
        self.assertEqual(
            runner.calls,
            [
                [efi_update.BOOTUPCTL_COMMAND, "--version"],
                [efi_update.FINDMNT_COMMAND, "--json", "--target", "/boot", "--output", "SOURCE"],
                [efi_update.LSBLK_COMMAND, "-J", "-b", "-O", "--inverse", "/dev/sda4"],
                [
                    efi_update.FINDMNT_COMMAND,
                    "--json",
                    "--mountpoint",
                    "/boot/efi",
                    "--output",
                    "TARGET,SOURCE,FSTYPE,UUID",
                ],
                [efi_update.BOOTUPCTL_COMMAND, "update"],
                [
                    efi_update.FINDMNT_COMMAND,
                    "--json",
                    "--mountpoint",
                    "/boot/efi",
                    "--output",
                    "TARGET,SOURCE,FSTYPE,UUID",
                ],
            ],
        )

    def test_unmounted_zeus_esp_is_mounted_for_update_then_cleaned_up(self) -> None:
        runner = MountRunner()
        result = efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["mounted_for_update"])
        self.assertIsNone(runner.mounted_uuid)
        self.assertEqual(
            runner.calls,
            [
                [efi_update.BOOTUPCTL_COMMAND, "--version"],
                [efi_update.FINDMNT_COMMAND, "--json", "--target", "/boot", "--output", "SOURCE"],
                [efi_update.LSBLK_COMMAND, "-J", "-b", "-O", "--inverse", "/dev/sda4"],
                [
                    efi_update.FINDMNT_COMMAND,
                    "--json",
                    "--mountpoint",
                    "/boot/efi",
                    "--output",
                    "TARGET,SOURCE,FSTYPE,UUID",
                ],
                [efi_update.MOUNT_COMMAND, "--uuid", ZEUS_UUID, "/boot/efi"],
                [
                    efi_update.FINDMNT_COMMAND,
                    "--json",
                    "--mountpoint",
                    "/boot/efi",
                    "--output",
                    "TARGET,SOURCE,FSTYPE,UUID",
                ],
                [efi_update.BOOTUPCTL_COMMAND, "update"],
                [
                    efi_update.FINDMNT_COMMAND,
                    "--json",
                    "--mountpoint",
                    "/boot/efi",
                    "--output",
                    "TARGET,SOURCE,FSTYPE,UUID",
                ],
                [efi_update.UMOUNT_COMMAND, "/boot/efi"],
            ],
        )

    def test_fedora_mount_is_never_unmounted_or_updated(self) -> None:
        runner = MountRunner(FEDORA_UUID)
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertEqual(context.exception.code, "wrong_esp_mounted")
        self.assertNotIn([efi_update.BOOTUPCTL_COMMAND, "update"], runner.calls)
        self.assertNotIn([efi_update.MOUNT_COMMAND, "--uuid", ZEUS_UUID, "/boot/efi"], runner.calls)
        self.assertEqual(runner.mounted_uuid, FEDORA_UUID)

    def test_bootupd_failure_still_leaves_preexisting_zeus_mount(self) -> None:
        runner = MountRunner(ZEUS_UUID)
        runner.bootupd_returncode = 1
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertEqual(context.exception.code, "bootupd_failed")
        self.assertEqual(runner.mounted_uuid, ZEUS_UUID)

    def test_unsupported_bootupctl_version_fails_before_mount_probe(self) -> None:
        runner = MountRunner(ZEUS_UUID)
        runner.bootupd_version = "0.2.34"
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertEqual(context.exception.code, "unsupported_bootupd_version")
        self.assertEqual(runner.calls, [[efi_update.BOOTUPCTL_COMMAND, "--version"]])
        self.assertEqual(runner.mounted_uuid, ZEUS_UUID)

    def test_multiple_backing_disks_fail_before_esp_access(self) -> None:
        runner = MountRunner(ZEUS_UUID)
        runner.root_count = 2
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertEqual(context.exception.code, "multiple_root_devices")
        self.assertNotIn([efi_update.BOOTUPCTL_COMMAND, "update"], runner.calls)
        self.assertNotIn(
            [
                efi_update.FINDMNT_COMMAND,
                "--json",
                "--mountpoint",
                "/boot/efi",
                "--output",
                "TARGET,SOURCE,FSTYPE,UUID",
            ],
            runner.calls,
        )
        self.assertEqual(runner.mounted_uuid, ZEUS_UUID)

    def test_post_update_mount_drift_fails_closed_without_unmounting_drifted_mount(self) -> None:
        runner = MountRunner()
        runner.drift_after_update = FEDORA_UUID
        with self.assertRaises(efi_update.EfiUpdateError) as context:
            efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertEqual(context.exception.code, "wrong_esp_mounted")
        self.assertEqual(runner.mounted_uuid, FEDORA_UUID)
        self.assertNotIn(efi_update.UMOUNT_COMMAND, [call[0] for call in runner.calls])

    def test_preexisting_mount_that_disappears_is_restored(self) -> None:
        runner = MountRunner(ZEUS_UUID)
        runner.disappear_after_update = True
        result = efi_update.run_update(self.config, runner=runner, require_root=False)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.mounted_uuid, ZEUS_UUID)
        self.assertEqual(
            [call[0] for call in runner.calls].count(efi_update.MOUNT_COMMAND),
            1,
        )


if __name__ == "__main__":
    unittest.main()
