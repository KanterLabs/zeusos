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
from zeus_installer import launcher, removal  # noqa: E402


class RemovalLauncherTests(unittest.TestCase):
    def _fixture(self):
        from tests.test_installer_removal import fixture

        return fixture()

    def test_safe_review_exposes_data_choice_fedora_retention_and_no_reclaim(self):
        record, inventory = self._fixture()
        controller = launcher.RemovalController()
        plan = controller.review(record, inventory, require_root=False)
        summary = controller.summary()
        self.assertEqual(summary["mode"], removal.MENU_ONLY)
        self.assertEqual(summary["data_policy"], removal.DATA_RETAIN)
        self.assertEqual(summary["data_action"], "preserve")
        self.assertEqual(summary["fedora_preserved"]["default_boot"], "fedora")
        self.assertTrue(summary["fedora_preserved"]["esp"])
        self.assertFalse(summary["automatic_space_reclaim"])
        self.assertFalse(summary["confirmation_required"])
        self.assertEqual(plan["data"]["owner"], "journal")

    def test_destructive_review_requires_exact_confirmation_phrase(self):
        record, inventory = self._fixture()
        record["executor_state"]["vm_tested"] = True
        record["executor_state"]["backup"] = {
            "verified": True,
            "root_trusted": True,
            "backup_target": "/var/lib/zeus/recovery",
        }
        controller = launcher.RemovalController(executor=types.SimpleNamespace(qualified=True))
        controller.review(
            record,
            inventory,
            mode=removal.DESTRUCTIVE,
            data_policy=removal.DATA_DELETE,
            confirm_plan_id=record["operation_id"],
            require_root=False,
        )
        summary = controller.summary()
        self.assertTrue(summary["confirmation_required"])
        self.assertEqual(summary["confirmation_phrase"], record["operation_id"])
        with self.assertRaises(removal.RemovalError) as context:
            controller.apply(record, inventory, confirm_plan_id="wrong", require_root=False)
        self.assertEqual(context.exception.code, "confirmation_required")


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

    def test_progress_copy_distinguishes_stages_and_measured_bytes(self):
        self.assertEqual(gui._progress_stage_text("checking_target"), "Checking Fedora…")
        self.assertEqual(
            gui._progress_stage_text("waiting_for_operation"),
            "Waiting for another installer operation…",
        )
        self.assertEqual(gui._progress_stage_text("verifying"), "Verifying download…")
        self.assertEqual(gui._format_progress_bytes(0), "0 B")
        self.assertEqual(gui._format_progress_bytes(8 * 1024**2), "8.0 MiB")
        self.assertEqual(gui._format_progress_bytes(-1), "unknown size")
        self.assertEqual(gui._format_elapsed(65), "Elapsed: 1m 05s")

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

    def test_refresh_prefers_one_combined_review_call(self):
        calls: list[object] = []

        def review(*, allocation_gib):
            calls.append(allocation_gib)
            return {
                "ok": True,
                "state": "preflight",
                "supported": True,
                "blockers": [],
                "inventory": {"fixture": "review"},
                "target": {"allocation_gib": allocation_gib},
                "fingerprint": "sha256:" + "7" * 64,
            }

        def unexpected(*_args, **_kwargs):
            raise AssertionError("review must replace the legacy calls")

        backend = types.SimpleNamespace(
            review=review,
            status=unexpected,
            preflight=unexpected,
        )
        controller = gui.InstallerController(allocation_gib=160, backend_module=backend)
        plan = controller.refresh()
        self.assertEqual(calls, [160])
        self.assertEqual(plan["fingerprint"], "sha256:" + "7" * 64)
        self.assertEqual(plan["target"]["allocation_gib"], 160)

    def test_bad_combined_review_is_not_replaced_by_legacy_preflight(self):
        def unexpected(*_args, **_kwargs):
            raise AssertionError("a bad review must not fall through")

        backend = types.SimpleNamespace(
            review=lambda *, allocation_gib: {
                "ok": False,
                "state": "preflight",
                "error": "helper_timeout",
                "message": "The privileged installer did not finish safely.",
            },
            status=unexpected,
            preflight=unexpected,
        )
        controller = gui.InstallerController(backend_module=backend)
        with self.assertRaisesRegex(InstallerError, "finish safely"):
            controller.refresh()

    def test_unavailable_combined_review_allows_legacy_source_fallback(self):
        calls: list[str] = []

        class BackendFailure(RuntimeError):
            code = "helper_unavailable"

        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "6" * 64,
        }

        def review(*, allocation_gib):
            calls.append("review")
            raise BackendFailure("helper fixture unavailable")

        def preflight(*, allocation_gib):
            calls.append("preflight")
            return plan

        controller = gui.InstallerController(
            backend_module=types.SimpleNamespace(review=review, preflight=preflight)
        )
        result = controller.refresh()
        self.assertEqual(calls, ["review", "preflight"])
        self.assertEqual(result["fingerprint"], plan["fingerprint"])

    def test_active_combined_review_status_blocks_fresh_plan_collection(self):
        calls: list[str] = []

        def review(*, allocation_gib):
            calls.append("review")
            return {
                "ok": True,
                "state": "downloading",
                "phase": "downloading",
                "progress": {"bytes": 4, "total": 8},
                "allocation_gib": allocation_gib,
                "target": {"allocation_gib": allocation_gib},
                "fingerprint": "sha256:" + "8" * 64,
            }

        backend = types.SimpleNamespace(
            review=review,
            status=lambda: calls.append("status"),
            preflight=lambda **_kwargs: calls.append("preflight"),
        )
        controller = gui.InstallerController(backend_module=backend)
        plan = controller.refresh()
        self.assertEqual(calls, ["review"])
        self.assertTrue(controller.operation_pending)
        self.assertTrue(controller.operation_blocked)
        self.assertEqual(controller.resume_status["progress"], {"bytes": 4, "total": 8})
        self.assertEqual(plan["fingerprint"], "sha256:" + "8" * 64)

    def test_recovery_is_gated_only_by_backend_eligibility(self):
        calls: list[str] = []
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "a" * 64,
        }
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": False,
                "phase": "error",
                "error": "file_missing",
                "message": "The prepared release file is missing.",
                "can_recover_prewrite": False,
                "plan": plan,
            },
            recover_prewrite=lambda: calls.append("recover"),
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertFalse(controller.can_recover_prewrite())
        with self.assertRaisesRegex(InstallerError, "backend-approved"):
            controller.recover_prewrite()
        self.assertEqual(calls, [])

    def test_recovery_uses_saved_plan_without_redundant_refresh_and_preserves_allocation(self):
        calls: list[str] = []
        plan = {
            "schema_version": 1,
            "supported": True,
            "blockers": [],
            "inventory": {"fixture": "saved"},
            "target": {"allocation_gib": 160, "disk": "/dev/vda"},
            "fingerprint": "sha256:" + "b" * 64,
            "allocation_gib": 160,
        }

        def status():
            calls.append("status")
            return {
                "ok": False,
                "phase": "error",
                "error": "file_missing",
                "message": "The prepared release file is missing.",
                "can_recover_prewrite": True,
                "progress": {"bytes": 8, "total": 8},
                "plan": plan,
            }

        def recover_prewrite():
            calls.append("recover")
            return {
                "ok": True,
                "state": "prepared",
                "phase": "prepared",
                "prepared": True,
                "plan": plan,
                "allocation_gib": 160,
            }

        backend = types.SimpleNamespace(status=status, recover_prewrite=recover_prewrite)
        controller = gui.InstallerController(allocation_gib=160, backend_module=backend)
        controller.refresh()
        self.assertTrue(controller.can_recover_prewrite())
        result = controller.recover_prewrite()
        self.assertEqual(calls, ["status", "recover"])
        self.assertIs(result["plan"], plan)
        self.assertTrue(controller.prepared)
        self.assertFalse(controller.can_recover_prewrite())
        self.assertEqual(controller.allocation_gib, 160)
        self.assertEqual(controller.plan["target"]["allocation_gib"], 160)

    def test_recovery_error_preserves_backend_eligibility_and_error_code(self):
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "c" * 64,
        }
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": False,
                "phase": "error",
                "error": "file_missing",
                "message": "The prepared release file is missing.",
                "can_recover_prewrite": True,
                "plan": plan,
            },
            recover_prewrite=lambda: {
                "ok": False,
                "phase": "error",
                "error": "file_missing",
                "message": "The prepared release file is still missing.",
                "can_recover_prewrite": True,
                "plan": plan,
            },
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        with self.assertRaisesRegex(InstallerError, "file_missing"):
            controller.recover_prewrite()
        self.assertTrue(controller.can_recover_prewrite())
        self.assertIn("file_missing", controller.last_error or "")

    def test_install_forwards_without_backup_only_when_selected(self):
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "d" * 64,
        }

        for without_backup in (False, True):
            with self.subTest(without_backup=without_backup):
                calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

                def install(*args, **kwargs):
                    calls.append((args, kwargs))
                    return {"ok": True, "phase": "reboot_required"}

                backend = types.SimpleNamespace(
                    qualification=lambda: {"qualified": True, "status": "qualified"},
                    preflight=lambda **_kwargs: plan,
                    prepare=lambda *_args, **_kwargs: {
                        "ok": True,
                        "state": "ready",
                        "prepared": True,
                    },
                    install=install,
                    restart=lambda: {"ok": True},
                )
                controller = gui.InstallerController(backend_module=backend)
                controller.refresh()
                controller.prepare()
                controller.install(without_backup=without_backup)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], (controller.plan,))
                self.assertEqual(
                    calls[0][1],
                    {"without_backup": True} if without_backup else {},
                )

    def test_prepare_uses_exact_reviewed_plan_without_refreshing(self):
        calls: list[str] = []
        reviewed_fingerprint = "sha256:" + "9" * 64
        reviewed_plan = {
            "ok": True,
            "state": "preflight",
            "supported": True,
            "blockers": [],
            "inventory": {"fixture": "review"},
            "target": {"allocation_gib": 128, "disk": "/dev/nvme0n1"},
            "fingerprint": reviewed_fingerprint,
        }

        def prepare(plan, progress=None):
            calls.append("prepare")
            self.assertIs(plan, controller.plan)
            self.assertEqual(plan["fingerprint"], reviewed_fingerprint)
            self.assertIsNone(progress)
            return {"ok": True, "state": "ready", "prepared": True}

        backend = types.SimpleNamespace(
            review=lambda *, allocation_gib: calls.append("review") or dict(reviewed_plan),
            status=lambda: calls.append("status"),
            preflight=lambda **_kwargs: calls.append("preflight"),
            prepare=prepare,
        )
        controller = gui.InstallerController(allocation_gib=128, backend_module=backend)
        controller.refresh()
        result = controller.prepare(allocation_gib=128)
        self.assertEqual(calls, ["review", "prepare"])
        self.assertTrue(result["prepared"])
        self.assertTrue(controller.prepared)

    def test_prepare_rejects_allocation_that_was_not_reviewed(self):
        calls: list[str] = []
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "a" * 64,
        }
        backend = types.SimpleNamespace(
            preflight=lambda **_kwargs: plan,
            prepare=lambda *_args, **_kwargs: calls.append("prepare"),
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        with self.assertRaisesRegex(InstallerError, "Review the updated allocation first"):
            controller.prepare(allocation_gib=160)
        self.assertEqual(calls, [])

    def test_prepare_preserves_known_backend_transport_errors(self):
        class BackendFailure(RuntimeError):
            def __init__(self, code):
                self.code = code
                super().__init__("backend fixture failure")

        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "b" * 64,
        }
        for code, expected in (
            ("authorization_required", "Administrator approval"),
            ("authorization_denied", "approval was denied"),
            ("auth_canceled", "approval was canceled"),
            ("helper_timeout", "timed out"),
            ("helper_unavailable", "unavailable"),
        ):
            with self.subTest(code=code):
                backend = types.SimpleNamespace(
                    preflight=lambda **_kwargs: plan,
                    prepare=lambda *_args, _code=code, **_kwargs: (_ for _ in ()).throw(
                        BackendFailure(_code)
                    ),
                )
                controller = gui.InstallerController(backend_module=backend)
                controller.refresh()
                with self.assertRaisesRegex(InstallerError, expected):
                    controller.prepare()

        backend = types.SimpleNamespace(
            preflight=lambda **_kwargs: plan,
            prepare=lambda *_args, **_kwargs: (_ for _ in ()).throw(BackendFailure("other")),
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        with self.assertRaisesRegex(InstallerError, "verified Zeus payload"):
            controller.prepare()

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

    def test_finalization_error_flag_enables_only_qualified_continuation(self):
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "g" * 64,
        }
        for qualified, expected in ((True, True), (False, False)):
            with self.subTest(qualified=qualified):
                calls = []

                def install(*args, **kwargs):
                    calls.append((args, kwargs))
                    return {"ok": True, "phase": "installed"}

                backend = types.SimpleNamespace(
                    status=lambda: {
                        "ok": False,
                        "phase": "error",
                        "error": "grub_invalid",
                        "can_resume_finalization": True,
                        "plan": plan,
                    },
                    qualification=lambda: {"qualified": qualified, "status": "qualified"},
                    prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
                    install=install,
                    restart=lambda: {"ok": True},
                )
                controller = gui.InstallerController(backend_module=backend)
                controller.refresh()
                self.assertTrue(controller.resume_pending)
                self.assertEqual(controller.can_install(), expected)
                if expected:
                    controller.install()
                    self.assertEqual(calls, [((), {})])
                else:
                    self.assertEqual(calls, [])

    def test_unrelated_error_status_is_not_resume_eligible(self):
        plan = {
            "supported": True,
            "blockers": [],
            "target": {"allocation_gib": 128},
            "fingerprint": "sha256:" + "h" * 64,
        }
        backend = types.SimpleNamespace(
            status=lambda: {
                "ok": False,
                "phase": "error",
                "error": "executor_failed",
                "can_resume_finalization": False,
                "plan": plan,
            },
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=lambda *_args, **_kwargs: {"ok": True, "phase": "installed"},
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertFalse(controller.resume_pending)
        self.assertFalse(controller.can_install())
        self.assertFalse(controller.can_prepare())
        self.assertTrue(controller.operation_blocked)

    def test_failed_install_clears_old_resume_flags_without_extra_status_call(self):
        calls = []

        def status():
            calls.append("status")
            return {
                "ok": False,
                "phase": "error",
                "error": "grub_conflict",
                "can_resume_finalization": True,
                "plan": {
                    "supported": True,
                    "blockers": [],
                    "target": {"allocation_gib": 128},
                    "fingerprint": "sha256:" + "i" * 64,
                },
            }

        def install():
            calls.append("install")
            raise RuntimeError("fixture executor failure")

        backend = types.SimpleNamespace(
            status=status,
            qualification=lambda: {"qualified": True, "status": "qualified"},
            prepare=lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
            install=install,
            restart=lambda: {"ok": True},
        )
        controller = gui.InstallerController(backend_module=backend)
        controller.refresh()
        self.assertTrue(controller.resume_pending)
        self.assertTrue(controller.can_install())
        with self.assertRaisesRegex(InstallerError, "could not run safely"):
            controller.install()
        self.assertEqual(calls, ["status", "install"])
        self.assertFalse(controller.resume_pending)
        self.assertFalse(controller.prepared)
        self.assertFalse(controller.can_install())
        self.assertFalse(controller.can_prepare())
        self.assertTrue(controller.operation_blocked)

    def test_grub_error_message_includes_backend_path(self):
        message = gui._status_error_message(
            {
                "error": "grub_invalid",
                "message": "The managed entry at /etc/grub.d/40_zeus is invalid.",
            }
        )
        self.assertIn("GRUB", message)
        self.assertIn("/etc/grub.d/40_zeus", message)

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

    def test_backup_missing_error_explains_explicit_choice(self):
        message = gui._status_error_message({"error": "backup_missing"})
        self.assertIn("backup", message.lower())
        self.assertIn("Install without a backup", message)

    def test_file_missing_error_is_conservative_and_preserves_saved_message(self):
        message = gui._status_error_message(
            {
                "error": "file_missing",
                "message": "The required installer file is unavailable.",
                "can_recover_prewrite": True,
            }
        )
        self.assertIn("file_missing", message)
        self.assertIn("required installer file", message)
        self.assertIn("Review and retry", message)
        self.assertIn("The required installer file is unavailable", message)


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
