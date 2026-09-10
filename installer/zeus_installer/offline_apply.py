"""Apply one verified Zeus OS update staged by the Fedora installer.

This module is copied to ``/etc/zeus/offline-apply.py`` on an existing Zeus
installation.  It intentionally has no network-facing entry point: the
Fedora installer supplies a signed manifest, its detached signature, a public
allowed-signer file, and one OCI archive under the fixed offline directory.

The runner is kept independent of the desktop updater so that it can be
executed by a small systemd oneshot before the desktop starts.  It still uses
the desktop updater's operation lock and status schema, allowing the native
Updates window to report the result after boot.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1

# These paths are the installation contract.  The constructor accepts
# alternate roots only for isolated tests; production code always uses these
# values and never accepts a path from the signed manifest or the environment.
OFFLINE_ROOT = Path("/var/lib/zeus/offline-update")
UPDATER_ROOT = Path("/var/lib/zeus/updater")
SHARE_ROOT = Path("/usr/share/zeus")
ADJACENT_VERIFIER = Path("/etc/zeus/update_manifest.py")

MANIFEST_NAME = "preview.json"
SIGNATURE_NAME = "preview.json.sig"
SIGNERS_NAME = "update-allowed-signers"
PAYLOAD_NAME = "payload.oci"
PENDING_NAME = "pending"
STATUS_NAME = "status.json"
LOCK_NAME = "operation.lock"

MANIFEST_PATH = OFFLINE_ROOT / MANIFEST_NAME
SIGNATURE_PATH = OFFLINE_ROOT / SIGNATURE_NAME
SIGNERS_PATH = OFFLINE_ROOT / SIGNERS_NAME
PAYLOAD_PATH = OFFLINE_ROOT / PAYLOAD_NAME
PENDING_PATH = OFFLINE_ROOT / PENDING_NAME

BOOTC = "/usr/bin/bootc"
PLYMOUTH = "/usr/bin/plymouth"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
COMMAND_ENV = {
    "PATH": "/usr/sbin:/usr/bin",
    "LANG": "C.UTF-8",
    "HOME": "/root",
}

JSON_LIMIT = 128 * 1024
MAX_VERIFIER_BYTES = 512 * 1024
MAX_SIGNER_BYTES = 64 * 1024
MAX_TEXT_BYTES = 512
MAX_ARCHIVE_SIZE = 8 * 1024**3

_BUILD_RE = re.compile(r"\Agit-[0-9a-f]{12}\Z")
_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_VERSION_RE = re.compile(
    r"\A"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"\Z"
)
_ARCHIVE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_CODE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")


class OfflineApplyError(RuntimeError):
    """An expected, safe-to-display offline update failure."""

    def __init__(self, code: str, message: str = "The offline update failed."):
        self.code = str(code)
        super().__init__(str(message))


# Keep the familiar spelling available to small integrations that use the
# updater's error terminology.
UpdateError = OfflineApplyError


_MESSAGES = {
    "busy": "Another Zeus update operation is already running.",
    "unsafe_storage": "The offline update files are not protected.",
    "state_write_failed": "Offline update progress could not be saved.",
    "verifier_unavailable": "The signed update verifier is unavailable.",
    "signature_invalid": "The offline update signature is not trusted.",
    "signature_unavailable": "The offline update signer policy is unavailable.",
    "manifest_invalid": "The offline update manifest is invalid.",
    "manifest_too_large": "The offline update manifest is too large.",
    "offline_payload_missing": "The staged offline update is incomplete.",
    "artifact_invalid": "The staged OCI archive is unavailable or invalid.",
    "archive_too_large": "The staged OCI archive is too large.",
    "archive_hash_mismatch": "The staged OCI archive does not match the signed hash.",
    "wrong_image": "The staged OCI archive does not match the signed Zeus OS identity.",
    "installed_identity": "The installed Zeus OS identity could not be verified.",
    "unsupported_installation": "This Zeus installation cannot apply an offline update.",
    "not_newer": "The staged update is older than the installed Zeus OS.",
    "conflicting_build": "The staged update conflicts with the installed update sequence.",
    "rollback_queued": "An OS rollback is already queued; finish it before updating.",
    "invalid_staged": "The pending OS deployment could not be identified.",
    "staged_update_exists": "A different OS update is already staged; it was preserved.",
    "bootc_unavailable": "The installed OS update state could not be verified.",
    "command_failed": "The OS update command failed; the running OS remains selected.",
    "staging_failed": "The verified update could not be staged; restart was not requested.",
    "staging_unconfirmed": "The update command ended without a verified pending deployment.",
    "offline_apply_failed": "The offline update was interrupted; check its status and retry.",
}


def _message(code: str) -> str:
    return _MESSAGES.get(code, _MESSAGES["offline_apply_failed"])


def utc_now() -> str:
    # Importing datetime lazily keeps the standalone import small and avoids
    # any locale or timezone subprocesses.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _safe_code(value: Any, fallback: str) -> str:
    value = str(value)
    return value if _CODE_RE.fullmatch(value) else fallback


class OfflineUpdateRunner:
    """Verify and stage the one payload supplied by the Fedora installer.

    ``storage_check=False`` and injected roots/runners exist solely for
    disposable tests.  The installed script uses the strict defaults and
    consequently requires root-owned, non-group-writable paths.
    """

    def __init__(
        self,
        offline_root: str | os.PathLike[str] = OFFLINE_ROOT,
        updater_root: str | os.PathLike[str] = UPDATER_ROOT,
        share: str | os.PathLike[str] = SHARE_ROOT,
        *,
        verifier_path: str | os.PathLike[str] = ADJACENT_VERIFIER,
        verifier: Any | None = None,
        runner: Any | None = None,
        storage_check: bool = True,
        wait_for_lock: bool = False,
        plymouth: bool | None = None,
        boot_id: str | None = None,
        clock: Any | None = None,
    ) -> None:
        self.offline_root = Path(offline_root)
        self.updater_root = Path(updater_root)
        self.share = Path(share)
        self.verifier_path = Path(verifier_path)
        self.verifier = verifier
        self.runner = runner or subprocess.run
        self.storage_check = bool(storage_check)
        self.wait_for_lock = bool(wait_for_lock)
        # A test runner generally expects only bootc commands.  Production's
        # subprocess runner additionally gets best-effort Plymouth notices.
        self.plymouth = (runner is None) if plymouth is None else bool(plymouth)
        self._boot_id_override = boot_id
        self.clock = clock or utc_now
        self.last_state: dict[str, Any] | None = None

    @property
    def manifest_path(self) -> Path:
        return self.offline_root / MANIFEST_NAME

    @property
    def signature_path(self) -> Path:
        return self.offline_root / SIGNATURE_NAME

    @property
    def signers_path(self) -> Path:
        return self.offline_root / SIGNERS_NAME

    @property
    def payload_path(self) -> Path:
        # The archive path is deliberately independent of archive.name in the
        # signed document.  The installer copies its verified result here.
        return self.offline_root / PAYLOAD_NAME

    def retained_payload_path(self, manifest: Mapping[str, Any]) -> Path:
        """Return the immutable, per-build bootc archive path."""

        name = manifest.get("archive", {}).get("name")
        if not isinstance(name, str) or _ARCHIVE_NAME_RE.fullmatch(name) is None:
            raise OfflineApplyError("manifest_invalid")
        return self.offline_root / name

    @property
    def pending_path(self) -> Path:
        return self.offline_root / PENDING_NAME

    @property
    def native_status_path(self) -> Path:
        return self.updater_root / STATUS_NAME

    @property
    def offline_status_path(self) -> Path:
        return self.offline_root / STATUS_NAME

    @property
    def lock_path(self) -> Path:
        return self.updater_root / LOCK_NAME

    def _expected_uid(self) -> int:
        return 0 if self.storage_check else os.geteuid()

    def _check_directory(self, path: Path) -> os.stat_result:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OfflineApplyError("unsafe_storage")
        if self.storage_check and (
            metadata.st_uid != self._expected_uid() or metadata.st_mode & 0o022
        ):
            raise OfflineApplyError("unsafe_storage")
        return metadata

    def _check_image_share(self) -> None:
        # Early signed images carry root:root 0775 on this immutable /usr
        # directory. Do not apply private update-state permissions to image
        # metadata. Files themselves still require root ownership and no
        # group/world writes; only this known directory accepts root-group
        # write permission. No mutable update directory gets this exception.
        try:
            metadata = self.share.lstat()
        except OSError as error:
            raise OfflineApplyError("installed_identity") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OfflineApplyError("installed_identity")
        if self.storage_check and (
            metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o002
        ):
            raise OfflineApplyError("installed_identity")

    def _check_ancestors(self, path: Path, *, image_identity: bool = False) -> None:
        """Check existing components without resolving symlinks."""

        if not path.is_absolute() or path.anchor == "":
            raise OfflineApplyError("unsafe_storage")
        current = Path(path.anchor)
        # The anchor itself (usually /) is checked as well.  It may be mode
        # 0755 and root-owned; test roots skip ownership/mode checks.
        try:
            self._check_directory(current)
        except OfflineApplyError:
            raise
        for component in path.parts[1:]:
            current /= component
            try:
                if image_identity and current == self.share:
                    self._check_image_share()
                else:
                    self._check_directory(current)
            except OfflineApplyError as error:
                if not current.exists() and not current.is_symlink():
                    # A missing tail is safe to create one component at a
                    # time.  Existing ancestors have already been checked.
                    break
                raise error

    def _ensure_directory(self, path: Path, mode: int = 0o755) -> None:
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise OfflineApplyError("unsafe_storage")
        current = Path(path.anchor)
        self._check_directory(current)
        for component in path.parts[1:]:
            current /= component
            try:
                self._check_directory(current)
            except OfflineApplyError:
                try:
                    os.mkdir(current, mode)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise OfflineApplyError("unsafe_storage") from error
                self._check_directory(current)

    def _prepare_storage(self) -> None:
        self._ensure_directory(self.offline_root)
        self._ensure_directory(self.updater_root)

    def _check_regular(
        self,
        path: Path,
        *,
        label: str,
        max_size: int | None = None,
        missing_code: str = "offline_payload_missing",
    ) -> os.stat_result:
        if not path.is_absolute():
            raise OfflineApplyError("unsafe_storage")
        try:
            metadata = os.lstat(path)
        except FileNotFoundError as error:
            raise OfflineApplyError(missing_code) from error
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OfflineApplyError("unsafe_storage")
        if self.storage_check and (
            metadata.st_uid != self._expected_uid() or metadata.st_mode & 0o022
        ):
            raise OfflineApplyError("unsafe_storage")
        if max_size is not None and metadata.st_size > max_size:
            code = "archive_too_large" if label == "OCI archive" else "manifest_too_large"
            raise OfflineApplyError(code)
        self._check_ancestors(path.parent, image_identity=label in {"installed identity", "installed product"})
        return metadata

    def _read_bytes(
        self,
        path: Path,
        *,
        label: str,
        max_size: int,
        missing_code: str = "offline_payload_missing",
    ) -> bytes:
        self._check_regular(path, label=label, max_size=max_size, missing_code=missing_code)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise OfflineApplyError(missing_code) from error
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        try:
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if self.storage_check and (
                opened.st_uid != self._expected_uid() or opened.st_mode & 0o022
            ):
                raise OfflineApplyError("unsafe_storage")
            if opened.st_size > max_size:
                code = "archive_too_large" if label == "OCI archive" else "manifest_too_large"
                raise OfflineApplyError(code)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                result = stream.read(max_size + 1)
            if len(result) > max_size:
                code = "archive_too_large" if label == "OCI archive" else "manifest_too_large"
                raise OfflineApplyError(code)
            if len(result) != opened.st_size:
                raise OfflineApplyError("unsafe_storage")
            return result
        except OfflineApplyError:
            raise
        except (OSError, ValueError) as error:
            raise OfflineApplyError("unsafe_storage") from error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _atomic_json(self, path: Path, value: Mapping[str, Any]) -> None:
        if not path.is_absolute():
            raise OfflineApplyError("unsafe_storage")
        self._check_ancestors(path.parent)
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if self.storage_check and (
                existing.st_uid != self._expected_uid() or existing.st_mode & 0o022
            ):
                raise OfflineApplyError("unsafe_storage")
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".offline-status-", dir=path.parent)
            temporary = Path(name)
            os.fchmod(descriptor, 0o644)
            encoded = (
                json.dumps(
                    value,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OfflineApplyError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise OfflineApplyError("state_write_failed") from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _boot_id(self) -> str:
        if self._boot_id_override is not None:
            return self._boot_id_override
        try:
            value = BOOT_ID_PATH.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return "unknown"
        return value if len(value) <= 128 else "unknown"

    def _notice(self, phase: str) -> None:
        if not self.plymouth:
            return
        messages = {
            "verifying": "Zeus OS: verifying offline update",
            "staging": "Zeus OS: preparing update for restart",
            "ready": "Zeus OS: update ready; restart to apply",
            "done": "Zeus OS: offline update already installed",
            "error": "Zeus OS: offline update needs attention",
        }
        message = messages.get(phase)
        if message is None:
            return
        try:
            metadata = os.stat(PLYMOUTH, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return
            self.runner(
                [PLYMOUTH, "display-message", "--text=" + message],
                capture_output=True,
                text=True,
                timeout=5,
                env=COMMAND_ENV.copy(),
            )
        except Exception:
            # Plymouth is only a boot-time progress hint.  Its absence or
            # failure must never change update safety or durable state.
            return

    def _state(
        self,
        phase: str,
        manifest: Mapping[str, Any] | None,
        message: str,
        *,
        error: str | None = None,
        progress: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "phase": phase,
            "manifest": dict(manifest) if manifest is not None else None,
            "message": message,
            "error": error,
            "progress": dict(progress) if progress is not None else None,
            "boot_id": self._boot_id(),
            "updated_at": self.clock(),
        }
        # The offline copy is useful for installer diagnostics.  The native
        # updater copy is what the desktop Updates window reads.  Both are
        # replaced atomically while the shared operation lock is held.
        self._atomic_json(self.offline_status_path, value)
        self._atomic_json(self.native_status_path, value)
        self.last_state = value
        self._notice(phase)
        return value

    @contextmanager
    def lock(self, *, wait: bool | None = None) -> Iterator[None]:
        """Hold the exact lock also used by ``zeus_update.Installer``."""

        self._prepare_storage()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        try:
            metadata = os.fstat(descriptor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if self.storage_check and (
                metadata.st_uid != self._expected_uid() or metadata.st_mode & 0o077
            ):
                raise OfflineApplyError("unsafe_storage")
            blocking = self.wait_for_lock if wait is None else bool(wait)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise OfflineApplyError("busy") from error
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _load_verifier(self) -> Any:
        if self.verifier is not None:
            return self.verifier
        self._check_regular(
            self.verifier_path,
            label="trusted verifier",
            max_size=MAX_VERIFIER_BYTES,
            missing_code="verifier_unavailable",
        )
        module_name = f"_zeus_offline_update_manifest_{id(self):x}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, self.verifier_path)
            if spec is None or spec.loader is None:
                raise ValueError
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            if not callable(getattr(module, "verify_manifest", None)) or not callable(
                getattr(module, "inspect_archive", None)
            ):
                raise ValueError
            return module
        except OfflineApplyError:
            raise
        except Exception as error:
            sys.modules.pop(module_name, None)
            raise OfflineApplyError("verifier_unavailable") from error

    @staticmethod
    def _translate(error: BaseException, fallback: str) -> OfflineApplyError:
        if isinstance(error, OfflineApplyError):
            return error
        code = _safe_code(getattr(error, "code", fallback), fallback)
        return OfflineApplyError(code)

    def _manifest_contract(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise OfflineApplyError("manifest_invalid")
        required = {
            "schema_version",
            "product",
            "channel",
            "architecture",
            "version",
            "build_id",
            "source_commit",
            "sequence",
            "archive",
        }
        if not required.issubset(value.keys()):
            raise OfflineApplyError("manifest_invalid")
        if (
            value.get("schema_version") != 1
            or value.get("product") != "zeusos"
            or value.get("channel") != "preview"
            or value.get("architecture") != "amd64"
        ):
            raise OfflineApplyError("manifest_invalid")
        version = value.get("version")
        source_commit = value.get("source_commit")
        build_id = value.get("build_id")
        sequence = value.get("sequence")
        if (
            not isinstance(version, str)
            or not _VERSION_RE.fullmatch(version)
            or not isinstance(source_commit, str)
            or not _COMMIT_RE.fullmatch(source_commit)
            or not isinstance(build_id, str)
            or not _BUILD_RE.fullmatch(build_id)
            or build_id != f"git-{source_commit[:12]}"
            or type(sequence) is not int
            or sequence <= 0
        ):
            raise OfflineApplyError("manifest_invalid")
        archive = value.get("archive")
        if not isinstance(archive, Mapping):
            raise OfflineApplyError("manifest_invalid")
        name = archive.get("name")
        size = archive.get("size")
        digest = archive.get("sha256")
        manifest_digest = archive.get("manifest_digest")
        if (
            not isinstance(name, str)
            or _ARCHIVE_NAME_RE.fullmatch(name) is None
            or type(size) is not int
            or size <= 0
            or size > MAX_ARCHIVE_SIZE
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not isinstance(manifest_digest, str)
            or _DIGEST_RE.fullmatch(manifest_digest) is None
        ):
            raise OfflineApplyError("manifest_invalid")
        # Detach the data before any later status write.
        detached = dict(value)
        detached["archive"] = dict(archive)
        return detached

    def _verify_manifest(self, trusted: Any) -> dict[str, Any]:
        manifest_max = int(getattr(trusted, "MAX_MANIFEST_BYTES", 64 * 1024))
        signature_max = int(getattr(trusted, "MAX_SIGNATURE_BYTES", 64 * 1024))
        raw = self._read_bytes(
            self.manifest_path,
            label="manifest",
            max_size=min(manifest_max, 64 * 1024),
        )
        signature = self._read_bytes(
            self.signature_path,
            label="manifest signature",
            max_size=min(signature_max, 64 * 1024),
        )
        self._check_regular(
            self.signers_path,
            label="trusted signers",
            max_size=MAX_SIGNER_BYTES,
            missing_code="signature_unavailable",
        )
        try:
            value = trusted.verify_manifest(raw, signature, self.signers_path)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._translate(error, "signature_invalid") from error
        try:
            validator = getattr(trusted, "validate_manifest", None)
            if callable(validator):
                value = validator(value)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._translate(error, "manifest_invalid") from error
        return self._manifest_contract(value)

    def _current_identity(self) -> dict[str, Any]:
        self._check_image_share()

        def read(name: str) -> str:
            path = self.share / name
            try:
                return self._read_bytes(
                    path,
                    label="installed identity",
                    max_size=256,
                    missing_code="installed_identity",
                ).decode("utf-8", "strict").strip()
            except OfflineApplyError:
                raise
            except (UnicodeDecodeError, OSError, ValueError) as error:
                raise OfflineApplyError("installed_identity") from error

        version = read("version")
        source_commit = read("source-commit")
        build_id = read("build-id")
        sequence_text = read("update-sequence")
        if (
            _VERSION_RE.fullmatch(version) is None
            or _COMMIT_RE.fullmatch(source_commit) is None
            or _BUILD_RE.fullmatch(build_id) is None
            or build_id != f"git-{source_commit[:12]}"
            or not sequence_text.isdecimal()
        ):
            raise OfflineApplyError("installed_identity")
        try:
            sequence = int(sequence_text, 10)
        except (TypeError, ValueError, OverflowError) as error:
            raise OfflineApplyError("installed_identity") from error
        if sequence <= 0 or sequence > 2**63 - 1:
            raise OfflineApplyError("unsupported_installation")

        # Current images predating this route have no product marker.  When a
        # marker is present, honor it so a mounted non-Zeus share cannot pass
        # the identity check by merely carrying similarly named files.
        product_path = self.share / "product"
        try:
            product_metadata = os.lstat(product_path)
        except FileNotFoundError:
            product = "zeusos"
        except OSError as error:
            raise OfflineApplyError("installed_identity") from error
        else:
            if stat.S_ISLNK(product_metadata.st_mode) or not stat.S_ISREG(product_metadata.st_mode):
                raise OfflineApplyError("installed_identity")
            try:
                product = self._read_bytes(
                    product_path,
                    label="installed product",
                    max_size=64,
                    missing_code="installed_identity",
                ).decode("utf-8", "strict").strip()
            except (OfflineApplyError, UnicodeDecodeError) as error:
                raise OfflineApplyError("installed_identity") from error
            if product != "zeusos":
                raise OfflineApplyError("installed_identity")
        return {
            "product": product,
            "version": version,
            "source_commit": source_commit,
            "build_id": build_id,
            "sequence": sequence,
        }

    @staticmethod
    def _eligibility(manifest: Mapping[str, Any], current: Mapping[str, Any]) -> str:
        sequence = manifest["sequence"]
        current_sequence = current["sequence"]
        if sequence < current_sequence:
            raise OfflineApplyError("not_newer")
        if sequence == current_sequence:
            if manifest["build_id"] != current["build_id"]:
                raise OfflineApplyError("conflicting_build")
            if all(
                manifest[key] == current[key]
                for key in ("version", "source_commit", "build_id", "sequence")
            ):
                return "done"
            raise OfflineApplyError("conflicting_build")
        if manifest["build_id"] == current["build_id"]:
            # A build ID is derived from the source commit.  Reusing it with a
            # new sequence is a conflicting release, never an upgrade.
            raise OfflineApplyError("conflicting_build")
        return "new"

    def _hash_file(self, path: Path, archive: Mapping[str, Any]) -> None:
        expected_size = archive["size"]
        expected_hash = archive["sha256"]
        metadata = self._check_regular(
            path,
            label="OCI archive",
            max_size=MAX_ARCHIVE_SIZE,
        )
        if metadata.st_size != expected_size:
            raise OfflineApplyError("archive_hash_mismatch")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise OfflineApplyError("artifact_invalid") from error
        digest = hashlib.sha256()
        total = 0
        try:
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if self.storage_check and (
                opened.st_uid != self._expected_uid() or opened.st_mode & 0o022
            ):
                raise OfflineApplyError("unsafe_storage")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    if total > expected_size or total > MAX_ARCHIVE_SIZE:
                        raise OfflineApplyError("archive_hash_mismatch")
            if total != expected_size or not hmac.compare_digest(digest.hexdigest(), expected_hash):
                raise OfflineApplyError("archive_hash_mismatch")
        except OfflineApplyError:
            raise
        except (OSError, ValueError) as error:
            raise OfflineApplyError("artifact_invalid") from error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _hash_archive(self, manifest: Mapping[str, Any]) -> None:
        self._hash_file(self.payload_path, manifest["archive"])

    def _verify_archive(self, trusted: Any, manifest: Mapping[str, Any]) -> None:
        archive = manifest["archive"]
        self._state(
            "verifying",
            manifest,
            "Verifying the signed offline OCI archive.",
            progress={"bytes": 0, "total": archive["size"]},
        )
        self._hash_archive(manifest)
        try:
            actual = trusted.inspect_archive(self.payload_path)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise self._translate(error, "artifact_invalid") from error
        if not isinstance(actual, Mapping):
            raise OfflineApplyError("wrong_image")
        for key in ("architecture", "version", "source_commit", "build_id", "sequence"):
            if actual.get(key) != manifest[key]:
                raise OfflineApplyError("wrong_image")
        if actual.get("manifest_digest") != archive["manifest_digest"]:
            raise OfflineApplyError("wrong_image")
        self._state(
            "verifying",
            manifest,
            "Verifying the signed offline OCI archive.",
            progress={"bytes": archive["size"], "total": archive["size"]},
        )

    def _retain_archive(self, manifest: Mapping[str, Any]) -> Path:
        """Keep a verified archive under its signed, immutable release name.

        ``payload.oci`` is the installer's replaceable handoff name.  bootc is
        given the named hardlink so a later repair cannot change bytes behind a
        retained deployment's image reference.
        """

        source = self.payload_path
        destination = self.retained_payload_path(manifest)
        self._check_regular(source, label="OCI archive", max_size=MAX_ARCHIVE_SIZE)
        try:
            existing = os.lstat(destination)
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise OfflineApplyError("artifact_invalid") from error
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if self.storage_check and (
                existing.st_uid != self._expected_uid() or existing.st_mode & 0o022
            ):
                raise OfflineApplyError("unsafe_storage")
            source_metadata = os.lstat(source)
            if (existing.st_dev, existing.st_ino) != (
                source_metadata.st_dev,
                source_metadata.st_ino,
            ):
                if existing.st_size != manifest["archive"]["size"]:
                    raise OfflineApplyError("artifact_tampered")
                # A previous run may have completed the link step before a
                # process interruption.  Preserve that immutable file only if
                # it still hashes to the signed archive bytes.
                self._hash_file(destination, manifest["archive"])
            return destination
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            return self._retain_archive(manifest)
        except OSError as error:
            raise OfflineApplyError("artifact_invalid") from error
        try:
            linked = os.lstat(destination)
            source_metadata = os.lstat(source)
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
                raise OfflineApplyError("unsafe_storage")
            if (linked.st_dev, linked.st_ino) != (
                source_metadata.st_dev,
                source_metadata.st_ino,
            ):
                raise OfflineApplyError("artifact_invalid")
            directory = os.open(
                self.offline_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OfflineApplyError:
            raise
        except OSError as error:
            raise OfflineApplyError("artifact_invalid") from error
        return destination

    def _command(self, args: list[str], *, timeout: int) -> Any:
        try:
            result = self.runner(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=COMMAND_ENV.copy(),
            )
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError) as error:
            raise OfflineApplyError("command_failed") from error
        if not hasattr(result, "returncode"):
            raise OfflineApplyError("command_failed")
        return result

    def _bootc_status(self) -> dict[str, Any]:
        result = self._command([BOOTC, "status", "--json"], timeout=30)
        stdout = getattr(result, "stdout", "")
        if result.returncode or not isinstance(stdout, str) or len(stdout) > JSON_LIMIT:
            raise OfflineApplyError("bootc_unavailable")
        try:
            decoded = json.loads(stdout)
            status = decoded["status"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, RecursionError) as error:
            raise OfflineApplyError("bootc_unavailable") from error
        if not isinstance(status, dict) or not status.get("booted"):
            raise OfflineApplyError("bootc_unavailable")
        if status.get("rollbackQueued"):
            raise OfflineApplyError("rollback_queued")
        return status

    @staticmethod
    def _staged_digest(status: Mapping[str, Any]) -> str | None:
        staged = status.get("staged")
        if staged is None:
            return None
        try:
            digest = staged["image"]["imageDigest"]
        except (KeyError, TypeError) as error:
            raise OfflineApplyError("invalid_staged") from error
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise OfflineApplyError("invalid_staged")
        return digest

    def _check_stage(self, manifest: Mapping[str, Any]) -> bool:
        digest = self._staged_digest(self._bootc_status())
        expected = manifest["archive"]["manifest_digest"]
        if digest is not None and digest != expected:
            raise OfflineApplyError("staged_update_exists")
        return digest is not None

    def _consume_pending(self) -> None:
        """Consume the install activation marker after a verified result."""

        if not self._validate_pending():
            return
        try:
            os.unlink(self.pending_path)
            directory = os.open(
                self.offline_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise OfflineApplyError("state_write_failed") from error

    def _validate_pending(self) -> bool:
        """Validate the optional activation marker without following links."""

        try:
            metadata = os.lstat(self.pending_path)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise OfflineApplyError("unsafe_storage") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OfflineApplyError("unsafe_storage")
        if self.storage_check and (
            metadata.st_uid != self._expected_uid() or metadata.st_mode & 0o022
        ):
            raise OfflineApplyError("unsafe_storage")
        self._check_ancestors(self.pending_path.parent)
        return True

    def _run_locked(self) -> bool:
        manifest: dict[str, Any] | None = None
        try:
            self._state("verifying", None, "Verifying the signed offline Zeus OS update.")
            self._validate_pending()
            trusted = self._load_verifier()
            manifest = self._verify_manifest(trusted)
            self._state("verifying", manifest, "Checking the installed Zeus OS identity.")
            current = self._current_identity()
            eligibility = self._eligibility(manifest, current)
            if eligibility == "done":
                # Same-build replays are successful no-ops.  The payload is
                # intentionally left in place for audit/recovery and no
                # bootc command is issued.
                self._state("done", manifest, "The offline update is already installed.")
                self._consume_pending()
                return True
            self._verify_archive(trusted, manifest)
            if self._check_stage(manifest):
                self._state(
                    "ready",
                    manifest,
                    "The verified offline update is ready. Restart when convenient.",
                )
                self._consume_pending()
                return True
            retained = self._retain_archive(manifest)
            self._state(
                "staging",
                manifest,
                "Installing the verified offline image for the next restart.",
            )
            result = self._command(
                [BOOTC, "switch", "--transport", "oci-archive", "--retain", str(retained)],
                timeout=1800,
            )
            if result.returncode:
                raise OfflineApplyError("staging_failed")
            if not self._check_stage(manifest):
                raise OfflineApplyError("staging_unconfirmed")
            self._state(
                "ready",
                manifest,
                "The verified offline update is ready. Restart when convenient.",
            )
            self._consume_pending()
            return True
        except OfflineApplyError as error:
            try:
                self._state("error", manifest, _message(error.code), error=error.code)
            except OfflineApplyError:
                # The original state-write failure is already fail-closed; a
                # second write attempt must never mask it or emit a traceback.
                pass
            return False
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            translated = OfflineApplyError("offline_apply_failed")
            try:
                self._state(
                    "error",
                    manifest,
                    _message(translated.code),
                    error=translated.code,
                )
            except OfflineApplyError:
                pass
            return False

    def run(self) -> bool:
        """Run once and persist a safe result for both updater consumers."""

        try:
            with self.lock():
                return self._run_locked()
        except OfflineApplyError as error:
            # Busy/unsafe lock failures happen before the lock is held.  A
            # status write here could race the native updater, so leave its
            # existing state untouched.  The systemd journal receives no
            # command output and the next explicit invocation can retry.
            self.last_state = {
                "schema_version": SCHEMA_VERSION,
                "phase": "error",
                "manifest": None,
                "message": _message(error.code),
                "error": error.code,
                "progress": None,
                "boot_id": self._boot_id(),
                "updated_at": self.clock(),
            }
            return False
        except (OSError, TypeError, ValueError, RuntimeError):
            self.last_state = None
            return False

    # Small aliases keep the service integration readable and make the class
    # convenient to call from a fixture without duplicating behavior.
    apply = run
    execute = run


# Compatibility spelling for callers that describe this as an applier.
OfflineApplier = OfflineUpdateRunner


def main(argv: list[str] | None = None) -> int:
    """Systemd entry point; no arguments and no stdout/stderr output."""

    if argv not in (None, []):
        return 2
    if os.geteuid() != 0:
        return 1
    return 0 if OfflineUpdateRunner().run() else 1


__all__ = [
    "ADJACENT_VERIFIER",
    "LOCK_NAME",
    "MANIFEST_NAME",
    "MANIFEST_PATH",
    "OFFLINE_ROOT",
    "OfflineApplier",
    "OfflineApplyError",
    "OfflineUpdateRunner",
    "PAYLOAD_NAME",
    "PAYLOAD_PATH",
    "PENDING_NAME",
    "PENDING_PATH",
    "SCHEMA_VERSION",
    "SHARE_ROOT",
    "SIGNATURE_NAME",
    "SIGNATURE_PATH",
    "SIGNERS_NAME",
    "SIGNERS_PATH",
    "STATUS_NAME",
    "UPDATER_ROOT",
    "UpdateError",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by systemd
    raise SystemExit(main())
