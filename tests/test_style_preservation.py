import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'desktop/rootfs/usr/libexec/zeus-style-sync'

class StylePreservation(unittest.TestCase):
    def test_owner_css_survives_enable_repeat_and_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gtk-4.0/gtk.css'
            path.parent.mkdir()
            original = '/* owner preference */\nbutton { letter-spacing: 1px; }\n'
            path.write_text(original)
            env = dict(os.environ, XDG_CONFIG_HOME=directory, ZEUS_THEME_SOURCE=str(SCRIPT.parents[1] / 'share/themes/Zeus'))
            def run(mode):
                subprocess.run(['python3', str(SCRIPT), mode], env=env, check=True, capture_output=True)
            run('--enable')
            enabled = path.read_text()
            self.assertIn(original, enabled)
            run('--enable')
            self.assertEqual(enabled, path.read_text())
            run('--disable')
            self.assertEqual(original, path.read_text())
            subprocess.run(['python3', str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertEqual(original, path.read_text(), 'Login must preserve safe mode')

    def test_owner_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'owner.css'
            target.write_text('/* owner */')
            path = Path(directory) / 'gtk-4.0/gtk.css'
            path.parent.mkdir()
            path.symlink_to(target)
            subprocess.run(['python3', str(SCRIPT)], env=dict(os.environ, XDG_CONFIG_HOME=directory, ZEUS_THEME_SOURCE=str(SCRIPT.parents[1] / 'share/themes/Zeus')), check=True, capture_output=True)
            self.assertTrue(path.is_symlink())
            self.assertEqual('/* owner */', target.read_text())
