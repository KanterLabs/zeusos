import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop/rootfs/usr/lib"))

from zeus.zeus_temp import (  # noqa: E402
    ActiveFileError,
    DestinationError,
    HomeManager,
    MIN_CUSTOM_INTERVAL,
    PolicyError,
    TempCollisionError,
    UnsafeTempError,
    UnsupportedStateError,
)


class FakeClock:
    def __init__(self, value=1_000.0, boot="boot-a"):
        self.value = float(value)
        self.boot = boot

    def now(self):
        return self.value

    def boot_id(self):
        return self.boot

    def advance(self, seconds):
        self.value += seconds


class TempFixture(unittest.TestCase):
    def manager(self, directory, *, clock=None, proc_root=None):
        return HomeManager(
            directory,
            clock=clock or FakeClock(),
            proc_root=proc_root,
        )

    def test_setup_preserves_populated_downloads_and_requires_collision_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            downloads = home / "Downloads"
            downloads.mkdir()
            (downloads / "old.iso").write_bytes(b"existing download")
            temp = home / "Temp"
            temp.mkdir(mode=0o700)
            (temp / "owner-file").write_text("do not silently adopt")
            clock = FakeClock()
            manager = self.manager(home, clock=clock)

            with self.assertRaises(TempCollisionError):
                manager.setup()
            self.assertEqual((downloads / "old.iso").read_bytes(), b"existing download")
            self.assertEqual((temp / "owner-file").read_text(), "do not silently adopt")

            adopted = manager.setup(adopt=True)
            self.assertTrue(adopted["enabled"])
            self.assertTrue(adopted["adopted"])
            self.assertEqual((downloads / "old.iso").read_bytes(), b"existing download")
            self.assertEqual(stat.S_IMODE(os.stat(temp).st_mode), 0o700)
            self.assertEqual(manager.setup()["policy"]["mode"], "boot")

    def test_default_boot_policy_runs_once_and_waits_for_next_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            clock = FakeClock()
            manager = self.manager(home, clock=clock)
            manager.setup()
            (home / "Downloads" ).mkdir()
            (home / "Downloads" / "kept").write_text("permanent")
            (home / "Temp" / "old.txt").write_text("old")

            first = manager.sweep()
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["deleted"], 1)
            self.assertFalse((home / "Temp" / "old.txt").exists())

            (home / "Temp" / "new.txt").write_text("new")
            replay = manager.sweep()
            self.assertFalse(replay["swept"])
            self.assertEqual(replay["reason"], "boot-complete")
            self.assertTrue((home / "Temp" / "new.txt").exists())
            self.assertTrue((home / "Downloads" / "kept").exists())

            clock.boot = "boot-b"
            second_boot = manager.sweep()
            self.assertTrue(second_boot["ok"], second_boot)
            self.assertFalse((home / "Temp" / "new.txt").exists())

    def test_preset_custom_never_schedule_and_wall_clock_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            clock = FakeClock()
            manager = self.manager(home, clock=clock)
            manager.setup()

            hourly = manager.set_policy("hourly")
            self.assertEqual(hourly["interval_seconds"], 3600)
            self.assertEqual(hourly["next_cleanup_at"], 4600.0)
            (home / "Temp" / "hourly").write_text("hour")
            self.assertFalse(manager.sweep()["swept"])
            clock.advance(3600)
            done = manager.sweep()
            self.assertTrue(done["swept"], done)
            self.assertFalse((home / "Temp" / "hourly").exists())
            self.assertEqual(done["status"]["next_cleanup_at"], 8200.0)

            clock.value = 100.0  # backwards wall-clock jump: no repeated run
            self.assertFalse(manager.sweep()["swept"])
            with self.assertRaises(PolicyError):
                manager.set_policy("custom", MIN_CUSTOM_INTERVAL - 1)
            custom = manager.set_policy("custom", MIN_CUSTOM_INTERVAL)
            self.assertEqual(custom["interval_seconds"], MIN_CUSTOM_INTERVAL)
            never = manager.set_policy("never")
            self.assertEqual(never["next_cleanup"], "Never")
            (home / "Temp" / "manual").write_text("manual")
            self.assertFalse(manager.sweep()["swept"])
            forced = manager.sweep(force=True)
            self.assertTrue(forced["ok"], forced)
            self.assertFalse((home / "Temp" / "manual").exists())

    def test_keep_conflict_safe_and_destination_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            clock = FakeClock()
            manager = self.manager(home, clock=clock)
            manager.setup()
            documents = home / "Documents"
            documents.mkdir()
            (documents / "report.txt").write_text("existing")
            (home / "Temp" / "report.txt").write_text("new content")

            kept = manager.keep(["report.txt"], documents)
            self.assertTrue(kept["ok"], kept)
            self.assertEqual((documents / "report.txt").read_text(), "existing")
            self.assertEqual((documents / "report (1).txt").read_text(), "new content")
            self.assertFalse((home / "Temp" / "report.txt").exists())

            (home / "Temp" / "nested").mkdir()
            (home / "Temp" / "nested" / "x").write_text("x")
            with self.assertRaises(DestinationError):
                manager.keep(["nested/x"], home / "Temp" / "permanent")
            with self.assertRaises(DestinationError):
                manager.keep(["nested/x"], home.parent)

    def test_active_fd_and_partial_companion_are_deferred(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as proc_directory:
            home = Path(directory)
            proc = Path(proc_directory)
            (proc / "self").mkdir()
            (proc / "self" / "mountinfo").write_text("")
            pid = proc / str(os.getpid())
            (pid / "fd").mkdir(parents=True)
            clock = FakeClock()
            manager = self.manager(home, clock=clock, proc_root=proc)
            manager.setup()
            active_file = home / "Temp" / "active.bin"
            active_file.write_bytes(b"active")
            handle = active_file.open("rb")
            os.symlink(active_file, pid / "fd" / "9")
            try:
                result = manager.sweep()
                self.assertTrue(result["ok"], result)
                self.assertTrue(active_file.exists())
                self.assertIn(
                    {"name": "active.bin", "reason": "active-file"},
                    result["skipped"],
                )
            finally:
                handle.close()
                (pid / "fd" / "9").unlink()

            # A marker protects its completed-looking companion too, and does
            # not need to rely on filename age or a short timing window.
            (home / "Temp" / "video.mp4").write_text("partial companion")
            (home / "Temp" / "video.mp4.crdownload").write_text("download")
            with self.assertRaises(ActiveFileError):
                manager.keep(["video.mp4"])
            with self.assertRaises(ActiveFileError):
                manager.keep(["video.mp4.crdownload"])
            clock.boot = "boot-b"
            result = manager.sweep()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["reason"], "partial-download")
            self.assertTrue((home / "Temp" / "video.mp4").exists())

    def test_containment_rejects_symlink_root_and_child(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            home = Path(directory)
            clock = FakeClock()
            manager = self.manager(home, clock=clock)
            manager.setup()
            (home / "Temp" / "safe").write_text("safe")
            os.symlink(Path(outside), home / "Temp" / "outside-link")
            result = manager.sweep()
            self.assertTrue(result["ok"], result)
            self.assertFalse((home / "Temp" / "safe").exists())
            self.assertTrue((home / "Temp" / "outside-link").is_symlink())

            root = home / "Temp"
            replacement = home / "Temp.real"
            root.rename(replacement)
            os.symlink(Path(outside), root)
            with self.assertRaises(UnsafeTempError):
                manager.sweep(force=True)

    def test_uncertain_boot_attempt_is_recorded_and_unknown_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            clock = FakeClock()
            manager = self.manager(home, clock=clock)
            manager.setup()
            candidate = home / "Temp" / "before.txt"
            candidate.write_text("before")
            manager._scan_active = lambda candidates: (set(), False, ["test uncertainty"])
            blocked = manager.sweep()
            self.assertFalse(blocked["ok"])
            self.assertTrue(candidate.exists())
            (home / "Temp" / "after.txt").write_text("after")
            same_boot = manager.sweep()
            self.assertFalse(same_boot["swept"])
            self.assertTrue((home / "Temp" / "after.txt").exists())

            state = json.loads(manager.state_path.read_text())
            state["schema_version"] = 99
            manager.state_path.write_text(json.dumps(state))
            report = manager.status()
            self.assertFalse(report["enabled"])
            self.assertIn("unsupported", report["errors"][0].lower())
            with self.assertRaises(UnsupportedStateError):
                manager.set_policy("never")


if __name__ == "__main__":
    unittest.main()
