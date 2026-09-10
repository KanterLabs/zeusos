from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from zeus_installer import bootmenu


class BootMenuTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
