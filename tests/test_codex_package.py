"""Offline tests for the locked standalone Codex package installer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-codex.py"
SPEC = importlib.util.spec_from_file_location("install_codex", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


BASE_FILES = {
    "bin/codex": b"#!/bin/sh\necho codex\n",
    "codex-package.json": b'{"layoutVersion":1,"version":"fixture"}\n',
    "codex-path/rg": b"rg fixture\n",
    "codex-resources/zsh/bin/zsh": b"#!/bin/sh\necho zsh\n",
}
BASE_DIRECTORIES = {
    "bin",
    "codex-path",
    "codex-resources",
    "codex-resources/zsh",
    "codex-resources/zsh/bin",
}


def add_directory(archive: tarfile.TarFile, name: str, mode: int = 0o755) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = mode
    archive.addfile(member)


def add_file(
    archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o755
) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    member.mode = mode
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def make_archive(path: Path, extra: list[tuple] | None = None) -> None:
    """Write a small tar.gz with the same directory shape as the real package."""

    with tarfile.open(path, "w:gz") as archive:
        for directory in sorted(BASE_DIRECTORIES, key=lambda value: (value.count("/"), value)):
            add_directory(archive, directory)
        for name, data in BASE_FILES.items():
            add_file(archive, name, data, 0o644 if name == "codex-package.json" else 0o755)
        for entry in extra or []:
            kind = entry[0]
            if kind == "file":
                _, name, data = entry
                add_file(archive, name, data)
            elif kind == "symlink":
                _, name, target = entry
                member = tarfile.TarInfo(name)
                member.type = tarfile.SYMTYPE
                member.mode = 0o755
                member.linkname = target
                archive.addfile(member)
            elif kind == "hardlink":
                _, name, target = entry
                member = tarfile.TarInfo(name)
                member.type = tarfile.LNKTYPE
                member.mode = 0o755
                member.linkname = target
                archive.addfile(member)
            elif kind == "fifo":
                _, name = entry
                member = tarfile.TarInfo(name)
                member.type = tarfile.FIFOTYPE
                member.mode = 0o644
                archive.addfile(member)
            elif kind == "traversal":
                _, name, data = entry
                add_file(archive, name, data)
            elif kind == "duplicate":
                _, name, data = entry
                add_file(archive, name, data)
            else:
                raise AssertionError(f"unknown fixture entry kind: {kind}")


def make_lock(
    archive_path: Path,
    *,
    archive_bytes: bytes | None = None,
    expected_files: dict[str, bytes] = BASE_FILES,
    expected_directories: set[str] = BASE_DIRECTORIES,
) -> dict:
    raw = archive_path.read_bytes() if archive_bytes is None else archive_bytes
    entries = []
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        for path in sorted(expected_directories):
            entries.append({"path": path, "mode": members[path].mode})
        for path in sorted(expected_files):
            member = members[path]
            entries.append(
                {
                    "path": path,
                    "size": len(expected_files[path]),
                    "sha256": hashlib.sha256(expected_files[path]).hexdigest(),
                    "mode": member.mode,
                }
            )
    return {
        "schema_version": 1,
        "product": "codex",
        "version": "fixture",
        "release_tag": "fixture-tag",
        "target": "fixture-target",
        "url": "https://example.invalid/codex-package.tar.gz",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "directories": entries[: len(expected_directories)],
        "files": entries[len(expected_directories) :],
    }


def write_lock(directory: Path, lock: dict) -> Path:
    path = directory / "codex.lock.json"
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return path


class CodexPackageInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fixture(self, *, extra: list[tuple] | None = None) -> tuple[Path, Path, dict]:
        archive = self.root / "fixture.tar.gz"
        make_archive(archive, extra=extra)
        lock = make_lock(archive)
        lock_path = write_lock(self.root, lock)
        return archive, lock_path, lock

    def test_installs_complete_layout_and_records_receipt(self):
        archive, lock_path, lock = self.fixture()
        destination = self.root / "codex"

        result = installer.install(lock_path, destination, archive)

        self.assertEqual(result, destination)
        for path, data in BASE_FILES.items():
            installed = destination.joinpath(*path.split("/"))
            self.assertEqual(installed.read_bytes(), data)
            expected_mode = 0o644 if path == "codex-package.json" else 0o755
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), expected_mode)
        self.assertEqual(
            stat.S_IMODE((destination / "codex-resources/zsh/bin").stat().st_mode), 0o755
        )

        receipt_path = destination / installer.RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["source"]["kind"], "local")
        self.assertEqual(receipt["source"]["url"], lock["url"])
        self.assertEqual(receipt["source"]["sha256"], lock["sha256"])
        self.assertEqual(receipt["source"]["size"], lock["size"])
        files = {entry["path"]: entry for entry in receipt["files"]}
        self.assertEqual(set(files), set(BASE_FILES))
        self.assertEqual(files["bin/codex"]["size"], len(BASE_FILES["bin/codex"]))
        self.assertEqual(
            files["bin/codex"]["sha256"],
            hashlib.sha256(BASE_FILES["bin/codex"]).hexdigest(),
        )
        self.assertTrue(files["bin/codex"]["executable"])
        self.assertFalse(files["codex-package.json"]["executable"])
        self.assertFalse((destination / "node").exists())

        with self.assertRaisesRegex(installer.InstallError, "destination must be absent"):
            installer.install(lock_path, destination, archive)

    def test_archive_size_and_digest_are_checked_before_extraction(self):
        archive, lock_path, lock = self.fixture()
        original = archive.read_bytes()

        truncated = self.root / "truncated.tar.gz"
        truncated.write_bytes(original[:-1])
        destination = self.root / "truncated-install"
        with self.assertRaisesRegex(installer.InstallError, "archive size mismatch"):
            installer.install(lock_path, destination, truncated)
        self.assertFalse(destination.exists())

        changed = self.root / "changed.tar.gz"
        changed_bytes = bytearray(original)
        changed_bytes[len(changed_bytes) // 2] ^= 1
        changed.write_bytes(changed_bytes)
        destination = self.root / "changed-install"
        with self.assertRaisesRegex(installer.InstallError, "archive sha256 mismatch"):
            installer.install(lock_path, destination, changed)
        self.assertFalse(destination.exists())

        wrong_lock = dict(lock)
        wrong_lock["sha256"] = "0" * 64
        wrong_lock_path = write_lock(self.root, wrong_lock)
        destination = self.root / "wrong-lock-install"
        with self.assertRaisesRegex(installer.InstallError, "archive sha256 mismatch"):
            installer.install(wrong_lock_path, destination, archive)
        self.assertFalse(destination.exists())

    def test_malformed_tar_is_rejected_without_creating_destination(self):
        archive, _, lock = self.fixture()
        corrupt = self.root / "corrupt.tar.gz"
        corrupt_bytes = b"this is not a gzip tar archive"
        corrupt.write_bytes(corrupt_bytes)
        lock = dict(lock)
        lock["size"] = len(corrupt_bytes)
        lock["sha256"] = hashlib.sha256(corrupt_bytes).hexdigest()
        lock_path = write_lock(self.root, lock)
        destination = self.root / "corrupt-install"

        with self.assertRaisesRegex(installer.InstallError, "cannot read Codex archive"):
            installer.install(lock_path, destination, corrupt)
        self.assertFalse(destination.exists())

    def test_unsafe_members_and_unknown_layout_are_rejected(self):
        cases = {
            "traversal": [("traversal", "../outside", b"escape")],
            "symlink": [("symlink", "unexpected", "bin/codex")],
            "hardlink": [("hardlink", "unexpected", "bin/codex")],
            "fifo": [("fifo", "unexpected")],
            "unknown": [("file", "unexpected", b"extra")],
            "duplicate": [("duplicate", "bin/codex", BASE_FILES["bin/codex"])],
        }
        for name, extra in cases.items():
            with self.subTest(member=name):
                archive, _, lock = self.fixture(extra=extra)
                lock_path = write_lock(self.root, lock)
                destination = self.root / f"unsafe-{name}"
                with self.assertRaises(installer.InstallError):
                    installer.install(lock_path, destination, archive)
                self.assertFalse(destination.exists())

    def test_destination_symlink_or_existing_directory_is_rejected(self):
        archive, lock_path, _ = self.fixture()
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "keep"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "destination must be absent"):
            installer.install(lock_path, existing, archive)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

        target = self.root / "target"
        target.mkdir()
        symlink = self.root / "symlink"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "destination must be absent"):
            installer.install(lock_path, symlink, archive)

    def test_command_line_local_archive_mode_is_offline(self):
        archive, lock_path, _ = self.fixture()
        destination = self.root / "cli-codex"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--lock",
                str(lock_path),
                "--destination",
                str(destination),
                "--archive",
                str(archive),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed Codex fixture", result.stdout)
        self.assertTrue((destination / installer.RECEIPT_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
