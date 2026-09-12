"""Focused trust and transaction tests for the installed Developer runtime."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop/rootfs/usr/lib/zeus"))
import developer_manifest as manifest  # noqa: E402
import zeus_developer as developer  # noqa: E402


SOURCE = "desktop/rootfs/usr/libexec/test-window"
TARGET = "/usr/libexec/test-window"
BASE = "git-base000000"


def _tar_file(data: bytes, *, name: str = "usr/libexec/test-window", mode: int = 0o644) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.mode = mode
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def _component_map() -> dict[str, dict[str, object]]:
    return {
        SOURCE: {
            "component": "test-ui",
            "target": TARGET,
            "mode": 0o644,
            "max_size": 1024,
            "activation": ["restart-test"],
        }
    }


def _manifest(extension: bytes, *, level: str = BASE, source: str = SOURCE, payload: bytes = b"fixture") -> dict[str, object]:
    receipt = {
        "schema_version": 1,
        "source_commit": "a" * 40,
        "recorded_at": "2026-09-11T00:00:00Z",
        "results": [
            {
                "name": "tests/test_developer_runtime.py",
                "result": "passed",
                "recorded_at": "2026-09-11T00:00:00Z",
            }
        ],
    }
    receipt_data = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )
    return {
        "schema_version": 1,
        "product": "zeusos",
        "artifact_kind": "systemd-sysext",
        "architecture": "x86_64",
        "repository": "https://github.com/KanterLabs/zeusos.git",
        "branch": "main",
        "source_commit": "a" * 40,
        "base": {"id": "fedora", "version_id": "44", "sysext_level": level, "architecture": "x86_64"},
        "components": [
            {
                "name": "test-ui",
                "activation": "restart-test",
                "files": [
                    {
                        "source": source,
                        "target": TARGET,
                        "type": "regular",
                        "mode": "0644",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ],
        "required_activation": ["restart-test"],
        "focused_tests": ["tests/test_developer_runtime.py"],
        "test_receipt": receipt,
        "test_receipt_sha256": hashlib.sha256(receipt_data).hexdigest(),
        "artifact": {"size": len(extension), "sha256": hashlib.sha256(extension).hexdigest()},
        "created_at": "2026-09-11T00:00:00Z",
        "signature": {"algorithm": "ssh", "namespace": "zeusos-developer", "identity": None},
    }


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zeus-developer-")
        self.root = Path(self.temporary.name)
        self.os_root = self.root / "os"
        (self.os_root / "usr/lib").mkdir(parents=True)
        (self.os_root / "usr/lib/os-release").write_text(
            "ID=fedora\nVERSION_ID=44\nARCHITECTURE=x86_64\nSYSEXT_LEVEL=git-base000000\n"
        )
        self.state = self.root / "state"
        self.artifacts = self.root / "artifacts"
        self.extensions = self.root / "extensions"
        self.spool = self.root / "spool"
        self.spool.mkdir(mode=0o700)
        self.calls: list[list[str]] = []
        self.fail_refresh = False
        self.interrupt_refresh = False
        self.active_extension: Path | None = None

    def close(self) -> None:
        self.temporary.cleanup()

    def runner(self, args: list[str], **_kwargs: object) -> SimpleNamespace:
        self.calls.append(args)
        if args[1:] == ["refresh"]:
            if self.interrupt_refresh:
                self.interrupt_refresh = False
                raise KeyboardInterrupt
            return SimpleNamespace(returncode=int(self.fail_refresh), stdout="", stderr="")
        active = self.active_extension is not None and self.active_extension.exists()
        extensions = [{"name": "zeus-developer", "state": "merged"}] if active else "none"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"extensions": extensions}), stderr="")

    def manager(self) -> developer.DeveloperRuntime:
        manager = developer.DeveloperRuntime(
            state_root=self.state,
            artifact_root=self.artifacts,
            extensions_root=self.extensions,
            updater_lock=self.root / "updater" / "operation.lock",
            os_root=self.os_root,
            spool_root=self.spool,
            component_map=_component_map(),
            signature_verifier=lambda raw, _signature, *_args: json.loads(raw),
            runner=self.runner,
            storage_check=False,
        )
        self.active_extension = manager.extension_path
        return manager

    def put(self, data: bytes = b"fixture", *, level: str = BASE) -> str:
        extension = _tar_file(data)
        digest = hashlib.sha256(extension).hexdigest()
        directory = self.spool / digest
        directory.mkdir(mode=0o700)
        directory.joinpath("extension.raw").write_bytes(extension)
        directory.joinpath("manifest.json").write_text(json.dumps(_manifest(extension, level=level, payload=data), sort_keys=True))
        directory.joinpath("manifest.json.sig").write_bytes(b"fixture-signature")
        return digest


class DeveloperRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def test_manifest_accepts_absolute_usr_target_and_rejects_traversal(self) -> None:
        extension = _tar_file(b"fixture")
        value = manifest.validate_manifest(_manifest(extension), component_map=_component_map())
        self.assertEqual(value["components"][0]["target"], TARGET)
        branch_manifest = _manifest(extension)
        branch_manifest["branch"] = "feature/developer-mode"
        self.assertEqual(
            manifest.validate_manifest(branch_manifest, component_map=_component_map())["source"]["branch"],
            "feature/developer-mode",
        )
        forged = _manifest(extension)
        forged["components"][0]["files"][0]["target"] = "/usr/../etc/passwd"  # type: ignore[index]
        with self.assertRaises(manifest.DeveloperError) as error:
            manifest.validate_manifest(forged, component_map=_component_map())
        self.assertEqual(error.exception.code, "path_unsafe")

    def test_manifest_requires_passing_receipt_for_focused_tests(self) -> None:
        extension = _tar_file(b"fixture")
        missing = _manifest(extension)
        missing.pop("test_receipt")
        missing.pop("test_receipt_sha256")
        with self.assertRaises(manifest.DeveloperError) as error:
            manifest.validate_manifest(missing, component_map=_component_map())
        self.assertEqual(error.exception.code, "test_receipt_missing")

        skipped = _manifest(extension)
        skipped["test_receipt"]["results"][0]["result"] = "not-run"  # type: ignore[index]
        with self.assertRaises(manifest.DeveloperError) as error:
            manifest.validate_manifest(skipped, component_map=_component_map())
        self.assertEqual(error.exception.code, "tests_failed")

    def test_mounted_squashfs_tree_must_exactly_match_signed_files(self) -> None:
        extension = _tar_file(b"fixture")
        value = manifest.validate_manifest(_manifest(extension), component_map=_component_map())
        mounted = self.fixture.root / "mounted"
        target = mounted / TARGET.removeprefix("/")
        target.parent.mkdir(parents=True)
        for directory in (mounted / "usr", mounted / "usr/libexec"):
            directory.chmod(0o755)
        target.write_bytes(b"fixture")
        target.chmod(0o644)

        manifest._inspect_mounted_tree(mounted, value, require_root_owner=False)
        extra = mounted / "usr/libexec/zeus-developer-admin"
        extra.write_bytes(b"unexpected")
        extra.chmod(0o755)
        with self.assertRaises(manifest.DeveloperError) as error:
            manifest._inspect_mounted_tree(mounted, value, require_root_owner=False)
        self.assertEqual(error.exception.code, "component_not_allowed")

    def test_squashfs_dispatches_to_full_tree_inspection_and_unknown_raw_is_rejected(self) -> None:
        extension = b"hsqs" + b"fixture-image"
        value = manifest.validate_manifest(_manifest(extension), component_map=_component_map())
        path = self.fixture.root / "extension.raw"
        path.write_bytes(extension)
        with mock.patch.object(manifest, "_inspect_squashfs_extension") as inspect:
            manifest.verify_artifact(path, value)
        inspect.assert_called_once_with(path, value)

        unknown = b"not-a-supported-extension"
        unknown_value = manifest.validate_manifest(_manifest(unknown), component_map=_component_map())
        path.write_bytes(unknown)
        with self.assertRaises(manifest.DeveloperError) as error:
            manifest.verify_artifact(path, unknown_value)
        self.assertEqual(error.exception.code, "artifact_invalid")

    def test_enable_apply_second_apply_and_undo_are_content_addressed(self) -> None:
        first = self.fixture.put(b"first")
        second = self.fixture.put(b"second")
        manager = self.fixture.manager()
        self.assertEqual(manager.public_status()["state"], "disabled")
        manager.enable(1000)
        manager.apply(first, 1000)
        self.assertEqual(manager.public_status()["active_digest"], first)
        manager.apply(second, 1000)
        self.assertEqual(manager.public_status()["previous_digest"], first)
        manager.undo(1000)
        self.assertEqual(manager.public_status()["active_digest"], first)
        self.assertEqual(manager.extension_path.read_bytes(), _tar_file(b"first"))
        self.assertTrue((manager.artifact_root / first / "extension.raw").is_file())

    def test_reapplying_active_artifact_confirms_merge_without_overwriting_undo_history(self) -> None:
        first = self.fixture.put(b"first")
        second = self.fixture.put(b"second")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(first, 1000)
        manager.apply(second, 1000)
        calls_before = len(self.fixture.calls)
        result = manager.apply(second, 1000)
        self.assertEqual(result["active_digest"], second)
        self.assertEqual(result["previous_digest"], first)
        self.assertGreater(len(self.fixture.calls), calls_before)
        manager.undo(1000)
        self.assertEqual(manager.public_status()["active_digest"], first)

    def test_base_mismatch_fails_before_copy_or_refresh(self) -> None:
        digest = self.fixture.put(level="git-other00000")
        manager = self.fixture.manager()
        manager.enable(1000)
        with self.assertRaises(developer.DeveloperError) as error:
            manager.apply(digest, 1000)
        self.assertEqual(error.exception.code, "base_mismatch")
        self.assertFalse(self.fixture.calls)
        self.assertFalse(manager.extension_path.exists())

    def test_base_version_mismatch_fails_before_copy_or_refresh(self) -> None:
        digest = self.fixture.put()
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.os_root.joinpath("usr/lib/os-release").write_text(
            "ID=fedora\nVERSION_ID=45\nARCHITECTURE=x86_64\nSYSEXT_LEVEL=git-base000000\n"
        )
        with self.assertRaises(developer.DeveloperError) as error:
            manager.apply(digest, 1000)
        self.assertEqual(error.exception.code, "base_mismatch")
        self.assertFalse(self.fixture.calls)
        self.assertFalse(manager.extension_path.exists())

    def test_disable_removes_stale_extension_without_an_active_reference(self) -> None:
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.extension_path.parent.mkdir(parents=True, exist_ok=True)
        manager.extension_path.write_bytes(b"stale extension path")
        result = manager.disable(1000)
        self.assertEqual(result["state"], "disabled")
        self.assertFalse(manager.extension_path.exists())
        self.assertEqual(manager.public_status()["state"], "disabled")

    def test_boot_mismatch_removes_extension_and_publishes_incompatible_state(self) -> None:
        digest = self.fixture.put()
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        manager.os_root.joinpath("usr/lib/os-release").write_text(
            "ID=fedora\nVERSION_ID=44\nARCHITECTURE=x86_64\nSYSEXT_LEVEL=git-newbase\n"
        )
        result = manager.boot_activate()
        self.assertEqual(result["state"], "incompatible")
        self.assertEqual(result["error"], "base_mismatch")
        self.assertFalse(manager.extension_path.exists())
        calls_after_mismatch = len(self.fixture.calls)
        second = manager.boot_activate()
        self.assertEqual(second["state"], "incompatible")
        self.assertEqual(len(self.fixture.calls), calls_after_mismatch)

    def test_enable_reports_a_stored_artifact_mismatch_without_remerging(self) -> None:
        digest = self.fixture.put()
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        manager.pause_for_update()
        manager.os_root.joinpath("usr/lib/os-release").write_text(
            "ID=fedora\nVERSION_ID=45\nARCHITECTURE=x86_64\nSYSEXT_LEVEL=git-newbase\n"
        )
        calls_before = len(self.fixture.calls)
        result = manager.enable(1000)
        self.assertEqual(result["state"], "incompatible")
        self.assertEqual(result["error"], "base_mismatch")
        self.assertEqual(len(self.fixture.calls), calls_before)

    def test_failed_refresh_restores_previous_extension(self) -> None:
        first = self.fixture.put(b"first")
        second = self.fixture.put(b"second")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(first, 1000)
        self.fixture.fail_refresh = True
        with self.assertRaises(developer.DeveloperError) as error:
            manager.apply(second, 1000)
        self.assertEqual(error.exception.code, "sysext_refresh_failed")
        self.assertEqual(manager.public_status()["active_digest"], first)
        self.assertEqual(manager.extension_path.read_bytes(), _tar_file(b"first"))

    def test_failed_disable_restores_active_extension_and_enabled_state(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)

        # Fail the requested unmerge once, then let the rollback refresh
        # succeed.  This models a recoverable systemd-sysext failure and
        # verifies that the journal's prior enabled state is used.
        failed = False

        def fail_once(args: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal failed
            if args[1:] == ["refresh"] and not failed:
                failed = True
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return self.fixture.runner(args, **kwargs)

        manager.runner = fail_once
        with self.assertRaises(developer.DeveloperError) as error:
            manager.disable(1000)
        self.assertEqual(error.exception.code, "sysext_refresh_failed")
        status = manager.public_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["active_digest"], digest)
        self.assertEqual(manager.extension_path.read_bytes(), _tar_file(b"first"))

    def test_interrupted_disable_restores_active_extension_and_enabled_state(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        self.fixture.interrupt_refresh = True
        with self.assertRaises(KeyboardInterrupt):
            manager.disable(1000)
        status = manager.public_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["active_digest"], digest)
        self.assertEqual(manager.extension_path.read_bytes(), _tar_file(b"first"))

    def test_pending_disable_recovery_publishes_restored_active_state(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)

        # A hard-killed helper leaves the disable journal and may leave the
        # extension path absent.  Recovery must use old_enabled, rather than
        # the intended post-disable value in enabled.
        pending = manager._pending_payload(
            None,
            digest,
            None,
            False,
            1000,
            old_enabled=True,
        )
        developer._atomic_json(manager.state_root / developer.PENDING_FILENAME, pending, 0o600)
        manager._remove_active_extension()
        result = manager.recover_interrupted(1000)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["active_digest"], digest)
        self.assertEqual(result["error"], "interrupted")
        self.assertIn("restored", result["message"])
        self.assertFalse((manager.state_root / developer.PENDING_FILENAME).exists())
        self.assertEqual(manager.extension_path.read_bytes(), _tar_file(b"first"))

    def test_interrupted_apply_restores_previous_and_leaves_safe_status(self) -> None:
        first = self.fixture.put(b"first")
        second = self.fixture.put(b"second")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(first, 1000)
        self.fixture.interrupt_refresh = True
        with self.assertRaises(KeyboardInterrupt):
            manager.apply(second, 1000)
        status = manager.public_status()
        self.assertEqual(status["active_digest"], first)
        self.assertEqual(status["state"], "active")
        self.assertNotIn("fixture-signature", json.dumps(status))

    def test_normalized_verifier_does_not_persist_runtime_metadata(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        component_map = manager._component_policy()

        # A verifier adapter may return the normalised form.  Runtime-only
        # fields such as component_names must not turn into signed-manifest
        # fields on the content-addressed receipt.
        manager.signature_verifier = lambda raw, *_args: manifest.validate_manifest(
            json.loads(raw), component_map=component_map
        )
        manager.enable(1000)
        manager.apply(digest, 1000)
        manager.undo(1000)
        stored = json.loads((manager.artifact_root / digest / "manifest.json").read_text())
        self.assertNotIn("component_names", stored)
        self.assertEqual(manager.public_status()["state"], "enabled")

    def test_stored_manifest_requires_its_detached_signature_on_boot(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        stored_path = manager.artifact_root / digest / "manifest.json"
        signed_raw = stored_path.read_bytes()

        def verify_stored(raw: bytes, *_args: object) -> dict[str, object]:
            if raw != signed_raw:
                raise developer.DeveloperError("signature_invalid", "stored manifest signature is invalid")
            return json.loads(raw)

        manager.signature_verifier = verify_stored
        tampered = json.loads(stored_path.read_text())
        tampered["source_commit"] = "b" * 40
        stored_path.write_text(json.dumps(tampered, sort_keys=True))
        with self.assertRaises(developer.DeveloperError) as error:
            manager._stored_manifest(digest)
        self.assertEqual(error.exception.code, "signature_invalid")
        with self.assertRaises(developer.DeveloperError) as error:
            manager.boot_activate()
        self.assertEqual(error.exception.code, "signature_invalid")

    def test_unprivileged_status_uses_public_receipt_without_private_store_access(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        with mock.patch.object(developer.os, "geteuid", return_value=1000), \
             mock.patch.object(manager, "_read_ref", side_effect=AssertionError("private ref read")), \
             mock.patch.object(manager, "_stored_manifest", side_effect=AssertionError("private artifact read")):
            status = manager.public_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["active_digest"], digest)

    def test_spool_rejects_flat_selection_symlink_and_devices(self) -> None:
        digest = "a" * 64
        self.fixture.spool.joinpath("prepared.json").write_text(json.dumps({"digest": digest}))
        manager = self.fixture.manager()
        with self.assertRaises(developer.DeveloperError):
            manager._artifact_paths(1000, digest)
        directory = self.fixture.spool / digest
        directory.mkdir(mode=0o700)
        directory.joinpath("extension.raw").write_bytes(b"x")
        directory.joinpath("manifest.json").write_bytes(b"{}")
        directory.joinpath("manifest.json.sig").write_bytes(b"sig")
        directory.joinpath("unexpected").write_bytes(b"x")
        with self.assertRaises(developer.DeveloperError) as error:
            manager._artifact_paths(1000, digest)
        self.assertEqual(error.exception.code, "unsafe_spool")

    def test_no_argument_apply_uses_only_the_bounded_prepared_digest(self) -> None:
        digest = self.fixture.put(b"prepared")
        self.fixture.spool.joinpath("prepared.json").write_text(
            json.dumps({"schema_version": 1, "digest": digest})
        )
        manager = self.fixture.manager()
        manager.enable(1000)
        result = manager.apply_prepared(1000)
        self.assertEqual(result["active_digest"], digest)

        self.fixture.spool.joinpath("prepared.json").write_text(
            json.dumps({"schema_version": 1, "digest": digest, "extension": "/tmp/other.raw"})
        )
        with self.assertRaises(developer.DeveloperError) as error:
            manager.apply_prepared(1000)
        self.assertEqual(error.exception.code, "prepared_invalid")

    def test_calling_uid_accepts_only_pkexec_identity(self) -> None:
        self.assertEqual(
            developer.resolve_calling_uid(
                {"PKEXEC_UID": "1000"}, effective_uid=lambda: 0, real_uid=lambda: 0, lookup=lambda uid: object()
            ),
            1000,
        )
        with self.assertRaises(developer.DeveloperError):
            developer.resolve_calling_uid(
                {"PKEXEC_UID": "../1000"}, effective_uid=lambda: 0, real_uid=lambda: 0, lookup=lambda uid: object()
            )

    def test_pause_for_update_unmerges_without_destroying_verified_reference(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        result = manager.pause_for_update()
        self.assertEqual(result["state"], "paused")
        self.assertFalse(manager.extension_path.exists())
        self.assertEqual(manager.public_status()["active_digest"], digest)

    def test_final_status_is_durable_before_transaction_journal_is_removed(self) -> None:
        digest = self.fixture.put(b"first")
        manager = self.fixture.manager()
        manager.enable(1000)
        manager.apply(digest, 1000)
        events: list[str] = []
        original_status = manager._write_status
        original_clear = manager._clear_pending

        def record_status(state: str, **kwargs: object) -> dict[str, object]:
            result = original_status(state, **kwargs)
            events.append(f"status:{state}")
            return result

        def record_clear() -> None:
            events.append("clear")
            original_clear()

        manager._write_status = record_status  # type: ignore[method-assign]
        manager._clear_pending = record_clear  # type: ignore[method-assign]
        manager.disable(1000)
        self.assertEqual(events[-2:], ["status:disabled", "clear"])


if __name__ == "__main__":
    unittest.main()
