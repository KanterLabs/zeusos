"""Signed Zeus release retrieval and archive verification.

The Fedora launcher is deliberately kept independent of the desktop updater's
entry points, but it uses the same verification implementation.  The trusted
module is loaded from the installed Zeus module directory (or from the source
tree while the installer is tested).  There is no API for supplying a release
URL, a signer path, or a signing key: those values remain policy owned by the
trusted module.

This module only downloads and verifies an OCI archive.  It never mounts a
filesystem, invokes bootc, changes a partition, or executes a command.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Mapping


CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_SIZE = 8 * 1024**3

# Keep the installer verifier and public signer pinned to the exact copies
# shipped by the desktop updater.  The hashes are public integrity metadata;
# no private signing material is included here.
_PINNED_UPDATE_MANIFEST_SHA256 = "b602decdb0cb41919ec92a4b011c78a73076e50b7e733300f7b3a3e19488778c"
_PINNED_SIGNERS_SHA256 = "5df19482073c9a1c2f1c77da2a3c50ae2d9b97c20a46d523a5ca6dc09a5a1b51"
_PINNED_FEED_URL = (
    "https://raw.githubusercontent.com/KanterLabs/zeusos/"
    "3c9ad01c11739af250745be37e15b07e7dba85cd/updates/preview.json"
)


class ArtifactError(RuntimeError):
    """A safe, stable artifact operation failure."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(str(message))


def _load_trusted_module() -> Any:
    """Load the exact updater verifier without accepting caller paths.

    The Fedora package carries a byte-for-byte copy under ``vendor`` so it can
    verify a Zeus release before Zeus is installed.  The image path and source
    tree are retained only as compatible locations for an already deployed
    copy and repository checks.  Ambient ``sys.path`` imports are intentionally
    excluded: a desktop working directory must never select the verifier.
    """

    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "vendor" / "update_manifest.py",
        Path("/usr/lib/zeus/update_manifest.py"),
        package_root.parents[1]
        / "desktop"
        / "rootfs"
        / "usr"
        / "lib"
        / "zeus"
        / "update_manifest.py",
    )
    for candidate in candidates:
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 512 * 1024:
            continue
        try:
            raw = candidate.read_bytes()
        except (OSError, ValueError):
            continue
        if hashlib.sha256(raw).hexdigest() != _PINNED_UPDATE_MANIFEST_SHA256:
            continue
        spec = importlib.util.spec_from_file_location(
            "zeus_installer._trusted_update_manifest", candidate
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        # The trusted module does not import this package.  Registering it
        # before execution also keeps normal module semantics for tooling.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            continue
        return module
    raise ArtifactError(
        "verifier_unavailable", "The signed release verifier is unavailable."
    )


def _configure_trusted_signers(module: Any) -> None:
    """Point the pinned verifier at fixed installer policy files."""

    signer = Path(__file__).resolve().parent / "data" / "update-allowed-signers"
    try:
        metadata = os.stat(signer, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
            raise OSError
        raw = signer.read_bytes()
    except (OSError, ValueError):
        raise ArtifactError(
            "verifier_unavailable", "The signed release verifier is unavailable."
        ) from None
    if hashlib.sha256(raw).hexdigest() != _PINNED_SIGNERS_SHA256:
        raise ArtifactError(
            "verifier_unavailable", "The signed release verifier is unavailable."
        )
    # ``fetch_manifest`` evaluates DEFAULT_SIGNERS at call time.  Set it on
    # the module instance rather than changing the pinned verifier source.
    if not hasattr(module, "DEFAULT_SIGNERS"):
        raise ArtifactError(
            "verifier_unavailable", "The signed release verifier is unavailable."
        )
    module.DEFAULT_SIGNERS = str(signer)
    if not hasattr(module, "DEFAULT_FEED_URL"):
        raise ArtifactError(
            "verifier_unavailable", "The signed release verifier is unavailable."
        )
    # The desktop updater follows ``main`` for ordinary updates.  The
    # installer must consume the exact feed reviewed with its qualified
    # executor, so bind the vendored verifier to an immutable commit here.
    # There is deliberately no caller-facing URL or host override.
    module.DEFAULT_FEED_URL = _PINNED_FEED_URL


def _trusted() -> Any:
    # Resolve lazily so importing the launcher remains possible in a minimal
    # Fedora environment and tests can replace the verifier at the boundary.
    global _TRUSTED_MODULE
    if _TRUSTED_MODULE is None:
        _TRUSTED_MODULE = _load_trusted_module()
    _configure_trusted_signers(_TRUSTED_MODULE)
    return _TRUSTED_MODULE


_TRUSTED_MODULE: Any = None


def _error_from_exception(error: BaseException, fallback: str, message: str) -> ArtifactError:
    trusted = _TRUSTED_MODULE
    if trusted is not None:
        trusted_error = getattr(trusted, "UpdateError", ())
        if trusted_error and isinstance(error, trusted_error):
            return ArtifactError(getattr(error, "code", fallback), message)
    if isinstance(error, ArtifactError):
        return error
    return ArtifactError(fallback, message)


def validate_release(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a release with the pinned desktop updater verifier."""

    try:
        result = _trusted().validate_manifest(manifest)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise _error_from_exception(
            error, "release_invalid", "The signed release metadata is invalid."
        ) from error
    if not isinstance(result, dict):
        raise ArtifactError("release_invalid", "The signed release metadata is invalid.")
    return result


def fetch_current_release() -> dict[str, Any]:
    """Fetch and authenticate the installer-pinned preview release.

    The verifier receives both the immutable feed URL and the installed
    allowed signer file from fixed installer policy.  This wrapper
    intentionally takes no arguments.
    """

    try:
        result = _trusted().fetch_manifest()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise _error_from_exception(
            error, "release_fetch_failed", "The signed release could not be fetched."
        ) from error
    return validate_release(result)


# Convenient spelling for callers that used the updater's terminology.
fetch_release = fetch_current_release


def _safe_private_destination(path: str | os.PathLike[str]) -> Path:
    """Check a download destination without resolving symlinks."""

    try:
        destination = Path(path)
    except (TypeError, ValueError) as error:
        raise ArtifactError("unsafe_destination", "The artifact destination is invalid.") from error
    if not destination.is_absolute() or destination.name in {"", ".", ".."}:
        raise ArtifactError(
            "unsafe_destination", "The artifact destination must be absolute."
        )

    current = Path(destination.anchor)
    for component in destination.parent.parts:
        if component == destination.anchor:
            continue
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ArtifactError(
                "unsafe_destination", "The artifact destination directory is unavailable."
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactError(
                "unsafe_destination", "The artifact destination directory is unsafe."
            )
    try:
        parent_metadata = os.lstat(destination.parent)
    except OSError as error:
        raise ArtifactError(
            "unsafe_destination", "The artifact destination directory is unavailable."
        ) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ArtifactError(
            "unsafe_destination", "The artifact destination directory is unsafe."
        )
    # A download directory must be private.  The owner is checked by the
    # backend as root/current-user policy; accepting either here keeps this
    # helper usable by an isolated fixture while preserving the mode boundary.
    if parent_metadata.st_mode & 0o077:
        raise ArtifactError(
            "unsafe_destination", "The artifact destination directory is not private."
        )
    try:
        existing = os.lstat(destination)
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ArtifactError(
            "unsafe_destination", "The artifact destination is unavailable."
        ) from error
    if existing is not None:
        raise ArtifactError(
            "destination_exists", "The artifact destination already exists."
        )
    return destination


def download_release(
    manifest: Mapping[str, Any],
    destination: str | os.PathLike[str],
    progress: Callable[[int, int], Any] | None = None,
) -> None:
    """Download one exact, signed archive through the trusted verifier."""

    validated = validate_release(manifest)
    destination_path = _safe_private_destination(destination)
    try:
        _trusted().download_archive(validated, destination_path, progress=progress)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise _error_from_exception(
            error, "download_failed", "The signed release archive could not be downloaded."
        ) from error


# Alias that makes the backend's injected boundary explicit in tests.
download_archive = download_release


def _open_archive(path: str | os.PathLike[str]) -> tuple[int, Path, os.stat_result]:
    try:
        archive_path = Path(path)
    except (TypeError, ValueError) as error:
        raise ArtifactError("archive_invalid", "The release archive path is invalid.") from error
    # ``O_NONBLOCK`` prevents a corrupted FIFO at the staging path from
    # hanging the privileged helper before ``fstat`` can reject it.  It has no
    # effect on regular files.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(archive_path, flags)
    except OSError as error:
        raise ArtifactError("archive_invalid", "The release archive is unavailable.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactError("archive_invalid", "The release archive is not a regular file.")
        if metadata.st_size < 0 or metadata.st_size > MAX_ARCHIVE_SIZE:
            raise ArtifactError("archive_too_large", "The release archive is too large.")
        return descriptor, archive_path, metadata
    except (OSError, ValueError) as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ArtifactError("archive_invalid", "The release archive could not be inspected.") from error
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _hash_open_descriptor(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size or total > MAX_ARCHIVE_SIZE:
                raise ArtifactError(
                    "archive_size_mismatch", "The release archive size is not signed."
                )
            digest.update(chunk)
    except ArtifactError:
        raise
    except (OSError, ValueError) as error:
        raise ArtifactError("archive_invalid", "The release archive could not be read.") from error
    if total != expected_size:
        raise ArtifactError(
            "archive_size_mismatch", "The release archive size is not signed."
        )
    return digest.hexdigest()


def verify_archive(
    path: str | os.PathLike[str], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify archive bytes and OCI identity against signed release metadata.

    The archive is opened with ``O_NOFOLLOW`` and its device/inode is checked
    before and after the trusted OCI parser runs.  The backend only supplies a
    root-owned private path, but the checks also protect fixture callers from
    symlink replacement and basic path races.
    """

    validated = validate_release(manifest)
    archive = validated["archive"]
    expected_size = archive["size"]
    expected_hash = archive["sha256"]
    descriptor, archive_path, opened = _open_archive(path)
    try:
        actual_hash = _hash_open_descriptor(descriptor, expected_size)
        try:
            after_hash = os.fstat(descriptor)
        except (OSError, ValueError) as error:
            raise ArtifactError("archive_changed", "The release archive changed while being verified.") from error
        if (after_hash.st_dev, after_hash.st_ino, after_hash.st_size) != (
            opened.st_dev,
            opened.st_ino,
            expected_size,
        ):
            raise ArtifactError("archive_changed", "The release archive changed while being verified.")
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise ArtifactError(
                "archive_hash_mismatch", "The release archive hash does not match the signed release."
            )

        try:
            current = os.lstat(archive_path)
        except OSError as error:
            raise ArtifactError("archive_changed", "The release archive changed while being verified.") from error
        if (current.st_dev, current.st_ino, current.st_size) != (
            opened.st_dev,
            opened.st_ino,
            expected_size,
        ) or stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise ArtifactError("archive_changed", "The release archive changed while being verified.")

        try:
            identity = _trusted().inspect_archive(archive_path)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _error_from_exception(
                error, "archive_invalid", "The release archive is not a valid Zeus image."
            ) from error

        # Keep the original descriptor open across the path based OCI parser.
        # Rehashing it catches an in-place same-size rewrite that an inode/size
        # check alone cannot detect.
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as error:
            raise ArtifactError("archive_changed", "The release archive changed while being verified.") from error
        final_hash = _hash_open_descriptor(descriptor, expected_size)
        try:
            current = os.fstat(descriptor)
        except (OSError, ValueError) as error:
            raise ArtifactError("archive_changed", "The release archive changed while being verified.") from error
        if (
            (current.st_dev, current.st_ino, current.st_size)
            != (opened.st_dev, opened.st_ino, expected_size)
            or not hmac.compare_digest(final_hash, actual_hash)
        ):
            raise ArtifactError("archive_changed", "The release archive changed while being verified.")
        try:
            current_path = os.lstat(archive_path)
        except OSError as error:
            raise ArtifactError("archive_changed", "The release archive changed while being verified.") from error
        if (current_path.st_dev, current_path.st_ino, current_path.st_size) != (
            opened.st_dev,
            opened.st_ino,
            expected_size,
        ) or stat.S_ISLNK(current_path.st_mode) or not stat.S_ISREG(current_path.st_mode):
            raise ArtifactError("archive_changed", "The release archive changed while being verified.")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    if not isinstance(identity, Mapping):
        raise ArtifactError("archive_invalid", "The release archive identity is invalid.")
    for key in ("architecture", "version", "source_commit", "build_id", "sequence"):
        if identity.get(key) != validated.get(key):
            raise ArtifactError(
                "archive_identity_mismatch", "The release archive is not the selected Zeus build."
            )
    if identity.get("manifest_digest") != archive["manifest_digest"]:
        raise ArtifactError(
            "archive_identity_mismatch", "The release archive manifest is not the signed build."
        )

    return {
        "name": archive["name"],
        "path": str(archive_path),
        "size": expected_size,
        "sha256": actual_hash,
        "manifest_digest": identity["manifest_digest"],
        "architecture": identity["architecture"],
        "version": identity["version"],
        "source_commit": identity["source_commit"],
        "build_id": identity["build_id"],
        "sequence": identity.get("sequence"),
    }


# Keep a descriptive alias for callers that treat OCI verification as a
# separate operation.
verify_release_archive = verify_archive


__all__ = [
    "ArtifactError",
    "CHUNK_SIZE",
    "MAX_ARCHIVE_SIZE",
    "download_archive",
    "download_release",
    "fetch_current_release",
    "fetch_release",
    "validate_release",
    "verify_archive",
    "verify_release_archive",
]
