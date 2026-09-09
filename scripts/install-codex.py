#!/usr/bin/env python3
"""Install a locked, standalone Codex release into a fresh directory.

The installer deliberately has no product-specific runtime setup.  It verifies
the complete archive and its expected members before extracting anything, then
renames a private staging directory into the requested destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


RECEIPT_NAME = "zeus-package-receipt.json"
CHUNK_SIZE = 1024 * 1024
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class InstallError(RuntimeError):
    """Raised when the locked artifact cannot be safely installed."""


def _relative_path(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise InstallError(f"{description} must be a non-empty relative path")
    if "\x00" in value or "\\" in value:
        raise InstallError(f"{description} contains an unsafe character: {value!r}")
    if value.startswith("/") or value.endswith("/"):
        raise InstallError(f"{description} must be relative and canonical: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InstallError(f"{description} contains an unsafe component: {value!r}")
    if posixpath.normpath(value) != value:
        raise InstallError(f"{description} is not canonical: {value!r}")
    return value


def _digest(value: object, description: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise InstallError(f"{description} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InstallError(f"{description} must be an integer >= {minimum}")
    return value


def _mode(value: object, description: str) -> int:
    mode = _integer(value, description)
    if mode & ~0o7777:
        raise InstallError(f"{description} contains unsupported permission bits")
    return mode


def _validate_url(value: object) -> str:
    if not isinstance(value, str):
        raise InstallError("lock url must be an HTTPS URL")
    parsed = urlparse.urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InstallError("lock url must be an HTTPS URL without credentials")
    return value


def _validate_lock(lock: object) -> dict:
    if not isinstance(lock, dict):
        raise InstallError("lock file must contain a JSON object")
    if lock.get("schema_version") != 1:
        raise InstallError("unsupported Codex lock schema")
    if lock.get("product") != "codex":
        raise InstallError("lock product must be codex")

    version = lock.get("version")
    release_tag = lock.get("release_tag")
    target = lock.get("target")
    for name, value in (
        ("version", version),
        ("release_tag", release_tag),
        ("target", target),
    ):
        if not isinstance(value, str) or not value:
            raise InstallError(f"lock {name} must be a non-empty string")
    url = _validate_url(lock.get("url"))
    archive_size = _integer(lock.get("size"), "lock size", minimum=1)
    archive_sha256 = _digest(lock.get("sha256"), "lock sha256")

    directories_raw = lock.get("directories")
    files_raw = lock.get("files")
    if not isinstance(directories_raw, list) or not directories_raw:
        raise InstallError("lock directories must be a non-empty list")
    if not isinstance(files_raw, list) or not files_raw:
        raise InstallError("lock files must be a non-empty list")

    directories: dict[str, dict] = {}
    for index, entry in enumerate(directories_raw):
        if not isinstance(entry, dict):
            raise InstallError(f"lock directories[{index}] must be an object")
        path = _relative_path(entry.get("path"), f"lock directories[{index}].path")
        if path in directories:
            raise InstallError(f"lock contains duplicate directory: {path}")
        directories[path] = {
            "path": path,
            "mode": _mode(entry.get("mode"), f"lock directories[{index}].mode"),
        }

    files: dict[str, dict] = {}
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            raise InstallError(f"lock files[{index}] must be an object")
        path = _relative_path(entry.get("path"), f"lock files[{index}].path")
        if path in files:
            raise InstallError(f"lock contains duplicate file: {path}")
        files[path] = {
            "path": path,
            "size": _integer(entry.get("size"), f"lock files[{index}].size"),
            "sha256": _digest(
                entry.get("sha256"), f"lock files[{index}].sha256"
            ),
            "mode": _mode(entry.get("mode"), f"lock files[{index}].mode"),
        }

    overlap = sorted(set(directories) & set(files))
    if overlap:
        raise InstallError(f"lock path is both a file and directory: {overlap[0]}")
    all_paths = set(directories) | set(files)
    for path in files:
        components = path.split("/")
        for end in range(1, len(components)):
            parent = "/".join(components[:end])
            if parent not in directories:
                raise InstallError(f"lock is missing parent directory for {path}: {parent}")
    for path in directories:
        components = path.split("/")
        for end in range(1, len(components)):
            parent = "/".join(components[:end])
            if parent not in directories:
                raise InstallError(
                    f"lock is missing parent directory for {path}: {parent}"
                )
    for path in all_paths:
        components = path.split("/")
        for end in range(1, len(components)):
            if "/".join(components[:end]) in files:
                raise InstallError(f"lock has a file path prefix conflict: {path}")

    # Keep the public lock shape intact, while using canonical maps internally.
    return {
        "schema_version": 1,
        "product": "codex",
        "version": version,
        "release_tag": release_tag,
        "target": target,
        "url": url,
        "sha256": archive_sha256,
        "size": archive_size,
        "directories": [directories[path] for path in sorted(directories)],
        "files": [files[path] for path in sorted(files)],
        "_directory_map": directories,
        "_file_map": files,
    }


def load_lock(path: str | os.PathLike[str]) -> dict:
    """Read and validate a Codex lock file."""

    lock_path = Path(path)
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise InstallError(f"lock path is not a regular file: {lock_path}")
        with lock_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except InstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read lock file {lock_path}: {exc}") from exc
    return _validate_lock(raw)


def _open_archive(path: Path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise InstallError(f"cannot open archive {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallError(f"archive path is not a regular file: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _verify_archive_bytes(handle, lock: dict, archive_path: Path) -> None:
    expected_size = lock["size"]
    expected_digest = lock["sha256"]
    handle.seek(0)
    metadata = os.fstat(handle.fileno())
    if metadata.st_size != expected_size:
        raise InstallError(
            f"archive size mismatch for {archive_path}: "
            f"expected {expected_size}, got {metadata.st_size}"
        )
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
    actual = digest.hexdigest()
    if total != expected_size or actual != expected_digest:
        raise InstallError(
            f"archive sha256 mismatch for {archive_path}: "
            f"expected {expected_digest}, got {actual}"
        )


def _member_path(member: tarfile.TarInfo) -> str:
    return _relative_path(member.name, "archive member name")


def _check_mode(actual: int, expected: int, path: str) -> None:
    if actual != expected:
        raise InstallError(
            f"archive mode mismatch for {path}: expected {expected:o}, got {actual:o}"
        )


def _hash_member(handle, member: tarfile.TarInfo, expected: dict) -> str:
    try:
        source = handle.extractfile(member)
        if source is None:
            raise InstallError(f"archive file has no readable payload: {member.name}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    except InstallError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"cannot read archive member {member.name}: {exc}") from exc
    if total != expected["size"]:
        raise InstallError(
            f"archive member size mismatch for {member.name}: "
            f"expected {expected['size']}, got {total}"
        )
    return digest.hexdigest()


def _validate_tar(handle, lock: dict) -> list[tarfile.TarInfo]:
    expected_directories = lock["_directory_map"]
    expected_files = lock["_file_map"]
    expected_paths = set(expected_directories) | set(expected_files)
    members: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=handle, mode="r:gz") as archive:
            for member in archive.getmembers():
                path = _member_path(member)
                if path in seen:
                    raise InstallError(f"archive contains duplicate member: {path}")
                seen.add(path)
                if member.issym():
                    raise InstallError(f"archive contains symlink: {path}")
                if member.islnk():
                    raise InstallError(f"archive contains hardlink: {path}")
                if member.isdir():
                    expected = expected_directories.get(path)
                    if expected is None:
                        raise InstallError(f"archive contains unknown directory: {path}")
                    if member.size != 0:
                        raise InstallError(f"archive directory has a payload: {path}")
                    _check_mode(member.mode, expected["mode"], path)
                elif member.isreg():
                    expected = expected_files.get(path)
                    if expected is None:
                        raise InstallError(f"archive contains unknown file: {path}")
                    if member.size != expected["size"]:
                        raise InstallError(
                            f"archive member size mismatch for {path}: "
                            f"expected {expected['size']}, got {member.size}"
                        )
                    _check_mode(member.mode, expected["mode"], path)
                    actual = _hash_member(archive, member, expected)
                    if actual != expected["sha256"]:
                        raise InstallError(
                            f"archive member sha256 mismatch for {path}: "
                            f"expected {expected['sha256']}, got {actual}"
                        )
                else:
                    raise InstallError(f"archive contains unsupported member type: {path}")
                members.append(member)
    except InstallError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"cannot read Codex archive: {exc}") from exc

    missing = sorted(expected_paths - seen)
    if missing:
        raise InstallError(f"archive is missing expected member: {missing[0]}")
    unknown = sorted(seen - expected_paths)
    if unknown:
        raise InstallError(f"archive contains unknown member: {unknown[0]}")
    return members


def _destination_path(path: str | os.PathLike[str]) -> Path:
    destination = Path(path)
    if not destination.is_absolute():
        destination = destination.resolve()
    if os.path.lexists(destination):
        raise InstallError(f"destination must be absent: {destination}")
    return destination


def _extract_tar(handle, payload: Path, lock: dict) -> None:
    directories = lock["_directory_map"]
    payload.mkdir(mode=0o755)
    os.chmod(payload, 0o755)
    # Create only directories declared in the lock, in parent-first order.
    for path in sorted(directories, key=lambda value: (value.count("/"), value)):
        target = payload.joinpath(*path.split("/"))
        target.mkdir(mode=directories[path]["mode"])
        os.chmod(target, directories[path]["mode"])

    try:
        with tarfile.open(fileobj=handle, mode="r:gz") as archive:
            for member in archive.getmembers():
                path = _member_path(member)
                if not member.isreg():
                    continue
                target = payload.joinpath(*path.split("/"))
                expected = lock["_file_map"][path]
                try:
                    source = archive.extractfile(member)
                    if source is None:
                        raise InstallError(f"archive file has no readable payload: {path}")
                    with target.open("xb") as output:
                        remaining = expected["size"]
                        while remaining:
                            chunk = source.read(min(CHUNK_SIZE, remaining))
                            if not chunk:
                                raise InstallError(f"archive file was truncated: {path}")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise InstallError(f"archive file was expanded: {path}")
                        output.flush()
                        os.fsync(output.fileno())
                except InstallError:
                    raise
                except (OSError, tarfile.TarError) as exc:
                    raise InstallError(f"cannot extract archive member {path}: {exc}") from exc
                os.chmod(target, expected["mode"])
    except InstallError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"cannot extract Codex archive: {exc}") from exc


def _receipt(lock: dict, source_kind: str) -> dict:
    files = []
    for path in sorted(lock["_file_map"]):
        entry = lock["_file_map"][path]
        files.append(
            {
                "path": path,
                "sha256": entry["sha256"],
                "size": entry["size"],
                "mode": entry["mode"],
                "executable": bool(entry["mode"] & 0o111),
            }
        )
    directories = []
    for path in sorted(lock["_directory_map"]):
        entry = lock["_directory_map"][path]
        directories.append({"path": path, "mode": entry["mode"]})
    archive_name = Path(urlparse.urlparse(lock["url"]).path).name
    return {
        "schema_version": 1,
        "product": "codex",
        "version": lock["version"],
        "release_tag": lock["release_tag"],
        "target": lock["target"],
        "source": {
            "kind": source_kind,
            "url": lock["url"],
            "archive": archive_name,
            "sha256": lock["sha256"],
            "size": lock["size"],
        },
        "directories": directories,
        "files": files,
    }


def _write_receipt(payload: Path, lock: dict, source_kind: str) -> None:
    receipt = payload / RECEIPT_NAME
    try:
        with receipt.open("x", encoding="utf-8") as output:
            json.dump(_receipt(lock, source_kind), output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(receipt, 0o644)
    except OSError as exc:
        raise InstallError(f"cannot write installation receipt: {exc}") from exc


def _download_archive(url: str, destination: Path, expected_size: int) -> None:
    request = urlrequest.Request(url, headers={"User-Agent": "zeusos-codex-installer/1"})
    try:
        response_context = urlrequest.urlopen(request, timeout=120)
        with response_context as response, destination.open("xb") as output:
            final_url = response.geturl()
            if urlparse.urlparse(final_url).scheme.lower() != "https":
                raise InstallError("archive download redirected to a non-HTTPS URL")
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise InstallError(f"archive download returned HTTP status {status}")
            total = 0
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise InstallError("archive download exceeds locked size")
                output.write(chunk)
            if total != expected_size:
                raise InstallError(
                    f"archive download size mismatch: expected {expected_size}, got {total}"
                )
            output.flush()
            os.fsync(output.fileno())
        os.chmod(destination, 0o600)
    except InstallError:
        raise
    except (OSError, urlerror.URLError) as exc:
        raise InstallError(f"cannot download locked Codex archive: {exc}") from exc


def install(
    lock_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    archive_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Install a verified lock artifact and return its destination path."""

    lock = load_lock(lock_path)
    destination_path = _destination_path(destination)
    parent = destination_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"cannot create destination parent {parent}: {exc}") from exc

    source_kind = "local" if archive_path is not None else "download"
    local_archive: Path | None = None
    if archive_path is not None:
        local_archive = Path(archive_path)
        if local_archive.is_symlink() or not local_archive.is_file():
            raise InstallError(f"archive path is not a regular file: {local_archive}")

    staging_root = Path(tempfile.mkdtemp(prefix=".codex-install-", dir=str(parent)))
    payload = staging_root / "payload"
    try:
        if local_archive is None:
            local_archive = staging_root / "codex-package.tar.gz"
            _download_archive(lock["url"], local_archive, lock["size"])
        with _open_archive(local_archive) as archive:
            # This pass verifies the complete locked byte stream before tarfile
            # is allowed to read any member for extraction.
            _verify_archive_bytes(archive, lock, local_archive)
            archive.seek(0)
            _validate_tar(archive, lock)
            archive.seek(0)
            _extract_tar(archive, payload, lock)
        _write_receipt(payload, lock, source_kind)
        if os.path.lexists(destination_path):
            raise InstallError(f"destination appeared during installation: {destination_path}")
        os.rename(payload, destination_path)
        return destination_path
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(f"Codex installation failed: {exc}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path, help="pinned Codex lock JSON")
    parser.add_argument(
        "--destination", required=True, type=Path, help="fresh installation directory"
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="verified local archive fixture; download the locked URL when omitted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        destination = install(args.lock, args.destination, args.archive)
    except (InstallError, OSError) as exc:
        print(f"install-codex: error: {exc}", file=sys.stderr)
        return 2
    print(f"installed Codex {load_lock(args.lock)['version']} at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
