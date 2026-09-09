import importlib.machinery
import importlib.util
import json
import subprocess
import threading
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "desktop/rootfs/usr/libexec/zeus-temp-scheduler"
loader = importlib.machinery.SourceFileLoader("zeus_temp_scheduler_test", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


class FakeSystemctl:
    def __init__(self):
        self.calls = []

    def __call__(self, arguments):
        self.calls.append(tuple(arguments))
        return subprocess.CompletedProcess(arguments, 0, "", "")


def timed_plan(deadline=4600.0):
    return {
        "ok": True,
        "enabled": True,
        "enrolled": True,
        "scheduled": True,
        "mode": "hourly",
        "interval_seconds": 3600,
        "next_cleanup_at": deadline,
        "due": False,
        "due_reason": "not-due",
    }


class TempSchedulerTests(unittest.TestCase):
    def test_installed_policy_hook_runs_after_persistence_and_surfaces_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = module.zeus_temp.HomeManager(directory, proc_root=None)
            manager.setup()

            def arm(command, **kwargs):
                # This acquires the engine lock again; the policy operation
                # must have released it before invoking the scheduler.
                self.assertEqual(manager.schedule()["mode"], "daily")
                self.assertEqual(command[-2:], ["/usr/libexec/zeus-temp-scheduler", "arm"])
                return subprocess.CompletedProcess(command, 1, '{"ok":false}', '')

            with mock.patch.object(module.zeus_temp, "_manager", return_value=manager), \
                 mock.patch.object(module.zeus_temp.subprocess, "run", side_effect=arm):
                result = module.zeus_temp.set_policy("daily")
            self.assertTrue(result["policy_saved"])
            self.assertFalse(result["ok"])
            self.assertTrue(result["scheduler_error"])
            self.assertEqual(manager.status()["policy"]["mode"], "daily")

    def test_manual_sweep_rearms_and_scheduler_sweep_does_not_recurse(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = module.zeus_temp.HomeManager(directory, proc_root=None)
            manager.setup()
            manager.set_policy("never")
            response = subprocess.CompletedProcess([], 0, json.dumps(manager.schedule()), '')
            with mock.patch.object(module.zeus_temp, "_manager", return_value=manager), \
                 mock.patch.object(module.zeus_temp.subprocess, "run", return_value=response) as command:
                self.assertTrue(module.zeus_temp.sweep()["ok"])
                command.assert_called_once()
                command.reset_mock()
                self.assertTrue(module.zeus_temp.sweep(include_usage=False, synchronize_timer=False)["ok"])
                command.assert_not_called()

    def scheduler(self, directory, *, schedule_source=None, sweep_source=None):
        systemctl = FakeSystemctl()
        scheduler = module.TimerScheduler(
            home=directory,
            config_home=Path(directory) / "config",
            runtime_dir=Path(directory) / "runtime",
            schedule_source=schedule_source,
            sweep_source=sweep_source,
            systemctl=systemctl,
        )
        return scheduler, systemctl

    def test_timed_plan_writes_one_shot_calendar_and_can_rearm(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, systemctl = self.scheduler(directory)

            first = scheduler.arm(timed_plan())
            self.assertTrue(first["ok"], first)
            self.assertTrue(first["timer_armed"], first)
            self.assertEqual(
                systemctl.calls,
                [
                    ("stop", "zeus-temp-clean.timer"),
                    ("daemon-reload",),
                    ("start", "zeus-temp-clean.timer"),
                ],
            )
            self.assertIn("OnCalendar=@4600\n", scheduler.dropin_path.read_text())
            self.assertTrue(scheduler.marker_path.exists())

            scheduler.arm(timed_plan(8200.25))
            contents = scheduler.dropin_path.read_text()
            self.assertIn("OnCalendar=1970-01-01 02:16:40.250000 UTC\n", contents)
            self.assertNotIn("OnCalendar=@4600\n", contents)
            self.assertEqual(systemctl.calls.count(("start", "zeus-temp-clean.timer")), 2)

    def test_overdue_plan_uses_single_monotonic_catchup_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, _systemctl = self.scheduler(directory)
            plan = timed_plan()
            plan["due"] = True
            plan["due_reason"] = "due"

            result = scheduler.arm(plan)

            self.assertTrue(result["timer_armed"], result)
            contents = scheduler.dropin_path.read_text()
            self.assertIn("OnCalendar=\n", contents)
            self.assertIn("OnActiveSec=1s\n", contents)
            self.assertNotIn("OnCalendar=@4600\n", contents)

    def test_boot_and_never_plans_disarm_and_remove_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, systemctl = self.scheduler(directory)
            scheduler.arm(timed_plan())
            self.assertTrue(scheduler.dropin_path.exists())
            self.assertTrue(scheduler.marker_path.exists())

            result = scheduler.arm(
                {
                    "ok": True,
                    "enabled": True,
                    "enrolled": True,
                    "scheduled": False,
                    "mode": "never",
                    "interval_seconds": None,
                    "next_cleanup_at": None,
                    "due": False,
                    "due_reason": "never",
                }
            )

            self.assertTrue(result["ok"], result)
            self.assertFalse(result["timer_armed"], result)
            self.assertFalse(scheduler.dropin_path.exists())
            self.assertFalse(scheduler.marker_path.exists())
            self.assertEqual(systemctl.calls.count(("start", "zeus-temp-clean.timer")), 1)

    def test_invalid_deadline_does_not_disarm_existing_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, systemctl = self.scheduler(directory)
            scheduler.arm(timed_plan())
            before = scheduler.dropin_path.read_text()
            calls_before = list(systemctl.calls)

            with self.assertRaises(module.SchedulerError):
                scheduler.arm(timed_plan(float("nan")))

            self.assertEqual(scheduler.dropin_path.read_text(), before)
            self.assertEqual(systemctl.calls, calls_before)

    def test_run_sweeps_then_rearms_from_new_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler, _systemctl = self.scheduler(
                directory,
                schedule_source=lambda: timed_plan(8200.0),
                sweep_source=lambda: {"ok": True, "swept": True, "deleted": 1},
            )

            result = scheduler.run()

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sweep"]["deleted"], 1)
            self.assertTrue(result["schedule"]["timer_armed"], result)
            self.assertIn("OnCalendar=@8200\n", scheduler.dropin_path.read_text())

    def test_default_sweep_source_skips_usage_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            original_sweep = module.zeus_temp.sweep

            def fake_sweep(*, include_usage=True, synchronize_timer=True):
                self.assertFalse(synchronize_timer)
                calls.append(include_usage)
                return {"ok": True, "swept": True, "deleted": 0}

            module.zeus_temp.sweep = fake_sweep
            try:
                scheduler, _systemctl = self.scheduler(
                    directory,
                    schedule_source=lambda: timed_plan(8200.0),
                )
                result = scheduler.run()
            finally:
                module.zeus_temp.sweep = original_sweep

            self.assertTrue(result["ok"], result)
            self.assertEqual(calls, [False])

    def test_unexpected_sweep_error_disarms_to_avoid_busy_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail_sweep():
                raise RuntimeError("temporary backend failure")

            scheduler, systemctl = self.scheduler(
                directory,
                schedule_source=lambda: timed_plan(),
                sweep_source=fail_sweep,
            )
            scheduler.arm(timed_plan())

            result = scheduler.run()

            self.assertFalse(result["ok"], result)
            self.assertFalse(result["schedule"]["timer_armed"], result)
            self.assertFalse(scheduler.dropin_path.exists())
            self.assertFalse(scheduler.marker_path.exists())
            self.assertEqual(systemctl.calls.count(("start", "zeus-temp-clean.timer")), 1)

    def test_newer_disarm_waits_for_older_arm_and_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            entered_stop = threading.Event()
            release_stop = threading.Event()
            newer_query = threading.Event()

            class BlockingSystemctl(FakeSystemctl):
                def __call__(self, arguments):
                    result = super().__call__(arguments)
                    if tuple(arguments) == ("stop", "zeus-temp-clean.timer") and not entered_stop.is_set():
                        entered_stop.set()
                        self.assert_release()
                    return result

                def assert_release(self):
                    release_stop.wait(timeout=5)

            systemctl = BlockingSystemctl()
            old_scheduler = module.TimerScheduler(
                home=directory,
                config_home=Path(directory) / "config",
                runtime_dir=Path(directory) / "runtime",
                schedule_source=lambda: timed_plan(4600),
                systemctl=systemctl,
            )

            def newer_schedule():
                newer_query.set()
                return {
                    "ok": True,
                    "enabled": True,
                    "enrolled": True,
                    "scheduled": False,
                    "mode": "never",
                    "interval_seconds": None,
                    "next_cleanup_at": None,
                    "due": False,
                    "due_reason": "never",
                }

            newer_scheduler = module.TimerScheduler(
                home=directory,
                config_home=Path(directory) / "config",
                runtime_dir=Path(directory) / "runtime",
                schedule_source=newer_schedule,
                systemctl=systemctl,
            )
            results = []
            first = threading.Thread(target=lambda: results.append(old_scheduler.arm()))
            second = threading.Thread(target=lambda: results.append(newer_scheduler.arm()))
            first.start()
            self.assertTrue(entered_stop.wait(timeout=5))
            second.start()
            self.assertFalse(newer_query.wait(timeout=0.1))
            release_stop.set()
            first.join(timeout=5)
            second.join(timeout=5)

            self.assertEqual(len(results), 2)
            self.assertFalse(newer_scheduler.dropin_path.exists())
            self.assertFalse(newer_scheduler.marker_path.exists())
            self.assertTrue(newer_query.is_set())
            self.assertEqual(systemctl.calls.count(("start", "zeus-temp-clean.timer")), 1)


class TempTimerUnitTests(unittest.TestCase):
    def test_timer_is_dormant_and_has_no_minute_polling(self):
        timer = (ROOT / "desktop/rootfs/usr/lib/systemd/user/zeus-temp-clean.timer").read_text()
        service = (ROOT / "desktop/rootfs/usr/lib/systemd/user/zeus-temp-clean.service").read_text()

        self.assertNotIn("OnStartupSec=", timer)
        self.assertNotIn("OnUnitActiveSec=", timer)
        self.assertIn("OnCalendar=2100-01-01", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("ExecStart=/usr/libexec/zeus-temp-scheduler run", service)
        self.assertFalse(
            (ROOT / "desktop/rootfs/usr/lib/systemd/user/timers.target.wants/zeus-temp-clean.timer").exists()
        )


if __name__ == "__main__":
    unittest.main()
