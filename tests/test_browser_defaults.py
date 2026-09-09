"""Fresh and populated fixtures for the Chrome default migration."""

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "desktop/rootfs/usr/libexec/zeus-browser-session"
loader = importlib.machinery.SourceFileLoader("zeus_browser_session_test", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


class FakeSettings:
    def __init__(self, favorites=None, explicit=True):
        self.favorites = list(favorites or [])
        self.explicit = explicit
        self.set_calls = []
        self.sync_calls = 0

    def get_user_value(self, key):
        self.assert_key(key)
        return object() if self.explicit else None

    def get_strv(self, key):
        self.assert_key(key)
        return list(self.favorites)

    def set_strv(self, key, values):
        self.assert_key(key)
        self.set_calls.append((key, list(values)))
        self.favorites = list(values)

    def sync(self):
        self.sync_calls += 1

    @staticmethod
    def assert_key(key):
        if key != module.FAVORITES_KEY:
            raise AssertionError(key)


class BrowserDefaults(unittest.TestCase):
    def test_system_mime_defaults_are_ordered_and_leave_pdf_unmanaged(self):
        content = (ROOT / "desktop/rootfs/etc/xdg/mimeapps.list").read_text()
        for mime_type in module.TARGET_MIME_TYPES:
            prefix = mime_type + "="
            line = next(line for line in content.splitlines() if line.startswith(prefix))
            self.assertEqual(
                line.removeprefix(prefix).split(";"),
                [module.CHROME_DESKTOP_ID, module.FIREFOX_FAVORITE_ID, ""],
            )
        self.assertNotIn("application/pdf=", content)

    def test_fresh_account_uses_system_defaults_without_creating_user_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            settings = FakeSettings(explicit=False)

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}, clear=False):
                self.assertTrue(module.run(home, settings=settings))

            self.assertFalse(settings.set_calls)
            self.assertFalse((config / "mimeapps.list").exists())
            marker = config / "zeus" / module.MARKER_FILENAME
            self.assertEqual(marker.read_text(), module.MARKER_CONTENT)

    def test_populated_fixture_migrates_only_stale_browser_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            profile = config / "google-chrome/Default"
            profile.mkdir(parents=True)
            profile_files = {
                profile / "History": b"existing history",
                profile / "Bookmarks": b"existing bookmarks",
                home / "Downloads/existing-download.bin": b"owner download",
            }
            (home / "Downloads").mkdir()
            for path, payload in profile_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            mime_path = config / "mimeapps.list"
            original = (
                "# owner MIME choices\n"
                "[Added Associations]\n"
                "application/pdf=org.gnome.Evince.desktop;\n"
                "[Default Applications]\n"
                "x-scheme-handler/http=org.mozilla.firefox.desktop;custom-browser.desktop;\n"
                "x-scheme-handler/https=custom-browser.desktop;\n"
                "text/html=firefox.desktop;\n"
                "application/xhtml+xml=org.mozilla.firefox.desktop;\n"
                "application/pdf=org.gnome.Evince.desktop;\n"
                "[Other Owner Group]\n"
                "owner-key=owner-value\n"
            )
            mime_path.write_text(original, encoding="utf-8")
            mime_path.chmod(0o640)
            pdf_lines = [
                line for line in original.splitlines(keepends=True)
                if line.startswith("application/pdf=")
            ]
            settings = FakeSettings(
                [
                    "org.gnome.Nautilus.desktop",
                    module.FIREFOX_FAVORITE_ID,
                    "custom-browser.desktop",
                    module.CHROME_DESKTOP_ID,
                    module.FIREFOX_FAVORITE_ID,
                    "org.gnome.Settings.desktop",
                ]
            )

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}, clear=False):
                self.assertTrue(module.run(home, settings=settings))

            self.assertEqual(
                settings.favorites,
                [
                    "org.gnome.Nautilus.desktop",
                    module.CHROME_DESKTOP_ID,
                    "custom-browser.desktop",
                    "org.gnome.Settings.desktop",
                ],
            )
            migrated = mime_path.read_text(encoding="utf-8")
            self.assertIn(
                "x-scheme-handler/http=google-chrome.desktop;org.mozilla.firefox.desktop;custom-browser.desktop;",
                migrated,
            )
            self.assertIn(
                "text/html=google-chrome.desktop;firefox.desktop;",
                migrated,
            )
            self.assertIn("x-scheme-handler/https=custom-browser.desktop;", migrated)
            self.assertIn("[Other Owner Group]\nowner-key=owner-value\n", migrated)
            for line in pdf_lines:
                self.assertEqual(migrated.count(line), original.count(line))
            self.assertEqual(mime_path.stat().st_mode & 0o777, 0o640)
            for path, payload in profile_files.items():
                self.assertEqual(path.read_bytes(), payload)
            self.assertTrue((config / "zeus" / module.MARKER_FILENAME).exists())

    def test_retry_is_idempotent_and_later_owner_choices_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            mime_path = config / "mimeapps.list"
            mime_path.write_text(
                "[Default Applications]\n"
                "text/html=org.mozilla.firefox.desktop;\n",
                encoding="utf-8",
            )
            settings = FakeSettings([module.FIREFOX_FAVORITE_ID])
            env = {"XDG_CONFIG_HOME": str(config)}
            with patch.dict(os.environ, env, clear=False):
                module.run(home, settings=settings)
                after = mime_path.read_bytes()
                settings.favorites = ["owner-browser.desktop"]
                module.run(home, settings=settings)

            self.assertEqual(mime_path.read_bytes(), after)
            self.assertEqual(settings.favorites, ["owner-browser.desktop"])
            self.assertEqual(len(settings.set_calls), 1)
            self.assertEqual(settings.sync_calls, 1)

    def test_symlink_malformed_and_failed_inputs_are_preserved_without_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            target = home / "mimeapps.owner"
            target.write_text("[Default Applications]\ntext/html=org.mozilla.firefox.desktop;\n")
            mime_path = config / "mimeapps.list"
            mime_path.symlink_to(target)
            settings = FakeSettings([module.FIREFOX_FAVORITE_ID])
            env = {"XDG_CONFIG_HOME": str(config)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(module.BrowserMigrationError):
                    module.run(home, settings=settings)
            self.assertTrue(mime_path.is_symlink())
            self.assertFalse((config / "zeus").exists())

            mime_path.unlink()
            mime_path.write_text("[Default Applications\ntext/html=org.mozilla.firefox.desktop;\n")
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(module.BrowserMigrationError):
                    module.run(home, settings=settings)
            self.assertEqual(mime_path.read_text(), "[Default Applications\ntext/html=org.mozilla.firefox.desktop;\n")
            self.assertFalse((config / "zeus").exists())

    def test_symlinked_config_directory_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            real_config = home / "real-config"
            real_config.mkdir()
            mime_path = real_config / "mimeapps.list"
            original = "[Default Applications]\ntext/html=org.mozilla.firefox.desktop;\n"
            mime_path.write_text(original, encoding="utf-8")
            link = home / "config-link"
            link.symlink_to(real_config, target_is_directory=True)
            settings = FakeSettings([module.FIREFOX_FAVORITE_ID])
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(link)}, clear=False):
                with self.assertRaises(module.BrowserMigrationError):
                    module.run(home, settings=settings)
            self.assertEqual(mime_path.read_text(encoding="utf-8"), original)
            self.assertFalse((real_config / "zeus").exists())

    def test_favorite_write_failure_does_not_mark_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            settings = FakeSettings([module.FIREFOX_FAVORITE_ID])
            env = {"XDG_CONFIG_HOME": str(config)}
            with patch.dict(os.environ, env, clear=False), patch.object(
                settings, "set_strv", side_effect=OSError("test failure")
            ):
                with self.assertRaises(module.BrowserMigrationError):
                    module.run(home, settings=settings)
            self.assertFalse((config / "zeus" / module.MARKER_FILENAME).exists())

    def test_atomic_mime_write_failure_leaves_original_and_no_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config"
            config.mkdir()
            mime_path = config / "mimeapps.list"
            original = "[Default Applications]\ntext/html=org.mozilla.firefox.desktop;\n"
            mime_path.write_text(original, encoding="utf-8")
            settings = FakeSettings(explicit=False)
            env = {"XDG_CONFIG_HOME": str(config)}
            with patch.dict(os.environ, env, clear=False), patch.object(
                module.os, "replace", side_effect=OSError("test failure")
            ):
                with self.assertRaises(module.BrowserMigrationError):
                    module.run(home, settings=settings)
            self.assertEqual(mime_path.read_text(encoding="utf-8"), original)
            self.assertFalse((config / "zeus" / module.MARKER_FILENAME).exists())
            self.assertFalse(list(config.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
