"""Focused tests for the standalone Fedora-to-existing-Zeus update runner."""

from __future__ import annotations

import fcntl
import hashlib
import json
import stat
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

from zeus_installer import offline_apply  # noqa: E402


def manifest(*, sequence: int = 12, commit: str = "b" * 40, payload: bytes = b"payload") -> dict:
    build = f"git-{commit[:12]}"
    archive_name = f"zeusos-0.1.0-preview.2-{build}.oci"
    return {
        "schema_version": 1,
        "product": "zeusos",
        "channel": "preview",
        "architecture": "amd64",
        "version": "0.1.0-preview.2",
        "build_id": build,
        "source_commit": commit,
        "sequence": sequence,
        "published_at": "2026-09-10T00:00:00Z",
        "notes": "offline fixture",
        "archive": {
            "name": archive_name,
            "url": f"https://github.com/KanterLabs/zeusos/releases/download/v0.1.0-preview.2/{archive_name}",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_digest": "sha256:" + "d" * 64,
        },
    }


class FakeTrusted:
    MAX_MANIFEST_BYTES = 64 * 1024
    MAX_SIGNATURE_BYTES = 64 * 1024

    def __init__(self, candidate: dict):
        self.candidate = candidate
        self.verify_calls: list[tuple[bytes, bytes, Path]] = []
        self.inspect_calls: list[Path] = []
        self.inspect_override: dict | None = None
        self.signature_error: Exception | None = None

    def verify_manifest(self, raw: bytes, signature: bytes, signers: Path) -> dict:
        self.verify_calls.append((raw, signature, Path(signers)))
        if self.signature_error:
            raise self.signature_error
        return self.candidate

    def validate_manifest(self, value: dict) -> dict:
        return value

    def inspect_archive(self, path: Path) -> dict:
        self.inspect_calls.append(Path(path))
        if self.inspect_override is not None:
            return self.inspect_override
        return {
            "architecture": self.candidate["architecture"],
            "version": self.candidate["version"],
            "source_commit": self.candidate["source_commit"],
            "build_id": self.candidate["build_id"],
            "sequence": self.candidate["sequence"],
            "manifest_digest": self.candidate["archive"]["manifest_digest"],
        }


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.offline = self.base / "offline-update"
        self.updater = self.base / "updater"
        self.share = self.base / "share"
        self.offline.mkdir()
        self.updater.mkdir()
        self.share.mkdir()
        self.payload = b"verified OCI fixture"
        self.candidate = manifest(payload=self.payload)
        self.installed = manifest(sequence=11, commit="a" * 40, payload=self.payload)
        self._install_identity(self.installed)
        self._write_payload(self.candidate)
        self.trusted = FakeTrusted(self.candidate)
        self.commands: list[list[str]] = []
        self.bootc_state = {
            "booted": {"image": {"imageDigest": "sha256:" + "a" * 64}},
            "staged": None,
            "rollbackQueued": False,
        }

        def runner(args, **kwargs):
            self.assertEqual(kwargs["env"]["HOME"], "/root")
            self.assertFalse(kwargs.get("shell", False))
            self.commands.append(list(args))
            if list(args) == [offline_apply.BOOTC, "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": self.bootc_state}), stderr="")
            if list(args[:2]) == [offline_apply.BOOTC, "switch"]:
                self.bootc_state["staged"] = {
                    "image": {"imageDigest": self.candidate["archive"]["manifest_digest"]}
                }
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.fail(f"unexpected command: {args}")

        self.runner = runner
        self.applier = offline_apply.OfflineUpdateRunner(
            self.offline,
            self.updater,
            self.share,
            verifier=self.trusted,
            runner=self.runner,
            storage_check=False,
            boot_id="boot-fixture",
            clock=lambda: "2026-09-10T01:02:03Z",
            plymouth=False,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _install_identity(self, value: dict) -> None:
        for filename, key in (
            ("version", "version"),
            ("source-commit", "source_commit"),
            ("build-id", "build_id"),
            ("update-sequence", "sequence"),
        ):
            (self.share / filename).write_text(str(value[key]))

    def _write_payload(self, value: dict) -> None:
        (self.offline / offline_apply.MANIFEST_NAME).write_bytes(json.dumps(value).encode())
        (self.offline / offline_apply.SIGNATURE_NAME).write_bytes(b"signed")
        (self.offline / offline_apply.SIGNERS_NAME).write_text("zeusos-preview ssh-ed25519 fixture\n")
        (self.offline / offline_apply.PAYLOAD_NAME).write_bytes(self.payload)

    def status(self, root: Path | None = None) -> dict:
        path = (root or self.updater) / offline_apply.STATUS_NAME
        return json.loads(path.read_text())

    def test_verified_new_payload_stages_fixed_path_and_reports_ready(self) -> None:
        self.assertTrue(self.applier.run())
        switch = [command for command in self.commands if command[:2] == [offline_apply.BOOTC, "switch"]]
        self.assertEqual(len(switch), 1)
        self.assertEqual(
            switch[0],
            [offline_apply.BOOTC, "switch", "--transport", "oci-archive", "--retain", str(self.offline / self.candidate["archive"]["name"])],
        )
        self.assertEqual(self.status()["phase"], "ready")
        self.assertIn("Restart", self.status()["message"])
        self.assertEqual(self.status(self.offline)["phase"], "ready")
        self.assertNotIn("reboot", " ".join(" ".join(command) for command in self.commands))
        self.assertEqual(self.trusted.inspect_calls, [self.offline / "payload.oci"])

    def test_same_build_is_idempotent_without_rehash_or_bootc(self) -> None:
        self.candidate = manifest(sequence=12, commit="b" * 40, payload=self.payload)
        self.trusted.candidate = self.candidate
        self._install_identity(self.candidate)
        self.assertTrue(self.applier.run())
        self.assertEqual(self.status()["phase"], "done")
        self.assertEqual(self.trusted.inspect_calls, [])
        self.assertEqual(self.commands, [])

    def test_older_build_is_rejected_before_bootc(self) -> None:
        self.candidate = manifest(sequence=10, commit="c" * 40, payload=self.payload)
        self.trusted.candidate = self.candidate
        self._write_payload(self.candidate)
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["phase"], "error")
        self.assertEqual(self.status()["error"], "not_newer")
        self.assertEqual(self.commands, [])

    def test_different_staged_deployment_is_preserved(self) -> None:
        self.bootc_state["staged"] = {"image": {"imageDigest": "sha256:" + "e" * 64}}
        before = json.loads(json.dumps(self.bootc_state))
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["error"], "staged_update_exists")
        self.assertEqual(self.bootc_state, before)
        self.assertFalse(any(command[:2] == [offline_apply.BOOTC, "switch"] for command in self.commands))

    def test_archive_hash_and_identity_are_verified(self) -> None:
        (self.offline / offline_apply.PAYLOAD_NAME).write_bytes(b"tampered")
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["error"], "archive_hash_mismatch")
        self.assertFalse(any(command[:2] == [offline_apply.BOOTC, "switch"] for command in self.commands))

        (self.offline / offline_apply.PAYLOAD_NAME).write_bytes(self.payload)
        self.trusted.inspect_override = {**self.trusted.inspect_archive(self.offline / "payload.oci"), "build_id": "git-" + "e" * 12}
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["error"], "wrong_image")

    def test_signature_failure_and_symlink_fail_closed(self) -> None:
        self.trusted.signature_error = offline_apply.OfflineApplyError("signature_invalid")
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["error"], "signature_invalid")

        self.trusted.signature_error = None
        (self.offline / offline_apply.SIGNATURE_NAME).unlink()
        (self.offline / offline_apply.SIGNATURE_NAME).symlink_to(self.share / "version")
        self.assertFalse(self.applier.run())
        self.assertEqual(self.status()["error"], "unsafe_storage")

    def test_success_consumes_pending_activation_marker(self) -> None:
        pending = self.offline / offline_apply.PENDING_NAME
        pending.write_text("apply once")
        self.assertTrue(self.applier.run())
        self.assertFalse(pending.exists())

    def test_shared_lock_rejects_concurrent_runner(self) -> None:
        descriptor = open(self.updater / offline_apply.LOCK_NAME, "a+b")
        self.addCleanup(descriptor.close)
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
        try:
            self.assertFalse(self.applier.run())
            self.assertEqual(self.applier.last_state["error"], "busy")
            self.assertEqual(self.commands, [])
        finally:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)

    def test_full_identity_read_accepts_legacy_root_group_mode(self) -> None:
        self.applier.storage_check = True
        legacy = SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=0)
        with patch.object(Path, 'lstat', return_value=legacy), \
             patch.object(self.applier, '_read_bytes', side_effect=lambda path, **kwargs: path.read_bytes()):
            identity = self.applier._current_identity()
        self.assertEqual(identity['build_id'], 'git-aaaaaaaaaaaa')

    def test_legacy_image_directory_is_not_confused_with_update_storage(self) -> None:
        self.applier.storage_check = True
        legacy = SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=0)
        with patch.object(Path, 'lstat', return_value=legacy):
            self.applier._check_image_share()
        for mode, group in ((0o777, 0), (0o775, 1000)):
            metadata = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=0, st_gid=group)
            with patch.object(Path, 'lstat', return_value=metadata):
                with self.assertRaises(offline_apply.OfflineApplyError):
                    self.applier._check_image_share()
        writable_file = SimpleNamespace(st_mode=stat.S_IFREG | 0o664, st_uid=0, st_gid=0, st_size=1)
        with patch.object(offline_apply.os, 'lstat', return_value=writable_file):
            with self.assertRaises(offline_apply.OfflineApplyError):
                self.applier._check_regular(self.share / 'version', label='installed identity')


if __name__ == "__main__":
    unittest.main()
