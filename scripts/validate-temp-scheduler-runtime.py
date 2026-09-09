#!/usr/bin/env python3
"""Exercise Zeus Temp scheduling through a real user systemd manager.

This is an acceptance harness for a deployed image.  It creates one UUID-named
timer and service in ``$XDG_RUNTIME_DIR/systemd/user`` and points the
``HomeManager`` at a disposable ``TemporaryDirectory`` home.  The production
``zeus-temp-clean.*`` units, the owner's home, preferences, and their failed
unit state are never addressed.

The scheduler's minimum timed interval is fifteen minutes, so the harness
shortens only the deadline supplied to ``TimerScheduler.arm`` through a
controlled fixture schedule.  The fixture service still calls the deployed
``HomeManager.sweep`` against the disposable home, then reads
``HomeManager.schedule`` before the scheduler rearms the UUID-named timer.
The output records both the actual backend calls and the controlled schedule
seam.  The fixture timer uses ``Persistent=false`` so systemd does not create
a timestamp in the owner's data home; the explicit overdue ``OnActiveSec``
path is covered, while persistent missed-event replay remains a deployment
check outside this bounded harness.
"""

from __future__ import annotations

import argparse
import datetime as datetime_module
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Callable, Mapping, Sequence


DEFAULT_SCHEDULER = "/usr/libexec/zeus-temp-scheduler"
DEFAULT_MODULE_DIR = "/usr/lib/zeus"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"
DEFAULT_PYTHON = "/usr/bin/python3"
MAX_TIMEOUT_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 28.0
MIN_CUSTOM_INTERVAL_SECONDS = 15 * 60


class HarnessError(RuntimeError):
    """The deployed runtime could not satisfy one fixture assertion."""


def _utc_now() -> str:
    return datetime_module.datetime.now(datetime_module.timezone.utc).isoformat()


def _write_text(path: Path, payload: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_source(name: str, path: Path) -> Any:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise HarnessError(f"cannot load Python source: {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and other reflection-based modules expect their defining
    # module to be visible while class bodies execute.
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class UserSystemd:
    """Small subprocess adapter that never invokes a shell."""

    def __init__(self, executable: Path, *, timeout: float) -> None:
        self.executable = executable
        self.timeout = timeout

    def run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [str(self.executable), "--user", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HarnessError(f"systemctl --user {' '.join(arguments)}: {error}") from error

    def require(self, arguments: Sequence[str], *, context: str) -> subprocess.CompletedProcess[str]:
        result = self.run(arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise HarnessError(
                f"{context} failed ({result.returncode}){suffix}"
            )
        return result

    def is_active(self, unit: str) -> tuple[bool, str]:
        result = self.run(("is-active", unit))
        state = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, state

    def value(self, unit: str, property_name: str) -> str:
        result = self.require(
            ("show", unit, f"--property={property_name}", "--value"),
            context=f"show {unit} {property_name}",
        )
        return result.stdout.strip()


def _runner_source() -> str:
    """Return the isolated service entry point written into the fixture."""

    return r'''#!/usr/bin/env python3
"""Private entry point for validate-temp-scheduler-runtime.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def load_source(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load scheduler source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def append_event(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    module_dir = Path(config["module_dir"]).resolve()
    sys.path.insert(0, str(module_dir.parent))
    from zeus import zeus_temp

    scheduler_module = load_source("zeus_temp_scheduler_fixture", Path(config["scheduler"]))
    scheduler_module.TIMER_UNIT = config["timer_unit"]
    scheduler_module.SERVICE_UNIT = config["service_unit"]
    scheduler_module.ARMED_MARKER = config["marker_name"]
    scheduler_module.LOCK_FILE = config["lock_name"]

    manager = zeus_temp.HomeManager(
        config["home"],
        now=time.time,
        boot_id=config["boot_id"],
        proc_root=None,
    )
    backend_schedule = None
    sweep_called = False

    def schedule():
        nonlocal backend_schedule
        backend_schedule = dict(manager.schedule())
        if backend_schedule.get("ok") is not True:
            return backend_schedule
        result = dict(backend_schedule)
        result.update({
            "scheduled": True,
            "mode": "custom",
            "interval_seconds": config["backend_interval_seconds"],
            "next_cleanup_at": time.time() + float(config["rearm_seconds"]),
            "due": False,
            "due_reason": "not-due",
        })
        return result

    def sweep():
        nonlocal sweep_called
        sweep_called = True
        if config.get("fail_sweep"):
            raise RuntimeError("fixture injected sweep failure")
        return manager.sweep(force=True, include_usage=False)

    def systemctl(arguments):
        return subprocess.run(
            [config["systemctl"], "--user", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=float(config["systemctl_timeout"]),
        )

    started = time.monotonic()
    try:
        scheduler = scheduler_module.TimerScheduler(
            home=config["home"],
            config_home=config["systemd_runtime"],
            runtime_dir=config["runtime_dir"],
            schedule_source=schedule,
            sweep_source=sweep,
            systemctl=systemctl,
        )
        result = scheduler.run()
    except Exception as error:
        result = {
            "ok": False,
            "phase": name,
            "error": str(error),
            "error_type": type(error).__name__,
        }

    event = {
        "phase": config["phase"],
        "started_at": started,
        "finished_at": time.monotonic(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "backend_calls": {
            "home_manager_schedule": backend_schedule is not None,
            "home_manager_sweep": sweep_called,
        },
        "backend_schedule": backend_schedule,
        "result": result,
    }
    append_event(Path(config["events"]), event)
    print(json.dumps(event, sort_keys=True, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


class RuntimeFixture:
    """Own every path the harness creates, including runtime unit files."""

    def __init__(
        self,
        root: Path,
        *,
        module_dir: Path,
        scheduler_path: Path,
        systemctl: UserSystemd,
        python_executable: Path,
        runtime_base: Path,
        scheduler_module: Any,
    ) -> None:
        self.root = root.resolve()
        self.module_dir = module_dir.resolve()
        self.scheduler_path = scheduler_path.resolve()
        self.systemctl = systemctl
        self.python_executable = python_executable.resolve()
        self.runtime_base = runtime_base.resolve()
        self.systemd_runtime = self.runtime_base
        self.scheduler_module = scheduler_module

        nonce = uuid.uuid4().hex
        self.timer_unit = f"zeus-temp-fixture-{nonce}.timer"
        self.service_unit = f"zeus-temp-fixture-{nonce}.service"
        self.marker_name = f"zeus-temp-fixture-{nonce}.armed"
        self.lock_name = f"zeus-temp-fixture-{nonce}.lock"
        self.unit_dir = self.runtime_base / "systemd" / "user"
        self.timer_path = self.unit_dir / self.timer_unit
        self.service_path = self.unit_dir / self.service_unit
        self.dropin_dir = self.unit_dir / f"{self.timer_unit}.d"
        self.dropin_path = self.dropin_dir / "10-zeus-deadline.conf"
        self.fixture_runtime = self.root / "runtime"
        self.fixture_runtime.mkdir(mode=0o700)
        self.events_path = self.root / "events.jsonl"
        self.config_path = self.root / "service-config.json"
        self.runner_path = self.root / "fixture-service.py"
        self.active_scheduler: Any | None = None
        self.created_unit_payloads: dict[Path, str] = {}

    def _assert_safe_paths(self) -> None:
        if self.timer_unit in {"zeus-temp-clean.timer", "zeus-temp-clean.service"}:
            raise HarnessError("fixture unit name collided with production unit")
        if self.service_unit in {"zeus-temp-clean.timer", "zeus-temp-clean.service"}:
            raise HarnessError("fixture service name collided with production unit")
        if self.root == Path.home().resolve() or Path.home().resolve() in self.root.parents:
            raise HarnessError("fixture root may not be the owner home")
        if self.runtime_base != Path(os.environ.get("XDG_RUNTIME_DIR", self.runtime_base)).resolve():
            raise HarnessError("user runtime path changed while creating fixture")
        for path in (self.timer_path, self.service_path, self.dropin_dir):
            if path.exists() or path.is_symlink():
                raise HarnessError(f"unexpected pre-existing fixture path: {path}")

    def install_units(self) -> None:
        self._assert_safe_paths()
        if not self.runtime_base.is_dir():
            raise HarnessError(f"XDG_RUNTIME_DIR is unavailable: {self.runtime_base}")
        if self.runtime_base.stat().st_uid != os.getuid():
            raise HarnessError("XDG_RUNTIME_DIR is not owned by the current user")
        self.unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runner_path.write_text(_runner_source(), encoding="utf-8")
        self.runner_path.chmod(0o700)
        marker = self.fixture_runtime / self.marker_name
        if any(char.isspace() for char in str(self.root)):
            raise HarnessError("fixture path contains whitespace unsupported by unit syntax")

        service_payload = (
            "[Unit]\n"
            "Description=Disposable Zeus Temp scheduler service\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={self.python_executable} {self.runner_path} {self.config_path}\n"
            "TimeoutStartSec=10s\n"
        )
        timer_payload = (
            "[Unit]\n"
            "Description=Disposable Zeus Temp scheduler timer\n"
            f"ConditionPathExists={marker}\n\n"
            "[Timer]\n"
            "OnCalendar=2100-01-01 00:00:00 UTC\n"
            "Persistent=false\n"
            "AccuracySec=1s\n"
            f"Unit={self.service_unit}\n"
        )
        for path, payload in (
            (self.service_path, service_payload),
            (self.timer_path, timer_payload),
        ):
            if path.exists() or path.is_symlink():
                raise HarnessError(f"unexpected pre-existing fixture unit: {path}")
            _write_text(path, payload, mode=0o644)
            self.created_unit_payloads[path] = payload

        self.systemctl.require(("daemon-reload",), context="fixture daemon-reload")
        load_state = self.systemctl.value(self.timer_unit, "LoadState")
        if load_state != "loaded":
            raise HarnessError(f"fixture timer did not load: {load_state!r}")

    def write_service_config(
        self,
        *,
        home: Path,
        phase: str,
        fail_sweep: bool = False,
        rearm_seconds: float = 8.0,
    ) -> None:
        payload = {
            "home": str(home.resolve()),
            "module_dir": str(self.module_dir),
            "scheduler": str(self.scheduler_path),
            "systemctl": str(self.systemctl.executable),
            "systemctl_timeout": self.systemctl.timeout,
            "systemd_runtime": str(self.systemd_runtime),
            "runtime_dir": str(self.fixture_runtime),
            "timer_unit": self.timer_unit,
            "service_unit": self.service_unit,
            "marker_name": self.marker_name,
            "lock_name": self.lock_name,
            "events": str(self.events_path),
            "phase": phase,
            "boot_id": f"zeus-runtime-fixture-{phase}",
            "backend_interval_seconds": MIN_CUSTOM_INTERVAL_SECONDS,
            "rearm_seconds": rearm_seconds,
            "fail_sweep": fail_sweep,
        }
        _write_text(self.config_path, json.dumps(payload, sort_keys=True) + "\n", mode=0o600)

    def scheduler_for(self, manager: Any) -> Any:
        self.scheduler_module.TIMER_UNIT = self.timer_unit
        self.scheduler_module.SERVICE_UNIT = self.service_unit
        self.scheduler_module.ARMED_MARKER = self.marker_name
        self.scheduler_module.LOCK_FILE = self.lock_name
        self.active_scheduler = self.scheduler_module.TimerScheduler(
            home=manager.home,
            config_home=self.systemd_runtime,
            runtime_dir=self.fixture_runtime,
            schedule_source=manager.schedule,
            sweep_source=manager.sweep,
            systemctl=self.systemctl.run,
        )
        return self.active_scheduler

    def disarm(self) -> None:
        if self.active_scheduler is None:
            return
        self.active_scheduler.arm({
            "ok": True,
            "enabled": True,
            "enrolled": True,
            "scheduled": False,
            "mode": "never",
            "interval_seconds": None,
            "next_cleanup_at": None,
            "due": False,
            "due_reason": "never",
        })

    def cleanup(self) -> dict[str, Any]:
        errors: list[str] = []
        for unit in (self.timer_unit, self.service_unit):
            result = self.systemctl.run(("stop", unit))
            if result.returncode not in (0, 4, 5):
                detail = (result.stderr or result.stdout or "").strip()
                errors.append(f"stop {unit}: {detail or result.returncode}")
        for unit in (self.timer_unit, self.service_unit):
            result = self.systemctl.run(("reset-failed", unit))
            detail = (result.stderr or result.stdout or "").strip()
            if result.returncode not in (0, 4, 5) and "not loaded" not in detail.lower():
                errors.append(f"reset-failed {unit}: {detail or result.returncode}")

        # Remove only files this fixture created.  A changed or replaced path
        # is left in place so cleanup cannot delete another actor's file.
        for path, payload in self.created_unit_payloads.items():
            try:
                if path.is_symlink():
                    errors.append(f"refusing to remove replaced symlink: {path}")
                elif path.exists():
                    if path.read_text(encoding="utf-8") != payload:
                        errors.append(f"refusing to remove changed unit: {path}")
                    else:
                        path.unlink()
            except OSError as error:
                errors.append(f"remove {path}: {error}")
        try:
            if self.dropin_dir.is_symlink():
                errors.append(f"refusing to inspect replaced drop-in directory: {self.dropin_dir}")
            elif self.dropin_path.is_symlink():
                errors.append(f"refusing to remove replaced symlink: {self.dropin_path}")
            else:
                if self.dropin_path.exists():
                    self.dropin_path.unlink()
                if self.dropin_dir.exists():
                    leftovers = list(self.dropin_dir.iterdir())
                    if leftovers:
                        errors.append(
                            "refusing to remove fixture drop-in directory with leftovers: "
                            + ", ".join(str(path) for path in leftovers)
                        )
                    else:
                        self.dropin_dir.rmdir()
        except OSError as error:
            errors.append(f"remove fixture drop-in: {error}")

        reload_result = self.systemctl.run(("daemon-reload",))
        if reload_result.returncode != 0:
            detail = (reload_result.stderr or reload_result.stdout or "").strip()
            errors.append(f"final fixture daemon-reload: {detail or reload_result.returncode}")
        return {"ok": not errors, "errors": errors}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def _new_home(root: Path, zeus_temp: Any, name: str) -> Any:
    home = root / "homes" / name
    home.mkdir(mode=0o700, parents=True)
    manager = zeus_temp.HomeManager(
        home,
        now=time.time,
        boot_id=f"zeus-runtime-{name}",
        proc_root=None,
    )
    setup = manager.setup(include_usage=False)
    _require(setup.get("ok") is True, f"fixture setup failed: {setup}")
    return manager


def _timed_plan(base: Mapping[str, Any], deadline: float, *, due: bool) -> dict[str, Any]:
    _require(base.get("ok") is True, f"backend schedule failed: {base}")
    result = dict(base)
    result.update({
        "ok": True,
        "enabled": True,
        "enrolled": True,
        "scheduled": True,
        "mode": "custom",
        "interval_seconds": MIN_CUSTOM_INTERVAL_SECONDS,
        "next_cleanup_at": deadline,
        "due": due,
        "due_reason": "due" if due else "not-due",
    })
    return result


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _wait_for_event_count(path: Path, count: int, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _read_events(path)
        if len(events) >= count:
            return events
        time.sleep(0.1)
    events = _read_events(path)
    raise HarnessError(f"timed out waiting for {count} fixture event(s); saw {len(events)}")


def _sleep_with_budget(seconds: float, deadline: float) -> None:
    remaining = max(0.0, deadline - time.monotonic())
    time.sleep(min(seconds, remaining))


def _assert_no_armed_timer(fixture: RuntimeFixture, *, context: str) -> dict[str, Any]:
    active, state = fixture.systemctl.is_active(fixture.timer_unit)
    _require(not active, f"{context}: fixture timer is active ({state})")
    _require(not (fixture.fixture_runtime / fixture.marker_name).exists(), f"{context}: marker remains")
    _require(not fixture.dropin_path.exists(), f"{context}: scheduler drop-in remains")
    return {"active": active, "state": state, "marker": False, "dropin": False}


def _case_boot_and_never(fixture: RuntimeFixture, zeus_temp: Any) -> dict[str, Any]:
    manager = _new_home(fixture.root, zeus_temp, "boot-never")
    scheduler = fixture.scheduler_for(manager)
    boot_plan = manager.schedule()
    _require(boot_plan.get("mode") == "boot", f"expected boot policy: {boot_plan}")
    boot_result = scheduler.arm(boot_plan)
    _require(boot_result.get("ok") is True, f"boot disarm failed: {boot_result}")
    boot_state = _assert_no_armed_timer(fixture, context="boot")

    never_status = manager.set_policy("never")
    _require(never_status.get("ok") is True, f"never policy failed: {never_status}")
    never_plan = manager.schedule()
    _require(never_plan.get("mode") == "never", f"expected never policy: {never_plan}")
    never_result = scheduler.arm(never_plan)
    _require(never_result.get("ok") is True, f"never disarm failed: {never_result}")
    never_state = _assert_no_armed_timer(fixture, context="never")
    return {
        "ok": True,
        "assertions": {
            "boot_has_no_armed_timer": True,
            "never_has_no_armed_timer": True,
            "boot_backend_schedule_used": True,
            "never_backend_schedule_used": True,
        },
        "observations": {"boot": boot_state, "never": never_state},
    }


def _case_future_fires_once_and_rearms(
    fixture: RuntimeFixture,
    zeus_temp: Any,
    budget_deadline: float,
) -> dict[str, Any]:
    baseline_events = len(_read_events(fixture.events_path))
    manager = _new_home(fixture.root, zeus_temp, "future-rearm")
    manager.set_policy("custom", MIN_CUSTOM_INTERVAL_SECONDS)
    payload = manager.home / "Temp" / "future-fixture.bin"
    payload.write_bytes(b"disposable scheduler fixture")
    scheduler = fixture.scheduler_for(manager)
    fixture.write_service_config(
        home=manager.home,
        phase="future-rearm",
        rearm_seconds=8.0,
    )
    deadline = time.time() + 2.5
    plan = _timed_plan(manager.schedule(), deadline, due=False)
    arm_result = scheduler.arm(plan)
    _require(arm_result.get("ok") is True and arm_result.get("timer_armed") is True, f"future arm failed: {arm_result}")
    active, state = fixture.systemctl.is_active(fixture.timer_unit)
    _require(active, f"future timer did not start: {state}")
    _require((fixture.fixture_runtime / fixture.marker_name).exists(), "future marker was not written")

    events = _wait_for_event_count(
        fixture.events_path,
        baseline_events + 1,
        min(8.0, max(0.1, budget_deadline - time.monotonic())),
    )
    _sleep_with_budget(1.0, budget_deadline)
    events_after_settle = _read_events(fixture.events_path)
    _require(
        len(events_after_settle) == baseline_events + 1,
        f"future timer fired more than once: {events_after_settle}",
    )
    event = events[-1]
    result = event.get("result", {})
    sweep = result.get("sweep", {})
    schedule = result.get("schedule", {})
    _require(event.get("backend_calls", {}).get("home_manager_schedule") is True, "future run did not query backend schedule")
    _require(event.get("backend_calls", {}).get("home_manager_sweep") is True, "future run did not sweep fixture backend")
    _require(sweep.get("swept") is True and int(sweep.get("deleted", 0)) >= 1, f"future backend sweep did not delete fixture: {event}")
    _require(schedule.get("timer_armed") is True, f"future run did not rearm: {event}")
    rearm_deadline = float(schedule.get("next_cleanup_at", 0))
    _require(rearm_deadline > time.time(), f"future rearm deadline is not in the future: {event}")
    _require(not payload.exists(), "future fixture payload survived actual backend sweep")
    active_after, state_after = fixture.systemctl.is_active(fixture.timer_unit)
    _require(active_after, f"future timer is not active after rearm: {state_after}")
    next_elapse = fixture.systemctl.value(fixture.timer_unit, "NextElapseUSecRealtime")
    _require(next_elapse not in {"", "0", "-"}, f"future timer has no next elapse: {next_elapse!r}")
    return {
        "ok": True,
        "assertions": {
            "absolute_future_deadline_fired": True,
            "fired_exactly_once_before_rearm": True,
            "actual_home_manager_sweep_deleted_fixture": True,
            "scheduler_rearmed_timer": True,
            "rearm_deadline_is_future": True,
            "next_systemd_elapse_present": True,
        },
        "observations": {
            "initial_deadline": deadline,
            "initial_timer_state": state,
            "post_rearm_timer_state": state_after,
            "rearm_deadline": rearm_deadline,
            "next_elapse": next_elapse,
            "event": event,
        },
    }


def _case_overdue_catchup(
    fixture: RuntimeFixture,
    zeus_temp: Any,
    budget_deadline: float,
) -> dict[str, Any]:
    baseline_events = len(_read_events(fixture.events_path))
    manager = _new_home(fixture.root, zeus_temp, "overdue")
    manager.set_policy("custom", MIN_CUSTOM_INTERVAL_SECONDS)
    payload = manager.home / "Temp" / "overdue-fixture.bin"
    payload.write_bytes(b"disposable overdue fixture")
    scheduler = fixture.scheduler_for(manager)
    fixture.write_service_config(home=manager.home, phase="overdue", rearm_seconds=8.0)
    deadline = time.time() - 30.0
    plan = _timed_plan(manager.schedule(), deadline, due=True)
    arm_result = scheduler.arm(plan)
    _require(arm_result.get("timer_armed") is True, f"overdue arm failed: {arm_result}")
    dropin_before = fixture.dropin_path.read_text(encoding="utf-8")
    _require("OnActiveSec=1s\n" in dropin_before, f"overdue arm did not use catch-up trigger: {dropin_before!r}")
    events = _wait_for_event_count(
        fixture.events_path,
        baseline_events + 1,
        min(7.0, max(0.1, budget_deadline - time.monotonic())),
    )
    _require(
        len(events) == baseline_events + 1,
        f"unexpected event count after overdue case: {events}",
    )
    event = events[-1]
    result = event.get("result", {})
    _require(result.get("ok") is True, f"overdue run failed: {event}")
    _require(result.get("schedule", {}).get("timer_armed") is True, f"overdue run did not rearm: {event}")
    _require(not payload.exists(), "overdue fixture payload survived actual backend sweep")
    return {
        "ok": True,
        "assertions": {
            "overdue_plan_used_one_second_catchup": True,
            "overdue_timer_fired_once": True,
            "actual_home_manager_sweep_deleted_fixture": True,
            "scheduler_rearmed_after_catchup": True,
        },
        "observations": {
            "overdue_deadline": deadline,
            "catchup_dropin": dropin_before,
            "event": event,
        },
    }


def _case_never_cancels(
    fixture: RuntimeFixture,
    zeus_temp: Any,
    budget_deadline: float,
) -> dict[str, Any]:
    baseline_events = len(_read_events(fixture.events_path))
    manager = _new_home(fixture.root, zeus_temp, "never-cancel")
    manager.set_policy("custom", MIN_CUSTOM_INTERVAL_SECONDS)
    scheduler = fixture.scheduler_for(manager)
    fixture.write_service_config(home=manager.home, phase="never-cancel", rearm_seconds=8.0)
    deadline = time.time() + 2.0
    arm_result = scheduler.arm(_timed_plan(manager.schedule(), deadline, due=False))
    _require(arm_result.get("timer_armed") is True, f"cancel case arm failed: {arm_result}")
    never_status = manager.set_policy("never")
    _require(never_status.get("ok") is True, f"cancel case policy change failed: {never_status}")
    cancel_result = scheduler.arm(manager.schedule())
    _require(cancel_result.get("ok") is True and cancel_result.get("timer_armed") is False, f"Never did not cancel: {cancel_result}")
    state = _assert_no_armed_timer(fixture, context="Never cancellation")
    _sleep_with_budget(3.0, budget_deadline)
    events_after_wait = _read_events(fixture.events_path)
    _require(
        len(events_after_wait) == baseline_events,
        "cancelled timer still invoked fixture service",
    )
    return {
        "ok": True,
        "assertions": {
            "timed_timer_was_armed": True,
            "changing_to_never_cancelled_timer": True,
            "cancel_removed_marker_and_dropin": True,
            "cancelled_deadline_did_not_fire": True,
        },
        "observations": {"cancelled_deadline": deadline, "state": state},
    }


def _case_failure_does_not_loop(
    fixture: RuntimeFixture,
    zeus_temp: Any,
    budget_deadline: float,
) -> dict[str, Any]:
    baseline_events = len(_read_events(fixture.events_path))
    manager = _new_home(fixture.root, zeus_temp, "failure")
    manager.set_policy("custom", MIN_CUSTOM_INTERVAL_SECONDS)
    payload = manager.home / "Temp" / "failure-fixture.bin"
    payload.write_bytes(b"must survive injected scheduler failure")
    scheduler = fixture.scheduler_for(manager)
    fixture.write_service_config(
        home=manager.home,
        phase="failure",
        fail_sweep=True,
        rearm_seconds=8.0,
    )
    deadline = time.time() - 30.0
    arm_result = scheduler.arm(_timed_plan(manager.schedule(), deadline, due=True))
    _require(arm_result.get("timer_armed") is True, f"failure case arm failed: {arm_result}")
    events = _wait_for_event_count(
        fixture.events_path,
        baseline_events + 1,
        min(7.0, max(0.1, budget_deadline - time.monotonic())),
    )
    _sleep_with_budget(2.0, budget_deadline)
    events_after_settle = _read_events(fixture.events_path)
    _require(
        len(events_after_settle) == baseline_events + 1,
        f"failure path entered a rapid loop: {events_after_settle}",
    )
    event = events[-1]
    result = event.get("result", {})
    _require(result.get("ok") is False, f"injected failure unexpectedly succeeded: {event}")
    _require(result.get("schedule", {}).get("timer_armed") is False, f"failure path left timer armed: {event}")
    _require(payload.exists(), "injected failure unexpectedly deleted fixture payload")
    state = _assert_no_armed_timer(fixture, context="failure")
    return {
        "ok": True,
        "assertions": {
            "injected_failure_reported": True,
            "failure_disarmed_timer": True,
            "failure_did_not_rapid_loop": True,
            "fixture_payload_survived_failure": True,
        },
        "observations": {"failure_deadline": deadline, "state": state, "event": event},
    }


def _run_case(name: str, function: Callable[[], dict[str, Any]], fixture: RuntimeFixture) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = function()
        result.setdefault("ok", True)
    except Exception as error:
        result = {
            "ok": False,
            "error": str(error),
            "error_type": type(error).__name__,
        }
    try:
        fixture.disarm()
    except Exception as error:
        result["ok"] = False
        result["disarm_error"] = str(error)
        result.setdefault("error_type", type(error).__name__)
    result.setdefault("phase", name)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def _validate_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    scheduler_path = Path(args.scheduler).resolve()
    module_dir = Path(args.module_dir).resolve()
    systemctl_path = Path(args.systemctl).resolve()
    python_path = Path(args.python).resolve()
    for path, label in (
        (scheduler_path, "scheduler"),
        (module_dir / "zeus_temp.py", "zeus_temp module"),
        (systemctl_path, "systemctl"),
        (python_path, "service Python"),
    ):
        if not path.is_file() or not os.access(path, os.R_OK):
            raise HarnessError(f"{label} is unavailable: {path}")
    if not os.access(systemctl_path, os.X_OK):
        raise HarnessError(f"systemctl is not executable: {systemctl_path}")
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    runtime_base = Path(runtime_value).resolve() if runtime_value else Path(f"/run/user/{os.getuid()}").resolve()
    if not runtime_base.is_dir():
        raise HarnessError(f"user runtime directory is unavailable: {runtime_base}")
    return scheduler_path, module_dir, systemctl_path, python_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--scheduler", default=DEFAULT_SCHEDULER, help="deployed scheduler wrapper")
    parser.add_argument("--module-dir", default=DEFAULT_MODULE_DIR, help="deployed Zeus Python module directory")
    parser.add_argument("--systemctl", default=DEFAULT_SYSTEMCTL, help="systemctl executable")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Python executable available to fixture service")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"overall fixture budget in seconds (maximum {MAX_TIMEOUT_SECONDS:g})",
    )
    args = parser.parse_args(argv)
    started_at = _utc_now()
    started = time.monotonic()
    output: dict[str, Any] = {
        "schema": "zeus-temp-scheduler-runtime-v1",
        "ok": False,
        "started_at": started_at,
        "scope": {
            "real_user_systemd": True,
            "production_units_touched": False,
            "owner_home_touched": False,
            "owner_preferences_touched": False,
            "fixture_home": "TemporaryDirectory",
        },
        "backend": {
            "manager": "zeus_temp.HomeManager",
            "schedule_api": "HomeManager.schedule",
            "sweep_api": "HomeManager.sweep(force=True, include_usage=False)",
            "proc_root": None,
            "schedule_deadlines": {
                "boot_never": "actual HomeManager.schedule() result",
                "timed_arm": "controlled shortened fixture deadline validated against actual backend plan",
                "service_rearm": "controlled short deadline after actual HomeManager.schedule() read",
            },
            "sweep_execution": "actual HomeManager.sweep() against disposable fixture home",
            "systemd_persistence": (
                "Persistent=false fixture avoids an owner data-home timestamp; "
                "explicit overdue catch-up is covered"
            ),
        },
        "cases": {},
    }
    fixture: RuntimeFixture | None = None
    cleanup: dict[str, Any] = {"ok": True, "errors": []}
    try:
        if not 0 < args.timeout <= MAX_TIMEOUT_SECONDS:
            raise HarnessError(f"--timeout must be in (0, {MAX_TIMEOUT_SECONDS:g}] seconds")
        scheduler_path, module_dir, systemctl_path, python_path = _validate_paths(args)
        runtime_value = os.environ.get("XDG_RUNTIME_DIR")
        runtime_base = Path(runtime_value).resolve() if runtime_value else Path(f"/run/user/{os.getuid()}").resolve()
        systemd = UserSystemd(systemctl_path, timeout=min(8.0, args.timeout))
        systemd.require(
            ("show", "--property=Version", "--value"),
            context="connect to user systemd manager",
        )
        parent_module_dir = module_dir.parent
        if str(parent_module_dir) not in sys.path:
            sys.path.insert(0, str(parent_module_dir))
        zeus_temp = _load_source("zeus_temp_runtime_fixture", module_dir / "zeus_temp.py")
        scheduler_module = _load_source("zeus_temp_scheduler_runtime", scheduler_path)
        with tempfile.TemporaryDirectory(prefix="zeus-temp-scheduler-runtime-") as temporary:
            fixture = RuntimeFixture(
                Path(temporary),
                module_dir=module_dir,
                scheduler_path=scheduler_path,
                systemctl=systemd,
                python_executable=python_path,
                runtime_base=runtime_base,
                scheduler_module=scheduler_module,
            )
            fixture.install_units()
            output["systemd"] = {
                "runtime_unit_dir": str(fixture.unit_dir),
                "timer_unit": fixture.timer_unit,
                "service_unit": fixture.service_unit,
                "marker_path": str(fixture.fixture_runtime / fixture.marker_name),
                "dropin_path": str(fixture.dropin_path),
            }
            budget_deadline = started + args.timeout
            output["cases"]["boot_never"] = _run_case(
                "boot_never",
                lambda: _case_boot_and_never(fixture, zeus_temp),
                fixture,
            )
            output["cases"]["future_rearm"] = _run_case(
                "future_rearm",
                lambda: _case_future_fires_once_and_rearms(fixture, zeus_temp, budget_deadline),
                fixture,
            )
            output["cases"]["overdue_catchup"] = _run_case(
                "overdue_catchup",
                lambda: _case_overdue_catchup(fixture, zeus_temp, budget_deadline),
                fixture,
            )
            output["cases"]["never_cancels"] = _run_case(
                "never_cancels",
                lambda: _case_never_cancels(fixture, zeus_temp, budget_deadline),
                fixture,
            )
            output["cases"]["failure_no_loop"] = _run_case(
                "failure_no_loop",
                lambda: _case_failure_does_not_loop(fixture, zeus_temp, budget_deadline),
                fixture,
            )
            output["fixture"] = {
                "home_root": str(fixture.root),
                "all_backend_state_under_fixture": True,
            }
            cleanup = fixture.cleanup()
    except Exception as error:
        output["error"] = str(error)
        output["error_type"] = type(error).__name__
        if fixture is not None:
            cleanup = fixture.cleanup()
    output["cleanup"] = cleanup
    output["finished_at"] = _utc_now()
    output["duration_seconds"] = round(time.monotonic() - started, 3)
    cases_ok = bool(output["cases"]) and all(
        case.get("ok") is True for case in output["cases"].values()
    )
    output["ok"] = not output.get("error") and cases_ok and cleanup.get("ok") is True
    print(json.dumps(output, sort_keys=True, indent=2, default=str))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
