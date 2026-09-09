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

    def test_custom_destination_stays_custom_while_temp_is_bookmarked_in_both_gtk_versions(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / '.config'
            config.mkdir()
            dirs = config / 'user-dirs.dirs'
            original = 'XDG_DOWNLOAD_DIR="$HOME/Work folder"\nXDG_DOCUMENTS_DIR="$HOME/Documents"\n'
            dirs.write_text(original)
            gtk3 = config / 'gtk-3.0'
            gtk3.mkdir()
            uri = (home / 'Temp').as_uri()
            bookmarks = gtk3 / 'bookmarks'
            bookmarks.write_text(f'file:///tmp/owner-place Owner place\n{uri} Owner label\n{uri} Duplicate\n')

            module.route(home)

            self.assertEqual(dirs.read_text(), original)
            content = bookmarks.read_text()
            self.assertEqual(content.count(uri), 1)
            self.assertIn(f'{uri} Owner label', content)
            gtk4_bookmarks = config / 'gtk-4.0' / 'bookmarks'
            self.assertEqual(gtk4_bookmarks.read_text(), f'{uri} Temp\n')

    def test_symlinked_bookmark_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / '.config'
            config.mkdir()
            (config / 'user-dirs.dirs').write_text('XDG_DOWNLOAD_DIR="$HOME/Downloads"\n')
            gtk3 = config / 'gtk-3.0'
            gtk3.mkdir()
            target = home / 'owner-bookmarks'
            target.write_text('file:///tmp/owner Owner\n')
            (gtk3 / 'bookmarks').symlink_to(target)

            module.route(home)

            self.assertEqual(target.read_text(), 'file:///tmp/owner Owner\n')
            self.assertTrue((gtk3 / 'bookmarks').is_symlink())
