"""Focused contract tests for the unprivileged Fedora installer launcher."""

from __future__ import annotations

import contextlib
import importlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


INSTALLER_ROOT = Path(__file__).resolve().parents[1] / "installer"
if str(INSTALLER_ROOT) not in sys.path:
    sys.path.insert(0, str(INSTALLER_ROOT))

from zeus_installer import (  # noqa: E402
    InstallerError,
    PRODUCT_VERSION,
    build_plan,
    json_text,
    validate_allocation,
)
from zeus_installer import __main__ as cli  # noqa: E402
from zeus_installer import gui  # noqa: E402


class LauncherPlanTests(unittest.TestCase):
    def test_version_and_allocation_contract(self):
        self.assertEqual(PRODUCT_VERSION, "0.1.0-preview.2")
        self.assertEqual(validate_allocation("160"), 160)
        for value in (0, -1, True, "", "12.5", 12.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_allocation(value)

    def test_build_plan_exposes_target_alias_without_collecting_twice(self):
        inventory = {
            "disk": {"path": "/dev/vda", "serial": "fixture"},
            "mounts": [],
        }
        worker = types.SimpleNamespace(
            collect=lambda: inventory,
            plan=lambda value, allocation_gib: {
                "schema_version": 1,
                "supported": True,
                "blockers": [],
                "inventory": value,
                "proposed_target_layout": {"allocation_gib": allocation_gib},
                "fingerprint": "sha256:" + "a" * 64,
            },
        )
        plan = build_plan(160, preflight_module=worker)
        self.assertEqual(plan["allocation_gib"], 160)
        self.assertEqual(plan["target"]["allocation_gib"], 160)
        self.assertIn("proposed_target_layout", plan)

    def test_preflight_name_resolves_to_worker_module(self):
        package = importlib.import_module("zeus_installer")
        worker = package.preflight
        self.assertEqual(worker.__name__, "zeus_installer.preflight")
        self.assertTrue(callable(worker.collect))

    def test_json_text_is_stable_and_does_not_emit_binary_content(self):
        rendered = json_text({"path": Path("/tmp/plan"), "bytes": b"secret"})
        self.assertIn('"path": "/tmp/plan"', rendered)
        self.assertIn("<binary value omitted>", rendered)
        self.assertNotIn("secret", rendered)

    def test_target_summary_uses_partition_roles(self):
        target = {
            "allocation_gib": 128,
            "home_gib": 1,
            "new_partitions": [
                {"role": "esp", "size_gib": 1},
                {"role": "boot", "size_gib": 2},
                {"role": "root", "size_gib": 125},
            ],
        }
        rendered = gui._format_target({"target": target})
        self.assertEqual(
            rendered,
            "Allocation: 128 GiB · ESP: 1 GiB · Boot: 2 GiB · Root: 125 GiB",
        )
        self.assertNotIn("Home:", rendered)

    def test_target_summary_formats_storage_partition_sectors(self):
        for sector_size in (512, 4096):
            with self.subTest(sector_size=sector_size):
                target = {
                    "allocation_gib": 128,
                    "sector_size": sector_size,
                    "home_gib": 1,
                    "partitions": [
                        {"number": 4, "name": "Zeus EFI", "size": 1024**3 // sector_size},
                        {"number": 5, "name": "Zeus boot", "size": 2 * 1024**3 // sector_size},
                        {"number": 6, "name": "Zeus root", "size": 125 * 1024**3 // sector_size},
                    ],
                }
                rendered = gui._format_target({"target": target})
                self.assertEqual(
                    rendered,
                    "Allocation: 128 GiB · ESP: 1 GiB · Boot: 2 GiB · Root: 125 GiB",
                )
                self.assertNotIn("Home:", rendered)

    def test_disk_summary_keeps_identity_without_internal_fingerprint(self):
        rendered = gui._format_disk(
            {
                "inventory": {"disk": {"device": "/dev/sda", "size_gib": 931.5}},
                "fingerprint": "sha256:" + "a" * 64,
            }
        )
        self.assertEqual(rendered, "Device: /dev/sda · Size Gib: 931.5")
        self.assertNotIn("fingerprint", rendered.lower())

    def test_disk_summary_matches_authoritative_target_when_zram_is_present(self):
        rendered = gui._format_disk(
            {
                "target": {"source_disk": {"path": "/dev/nvme0n1"}},
                "inventory": {
                    "partition_table": {"device": "/dev/nvme0n1"},
                    "block_devices": [
                        {
                            "path": "/dev/zram0",
                            "type": "disk",
                            "model": "zram",
                            "size": 8 * 1024**3,
                        },
                        {
                            "path": "/dev/nvme0n1",
                            "type": "disk",
                            "model": "Fixture NVMe",
                            "size": int(931.5 * 1024**3),
                        },
                    ],
                },
            }
        )
        self.assertIn("Model: Fixture NVMe", rendered)
        self.assertIn("Path: /dev/nvme0n1", rendered)
        self.assertIn("Size Gib: 931.5", rendered)
        self.assertNotIn("zram", rendered.lower())

    def test_disk_summary_matches_storage_target_disk_shape(self):
        rendered = gui._format_disk(
            {
                "target": {"disk": "/dev/nvme0n1"},
                "inventory": {
                    "partition_table": {"device": "/dev/nvme0n1"},
                    "block_devices": [
                        {"path": "/dev/zram0", "type": "disk", "model": "zram"},
                        {
                            "path": "/dev/nvme0n1",
                            "type": "disk",
                            "model": "Fixture NVMe",
                            "size_bytes": 931 * 1024**3,
                        },
                    ],
                },
            }
        )
        self.assertIn("Model: Fixture NVMe", rendered)
        self.assertIn("Path: /dev/nvme0n1", rendered)
        self.assertIn("Size Gib: 931", rendered)
        self.assertNotIn("zram", rendered.lower())

    def test_disk_summary_does_not_guess_without_authoritative_target(self):
        rendered = gui._format_disk(
            {
                "inventory": {
                    "block_devices": [
                        {"path": "/dev/sda", "type": "disk", "model": "Unrelated"}
                    ]
                }
            }
        )
        self.assertEqual(rendered, "The preflight worker did not provide a target disk identity.")

    def test_disk_summary_rejects_conflicting_target_and_partition_paths(self):
        rendered = gui._format_disk(
            {
                "target": {"source_disk": {"path": "/dev/nvme0n1"}},
                "inventory": {
                    "disk": {"path": "/dev/sdb", "model": "Unrelated direct shape"},
                    "partition_table": {"device": "/dev/sda"},
                    "block_devices": [
                        {"path": "/dev/sda", "type": "disk", "model": "Unrelated"},
                        {"path": "/dev/nvme0n1", "type": "disk", "model": "Another"},
                    ],
                },
            }
        )
        self.assertEqual(rendered, "The preflight worker did not provide a target disk identity.")


class LauncherCliTests(unittest.TestCase):
    def test_parser_keeps_gui_import_out_of_preflight_help(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["preflight", "--json"]).command, "preflight")
        self.assertEqual(parser.parse_args(["gui"]).command, "gui")

    def test_human_plan_uses_blocker_explanations(self):
        output = io.StringIO()
        plan = {
            "supported": False,
            "blockers": ["insufficient_ram"],
            "blocker_details": [
                {"code": "insufficient_ram", "message": "At least 8 GiB is required"}
            ],
        }
        with contextlib.redirect_stdout(output):
            cli._print_human_plan(plan)
        self.assertIn("At least 8 GiB is required", output.getvalue())
        self.assertNotIn("  - insufficient_ram", output.getvalue())

    def test_preflight_error_json_is_machine_readable(self):
        output = io.StringIO()
        with mock.patch.object(cli, "build_plan", side_effect=InstallerError("fixture unavailable")):
            with contextlib.redirect_stdout(output):
                status = cli.main(["preflight", "--json"])
        self.assertEqual(status, 1)
        self.assertEqual(__import__("json").loads(output.getvalue())["error"], "preflight_unavailable")

    def test_installed_cli_preflight_facade_receives_only_numeric_allocation(self):
        calls: list[object] = []
        plan = {"supported": False, "blockers": ["fixture"], "allocation_gib": 160}
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "zeus-installer-helper"
            helper.write_text("fixed helper fixture", encoding="utf-8")
            backend = types.SimpleNamespace(
                HELPER_COMMAND=str(helper),
                preflight=lambda *, allocation_gib: calls.append(allocation_gib) or plan,
            )
            with mock.patch.object(cli.importlib, "import_module", return_value=backend):
                result = cli._installed_preflight(160)
        self.assertEqual(result, plan)
        self.assertEqual(calls, [160])

    def test_non_gui_cli_import_does_not_require_gi(self):
        code = (
            "import builtins, sys\n"
            "real = builtins.__import__\n"
            "def guarded(name, *args, **kwargs):\n"
            "    if name == 'gi' or name.startswith('gi.'):\n"
            "        raise ImportError('GI intentionally unavailable')\n"
            "    return real(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "import zeus_installer.__main__\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=INSTALLER_ROOT.parent,
            env={"PYTHONPATH": str(INSTALLER_ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ControllerGateTests(unittest.TestCase):
    def test_unavailable_cancellation_is_not_offered_or_reported_as_requested(self):
        controller = gui.InstallerController(backend_module=types.SimpleNamespace())
        self.assertFalse(controller.supports_cancel())
        with self.assertRaisesRegex(InstallerError, "cannot be cancelled"):
            controller.cancel()

    def test_cancellation_requires_an_explicit_acceptance(self):
        for response in (None, {}, {"ok": False}, {"ok": True, "state": "cancel_requested", "supported": False}):
            controller = gui.InstallerController(
                backend_module=types.SimpleNamespace(cancel=lambda: response)
            )
            with self.subTest(response=response), self.assertRaisesRegex(InstallerError, "not accepted"):
                controller.cancel()
        controller = gui.InstallerController(
            backend_module=types.SimpleNamespace(
                cancel=lambda: {"ok": True, "state": "cancel_requested"}
            )
        )
        self.assertTrue(controller.supports_cancel())
        self.assertEqual(controller.cancel()["state"], "cancel_requested")

    def _worker(self):
        return types.SimpleNamespace(
            collect=lambda: {"fixture": True},
            plan=lambda _inventory, allocation_gib: {
                "schema_version": 1,
                "supported": True,
                "blockers": [],
                "inventory": {"fixture": True},
                "proposed_target_layout": {"allocation_gib": allocation_gib},
                "fingerprint": "sha256:" + "b" * 64,
            },
        )

    def test_unqualified_backend_can_prepare_but_cannot_install_or_restart(self):
        backend = types.SimpleNamespace(
            qualification=lambda: {"qualified": False, "status": "preview"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda *_args, **_kwargs: {"ok": True, "phase": "reboot_required"},
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(
            preflight_module=self._worker(), backend_module=backend
        )
        controller.refresh()
        self.assertTrue(controller.can_prepare())
        result = controller.prepare()
        self.assertEqual(result["state"], "ready")
        self.assertTrue(controller.prepared)
        self.assertFalse(controller.can_install())
        with self.assertRaises(gui.BackendUnavailableError):
            controller.request_restart()

    def test_refresh_uses_numeric_privileged_preflight_facade(self):
        calls: list[object] = []

        def collect():
            raise AssertionError("the unprivileged worker must not replace the helper scan")

        worker = types.SimpleNamespace(
            collect=collect,
            plan=lambda *_args, **_kwargs: {},
        )

        def preflight(*, allocation_gib):
            calls.append(allocation_gib)
            return {
                "schema_version": 1,
                "supported": False,
                "blockers": ["fixture_blocked"],
                "proposed_target_layout": {"allocation_gib": allocation_gib},
                "fingerprint": "sha256:" + "c" * 64,
            }

        backend = types.SimpleNamespace(preflight=preflight)
        controller = gui.InstallerController(
            allocation_gib=160,
            preflight_module=worker,
            backend_module=backend,
        )
        plan = controller.refresh()
        self.assertEqual(calls, [160])
        self.assertEqual(plan["target"]["allocation_gib"], 160)
        self.assertEqual(plan["blockers"], ["fixture_blocked"])

    def test_reboot_resume_uses_status_journal_without_new_plan(self):
        calls: list[str] = []

        def collect():
            raise AssertionError("resume must not collect a replacement target plan")

        worker = types.SimpleNamespace(collect=collect, plan=lambda *_args, **_kwargs: {})

        def install_without_plan():
            calls.append("install")
            return {"ok": True, "phase": "installed"}

        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": True,
                "phase": "reboot_required",
                "current_boot_changed": True,
                "fingerprint": "sha256:" + "d" * 64,
                "allocation_gib": 160,
            },
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=install_without_plan,
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(
            allocation_gib=160,
            preflight_module=worker,
            backend_module=backend,
        )
        plan = controller.refresh()
        self.assertTrue(controller.resume_pending)
        self.assertFalse(controller.can_prepare())
        self.assertTrue(controller.can_install())
        self.assertEqual(plan["fingerprint"], "sha256:" + "d" * 64)
        controller.install()
        self.assertEqual(calls, ["install"])
        self.assertFalse(controller.resume_pending)
        self.assertTrue(controller.terminal)

    def test_reboot_boundary_status_enables_restart_before_reboot(self):
        calls: list[str] = []
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": True,
                "phase": "reboot_required",
                "current_boot_changed": False,
                "fingerprint": "sha256:" + "e" * 64,
                "allocation_gib": 128,
            },
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda *_args, **_kwargs: {"ok": True, "phase": "reboot_required"},
            restart=lambda: calls.append("restart") or {"ok": True},
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertFalse(controller.resume_pending)
        self.assertTrue(controller.reboot_ready)
        self.assertFalse(controller.can_install())
        controller.request_restart()
        self.assertEqual(calls, ["restart"])

    def test_failed_resume_proof_keeps_restart_and_continue_disabled(self):
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": True,
                "phase": "reboot_required",
                "current_boot_changed": False,
                "resume_verification": "failed",
                "resume_error": "target_mismatch",
            },
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda *_args, **_kwargs: {"ok": True, "phase": "installed"},
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertTrue(controller.resume_blocked)
        self.assertFalse(controller.reboot_ready)
        self.assertFalse(controller.can_prepare())
        self.assertFalse(controller.can_install())
        with self.assertRaises(gui.BackendUnavailableError):
            controller.request_restart()

    def test_prepared_status_reuses_journal_plan_and_enables_install(self):
        calls: list[str] = []

        def collect():
            raise AssertionError("a prepared journal must not collect a replacement plan")

        original_plan = {
            "schema_version": 1,
            "supported": True,
            "blockers": [],
            "inventory": {"fixture": "vm117"},
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "f" * 64,
            "allocation_gib": 128,
        }
        worker = types.SimpleNamespace(collect=collect, plan=lambda *_args, **_kwargs: {})
        backend = types.SimpleNamespace(
            status=lambda: {"ok": True, "phase": "prepared", "plan": original_plan},
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda plan: calls.append(plan["fingerprint"]) or {"ok": True, "phase": "reboot_required"},
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(
            preflight_module=worker,
            backend_module=backend,
        )
        plan = controller.refresh()
        self.assertEqual(plan["fingerprint"], original_plan["fingerprint"])
        self.assertTrue(controller.prepared)
        self.assertTrue(controller.can_install())
        self.assertFalse(controller.can_prepare())
        controller.install()
        self.assertEqual(calls, [original_plan["fingerprint"]])

    def test_installed_status_offers_manual_os_choice_restart(self):
        calls: list[str] = []
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": True,
                "phase": "installed",
                "plan": {
                    "schema_version": 1,
                    "supported": True,
                    "blockers": [],
                    "inventory": {"fixture": "vm118"},
                    "target": {"allocation_gib": 128},
                    "fingerprint": "sha256:" + "1" * 64,
                },
            },
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda *_args, **_kwargs: {"ok": True, "phase": "installed"},
            restart=lambda: calls.append("restart") or {"ok": True, "phase": "installed"},
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertTrue(controller.terminal)
        self.assertTrue(controller.can_restart())
        self.assertFalse(controller.can_install())
        controller.request_restart()
        self.assertEqual(calls, ["restart"])

    def test_qualified_backend_can_stage_and_only_then_restart(self):
        calls: list[str] = []

        def prepare(plan, progress=None):
            calls.append("prepare")
            self.assertEqual(plan["target"]["allocation_gib"], 128)
            if progress:
                progress({"bytes": 1, "total": 2})
            return {"ok": True, "state": "ready", "prepared": True}

        backend = types.SimpleNamespace(
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=prepare,
            install=lambda _plan: calls.append("install") or {"ok": True, "phase": "reboot_required"},
            restart=lambda: calls.append("restart") or {"ok": True},
        )
        controller = gui.InstallerController(
            preflight_module=self._worker(), backend_module=backend
        )
        controller.refresh()
        self.assertTrue(controller.can_prepare())
        with self.assertRaises(gui.BackendUnavailableError):
            controller.request_restart()
        result = controller.prepare()
        self.assertTrue(result["prepared"])
        self.assertTrue(controller.prepared)
        self.assertTrue(controller.can_install())
        controller.install()
        self.assertTrue(controller.reboot_ready)
        self.assertFalse(controller.can_prepare())
        self.assertFalse(controller.can_install())
        controller.request_restart()
        self.assertEqual(calls, ["prepare", "install", "restart"])

    def test_plan_blockers_prefers_worker_messages(self):
        plan = {
            "supported": False,
            "blockers": ["ac_required", "unknown"],
            "blocker_details": [{"code": "ac_required", "message": "Connect AC power"}],
        }
        self.assertEqual(gui.plan_blockers(plan), ["Connect AC power", "unknown"])

    def test_backup_receipt_errors_are_actionable(self):
        self.assertIn("missing", gui._status_error_message({"error": "backup_receipt_missing"}).lower())
        self.assertIn("invalid", gui._status_error_message({"error": "backup_unverified"}).lower())
        self.assertIn("another target", gui._status_error_message({"error": "backup_target_mismatch"}).lower())


class PackagingContractTests(unittest.TestCase):
    def test_entry_points_are_executable_and_helper_is_fixed(self):
        launcher = INSTALLER_ROOT / "zeus-installer"
        helper = INSTALLER_ROOT / "zeus-installer-helper"
        self.assertTrue(launcher.stat().st_mode & 0o111)
        self.assertTrue(helper.stat().st_mode & 0o111)
        self.assertIn("helper_main", helper.read_text(encoding="utf-8"))
        self.assertIn("/usr/libexec/zeus-installer-helper", (INSTALLER_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_source_package_script_builds_tarball_without_caches(self):
        script = INSTALLER_ROOT.parent / "scripts" / "package-installer.sh"
        test_tmp_root = INSTALLER_ROOT.parent / "out" / "installer" / "tmp"
        test_tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="zeus-installer-test-", dir=test_tmp_root) as temporary:
            output_dir = Path(temporary) / "out"
            temp_dir = output_dir / "tmp"
            temp_dir.mkdir(parents=True)
            result = subprocess.run(
                [str(script)],
                cwd=INSTALLER_ROOT.parent,
                env={
                    **__import__("os").environ,
                    "ZEUS_INSTALLER_OUTPUT_DIR": str(output_dir),
                    "TMPDIR": str(temp_dir),
                    "RPMBUILD_BIN": "/nonexistent/rpmbuild",
                    "ZEUS_INSTALLER_BUILD_ID": "test.gabc123",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            archive = output_dir / "zeus-installer-0.1.0-preview.2.tar.gz"
            self.assertTrue(archive.is_file())
            listing = subprocess.run(
                ["tar", "-tzf", str(archive)], capture_output=True, text=True, check=True
            ).stdout
            self.assertNotIn("__pycache__", listing)
            self.assertNotIn(".pyc", listing)
            self.assertIn("installer/zeus-installer-helper", listing)
            self.assertIn("BUILD-INFO", listing)
            build_info = subprocess.run(
                [
                    "tar",
                    "-xOzf",
                    str(archive),
                    "zeus-installer-0.1.0-preview.2/BUILD-INFO",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("product_version=0.1.0-preview.2", build_info)
            self.assertIn("build_id=test.gabc123", build_info)
            self.assertIn("rpm_release=0.preview.2.test.gabc123", build_info)

            repeat_output = Path(temporary) / "repeat-out"
            repeat_tmp = repeat_output / "tmp"
            repeat_tmp.mkdir(parents=True)
            repeat_result = subprocess.run(
                [str(script)],
                cwd=INSTALLER_ROOT.parent,
                env={
                    **__import__("os").environ,
                    "ZEUS_INSTALLER_OUTPUT_DIR": str(repeat_output),
                    "TMPDIR": str(repeat_tmp),
                    "RPMBUILD_BIN": "/nonexistent/rpmbuild",
                    "ZEUS_INSTALLER_BUILD_ID": "test.gabc123",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(repeat_result.returncode, 0, repeat_result.stderr)
            repeat_archive = repeat_output / "zeus-installer-0.1.0-preview.2.tar.gz"
            self.assertEqual(archive.read_bytes(), repeat_archive.read_bytes())


if __name__ == "__main__":
    unittest.main()
