import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'desktop/rootfs/usr/libexec/zeus-style-sync'
GTK3 = ROOT / 'desktop/rootfs/usr/share/themes/Zeus/gtk-3.0/gtk.css'
GTK4 = ROOT / 'desktop/rootfs/usr/share/themes/Zeus/gtk-4.0/gtk.css'

class StylePreservation(unittest.TestCase):
    def test_native_application_controls_are_not_globally_restyled(self):
        for path in (GTK3, GTK4):
            css = path.read_text(encoding='utf-8')
            self.assertNotRegex(css, r'(?m)^button(?:\s|,|\{)')
            self.assertNotRegex(css, r'(?m)^popover(?:\s|,|\{)')
            self.assertNotRegex(css, r'(?m)^menu(?:\s|,|\{)')
            self.assertIn('window.zeus-window button', css)
            self.assertIn('windowcontrols button.close' if path == GTK4 else 'headerbar button.close', css)

    def test_native_popover_nodes_keep_toolkit_geometry(self):
        for path in (GTK3, GTK4):
            css = path.read_text(encoding='utf-8')
            for selector in ('popover.background', '.context-menu'):
                self.assertNotIn(selector, css)
            self.assertFalse(
                re.search(r'(?s)popover[^\{]*\{[^}]*\bpadding\s*:', css),
                f'{path} must not add geometry to native popovers',
            )

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
