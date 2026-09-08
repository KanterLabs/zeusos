"""Fail closed on unsafe release inputs and accidentally shipped credentials."""
import configparser
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class ImageContract(unittest.TestCase):
    def test_immutable_container_inputs(self):
        inputs = json.loads((ROOT / 'image/inputs.json').read_text())
        for name in ('base', 'builder'):
            self.assertRegex(inputs[name], r'^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$')
        self.assertEqual(inputs['architecture'], 'x86_64')

    def test_generic_disk_has_no_owner_credentials(self):
        import tomllib
        disk = tomllib.loads((ROOT / 'image/disk.toml').read_text())
        self.assertNotIn('user', disk['customizations'])
        self.assertNotIn('sshkey', (ROOT / 'image/disk.toml').read_text())
        for folder in ('image/rootfs', 'desktop/rootfs', 'zeus/assets'):
            for path in (ROOT / folder).rglob('*'):
                if path.is_file():
                    text = path.read_bytes().decode('utf-8', errors='replace')
                    self.assertNotRegex(text, r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----')
                    self.assertNotIn('authorized_keys', str(path))
                    self.assertNotIn('/etc/shadow', str(path))

    def test_native_login_and_ssh_policy(self):
        config = configparser.ConfigParser()
        config.read(ROOT / 'image/rootfs/etc/gdm/custom.conf')
        self.assertFalse(config.getboolean('daemon', 'AutomaticLoginEnable'))
        self.assertFalse(config.getboolean('xdmcp', 'Enable'))
        text = (ROOT / 'image/rootfs/etc/ssh/sshd_config.d/20-zeus.conf').read_text()
        self.assertIn('PasswordAuthentication no', text)
        self.assertIn('PermitRootLogin no', text)

    def test_assets_parse(self):
        assets = list((ROOT / 'desktop/rootfs').rglob('*.svg'))
        self.assertTrue(assets, 'Missing desktop artwork')
        for path in assets + list((ROOT / 'desktop/rootfs').rglob('*.xml')):
            ET.parse(path)

    def test_workflows_use_homelab_tiers(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            for label in re.findall(r'runs-on:\s*(\S+)', path.read_text()):
                self.assertIn(label, ('homelab', 'homelab-heavy'))


if __name__ == '__main__':
    unittest.main()
