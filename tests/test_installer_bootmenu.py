from pathlib import Path
import copy
import json
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from zeus_installer import bootmenu


class BootMenuTests(unittest.TestCase):
    @staticmethod
    def _inventory():
        table = {
            "label": "gpt",
            "device": "/dev/nvme0n1",
            "partitions": [
                {"node": "/dev/nvme0n1p1", "uuid": "11111111-1111-4111-8111-111111111111"},
                {"node": "/dev/nvme0n1p2", "uuid": "22222222-2222-4222-8222-222222222222"},
                {"node": "/dev/nvme0n1p3", "uuid": "33333333-3333-4333-8333-333333333333"},
            ],
        }
        inventory = {
            "efi": {
                "variables_supported": True,
                "boot_order": ["0001", "0002"],
                "entries": [
                    {
                        "id": "0001",
                        "description": "Fedora HD(1,GPT,11111111-1111-4111-8111-111111111111,0x800)/File(\\EFI\\fedora\\shimx64.efi)",
                        "path": "\\EFI\\fedora\\shimx64.efi",
                    },
                    {"id": "0002", "description": "Linux Firmware", "path": None},
                ],
            },
            "block_devices": [
                {"path": "/dev/nvme0n1p1", "type": "part", "partuuid": table["partitions"][0]["uuid"]},
                {"path": "/dev/nvme0n1p2", "type": "part", "partuuid": table["partitions"][1]["uuid"]},
                {"path": "/dev/nvme0n1p3", "type": "part", "partuuid": table["partitions"][2]["uuid"]},
            ],
            "mounts": [
                {"target": "/boot/efi", "source": "/dev/nvme0n1p1", "fstype": "vfat"},
                {"target": "/boot", "source": "/dev/nvme0n1p2", "fstype": "ext4"},
            ],
            "partition_table": table,
        }
        return {"inventory": inventory, "target": {"sfdisk_table": copy.deepcopy(table)}}

    def test_emits_isolated_uuid_chainloader_without_fedora_default_change(self):
        rendered = bootmenu.render('1234-abcd')
        result = subprocess.run(['sh'], input=rendered, text=True, capture_output=True, check=True)
        self.assertIn('1234-ABCD', result.stdout)
        self.assertIn('chainloader ($zeus_esp)/EFI/fedora/shimx64.efi', result.stdout)
        self.assertNotIn('set default', result.stdout)
        self.assertNotIn('linux ', result.stdout)

    def test_rejects_grub_or_shell_injection(self):
        for value in ('../fedora', '1234-ABCD; reboot', '$(reboot)', '1234-ABCD\n', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootmenu.render(value)

    def test_menu_change_is_idempotent_and_preserves_defaults_and_args(self):
        text = 'GRUB_DEFAULT=saved\nGRUB_TIMEOUT=0\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_CMDLINE_LINUX="quiet rootflags=subvol=root"\n'
        updated = bootmenu.visible_menu_config(text)
        self.assertIn('GRUB_DEFAULT=saved', updated)
        self.assertIn('GRUB_CMDLINE_LINUX="quiet rootflags=subvol=root"', updated)
        self.assertIn('GRUB_TIMEOUT_STYLE=menu', updated)
        self.assertEqual(updated, bootmenu.visible_menu_config(updated))

    def test_boot_chooser_marker_binds_exact_fedora_record_and_partitions(self):
        marker = bootmenu.derive_boot_chooser_marker(self._inventory())
        self.assertEqual(
            marker,
            {
                "schema_version": 1,
                "fedora_boot_entry": "Boot0001",
                "fedora_boot_path": r"\EFI\fedora\shimx64.efi",
                "fedora_esp_partuuid": "11111111-1111-4111-8111-111111111111",
                "fedora_boot_partuuid": "22222222-2222-4222-8222-222222222222",
                "grub_entry_id": "zeusos-dualboot",
                "grub_timeout_style": "menu",
                "grub_timeout": 5,
                "default_preserved": True,
            },
        )
        self.assertTrue(bootmenu.validate_boot_chooser_marker(marker))
        payload = bootmenu.boot_chooser_marker_bytes(marker)
        self.assertLessEqual(len(payload), bootmenu.BOOT_CHOOSER_MAX_BYTES)
        self.assertEqual(json.loads(payload), marker)

    def test_boot_chooser_marker_rejects_missing_duplicate_and_mismatched_fedora_entry(self):
        for mutate in (
            lambda inventory: inventory["inventory"]["efi"]["entries"].pop(0),
            lambda inventory: inventory["inventory"]["efi"]["entries"].append(copy.deepcopy(inventory["inventory"]["efi"]["entries"][0])),
            lambda inventory: inventory["inventory"]["efi"]["entries"][0].update({"path": r"\EFI\fedora\other.efi"}),
            lambda inventory: inventory["inventory"]["efi"]["entries"][0].update({"active": False}),
        ):
            with self.subTest(mutate=mutate):
                inventory = self._inventory()
                mutate(inventory)
                with self.assertRaises(ValueError):
                    bootmenu.derive_boot_chooser_marker(inventory)

    def test_boot_chooser_marker_rejects_mismatched_partition_identity_and_path_injection(self):
        inventory = self._inventory()
        inventory["inventory"]["block_devices"][0]["partuuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with self.assertRaises(ValueError):
            bootmenu.derive_boot_chooser_marker(inventory)
        with self.assertRaises(ValueError):
            bootmenu.build_boot_chooser_marker(
                fedora_boot_entry_id="0001",
                fedora_efi_entry_label="Fedora",
                fedora_efi_entry_path=r"\EFI\fedora\shimx64.efi;touch /tmp/pwned",
                fedora_esp_partuuid="11111111-1111-4111-8111-111111111111",
                fedora_boot_partuuid="22222222-2222-4222-8222-222222222222",
            )


if __name__ == '__main__':
    unittest.main()
