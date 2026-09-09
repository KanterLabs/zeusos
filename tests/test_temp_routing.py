"""Populated routing fixtures never adopt or move existing user files."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'desktop/rootfs/usr/libexec/zeus-temp-session'
loader = importlib.machinery.SourceFileLoader('zeus_temp_session_test', str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'zeus_temp': types.ModuleType('zeus_temp')}):
    loader.exec_module(module)

class Routing(unittest.TestCase):
    def test_standard_download_destination_changes_without_moving_files(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.config').mkdir()
            (home / 'Downloads').mkdir()
            old = home / 'Downloads/keep.txt'
            old.write_bytes(b'existing download')
            dirs = home / '.config/user-dirs.dirs'
            dirs.write_text('XDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_DOCUMENTS_DIR="$HOME/Work"\n')
            module.route(home)
            self.assertEqual(old.read_bytes(), b'existing download')
            self.assertIn('XDG_DOWNLOAD_DIR="$HOME/Temp"', dirs.read_text())
            self.assertIn('XDG_DOCUMENTS_DIR="$HOME/Work"', dirs.read_text())
            before = dirs.read_bytes()
            module.route(home)
            self.assertEqual(dirs.read_bytes(), before)
            self.assertEqual((home / '.config/gtk-3.0/bookmarks').read_text().count(' Temp\n'), 1)

    def test_custom_and_symlink_configuration_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.config').mkdir()
            dirs = home / '.config/user-dirs.dirs'
            original = 'XDG_DOWNLOAD_DIR="$HOME/Work"\n'
            dirs.write_text(original)
            module.route(home)
            self.assertEqual(dirs.read_text(), original)
            dirs.unlink()
            target = home / 'private.txt'
            target.write_text(original)
            dirs.symlink_to(target)
            with self.assertRaises(RuntimeError):
                module.route(home)
            self.assertEqual(target.read_text(), original)
