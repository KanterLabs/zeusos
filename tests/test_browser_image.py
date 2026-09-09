"""Guard browser trust and the editable Temp/battery defaults."""
import configparser
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BrowserImage(unittest.TestCase):
    def test_google_repository_is_restricted_and_verified(self):
        repo = configparser.ConfigParser()
        repo.read(ROOT / 'image/repos/google-chrome.repo')
        chrome = repo['google-chrome']
        self.assertEqual(chrome['baseurl'], 'https://dl.google.com/linux/chrome/rpm/stable/$basearch')
        self.assertEqual(chrome['includepkgs'], 'google-chrome-stable')
        self.assertFalse(chrome.getboolean('enabled'))
        for field in ('gpgcheck', 'repo_gpgcheck', 'sslverify'):
            self.assertTrue(chrome.getboolean(field))
        # Reviewed primary EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796 and its
        # Google-signed subkeys. A rotation requires review of the new key file.
        key = (ROOT / 'image/keys/google-linux.asc').read_bytes()
        self.assertEqual(hashlib.sha256(key).hexdigest(), '54dea5f6c2a26091578cf52a999cebc6b64df478d37ad4dce96376b711e3b27c')

    def test_temp_and_background_are_recommended_not_locked(self):
        policy = ROOT / 'desktop/rootfs/etc/opt/chrome/policies'
        defaults = json.loads((policy / 'recommended/zeus.json').read_text())
        self.assertEqual(defaults, {'DownloadDirectory': '${user_home}/Temp', 'BackgroundModeEnabled': False})
        self.assertFalse((policy / 'managed/zeus.json').exists())
        self.assertFalse((ROOT / 'desktop/rootfs/etc/firefox/policies/policies.json').exists())

    def test_firefox_is_replaced_in_packages_and_system_dock(self):
        packages = (ROOT / 'image/packages.txt').read_text().splitlines()
        self.assertIn('google-chrome-stable', packages)
        self.assertNotIn('firefox', packages)
        defaults = (ROOT / 'desktop/rootfs/etc/dconf/db/local.d/00-zeus').read_text()
        self.assertIn("'google-chrome.desktop'", defaults)
        self.assertNotIn('org.mozilla.firefox.desktop', defaults)


if __name__ == '__main__':
    unittest.main()
