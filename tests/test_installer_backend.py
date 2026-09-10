"""Focused safety tests for the Fedora installer backend boundaries."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT / "installer")
import sys

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from zeus_installer import artifacts  # noqa: E402
from zeus_installer import backend as installer_backend  # noqa: E402


SOURCE = "0123456789abcdef0123456789abcdef01234567"
VERSION = "0.1.0-preview.2"
BUILD_ID = "git-0123456789ab"
ARCHIVE_NAME = f"zeusos-{VERSION}-{BUILD_ID}.oci"
ARCHIVE_URL = f"https://github.com/KanterLabs/zeusos/releases/download/v{VERSION}/{ARCHIVE_NAME}"


def make_manifest(*, payload: bytes = b"archive", **changes: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "product": "zeusos",
        "channel": "preview",
        "architecture": "amd64",
        "version": VERSION,
        "build_id": BUILD_ID,
        "source_commit": SOURCE,
        "sequence": 42,
        "published_at": "2026-09-09T00:00:00Z",
        "notes": "Release notes",
        "archive": {
            "name": ARCHIVE_NAME,
            "url": ARCHIVE_URL,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_digest": "sha256:" + "b" * 64,
        },
    }
    for key, value in changes.items():
        if key.startswith("archive_"):
            archive = manifest["archive"]
            assert isinstance(archive, dict)
            archive[key.removeprefix("archive_")] = value
        else:
            manifest[key] = value
    return manifest


def plan(fingerprint: str = "target-a") -> dict[str, object]:
    return {
        "schema_version": 1,
        "supported": True,
        "blockers": [],
        "fingerprint": fingerprint,
        "allocation_gib": 128,
        "target": {"fingerprint": fingerprint},
    }


class TempRootMixin:
    def setUp(self) -> None:
        # The shared environment has a deliberately small /tmp quota. Keep
        # installer fixtures under the repository's task-local output area.
        self.temp_parent = ROOT / "out" / "installer" / "tmp"
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_parent)
        self.root = Path(self.temp.name) / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()


class JournalTests(TempRootMixin, unittest.TestCase):
    def test_lock_symlink_fails_closed(self) -> None:
        journal = installer_backend.Journal(self.root, require_root=False)
        journal.ensure_storage()
        target = Path(self.temp.name) / "outside-lock"
        target.write_bytes(b"")
        journal.lock_path.symlink_to(target)
        with self.assertRaises(installer_backend.InstallError) as context:
            with journal.lock():
                pass
        self.assertEqual(context.exception.code, "unsafe_storage")

    def test_corrupt_journal_is_rejected_without_repairing_it(self) -> None:
        journal = installer_backend.Journal(self.root, require_root=False)
        journal.ensure_storage()
        journal.path.write_text("{", encoding="utf-8")
        journal.path.chmod(0o600)
        with self.assertRaises(installer_backend.InstallError) as context:
            journal.load()
        self.assertEqual(context.exception.code, "invalid_state")
        self.assertEqual(journal.path.read_text(encoding="utf-8"), "{")


class BackendTests(TempRootMixin, unittest.TestCase):
    def _backend(
        self,
        *,
        fingerprint: str = "target-a",
        inventory: dict[str, object] | None = None,
        executor: object | None = None,
        resume_verifier: object | None = None,
        download: object | None = None,
        verify: object | None = None,
        runner: object | None = None,
    ) -> installer_backend.InstallerBackend:
        payload = b"archive"
        manifest = make_manifest(payload=payload)

        def default_download(_manifest: object, destination: Path, *, progress: object = None) -> None:
            if callable(progress):
                progress(0, len(payload))
            destination.write_bytes(payload)
            destination.chmod(0o600)
            if callable(progress):
                progress(len(payload), len(payload))

        def default_verify(path: Path, _manifest: object) -> dict[str, object]:
            if path.read_bytes() != payload:
                raise artifacts.ArtifactError("archive_hash_mismatch", "tampered")
            return {
                "name": ARCHIVE_NAME,
                "path": str(path),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "manifest_digest": "sha256:" + "b" * 64,
                "architecture": "amd64",
                "version": VERSION,
                "source_commit": SOURCE,
                "build_id": BUILD_ID,
                "sequence": 42,
            }

        return installer_backend.InstallerBackend(
            self.root,
            require_root=False,
            release_fetch=lambda: manifest,
            artifact_download=download or default_download,
            artifact_verify=verify or default_verify,
            inventory_provider=inventory or {"fingerprint": fingerprint},
            maintenance_executor=executor,
            resume_verifier=resume_verifier,
            runner=runner,
            boot_id=lambda: "boot-a",
        )

    @staticmethod
    def _recovery_plan_and_inventory() -> tuple[dict[str, object], dict[str, object]]:
        table: dict[str, object] = {
            "label": "gpt",
            "id": "table-a",
            "device": "/dev/nvme0n1",
            "sectorsize": 512,
            "partitions": [
                {"node": "/dev/nvme0n1p1", "start": 2048, "size": 2048},
                {"node": "/dev/nvme0n1p2", "start": 4096, "size": 4096},
                {"node": "/dev/nvme0n1p3", "start": 8192, "size": 1048576},
            ],
        }
        candidate = plan()
        candidate["target"] = {
            "fingerprint": "target-a",
            "partition_table": copy.deepcopy(table),
        }
        inventory: dict[str, object] = {
            "fingerprint": "target-a",
            "partition_table": copy.deepcopy(table),
        }
        return candidate, inventory

    def _failed_prewrite_service(self) -> installer_backend.InstallerBackend:
        candidate, inventory = self._recovery_plan_and_inventory()
        service = self._backend(inventory=inventory)
        service.prepare(candidate)
        record = service.journal.load()
        assert record is not None
        record = service.journal.transition(record, installer_backend.PHASE_INSTALLING)
        service.journal.transition(
            record,
            installer_backend.PHASE_ERROR,
            error="backup_receipt_missing",
            message="A verified backup receipt is required before shrinking; no receipt was found.",
        )
        return service

    def test_recover_prewrite_reuses_archive_and_preserves_legacy_history(self) -> None:
        service = self._failed_prewrite_service()
        before = service.journal.load()
        assert before is not None
        before_history = copy.deepcopy(before["history"])
        before_artifact = copy.deepcopy(before["artifact"])
        before_release = copy.deepcopy(before["release"])
        artifact = before_artifact
        assert isinstance(artifact, dict)
        artifact_path = Path(str(artifact["path"]))

        status = service.status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["can_recover_prewrite"])
        recovered = service.recover_prewrite()

        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["state"], installer_backend.PHASE_PREPARED)
        self.assertFalse(recovered["can_recover_prewrite"])
        self.assertEqual(recovered["artifact"], before_artifact)
        self.assertEqual(recovered["release"], before_release)
        self.assertEqual(artifact_path.read_bytes(), b"archive")
        after = service.journal.load()
        assert after is not None
        self.assertEqual(after["history"][:-1], before_history)
        self.assertEqual(after["history"][-1]["phase"], installer_backend.PHASE_PREPARED)
        self.assertEqual(after["artifact"], before_artifact)
        self.assertEqual(after["release"], before_release)
        self.assertIsNone(after["error"])
        self.assertEqual(after["progress"], {"bytes": len(b"archive"), "total": len(b"archive")})
        self.assertIsNone(service._current_record)

    def test_recover_prewrite_rejects_executor_markers_even_when_malformed(self) -> None:
        service = self._failed_prewrite_service()
        record = service.journal.load()
        assert record is not None
        history = copy.deepcopy(record["history"])
        record["executor_state"] = None
        service.journal.write(record)
        status = service.status()
        self.assertFalse(status["can_recover_prewrite"])
        with self.assertRaises(installer_backend.InstallError) as context:
            service.recover_prewrite()
        self.assertEqual(context.exception.code, "recovery_not_allowed")
        unchanged = service.journal.load()
        assert unchanged is not None
        self.assertEqual(unchanged["phase"], installer_backend.PHASE_ERROR)
        self.assertEqual(unchanged["history"], history)

    def test_recover_prewrite_rejects_changed_target_and_tampered_archive(self) -> None:
        candidate, inventory = self._recovery_plan_and_inventory()
        service = self._backend(inventory=inventory)
        service.prepare(candidate)
        record = service.journal.load()
        assert record is not None
        record = service.journal.transition(record, installer_backend.PHASE_INSTALLING)
        service.journal.transition(record, installer_backend.PHASE_ERROR, error="file_missing")
        history = copy.deepcopy(service.journal.load()["history"])
        artifact = service.journal.load()["artifact"]
        assert isinstance(artifact, dict)
        artifact_path = Path(str(artifact["path"]))

        inventory["fingerprint"] = "target-b"
        with self.assertRaises(installer_backend.InstallError) as context:
            service.recover_prewrite()
        self.assertEqual(context.exception.code, "target_mismatch")
        self.assertEqual(service.journal.load()["history"], history)

        inventory["fingerprint"] = "target-a"
        artifact_path.write_bytes(b"tampered")
        with self.assertRaises(installer_backend.InstallError) as context:
            service.recover_prewrite()
        self.assertEqual(context.exception.code, "artifact_tampered")
        self.assertEqual(service.journal.load()["phase"], installer_backend.PHASE_ERROR)
        self.assertEqual(service.journal.load()["history"], history)

    def test_backend_install_forwards_only_explicit_without_backup_choice(self) -> None:
        calls: list[dict[str, object]] = []

        class Executor:
            qualified = True

            def execute(self, **kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(executor=Executor())
        service.prepare(plan())
        result = service.install(plan(), without_backup=True)
        self.assertEqual(result["state"], installer_backend.PHASE_INSTALLED)
        self.assertIs(calls[0]["without_backup"], True)

    def test_module_facade_restricts_without_backup_to_install_actions(self) -> None:
        payload = json.dumps({"ok": True, "state": "installed", "phase": "installed"})
        result = type("Result", (), {"returncode": 0, "stdout": payload})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result) as run:
            installer_backend.install(plan(), without_backup=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "--without-backup")

        with mock.patch.object(installer_backend.subprocess, "run", return_value=result) as run:
            installer_backend.install(plan(), without_backup=False)
        self.assertNotIn("--without-backup", run.call_args.args[0])

        for action in ("status", "prepare", "retry_prepare", "recover_prewrite", "restart"):
            with self.subTest(action=action), self.assertRaises(installer_backend.InstallError) as context:
                installer_backend._invoke_helper(action, without_backup=True)
            self.assertEqual(context.exception.code, "invalid_action")

    def test_finalization_error_requires_explicit_executor_proof(self) -> None:
        for proof in (True, False):
            with self.subTest(proof=proof):
                calls = []
                class Executor:
                    qualified = True
                    def can_resume_finalization(self, record):
                        return record.get("error") == "grub_invalid"
                    def verify_finalization_resume(self, **kwargs):
                        calls.append("verify")
                        return proof
                    def execute(self, **kwargs):
                        calls.append("execute")
                        return {"phase": "installed"}
                service = self._backend(executor=Executor())
                service.prepare(plan())
                record = service.journal.load()
                service.journal.transition(record, installer_backend.PHASE_ERROR, error="grub_invalid")
                self.assertTrue(service.status()["can_resume_finalization"])
                if proof:
                    self.assertEqual(service.install()["phase"], "installed")
                    self.assertEqual(calls, ["verify", "execute"])
                else:
                    with self.assertRaises(installer_backend.InstallError) as context:
                        service.install()
                    self.assertEqual(context.exception.code, "target_mismatch")
                    self.assertEqual(calls, ["verify"])
                # The fixture shares a temporary root across subtests.
                service.journal.path.unlink()
                for artifact in service.journal.artifacts_path.iterdir():
                    artifact.unlink()

    def test_finalization_still_verifies_artifact_before_executor_proof(self) -> None:
        calls = []
        class Executor:
            qualified = True
            def can_resume_finalization(self, record):
                return True
            def verify_finalization_resume(self, **kwargs):
                calls.append("verify")
                return True
            def execute(self, **kwargs):
                calls.append("execute")
                return {"phase": "installed"}
        service = self._backend(executor=Executor())
        service.prepare(plan())
        record = service.journal.load()
        Path(record["artifact"]["path"]).write_bytes(b"corrupt")
        service.journal.transition(record, installer_backend.PHASE_ERROR, error="grub_invalid")
        with self.assertRaises(installer_backend.InstallError) as context:
            service.install()
        self.assertEqual(context.exception.code, "artifact_tampered")
        self.assertEqual(calls, [])

    def test_unqualified_or_unknown_error_cannot_resume_finalization(self) -> None:
        service = self._backend()
        service.prepare(plan())
        record = service.journal.load()
        service.journal.transition(record, installer_backend.PHASE_ERROR, error="grub_invalid")
        self.assertFalse(service.status()["can_resume_finalization"])
        with self.assertRaises(installer_backend.InstallError):
            service.install()
        self.assertEqual(service.journal.load()["phase"], "error")

    def test_prepare_stages_verified_release_and_reports_progress(self) -> None:
        updates: list[dict[str, object]] = []
        service = self._backend()
        result = service.prepare(plan(), progress=updates.append)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], installer_backend.PHASE_PREPARED)
        self.assertEqual(updates[0], {"stage": "waiting_for_operation"})
        byte_updates = [update for update in updates if "bytes" in update]
        self.assertEqual(byte_updates[0], {"bytes": 0, "total": len(b"archive")})
        self.assertEqual(byte_updates[-1], {"bytes": len(b"archive"), "total": len(b"archive")})
        self.assertEqual(
            [update["stage"] for update in updates if "stage" in update],
            [
                "waiting_for_operation",
                "checking_target",
                "checking_release",
                "connecting",
                "downloading",
                "verifying",
            ],
        )
        downloading_index = updates.index({"stage": "downloading"})
        positive_index = next(index for index, update in enumerate(updates) if update.get("bytes") == len(b"archive"))
        self.assertLess(downloading_index, positive_index)
        record = service.journal.load()
        assert record is not None
        self.assertEqual(record["phase"], installer_backend.PHASE_PREPARED)
        artifact = record["artifact"]
        assert isinstance(artifact, dict)
        self.assertTrue(Path(str(artifact["path"])).is_file())

    def test_advisory_stage_callback_failure_preserves_prepared_journal_and_payload(self) -> None:
        service = self._backend()

        def progress(update: dict[str, object]) -> None:
            if "stage" in update:
                raise RuntimeError("UI callback failed")

        result = service.prepare(plan(), progress=progress)
        self.assertEqual(result["state"], installer_backend.PHASE_PREPARED)
        record = service.journal.load()
        assert record is not None
        self.assertEqual(record["phase"], installer_backend.PHASE_PREPARED)
        artifact = record["artifact"]
        assert isinstance(artifact, dict)
        self.assertEqual(Path(str(artifact["path"])).read_bytes(), b"archive")

    def test_stage_events_precede_slow_release_download_and_verify_calls(self) -> None:
        service = self._backend()
        timeline: list[object] = []
        manifest = make_manifest()

        def release_fetch() -> dict[str, object]:
            timeline.append("release_fetch")
            return manifest

        def download(_manifest: object, destination: Path, *, progress: object = None) -> None:
            timeline.append("artifact_download")
            if callable(progress):
                progress(0, len(b"archive"))
            destination.write_bytes(b"archive")
            destination.chmod(0o600)
            if callable(progress):
                progress(len(b"archive"), len(b"archive"))

        def verify(path: Path, _manifest: object) -> dict[str, object]:
            timeline.append("artifact_verify")
            self.assertEqual(path.read_bytes(), b"archive")
            return {
                "name": ARCHIVE_NAME,
                "path": str(path),
                "size": len(b"archive"),
                "sha256": hashlib.sha256(b"archive").hexdigest(),
                "manifest_digest": "sha256:" + "b" * 64,
            }

        service.release_fetch = release_fetch
        service.artifact_download = download
        service.artifact_verify = verify
        service.prepare(plan(), progress=timeline.append)
        self.assertLess(timeline.index({"stage": "checking_release"}), timeline.index("release_fetch"))
        self.assertLess(timeline.index({"stage": "connecting"}), timeline.index("artifact_download"))
        self.assertLess(timeline.index({"stage": "verifying"}), timeline.index("artifact_verify"))

    def test_status_without_plan_uses_original_journal_plan(self) -> None:
        service = self._backend()
        service.prepare(plan())
        result = service.status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["fingerprint"], "target-a")
        self.assertEqual(result["allocation_gib"], 128)
        self.assertEqual(result["plan"]["fingerprint"], "target-a")
        self.assertEqual(result["target"]["fingerprint"], "target-a")

    def test_status_marks_resume_only_after_boot_change_and_executor_proof(self) -> None:
        proof_calls: list[dict[str, object]] = []

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

            def verify_resume(self, **kwargs: object) -> bool:
                proof_calls.append(kwargs)
                return True

        service = self._backend(executor=Executor())
        service.prepare(plan())
        record = service.journal.load()
        assert record is not None
        record["executor_state"] = {
            "stage": 1,
            "phase": installer_backend.PHASE_REBOOT_REQUIRED,
            "status": "complete",
            "old_boot_id": "boot-a",
            "expected_table": {},
        }
        record["executor_result"] = {"phase": installer_backend.PHASE_REBOOT_REQUIRED}
        record["phase"] = installer_backend.PHASE_REBOOT_REQUIRED
        service.journal.write(record)

        # Before reboot, a same-boot status must stay pending and must not
        # allow a verifier to manufacture a changed-boot marker.
        before = service.status()
        self.assertFalse(before["current_boot_changed"])
        self.assertEqual(before["resume_verification"], "pending_reboot")
        self.assertEqual(proof_calls, [])

        service.journal.boot_id = lambda: "boot-b"
        after = service.status()
        self.assertTrue(after["current_boot_changed"])
        self.assertTrue(after["resume_verified"])
        self.assertEqual(after["resume_verification"], "passed")
        self.assertEqual(len(proof_calls), 1)
        self.assertEqual(after["plan"]["fingerprint"], "target-a")
        self.assertEqual(after["target"]["fingerprint"], "target-a")

    def test_target_mismatch_refuses_prepare_before_fetch(self) -> None:
        calls: list[str] = []
        service = self._backend(
            fingerprint="target-a",
            inventory={"fingerprint": "target-b"},
            download=lambda *args, **kwargs: calls.append("download"),
        )
        with self.assertRaises(installer_backend.InstallError) as context:
            service.prepare(plan())
        self.assertEqual(context.exception.code, "target_mismatch")
        self.assertEqual(calls, [])
        self.assertIsNone(service.journal.load())

    def test_interrupted_phase_is_refused_and_reconcile_marks_it(self) -> None:
        service = self._backend()
        record = service.journal.begin(plan())
        record = service.journal.transition(record, installer_backend.PHASE_DOWNLOADING)
        with self.assertRaises(installer_backend.InstallError) as context:
            service.prepare(plan())
        self.assertEqual(context.exception.code, "interrupted")
        self.assertEqual(service.journal.load()["phase"], installer_backend.PHASE_DOWNLOADING)
        reconciled = service.reconcile()
        self.assertEqual(reconciled["state"], installer_backend.PHASE_INTERRUPTED)
        self.assertEqual(service.journal.load()["error"], "interrupted")

    def test_stale_partial_is_not_resumed(self) -> None:
        service = self._backend()
        service.journal.ensure_storage()
        partial = service.journal.artifacts_path / f".{ARCHIVE_NAME}.part"
        partial.write_bytes(b"stale")
        partial.chmod(0o600)
        with self.assertRaises(installer_backend.InstallError) as context:
            service.prepare(plan())
        self.assertEqual(context.exception.code, "interrupted")
        self.assertEqual(service.journal.load()["error"], "interrupted")

    def test_retry_prepare_clears_owned_partial_and_reverifies(self) -> None:
        payload = b"archive"

        def failed_download(_manifest: object, destination: Path, *, progress: object = None) -> None:
            destination.write_bytes(b"truncated")
            destination.chmod(0o600)
            raise artifacts.ArtifactError("download_error", "network failure")

        failed = self._backend(download=failed_download)
        with self.assertRaises(installer_backend.InstallError) as context:
            failed.prepare(plan())
        self.assertEqual(context.exception.code, "download_error")
        partial = failed.journal.artifacts_path / f".{ARCHIVE_NAME}.part"
        self.assertTrue(partial.exists())
        self.assertEqual(failed.journal.load()["phase"], installer_backend.PHASE_ERROR)

        retried = self._backend()
        updates: list[dict[str, object]] = []
        result = retried.retry_prepare(progress=updates.append)
        self.assertEqual(result["state"], installer_backend.PHASE_PREPARED)
        self.assertEqual(updates[:2], [{"stage": "waiting_for_operation"}, {"stage": "checking_target"}])
        self.assertFalse(partial.exists())
        artifact = retried.journal.load()["artifact"]
        assert isinstance(artifact, dict)
        self.assertEqual(Path(str(artifact["path"])).read_bytes(), payload)

    def test_retry_prepare_rejects_executor_history_without_clearing_files(self) -> None:
        service = self._backend()
        service.journal.ensure_storage()
        record = service.journal.begin(plan())
        record = service.journal.transition(record, installer_backend.PHASE_INSTALLING)
        record["executor_state"] = {"phase": "ready"}
        service.journal.write(record)
        record = service.journal.transition(record, installer_backend.PHASE_ERROR, error="executor_failed")
        partial = service.journal.artifacts_path / f".{ARCHIVE_NAME}.part"
        partial.write_bytes(b"must remain")
        partial.chmod(0o600)
        with self.assertRaises(installer_backend.InstallError) as context:
            service.retry_prepare()
        self.assertEqual(context.exception.code, "retry_not_allowed")
        self.assertTrue(partial.exists())

    def test_retry_prepare_rejects_staging_symlink_without_unlinking(self) -> None:
        service = self._backend()

        def failed_download(_manifest: object, destination: Path, *, progress: object = None) -> None:
            raise artifacts.ArtifactError("download_error", "network failure")

        service.artifact_download = failed_download
        with self.assertRaises(installer_backend.InstallError):
            service.prepare(plan())
        partial = service.journal.artifacts_path / f".{ARCHIVE_NAME}.part"
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(b"outside")
        partial.symlink_to(outside)
        with self.assertRaises(installer_backend.InstallError) as context:
            service.retry_prepare()
        self.assertEqual(context.exception.code, "unsafe_storage")
        self.assertTrue(partial.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_corrupted_prepared_artifact_fails_status_and_install(self) -> None:
        service = self._backend()
        service.prepare(plan())
        record = service.journal.load()
        assert record is not None
        artifact = record["artifact"]
        assert isinstance(artifact, dict)
        path = Path(str(artifact["path"]))
        path.write_bytes(b"tampered")
        status = service.status(plan())
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "artifact_tampered")

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(executor=Executor())
        # Reuse the intentionally tampered journal and artifact.
        with self.assertRaises(installer_backend.InstallError) as context:
            service.install(plan())
        self.assertEqual(context.exception.code, "artifact_tampered")
        self.assertEqual(service.journal.load()["phase"], installer_backend.PHASE_ERROR)

    def test_world_readable_prepared_artifact_is_rejected(self) -> None:
        service = self._backend()
        service.prepare(plan())
        record = service.journal.load()
        assert record is not None
        artifact = record["artifact"]
        assert isinstance(artifact, dict)
        path = Path(str(artifact["path"]))
        path.chmod(0o644)
        status = service.status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "artifact_tampered")

    def test_install_without_qualified_executor_fails_closed(self) -> None:
        service = self._backend()
        service.prepare(plan())
        with self.assertRaises(installer_backend.InstallError) as context:
            service.install(plan())
        self.assertEqual(context.exception.code, "executor_unavailable")
        self.assertEqual(service.journal.load()["phase"], installer_backend.PHASE_PREPARED)

    def test_executor_state_and_commands_survive_backend_transitions(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        def runner(*_args: object, **_kwargs: object) -> Result:
            return Result()

        class Executor:
            qualified = True

            def execute(self, *, journal: installer_backend.Journal, runner: object, **_kwargs: object) -> dict[str, object]:
                record = journal.load()
                assert record is not None
                record["executor_state"] = {"stage": 1, "phase": "reboot_required", "status": "complete"}
                journal.write(record)
                assert isinstance(runner, installer_backend.CommandRunner)
                runner.run(["/usr/bin/bootc", "status"])
                return {"phase": installer_backend.PHASE_REBOOT_REQUIRED}

        service = self._backend(executor=Executor(), runner=runner)
        service.prepare(plan())
        result = service.install(plan())
        self.assertEqual(result["state"], installer_backend.PHASE_REBOOT_REQUIRED)
        record = service.journal.load()
        assert record is not None
        self.assertEqual(record["executor_state"]["phase"], "reboot_required")
        self.assertEqual(len(record["commands"]), 1)

    def test_reboot_resume_requires_explicit_expected_change_proof(self) -> None:
        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(executor=Executor())
        service.prepare(plan())
        record = service.journal.load()
        assert record is not None
        record = service.journal.transition(record, installer_backend.PHASE_REBOOT_REQUIRED)
        service.inventory_provider = {"fingerprint": "post-reboot"}
        with self.assertRaises(installer_backend.InstallError) as context:
            service.install(plan())
        self.assertEqual(context.exception.code, "target_mismatch")
        record = service.journal.load()
        assert record is not None
        service.journal.transition(record, installer_backend.PHASE_REBOOT_REQUIRED, error=None)

        proof_calls: list[dict[str, object]] = []

        def proof(**kwargs: object) -> bool:
            proof_calls.append(kwargs)
            return True

        service = self._backend(
            executor=Executor(),
            inventory={"fingerprint": "post-reboot"},
            resume_verifier=proof,
        )
        # Carry the reboot-required journal into the service with the proof.
        result = service.install()
        self.assertEqual(result["state"], installer_backend.PHASE_INSTALLED)
        self.assertEqual(len(proof_calls), 1)

    def test_restart_requires_durable_executor_boundary(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> Result:
            calls.append(argv)
            return Result()

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(runner=runner, executor=Executor())
        record = service.journal.begin(plan())
        service.journal.transition(record, installer_backend.PHASE_REBOOT_REQUIRED)
        with self.assertRaises(installer_backend.InstallError) as context:
            service.restart()
        self.assertEqual(context.exception.code, "reboot_not_ready")
        self.assertEqual(calls, [])

        record = service.journal.load()
        assert record is not None
        record["executor_state"] = {"stage": 1, "phase": "reboot_required", "status": "complete"}
        record["executor_result"] = {"phase": installer_backend.PHASE_REBOOT_REQUIRED}
        service.journal.write(record)
        result = service.restart()
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [["/usr/bin/systemctl", "reboot"]])

    def test_restart_refuses_after_boot_boundary_was_crossed(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> Result:
            calls.append(argv)
            return Result()

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(runner=runner, executor=Executor())
        record = service.journal.begin(plan())
        record["executor_state"] = {
            "stage": 1,
            "phase": installer_backend.PHASE_REBOOT_REQUIRED,
            "status": "complete",
            "old_boot_id": "boot-a",
        }
        record["executor_result"] = {"phase": installer_backend.PHASE_REBOOT_REQUIRED}
        record["phase"] = installer_backend.PHASE_REBOOT_REQUIRED
        service.journal.write(record)
        service.journal.boot_id = lambda: "boot-b"
        with self.assertRaises(installer_backend.InstallError) as context:
            service.restart()
        self.assertEqual(context.exception.code, "reboot_not_ready")
        self.assertEqual(calls, [])

    def test_restart_allows_only_durably_completed_installed_state(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> Result:
            calls.append(argv)
            return Result()

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(runner=runner, executor=Executor())
        record = service.journal.begin(plan())
        record["phase"] = installer_backend.PHASE_INSTALLED
        record["executor_state"] = {
            "stage": 2,
            "phase": installer_backend.PHASE_INSTALLED,
            "status": "complete",
        }
        record["executor_result"] = {"phase": installer_backend.PHASE_INSTALLED}
        service.journal.write(record)

        result = service.restart()
        self.assertTrue(result["ok"])
        self.assertEqual(result["phase"], installer_backend.PHASE_INSTALLED)
        self.assertEqual(result["message"], "Restart Fedora, then reopen Zeus Installer to continue.")
        self.assertEqual(calls, [["/usr/bin/systemctl", "reboot"]])

    def test_restart_installed_state_checks_recorded_full_table_when_available(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> Result:
            calls.append(argv)
            return Result()

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        expected = {"label": "gpt", "partitions": [{"number": 1}, {"number": 2}]}
        service = self._backend(
            runner=runner,
            executor=Executor(),
            inventory={"partition_table": {"label": "gpt", "partitions": [{"number": 1}] }},
        )
        record = service.journal.begin(plan())
        record["phase"] = installer_backend.PHASE_INSTALLED
        record["executor_state"] = {
            "stage": 2,
            "phase": installer_backend.PHASE_INSTALLED,
            "status": "complete",
            "allocated_table": expected,
        }
        record["executor_result"] = {"phase": installer_backend.PHASE_INSTALLED}
        service.journal.write(record)

        with self.assertRaises(installer_backend.InstallError) as context:
            service.restart()
        self.assertEqual(context.exception.code, "target_mismatch")
        self.assertEqual(calls, [])

    def test_restart_rejects_in_progress_or_nonterminal_state(self) -> None:
        class Result:
            returncode = 0
            stdout = ""

        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> Result:
            calls.append(argv)
            return Result()

        class Executor:
            qualified = True

            def execute(self, **_kwargs: object) -> dict[str, object]:
                return {"phase": installer_backend.PHASE_INSTALLED}

        service = self._backend(runner=runner, executor=Executor())
        for phase, state in (
            (installer_backend.PHASE_INSTALLING, None),
            (
                installer_backend.PHASE_INSTALLED,
                {"stage": 2, "phase": installer_backend.PHASE_INSTALLED, "status": "in_progress"},
            ),
        ):
            with self.subTest(phase=phase):
                record = service.journal.begin(plan())
                record["phase"] = phase
                if state is not None:
                    record["executor_state"] = state
                    record["executor_result"] = {"phase": installer_backend.PHASE_INSTALLED}
                service.journal.write(record)
                with self.assertRaises(installer_backend.InstallError) as context:
                    service.restart()
                self.assertEqual(context.exception.code, "reboot_not_ready")
                self.assertEqual(calls, [])


class CommandRunnerTests(unittest.TestCase):
    def test_only_absolute_allowlisted_commands_run_without_shell(self) -> None:
        calls: list[dict[str, object]] = []

        class Result:
            returncode = 0

        def runner(*args: object, **kwargs: object) -> Result:
            calls.append({"args": args, "kwargs": kwargs})
            return Result()

        command = installer_backend.CommandRunner(runner=runner)
        command.run(["/usr/bin/bootc", "status"])
        self.assertEqual(calls[0]["args"][0], ["/usr/bin/bootc", "status"])
        kwargs = calls[0]["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["capture_output"])
        for argv in ("/usr/bin/bootc status", ["/bin/sh", "-c", "touch /tmp/pwned"]):
            with self.subTest(argv=argv), self.assertRaises(installer_backend.InstallError):
                command.run(argv)

    def test_executor_command_paths_are_allowlisted_exactly(self) -> None:
        for executable in (
            "/usr/bin/podman",
            "/usr/sbin/btrfs",
            "/usr/sbin/blkid",
            "/usr/sbin/blockdev",
            "/usr/sbin/partx",
            "/usr/sbin/udevadm",
            "/usr/bin/bootupctl",
            "/usr/sbin/mkfs.fat",
            "/usr/sbin/mkfs.ext4",
            "/usr/sbin/grub2-mkconfig",
            "/usr/bin/systemd-inhibit",
            "/usr/bin/cat",
        ):
            with self.subTest(executable=executable):
                self.assertIn(executable, installer_backend.CommandRunner.ALLOWED_EXECUTABLES)

    def test_nonzero_command_result_fails(self) -> None:
        result = type("Result", (), {"returncode": 7})()
        command = installer_backend.CommandRunner(runner=lambda *args, **kwargs: result)
        with self.assertRaises(installer_backend.InstallError) as context:
            command.run(["/usr/bin/bootc", "status"])
        self.assertEqual(context.exception.code, "command_failed")

    def test_explicit_probe_return_code_is_narrowly_accepted(self) -> None:
        result = type("Result", (), {"returncode": 2})()
        command = installer_backend.CommandRunner(runner=lambda *args, **kwargs: result)
        command.run(
            ["/usr/sbin/blkid", "--probe", "--output", "export", "/dev/vda4"],
            accepted_returncodes=(0, 2),
        )
        with self.assertRaises(installer_backend.InstallError) as context:
            command.run(
                ["/usr/sbin/blkid", "--probe", "--output", "export", "/dev/vda4"],
                accepted_returncodes=(0,),
            )
        self.assertEqual(context.exception.code, "command_failed")

    def test_empty_target_mount_return_code_is_narrowly_accepted(self) -> None:
        result = type("Result", (), {"returncode": 1, "stdout": ""})()
        command = installer_backend.CommandRunner(runner=lambda *args, **kwargs: result)
        command.run(
            [
                "/usr/bin/findmnt",
                "--json",
                "--mountpoint",
                "/target",
                "--output",
                "SOURCE,FSTYPE,UUID,PARTUUID",
            ],
            accepted_returncodes=(0, 1),
        )
        with self.assertRaises(installer_backend.InstallError) as context:
            command.run(
                ["/usr/bin/findmnt", "--json", "--mountpoint", "/mnt"],
                accepted_returncodes=(0, 1),
            )
        self.assertEqual(context.exception.code, "invalid_command")

    def test_return_code_policy_must_be_a_small_integer_tuple(self) -> None:
        command = installer_backend.CommandRunner(
            runner=lambda *args, **kwargs: type("Result", (), {"returncode": 0})()
        )
        for policy in ([], [0, 2], (), (True,), tuple(range(9)), (256,), (0, 2)):
            with self.subTest(policy=policy), self.assertRaises(installer_backend.InstallError) as context:
                command.run(["/usr/bin/bootc", "status"], accepted_returncodes=policy)  # type: ignore[arg-type]
            self.assertEqual(context.exception.code, "invalid_command")

    def test_missing_command_result_fails_closed(self) -> None:
        command = installer_backend.CommandRunner(runner=lambda *args, **kwargs: object())
        with self.assertRaises(installer_backend.InstallError) as context:
            command.run(["/usr/bin/bootc", "status"])
        self.assertEqual(context.exception.code, "command_failed")

    def test_fixed_command_input_is_forwarded_without_recording_payload(self) -> None:
        calls: list[dict[str, object]] = []

        class Result:
            returncode = 0

        def runner(*args: object, **kwargs: object) -> Result:
            calls.append({"args": args, "kwargs": kwargs})
            return Result()

        command = installer_backend.CommandRunner(runner=runner)
        command.run(["/usr/sbin/sfdisk", "--append", "/dev/vda"], input="start=1\n")
        kwargs = calls[0]["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertEqual(kwargs["input"], "start=1\n")
        self.assertNotIn("input", command.commands[0])


class ArtifactPinTests(unittest.TestCase):
    def test_verifier_and_public_signer_are_exact_shared_copies(self) -> None:
        updater = ROOT / "desktop" / "rootfs" / "usr" / "lib" / "zeus" / "update_manifest.py"
        vendored = ROOT / "installer" / "zeus_installer" / "vendor" / "update_manifest.py"
        signers = ROOT / "security" / "allowed_signers"
        bundled = ROOT / "installer" / "zeus_installer" / "data" / "update-allowed-signers"
        self.assertEqual(updater.read_bytes(), vendored.read_bytes())
        self.assertEqual(signers.read_bytes(), bundled.read_bytes())

    def test_current_signed_feed_can_be_verified_with_bundled_policy(self) -> None:
        raw = (ROOT / "updates" / "preview.json").read_bytes()
        signature = (ROOT / "updates" / "preview.json.sig").read_bytes()
        trusted = artifacts._trusted()
        manifest = trusted.verify_manifest(
            raw,
            signature,
            ROOT / "installer" / "zeus_installer" / "data" / "update-allowed-signers",
        )
        self.assertEqual(manifest["build_id"], "git-ad091ecc2978")
        self.assertEqual(artifacts.validate_release(manifest)["sequence"], 1789068697)

    def test_installer_fetch_uses_immutable_feed_and_bounded_metadata(self) -> None:
        trusted = artifacts._trusted()
        raw = (ROOT / "updates" / "preview.json").read_bytes()
        signature = (ROOT / "updates" / "preview.json.sig").read_bytes()
        seen_urls: list[str] = []

        class Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.headers = {"Content-Length": str(len(payload))}

            def read(self, size: int) -> bytes:
                chunk, self.payload = self.payload[:size], self.payload[size:]
                return chunk

            def close(self) -> None:
                return None

        def open_network(url: str, **_kwargs: object) -> Response:
            seen_urls.append(url)
            return Response(signature if url.endswith(".sig") else raw)

        with mock.patch.object(trusted, "_open_network", side_effect=open_network):
            manifest = artifacts.fetch_current_release()

        self.assertEqual(
            trusted.DEFAULT_FEED_URL,
            artifacts._PINNED_FEED_URL,
        )
        self.assertIn("3c9ad01c11739af250745be37e15b07e7dba85cd", trusted.DEFAULT_FEED_URL)
        self.assertEqual(
            seen_urls,
            [artifacts._PINNED_FEED_URL, artifacts._PINNED_FEED_URL + ".sig"],
        )
        self.assertLessEqual(len(raw), trusted.MAX_MANIFEST_BYTES)
        self.assertLessEqual(len(signature), trusted.MAX_SIGNATURE_BYTES)
        self.assertEqual(manifest["build_id"], "git-ad091ecc2978")
        self.assertEqual(
            manifest["archive"]["manifest_digest"],
            "sha256:9b7ea1104c3398007600a502d4d73d6588b87735b85cad810a952aa73c7048e0",
        )
        self.assertEqual(manifest["sequence"], 1789068697)

    def test_archive_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "archive"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(artifacts.ArtifactError) as context:
                artifacts.verify_archive(fifo, make_manifest())
        self.assertEqual(context.exception.code, "archive_invalid")


class HelperCliTests(unittest.TestCase):
    def test_module_facade_preflight_uses_fixed_numeric_helper_command(self) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "state": "preflight",
                "supported": False,
                "blockers": ["root_required"],
                "blocker_details": [{"code": "root_required", "message": "root"}],
            }
        )
        result = type("Result", (), {"returncode": 0, "stdout": payload})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result) as run:
            value = installer_backend.preflight(allocation_gib=160)
        self.assertEqual(value["blockers"], ["root_required"])
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                installer_backend.PKEXEC_COMMAND,
                installer_backend.HELPER_COMMAND,
                "preflight",
                "--allocation-gib",
                "160",
            ],
        )
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["capture_output"])

    def test_module_facade_review_uses_one_fixed_privileged_call(self) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "state": "preflight",
                "phase": "preflight",
                "supported": True,
                "blockers": [],
                "fingerprint": "target-a",
            }
        )
        result = type("Result", (), {"returncode": 0, "stdout": payload})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result) as run:
            value = installer_backend.review(allocation_gib=160)
        self.assertEqual(value["state"], "preflight")
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                installer_backend.PKEXEC_COMMAND,
                installer_backend.HELPER_COMMAND,
                "review",
                "--allocation-gib",
                "160",
            ],
        )
        self.assertEqual(kwargs["timeout"], installer_backend._HELPER_TIMEOUTS["review"])
        self.assertFalse(kwargs["shell"])

    def test_module_facade_prepare_forwards_allocation_and_review_fingerprint(self) -> None:
        payload = json.dumps({"ok": True, "state": "prepared", "prepared": True})
        result = type("Result", (), {"returncode": 0, "stdout": payload})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result) as run:
            value = installer_backend.prepare(plan())
        self.assertTrue(value["ok"])
        args = run.call_args.args[0]
        self.assertEqual(
            args[-4:],
            ["--allocation-gib", "128", "--expected-fingerprint", "target-a"],
        )
        self.assertIn("target-a", args)

    def test_module_facade_streams_progress_before_helper_exits(self) -> None:
        updates: list[dict[str, int]] = []
        callback_times: list[float] = []
        started = time.monotonic()
        script = (
            "import json,time\n"
            "print(json.dumps({'event':'progress','progress':{'bytes':1,'total':2}}), flush=True)\n"
            "time.sleep(0.25)\n"
            "print(json.dumps({'ok':True,'state':'prepared'}), flush=True)\n"
        )
        real_popen = installer_backend.subprocess.Popen

        def launch(argv: list[str], **kwargs: object) -> object:
            self.assertIn("--progress-json", argv)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["env"], dict(installer_backend.CommandRunner.SAFE_ENV))
            return real_popen([sys.executable, "-c", script], **kwargs)

        def progress(update: dict[str, int]) -> None:
            updates.append(update)
            callback_times.append(time.monotonic())

        with mock.patch.object(installer_backend.subprocess, "Popen", side_effect=launch):
            result = installer_backend._invoke_helper(
                "prepare",
                allocation_gib=128,
                expected_fingerprint="target-a",
                progress=progress,
            )
        elapsed_after_callback = time.monotonic() - callback_times[0]
        self.assertTrue(result["ok"])
        self.assertEqual(updates, [{"bytes": 1, "total": 2}])
        # The child sleeps after emitting progress.  A replay-after-exit
        # implementation would leave no measurable gap here.
        self.assertGreaterEqual(elapsed_after_callback, 0.12)
        self.assertGreaterEqual(callback_times[0], started)

    def test_module_facade_stream_timeout_fails_closed(self) -> None:
        script = "import time\ntime.sleep(2)\n"
        real_popen = installer_backend.subprocess.Popen

        def launch(_argv: list[str], **kwargs: object) -> object:
            return real_popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(installer_backend.subprocess, "Popen", side_effect=launch), mock.patch.dict(
            installer_backend._HELPER_TIMEOUTS, {"prepare": 0.1}
        ):
            with self.assertRaises(installer_backend.InstallError) as context:
                installer_backend._invoke_helper(
                    "prepare",
                    allocation_gib=128,
                    expected_fingerprint="target-a",
                    progress=lambda _update: None,
                )
        self.assertEqual(context.exception.code, "helper_timeout")

    def test_module_facade_invalid_stream_fails_closed(self) -> None:
        script = "import time\nprint('not-json', flush=True)\ntime.sleep(2)\n"
        real_popen = installer_backend.subprocess.Popen

        def launch(_argv: list[str], **kwargs: object) -> object:
            return real_popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(installer_backend.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(installer_backend.InstallError) as context:
                installer_backend._invoke_helper(
                    "prepare",
                    allocation_gib=128,
                    expected_fingerprint="target-a",
                    progress=lambda _update: None,
                )
        self.assertEqual(context.exception.code, "helper_invalid_response")

    def test_module_facade_invalid_stage_stream_fails_closed(self) -> None:
        script = "import json\nprint(json.dumps({'event':'stage','stage':'unknown'}), flush=True)\n"
        real_popen = installer_backend.subprocess.Popen

        def launch(_argv: list[str], **kwargs: object) -> object:
            return real_popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(installer_backend.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(installer_backend.InstallError) as context:
                installer_backend._invoke_helper(
                    "prepare",
                    allocation_gib=128,
                    expected_fingerprint="target-a",
                    progress=lambda _update: None,
                )
        self.assertEqual(context.exception.code, "helper_invalid_response")

    def test_module_facade_stream_output_bound_fails_closed(self) -> None:
        script = (
            "import sys\n"
            f"sys.stdout.write('x' * {installer_backend._HELPER_OUTPUT_LIMIT + 1})\n"
            "sys.stdout.flush()\n"
        )
        real_popen = installer_backend.subprocess.Popen

        def launch(_argv: list[str], **kwargs: object) -> object:
            return real_popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(installer_backend.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(installer_backend.InstallError) as context:
                installer_backend._invoke_helper(
                    "prepare",
                    allocation_gib=128,
                    expected_fingerprint="target-a",
                    progress=lambda _update: None,
                )
        self.assertEqual(context.exception.code, "helper_invalid_response")

    def test_root_helper_progress_flag_emits_events_before_final_response(self) -> None:
        calls: list[dict[str, object]] = []

        class Service:
            def prepare(
                self,
                value: dict[str, object],
                *,
                progress: object = None,
            ) -> dict[str, object]:
                calls.append(value)
                assert callable(progress)
                progress({"bytes": 1, "total": 2})
                return {"ok": True, "state": "prepared"}

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", return_value=plan()
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(
                    ["prepare", "--progress-json", "--expected-fingerprint", "target-a"],
                    backend=Service(),
                )
        lines = output.getvalue().splitlines()
        self.assertEqual(status, 0)
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0]), {"event": "stage", "stage": "checking_target"})
        self.assertEqual(json.loads(lines[1])["progress"], {"bytes": 1, "total": 2})
        self.assertTrue(json.loads(lines[2])["ok"])
        self.assertEqual(len(calls), 1)

    def test_root_helper_review_preflights_only_after_idle_status(self) -> None:
        calls: list[str] = []

        class Service:
            def status(self) -> dict[str, object]:
                calls.append("status")
                return {"ok": True, "state": "idle", "phase": "idle", "marker": "status"}

        def root_plan(allocation: int) -> dict[str, object]:
            calls.append(f"preflight:{allocation}")
            return plan(f"target-{allocation}")

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", side_effect=root_plan
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(
                    ["review", "--allocation-gib", "160"], backend=Service()
                )
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["status", "preflight:160"])
        self.assertEqual(result["state"], "preflight")
        self.assertEqual(result["fingerprint"], "target-160")

    def test_root_helper_review_returns_non_idle_status_without_fallback_preflight(self) -> None:
        status_values = (
            {"ok": True, "state": "preparing", "phase": "preparing", "marker": "active"},
            {"ok": True, "state": "prepared", "phase": "prepared", "marker": "ready"},
            {"ok": False, "state": "error", "phase": "error", "marker": "failed"},
        )
        for status_value in status_values:
            with self.subTest(phase=status_value["phase"]):
                class Service:
                    def status(self) -> dict[str, object]:
                        return status_value

                with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
                    installer_backend,
                    "_root_preflight_plan",
                    side_effect=AssertionError("non-idle review must not preflight"),
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = installer_backend.helper_main(["review"], backend=Service())
                self.assertEqual(code, 0 if status_value["ok"] else 1)
                self.assertEqual(json.loads(output.getvalue()), status_value)

    def test_root_helper_long_stream_preserves_late_bytes_and_verifying_stage(self) -> None:
        total = 4000

        class Service:
            def prepare(
                self,
                _value: dict[str, object],
                *,
                progress: object = None,
            ) -> dict[str, object]:
                assert callable(progress)
                for done in range(total + 1):
                    progress({"bytes": done, "total": total})
                progress({"stage": "verifying"})
                return {"ok": True, "state": "prepared"}

        clock = [0.0]

        def monotonic() -> float:
            value = clock[0]
            clock[0] += 1.0
            return value

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", return_value=plan()
        ), mock.patch.object(installer_backend.time, "monotonic", side_effect=monotonic):
            output = io.StringIO()
            with redirect_stdout(output):
                code = installer_backend.helper_main(
                    ["prepare", "--progress-json", "--expected-fingerprint", "target-a"],
                    backend=Service(),
                )
        raw = output.getvalue().encode("utf-8")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        byte_events = [event["progress"] for event in events if event.get("event") == "progress"]
        stages = [event["stage"] for event in events if event.get("event") == "stage"]
        self.assertEqual(code, 0)
        self.assertLess(len(raw), installer_backend._HELPER_OUTPUT_LIMIT)
        self.assertLessEqual(len(raw), installer_backend._HELPER_PROGRESS_LIMIT + 1024)
        self.assertGreater(len(byte_events), 10)
        self.assertEqual(byte_events[-1], {"bytes": total, "total": total})
        self.assertIn("verifying", stages)
        self.assertTrue(events[-1]["ok"])

    def test_root_helper_rejects_progress_flag_for_other_actions(self) -> None:
        output = io.StringIO()
        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), redirect_stdout(output):
            status = installer_backend.helper_main(["status", "--progress-json"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "invalid_action")

    def test_module_facade_preserves_structured_helper_errors(self) -> None:
        payload = json.dumps(
            {
                "ok": False,
                "state": "error",
                "error": "unsupported_plan",
                "message": "The preflight contains blockers.",
                "blockers": ["insufficient_staging_space"],
                "blocker_details": [{"code": "insufficient_staging_space", "message": "staging"}],
            }
        )
        result = type("Result", (), {"returncode": 1, "stdout": payload})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result):
            value = installer_backend._invoke_helper("prepare", allocation_gib=128)
        self.assertEqual(value["error"], "unsupported_plan")
        self.assertEqual(value["blocker_details"][0]["code"], "insufficient_staging_space")

    def test_module_facade_reports_authentication_when_pkexec_returns_without_json(self) -> None:
        result = type("Result", (), {"returncode": 126, "stdout": ""})()
        with mock.patch.object(installer_backend.subprocess, "run", return_value=result):
            with self.assertRaises(installer_backend.InstallError) as context:
                installer_backend.status()
        self.assertEqual(context.exception.code, "authorization_required")

    def test_module_facade_install_and_restart_fail_closed_before_helper(self) -> None:
        with mock.patch.object(installer_backend, "QUALIFIED", False), mock.patch.object(
            installer_backend.subprocess, "run"
        ) as run:
            for function in (installer_backend.install, installer_backend.restart):
                with self.subTest(function=function.__name__), self.assertRaises(installer_backend.InstallError) as context:
                    function()
                self.assertEqual(context.exception.code, "executor_unavailable")
            run.assert_not_called()

    def test_root_preflight_and_prepare_use_numeric_allocation(self) -> None:
        calls: list[dict[str, object]] = []

        class Service:
            def prepare(self, value: dict[str, object]) -> dict[str, object]:
                calls.append(value)
                return {"ok": True, "state": "prepared"}

        def root_plan(allocation: int) -> dict[str, object]:
            value = plan(f"target-{allocation}")
            value["allocation_gib"] = allocation
            return value

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", side_effect=root_plan
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(
                    ["prepare", "--allocation-gib", "160"], backend=Service()
                )
        self.assertEqual(status, 0)
        self.assertEqual(calls[0]["allocation_gib"], 160)
        self.assertEqual(json.loads(output.getvalue())["state"], "prepared")

    def test_root_prepare_rejects_changed_review_fingerprint_before_download(self) -> None:
        calls: list[str] = []

        class Service:
            def prepare(self, _value: object) -> dict[str, object]:
                calls.append("prepare")
                return {"ok": True, "state": "prepared"}

        def root_plan(_allocation: int) -> dict[str, object]:
            return plan("target-root-current")

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", side_effect=root_plan
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(
                    [
                        "prepare",
                        "--allocation-gib",
                        "128",
                        "--expected-fingerprint",
                        "target-review-old",
                    ],
                    backend=Service(),
                )
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "target_mismatch")
        self.assertEqual(result["reviewed_fingerprint"], "target-review-old")
        self.assertEqual(calls, [])

    def test_root_service_factory_pins_executor_and_inventory_provider(self) -> None:
        collect = lambda: {"fingerprint": "target-a"}
        fake_preflight = type("Preflight", (), {"collect": staticmethod(collect)})()
        for qualified in (False, True):
            with self.subTest(qualified=qualified), mock.patch.object(
                installer_backend, "_root_preflight_module", return_value=fake_preflight
            ), mock.patch.object(installer_backend, "QUALIFIED", qualified):
                service = installer_backend.make_service()
                self.assertIs(service.inventory_provider, collect)
                self.assertIsNotNone(service.maintenance_executor)
                executor = service.maintenance_executor
                self.assertIs(getattr(executor, "inventory_provider", None), collect)
                self.assertIs(getattr(executor, "qualified", None), qualified)
                receipt = getattr(executor, "qualification_receipt", None)
                self.assertIsInstance(receipt, dict)
                self.assertEqual(receipt.get("vmid"), 118)
                self.assertEqual(receipt.get("bootupd_version"), "0.2.35")
                self.assertIs(receipt.get("physical"), False)

    def test_prepare_reports_root_preflight_blockers_without_downloading(self) -> None:
        calls: list[str] = []

        class Service:
            def prepare(self, _value: object) -> dict[str, object]:
                calls.append("prepare")
                return {"ok": True, "state": "prepared"}

        blocked = plan("target-blocked")
        blocked["supported"] = False
        blocked["blockers"] = ["insufficient_staging_space"]
        blocked["blocker_details"] = [
            {"code": "insufficient_staging_space", "message": "staging"}
        ]
        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", return_value=blocked
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(["prepare"], backend=Service())
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "unsupported_plan")
        self.assertEqual(result["blocker_details"][0]["code"], "insufficient_staging_space")
        self.assertEqual(calls, [])

    def test_preflight_reports_root_plan_without_accepting_plan_path(self) -> None:
        def root_plan(allocation: int) -> dict[str, object]:
            value = plan(f"target-{allocation}")
            value["allocation_gib"] = allocation
            return value

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0), mock.patch.object(
            installer_backend, "_root_preflight_plan", side_effect=root_plan
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = installer_backend.helper_main(
                    ["preflight", "--allocation-gib", "96"]
                )
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "preflight")
        self.assertEqual(result["allocation_gib"], 96)

        with mock.patch.object(installer_backend.os, "geteuid", return_value=0):
            with self.assertRaises(SystemExit):
                installer_backend.helper_main(["prepare", "/tmp/untrusted-plan.json"])


if __name__ == "__main__":
    unittest.main()
