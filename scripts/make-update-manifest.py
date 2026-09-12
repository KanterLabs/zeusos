#!/usr/bin/env python3
"""Publish a signed schema-1 Zeus OS preview update manifest.

The OCI archive and signing key are caller supplied.  This command never
copies or prints private key material; the key is passed directly to the
system ssh-keygen implementation for detached signing.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_DIR = _ROOT / "desktop" / "rootfs" / "usr" / "lib" / "zeus"
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from update_manifest import (  # noqa: E402
    GITHUB_ARCHIVE_ORIGIN,
    HOMELAB_ARCHIVE_ORIGIN,
    MAX_ARCHIVE_SIZE,
    MAX_NOTES_BYTES,
    SSH_KEYGEN,
    UpdateError,
    archive_url,
    inspect_archive,
    validate_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--build-info", required=True, type=Path)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--notes-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument(
        "--artifact-origin",
        choices=(GITHUB_ARCHIVE_ORIGIN, HOMELAB_ARCHIVE_ORIGIN),
        default=GITHUB_ARCHIVE_ORIGIN,
        help="fixed archive host to encode in the signed manifest",
    )
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise UpdateError("build_info_unavailable", "build information is unavailable")
    if len(raw) > 64 * 1024:
        raise UpdateError("build_info_invalid", "build information is too large")
    try:
        text = raw.decode("utf-8", "strict")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise UpdateError("build_info_invalid", "build information is invalid")
    if not isinstance(parsed, dict):
        raise UpdateError("build_info_invalid", "build information is invalid")
    return parsed


def _read_notes(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        raise UpdateError("notes_unavailable", "release notes are unavailable")
    if len(raw) > MAX_NOTES_BYTES:
        raise UpdateError("notes_too_large", "release notes are too large")
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise UpdateError("notes_invalid", "release notes are not UTF-8")


def _archive_digest(path: Path) -> tuple[int, str]:
    try:
        file_stat = os.lstat(path)
    except OSError:
        raise UpdateError("archive_invalid", "OCI archive is unavailable")
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise UpdateError("archive_invalid", "OCI archive is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > MAX_ARCHIVE_SIZE:
        raise UpdateError("archive_invalid", "OCI archive size is outside the supported range")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                if total > MAX_ARCHIVE_SIZE:
                    raise UpdateError("archive_too_large", "OCI archive is too large")
    except UpdateError:
        raise
    except (OSError, ValueError):
        raise UpdateError("archive_invalid", "OCI archive could not be read")
    if total != file_stat.st_size:
        raise UpdateError("archive_invalid", "OCI archive changed while being read")
    return total, digest.hexdigest()


def _existing(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _destination_pair(output: Path) -> tuple[Path, Path]:
    # Publication paths are ordinary CLI paths (unlike the privileged
    # downloader destination); anchor relative paths to this invocation's
    # working directory before applying the no-overwrite checks.
    output = Path(os.path.abspath(os.fspath(output)))
    if output.name in {"", ".", ".."}:
        raise UpdateError("output_invalid", "manifest output path is invalid")
    signature = Path(f"{output}.sig")
    if _existing(output) or _existing(signature):
        raise UpdateError("output_exists", "manifest output already exists")
    try:
        parent_stat = os.lstat(output.parent)
    except OSError:
        raise UpdateError("output_invalid", "manifest output directory is unavailable")
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise UpdateError("output_invalid", "manifest output directory is invalid")
    return output, signature


def _json_bytes(manifest: dict[str, Any]) -> bytes:
    # Compact, deterministic JSON is the exact byte string covered by the SSH
    # signature.  UTF-8 notes are retained rather than escaped for readability.
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _write_temp(directory: Path, data: bytes) -> Path:
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".zeus-update-publish-", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(raw_path)
    except OSError:
        raise UpdateError("output_error", "could not prepare manifest output")


def _sign(manifest_path: Path, signing_key: Path) -> Path:
    try:
        key_stat = os.lstat(signing_key)
    except OSError:
        raise UpdateError("signing_key_unavailable", "signing key is unavailable")
    if not stat.S_ISREG(key_stat.st_mode) or stat.S_ISLNK(key_stat.st_mode):
        raise UpdateError("signing_key_unavailable", "signing key is unavailable")
    try:
        completed = subprocess.run(
            [
                SSH_KEYGEN,
                "-Y",
                "sign",
                "-f",
                os.fspath(signing_key),
                "-n",
                "zeusos-update",
                os.fspath(manifest_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        raise UpdateError("signing_error", "could not sign update manifest")
    if completed.returncode != 0:
        raise UpdateError("signing_error", "could not sign update manifest")
    signature = Path(f"{manifest_path}.sig")
    if not _existing(signature):
        raise UpdateError("signing_error", "ssh-keygen did not produce a signature")
    try:
        signature_stat = os.lstat(signature)
    except OSError:
        raise UpdateError("signing_error", "could not read update signature")
    if not stat.S_ISREG(signature_stat.st_mode) or stat.S_ISLNK(signature_stat.st_mode):
        raise UpdateError("signing_error", "could not read update signature")
    return signature


def _publish(output: Path, signature: Path, data: bytes) -> None:
    """Create both final files without replacing a pre-existing file."""

    own_output = False
    output_signature = Path(f"{output}.sig")
    own_signature = False
    try:
        output_fd = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        own_output = True
        with os.fdopen(output_fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        signature_data = signature.read_bytes()
        signature_fd = os.open(
            output_signature,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        own_signature = True
        with os.fdopen(signature_fd, "wb") as stream:
            stream.write(signature_data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if own_output:
            try:
                os.unlink(output)
            except OSError:
                pass
        if own_signature:
            try:
                os.unlink(output_signature)
            except OSError:
                pass
        raise UpdateError("output_exists", "manifest output already exists")
    except (OSError, ValueError):
        if own_output:
            try:
                os.unlink(output)
            except OSError:
                pass
        if own_signature:
            try:
                os.unlink(output_signature)
            except OSError:
                pass
        raise UpdateError("output_error", "could not write manifest output")


def create_manifest(
    *,
    archive: Path,
    build_info: Path,
    sequence: int,
    notes_file: Path,
    artifact_origin: str = GITHUB_ARCHIVE_ORIGIN,
) -> dict[str, Any]:
    """Inspect inputs and construct a validated schema-1 manifest."""

    if type(sequence) is not int or sequence <= 0 or sequence > 2**63 - 1:
        raise UpdateError("sequence_invalid", "update sequence must be positive")
    image = inspect_archive(archive)
    if image.get("sequence") is None:
        raise UpdateError("sequence_missing", "OCI image has no update sequence label")
    if image["sequence"] != sequence:
        raise UpdateError("sequence_mismatch", "update sequence does not match OCI image")

    info = _read_json(build_info)
    for field in ("version", "source_commit", "build_id"):
        if info.get(field) != image[field]:
            raise UpdateError("build_info_mismatch", "build information does not match OCI image")
    info_sequence = info.get("update_sequence")
    if type(info_sequence) is not int or info_sequence != sequence:
        raise UpdateError("build_info_mismatch", "build update sequence does not match OCI image")

    size, archive_sha256 = _archive_digest(archive)
    version = image["version"]
    build_id = image["build_id"]
    name = f"zeusos-{version}-{build_id}.oci"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "zeusos",
        "channel": "preview",
        "architecture": image["architecture"],
        "version": version,
        "build_id": build_id,
        "source_commit": image["source_commit"],
        "sequence": sequence,
        "published_at": _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "notes": _read_notes(notes_file),
        "archive": {
            "name": name,
            "url": archive_url(version, name, artifact_origin),
            "size": size,
            "sha256": archive_sha256,
            "manifest_digest": image["manifest_digest"],
        },
    }
    return validate_manifest(manifest)


def run(args: argparse.Namespace) -> int:
    output, output_signature = _destination_pair(args.output)
    manifest = create_manifest(
        archive=args.archive,
        build_info=args.build_info,
        sequence=args.sequence,
        notes_file=args.notes_file,
        artifact_origin=args.artifact_origin,
    )
    data = _json_bytes(manifest)
    with tempfile.TemporaryDirectory(prefix="zeus-update-publish-", dir=output.parent) as directory:
        temporary_manifest = _write_temp(Path(directory), data)
        temporary_signature: Path | None = None
        try:
            temporary_signature = _sign(temporary_manifest, args.signing_key)
            _publish(output, temporary_signature, data)
        finally:
            # TemporaryDirectory removes only these publisher-owned files.  It
            # never touches an existing output or a caller's signing key.
            temporary_signature = None
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except UpdateError as exc:
        print(f"make-update-manifest: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
