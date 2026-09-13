"""Fixture-driven contracts for the one-shot boot chooser runtime."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "desktop/rootfs/usr/lib/zeus"
sys.path.insert(0, str(MODULES))

import boot_chooser as chooser  # noqa: E402


FEDORA_ENTRY = "Boot0001"
OTHER_ENTRY = "Boot0002"
FEDORA_ESP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FEDORA_BOOT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_ESP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def marker_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "fedora_boot_entry": FEDORA_ENTRY,
        "fedora_boot_path": chooser.FEDORA_SHIM_PATH,
        "fedora_esp_partuuid": FEDORA_ESP,
        "fedora_boot_partuuid": FEDORA_BOOT,
        "grub_entry_id": chooser.MANAGED_GRUB_ENTRY_ID,
        "grub_timeout_style": "menu",
        "grub_timeout": 5,
        "default_preserved": True,
    }
    payload.update(overrides)
    return payload


def efi_text(
    *,
    boot_next: str | None = None,
    duplicate: bool = False,
    changed: bool = False,
    shared_esp: bool = False,
    active: bool = True,
) -> str:
    path = r"\EFI\fedora\shimx64.efi"
    description = "Changed Fedora" if changed else "Fedora"
    output = [
        "BootCurrent: 0001",
        "Timeout: 1 seconds",
        "BootOrder: 0001,0002",
    ]
    if boot_next is not None:
        output.append(f"BootNext: {boot_next[4:]}")
    active_marker = "*" if active else ""
    output.append(
        f"Boot0001{active_marker} {description} HD(1,GPT,{FEDORA_ESP},0x800)/File({path})"
    )
    if duplicate:
        output.append(
            f"Boot0002* Fedora copy HD(1,GPT,{FEDORA_ESP},0x800)/File({path})"
        )
    elif shared_esp:
        output.append(
            f"Boot0002* Windows Boot Manager HD(1,GPT,{FEDORA_ESP},0x800)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)"
        )
    else:
        output.append("Boot0002* Linux Firmware")
    return "\n".join(output) + "\n"


class BootFixture:
    """Small command fixture with mount and EFI state transitions."""

    def __init__(self, mount_path: Path) -> None:
        self.mount_path = mount_path
        self.calls: list[list[str]] = []
        self.boot_next: str | None = None
        self.mount_state = False
        self.mount_failure = False
        self.config_changed = False
        self.readback_mismatch: str | None = None
        self.poweroff_code = 0
        self.nvram_changed = False
        self.path_changed = False
        self.entry_duplicate = False
        self.existing_mountpoint: str | None = None
        self.efi_query_code = 0
        self._set_seen = False

    def __call__(self, arguments: list[str], _timeout: int) -> tuple[int, str, str]:
        args = list(arguments)
        self.calls.append(args)
        name = Path(args[0]).name
        if name == "efibootmgr":
            if args[1:] == ["-v"]:
                if self.efi_query_code:
                    return self.efi_query_code, "", "EFI variables are unavailable"
                boot_next = self.readback_mismatch if self._set_seen and self.readback_mismatch else self.boot_next
                changed = self.nvram_changed and self._set_seen
                output = efi_text(
                    boot_next=boot_next,
                    duplicate=self.entry_duplicate,
                    changed=changed,
                )
                if self.path_changed:
                    output = output.replace(
                        r"\EFI\fedora\shimx64.efi", r"\EFI\other\shimx64.efi"
                    )
                return 0, output, ""
            if args[1:] == ["-n", "0001"]:
                self._set_seen = True
                self.boot_next = FEDORA_ENTRY
                return 0, "", ""
            if args[1:] == ["-N"]:
                self.boot_next = None
                self.readback_mismatch = None
                self.nvram_changed = False
                return 0, "", ""
            raise AssertionError(f"unexpected efibootmgr argv: {args!r}")
        if name == "findmnt":
            if not self.mount_state:
                return 1, "", "not mounted"
            payload = {
                "filesystems": [
                    {
                        "target": str(self.mount_path),
                        "source": f"/dev/disk/by-partuuid/{FEDORA_BOOT}",
                        "fstype": "ext4",
                        "options": "ro,nosuid,nodev,noexec,relatime",
                        "partuuid": FEDORA_BOOT,
                    }
                ]
            }
            return 0, json.dumps(payload), ""
        if name == "lsblk":
            return (
                0,
                json.dumps(
                    {
                        "blockdevices": [
                            {
                                "path": "/dev/fake-fedora-boot",
                                "type": "part",
                                "partuuid": FEDORA_BOOT,
                                "mountpoints": [self.existing_mountpoint],
                            }
                        ]
                    }
                ),
                "",
            )
        if name == "mount":
            if self.mount_failure:
                return 1, "", "mount failed"
            self.mount_state = True
            return 0, "", ""
        if name == "umount":
            self.mount_state = False
            return 0, "", ""
        if name == "systemctl":
            return self.poweroff_code, "", "poweroff failed" if self.poweroff_code else ""
        raise AssertionError(f"unexpected command: {args!r}")


class BootChooserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zeus-boot-chooser-")
        root = Path(self.temp.name)
        self.marker = root / "etc" / "zeus" / "boot-chooser.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.parent.chmod(0o700)
        self.mount = root / "run" / "zeus" / "fedora-boot"
        self.mount.mkdir(parents=True)
        self.mount.parent.chmod(0o755)
        (self.mount / "grub2").mkdir()
        self.mount.chmod(0o755)
        (self.mount / "grub2").chmod(0o755)
        self.grub = self.mount / "grub2" / "grub.cfg"
        self.grub.write_text(
            "set timeout_style=menu\n"
            "set timeout=5\n"
            "menuentry 'Fedora' --id 'fedora' { }\n"
            "menuentry 'Zeus OS' --id 'zeusos-dualboot' { }\n",
            encoding="utf-8",
        )
        self.grub.chmod(0o644)
        self.lock = root / "run" / "zeus" / "boot-chooser.lock"
        self.write_marker()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_marker(self, value: object | None = None) -> None:
        self.marker.write_text(
            json.dumps(marker_payload() if value is None else value), encoding="utf-8"
        )
        self.marker.chmod(0o644)

    def run_poweroff(self, fixture: BootFixture, **kwargs: object) -> dict[str, object]:
        with mock.patch.object(
            chooser,
            "_validate_partuuid_source",
            return_value=Path("/dev/fake-fedora-boot"),
        ):
            return chooser.poweroff(
                runner=fixture,
                effective_uid=0,
                marker_path=self.marker,
                lock_path=self.lock,
                mount_path=self.mount,
                require_root=False,
                **kwargs,
            )

    def test_success_unmounts_before_efi_write_and_preserves_order_and_entries(self) -> None:
        fixture = BootFixture(self.mount)
        result = self.run_poweroff(fixture)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "poweroff_requested")
        commands = fixture.calls
        set_index = commands.index([chooser.EFIBOOTMGR_COMMAND, "-n", "0001"])
        unmount_index = commands.index([chooser.UMOUNT_COMMAND, str(self.mount)])
        self.assertLess(unmount_index, set_index)
        self.assertEqual(commands[-1], [chooser.SYSTEMCTL_COMMAND, "poweroff"])
        self.assertNotIn([chooser.EFIBOOTMGR_COMMAND, "-o"], commands)

    def test_same_preexisting_bootnext_is_idempotent_and_not_rolled_back(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.boot_next = FEDORA_ENTRY
        fixture.poweroff_code = 1
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertNotIn([chooser.EFIBOOTMGR_COMMAND, "-N"], fixture.calls)

    def test_poweroff_failure_rolls_back_only_value_created_by_helper(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.poweroff_code = 1
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertIn([chooser.EFIBOOTMGR_COMMAND, "-N"], fixture.calls)
        self.assertIsNone(fixture.boot_next)
        self.assertEqual(fixture.calls[-1][1:], ["-v"])

    def test_nvram_readback_mismatch_does_not_clear_unowned_value(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.readback_mismatch = OTHER_ENTRY
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertNotIn([chooser.EFIBOOTMGR_COMMAND, "-N"], fixture.calls)

    def test_changed_entry_is_detected_and_rollback_preserves_original_listing(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.nvram_changed = True
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertIn([chooser.EFIBOOTMGR_COMMAND, "-N"], fixture.calls)

    def test_mount_or_generated_config_failure_happens_before_efi_write(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.mount_failure = True
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertFalse(
            any(
                call[0] == chooser.EFIBOOTMGR_COMMAND and call[1:] == ["-n", "0001"]
                for call in fixture.calls
            )
        )

        chooser._validate_unmounted_mountpoints([None])
        with self.assertRaises(chooser.BootChooserError) as error:
            chooser._validate_unmounted_mountpoints(["/run/media/owner/fedora-boot"])
        self.assertEqual(error.exception.code, "block_mounted")
        self.assertFalse(any(call[0] == chooser.SYSTEMCTL_COMMAND for call in fixture.calls))

        fixture = BootFixture(self.mount)
        self.grub.write_text("set timeout=0\nmenuentry 'only' { }\n", encoding="utf-8")
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertFalse(
            any(
                call[0] == chooser.EFIBOOTMGR_COMMAND and call[1:] == ["-n", "0001"]
                for call in fixture.calls
            )
        )

    def test_conflicting_bootnext_is_refused_without_mount_or_write(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.boot_next = OTHER_ENTRY
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertEqual([Path(call[0]).name for call in fixture.calls], ["efibootmgr"])

    def test_unprivileged_request_is_rejected_before_any_command(self) -> None:
        fixture = BootFixture(self.mount)
        result = chooser.poweroff(
            runner=fixture,
            effective_uid=1000,
            marker_path=self.marker,
            lock_path=self.lock,
            mount_path=self.mount,
            require_root=False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(fixture.calls, [])

    def test_unavailable_efi_variables_fail_before_mount_write_or_poweroff(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.efi_query_code = 1
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "efi_unavailable")
        self.assertEqual(fixture.calls, [[chooser.EFIBOOTMGR_COMMAND, "-v"]])

    def test_marker_missing_tampered_schema_and_symlink_are_unavailable(self) -> None:
        fixture = BootFixture(self.mount)
        self.marker.unlink()
        result = chooser.status(runner=fixture, marker_path=self.marker, require_root=False)
        self.assertFalse(result["available"])
        self.write_marker(marker_payload(default_preserved=False))
        self.assertFalse(chooser.status(runner=fixture, marker_path=self.marker, require_root=False)["available"])
        target = self.marker.with_name("real.json")
        target.write_text(json.dumps(marker_payload()), encoding="utf-8")
        target.chmod(0o644)
        self.marker.unlink()
        self.marker.symlink_to(target)
        self.assertFalse(chooser.status(runner=fixture, marker_path=self.marker, require_root=False)["available"])

    def test_status_requires_matching_live_identity_and_rejects_conflict(self) -> None:
        fixture = BootFixture(self.mount)
        result = chooser.status(runner=fixture, marker_path=self.marker, require_root=False)
        self.assertTrue(result["available"])
        fixture.boot_next = OTHER_ENTRY
        self.assertFalse(chooser.status(runner=fixture, marker_path=self.marker, require_root=False)["available"])
        self.write_marker(marker_payload(fedora_esp_partuuid=OTHER_ESP))
        fixture.boot_next = None
        self.assertFalse(chooser.status(runner=fixture, marker_path=self.marker, require_root=False)["available"])

    def test_changed_or_duplicate_efi_path_is_refused(self) -> None:
        fixture = BootFixture(self.mount)
        fixture.path_changed = True
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])

    def test_distinct_loader_on_shared_esp_is_allowed(self) -> None:
        snapshot = chooser.parse_efibootmgr(efi_text(shared_esp=True))
        marker = chooser._marker_from_payload(marker_payload())
        target = chooser._validate_live_identity(marker, snapshot)
        self.assertEqual(target.identifier, FEDORA_ENTRY)

    def test_inactive_target_entry_is_refused(self) -> None:
        fixture = BootFixture(self.mount)
        with mock.patch(
            "boot_chooser.query_efi",
            return_value=chooser.parse_efibootmgr(efi_text(active=False)),
        ):
            result = chooser.status(
                runner=fixture,
                marker_path=self.marker,
                require_root=False,
            )
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "efi_entry_inactive")

        fixture = BootFixture(self.mount)
        fixture.entry_duplicate = True
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])

    def test_strict_parser_rejects_duplicate_ids_and_injection_is_never_shell_code(self) -> None:
        with self.assertRaises(chooser.BootChooserError):
            chooser.parse_efibootmgr(efi_text(duplicate=True).replace("Boot0002*", "Boot0001*"))
        self.write_marker(marker_payload(fedora_boot_entry="Boot0001; reboot"))
        fixture = BootFixture(self.mount)
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])
        self.assertFalse(any(";" in part or "$" in part for call in fixture.calls for part in call))

    def test_marker_and_grub_permissions_are_checked(self) -> None:
        self.marker.chmod(0o664)
        self.assertFalse(
            chooser.status(
                runner=BootFixture(self.mount),
                marker_path=self.marker,
                require_root=False,
            )["available"]
        )
        self.write_marker()
        self.grub.unlink()
        self.grub.symlink_to(self.mount / "outside.cfg")
        (self.mount / "outside.cfg").write_text("bad", encoding="utf-8")
        fixture = BootFixture(self.mount)
        result = self.run_poweroff(fixture)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
