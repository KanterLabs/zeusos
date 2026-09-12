"""Transactional Developer Mode runtime for Zeus OS.

The unprivileged command has one useful operation of its own: bounded status
reading.  Every state-changing operation goes through ``pkexec`` to the
root-only helper below.  The helper accepts an artifact digest, derives the
calling UID from the authenticated process environment, and never accepts a
path or command supplied by the desktop process.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable, Iterator, Mapping

import developer_manifest as manifest


SCHEMA_VERSION = manifest.SCHEMA_VERSION
STATE_ROOT = Path("/var/lib/zeus/developer-mode")
ARTIFACT_ROOT = STATE_ROOT / "artifacts"
EXTENSIONS_ROOT = Path("/var/lib/extensions")
ACTIVE_EXTENSION = EXTENSIONS_ROOT / "zeus-developer.raw"
UPDATER_LOCK = Path("/var/lib/zeus/updater/operation.lock")
STATUS_FILENAME = "status.json"
ACTIVE_FILENAME = "active"
PREVIOUS_FILENAME = "previous"
PENDING_FILENAME = "pending.json"
ADMIN_COMMAND = "/usr/libexec/zeus-developer-admin"
PKEXEC_COMMAND = "/usr/bin/pkexec"
SYSTEMD_SYSEXT = "/usr/bin/systemd-sysext"
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin", "LANG": "C.UTF-8", "HOME": "/root"}

MAX_STATUS_BYTES = 64 * 1024
MAX_REF_BYTES = 256
MAX_SPOOL_FILE_BYTES = manifest.MAX_ARTIFACT_SIZE
MAX_OUTPUT_BYTES = 64 * 1024
UID_RE = re.compile(r"\A[0-9]{1,10}\Z")
HEX64_RE = manifest.HEX64_RE
REF_RE = HEX64_RE
SAFE_STATES = frozenset(
    {"disabled", "enabled", "active", "applying", "incompatible", "interrupted", "paused", "needs_attention"}
)


class DeveloperError(manifest.DeveloperError):
    """Runtime failures with stable, safe error codes."""


def _error(code: str, message: str) -> None:
    raise DeveloperError(code, message)


def _under_root(root: Path, absolute: Path) -> Path:
    if root == Path("/"):
        return absolute
    return root / absolute.relative_to("/")


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bounded_json_bytes(raw: bytes, *, limit: int, label: str) -> Any:
    if not isinstance(raw, bytes) or len(raw) > limit:
        _error("state_invalid", f"{label} is unavailable")
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError, MemoryError):
        _error("state_invalid", f"{label} is unavailable")
    return value


def _safe_path_components(path: Path) -> None:
    """Reject symlinked ancestors without resolving a user-controlled path."""

    if not path.is_absolute():
        _error("unsafe_storage", "Developer Mode storage path is not absolute")
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError:
            _error("unsafe_storage", "Developer Mode storage is unavailable")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _error("unsafe_storage", "Developer Mode storage path is unsafe")


def _lstat(path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        _error("state_invalid", "Developer Mode state is unavailable")
    except OSError:
        _error("state_invalid", "Developer Mode state is unavailable")


def _regular_file(path: Path, *, label: str, limit: int, writable: bool = False) -> os.stat_result:
    _safe_path_components(path)
    info = _lstat(path)
    assert info is not None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _error("unsafe_storage", f"{label} is not a regular file")
    if info.st_size > limit:
        _error("state_too_large", f"{label} is too large")
    if not writable and info.st_mode & 0o022:
        _error("unsafe_storage", f"{label} is writable by another user")
    return info


def _read_file(path: Path, *, label: str, limit: int, writable: bool = False) -> bytes:
    _regular_file(path, label=label, limit=limit, writable=writable)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(limit + 1)
    except (OSError, ValueError):
        _error("state_invalid", f"{label} is unavailable")
    if len(raw) > limit:
        _error("state_too_large", f"{label} is too large")
    return raw


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # Some fixture filesystems do not implement directory fsync.  The
        # atomic rename still provides the important visibility boundary.
        return


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    if not path.is_absolute():
        _error("unsafe_storage", "Developer Mode state path is not absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path_components(path)
    existing = _lstat(path, missing_ok=True)
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        _error("unsafe_storage", "Developer Mode state path is unsafe")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(name)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError:
        _error("state_write_failed", "Developer Mode state could not be saved")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _atomic_json(path: Path, value: Mapping[str, Any], mode: int = 0o600) -> None:
    try:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        _error("state_invalid", "Developer Mode state is invalid")
    if len(payload) > MAX_STATUS_BYTES:
        _error("state_too_large", "Developer Mode state is too large")
    _atomic_write(path, payload, mode)


def _remove_regular(path: Path, *, missing_ok: bool = True) -> None:
    info = _lstat(path, missing_ok=missing_ok)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _error("unsafe_storage", "Developer Mode state path is unsafe")
    try:
        path.unlink()
    except OSError:
        _error("state_write_failed", "Developer Mode state could not be removed")
    _fsync_directory(path.parent)


def _copy_file(source: Path, destination: Path, *, mode: int, limit: int) -> tuple[int, str]:
    """Copy one already selected file through a no-follow descriptor."""

    source_info = _regular_file(source, label="developer artifact", limit=limit, writable=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_path_components(destination)
    old = _lstat(destination, missing_ok=True)
    if old is not None and (stat.S_ISLNK(old.st_mode) or not stat.S_ISREG(old.st_mode)):
        _error("unsafe_storage", "Developer artifact storage is unsafe")
    temporary: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
        temporary = Path(name)
        os.fchmod(fd, mode)
        with os.fdopen(source_fd, "rb") as input_stream, os.fdopen(fd, "wb") as output_stream:
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                total += len(block)
                if total > limit:
                    _error("artifact_too_large", "developer artifact is too large")
                digest.update(block)
                output_stream.write(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if total != source_info.st_size:
            _error("artifact_changed", "developer artifact changed while it was copied")
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    except DeveloperError:
        raise
    except (OSError, ValueError):
        _error("artifact_unavailable", "developer artifact could not be copied")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return total, digest.hexdigest()


def resolve_calling_uid(
    environ: Mapping[str, str] | None = None,
    *,
    effective_uid: Callable[[], int] | None = None,
    real_uid: Callable[[], int] | None = None,
    lookup: Callable[[int], Any] | None = None,
) -> int:
    """Derive the authenticated actor without accepting a CLI UID.

    ``pkexec`` records the original caller in ``PKEXEC_UID``.  The value is
    accepted only as a decimal UID that resolves through the system passwd
    database.  A direct root service invocation is actor UID 0; an
    unprivileged process reports its real UID for read-only/test callers.
    """

    env = os.environ if environ is None else environ
    geteuid = effective_uid or os.geteuid
    getuid = real_uid or os.getuid
    try:
        effective = int(geteuid())
        real = int(getuid())
    except (TypeError, ValueError, OSError):
        _error("authorization_required", "Administrator authentication is required.")
    if effective != 0:
        return real
    raw = env.get("PKEXEC_UID")
    if raw is None:
        return 0
    if not isinstance(raw, str) or UID_RE.fullmatch(raw) is None:
        _error("authorization_required", "Administrator authentication is required.")
    uid = int(raw, 10)
    if uid < 0 or uid > 2**31 - 1:
        _error("authorization_required", "Administrator authentication is required.")
    try:
        (lookup or pwd.getpwuid)(uid)
    except (KeyError, OSError, TypeError, ValueError):
        _error("authorization_required", "Administrator authentication is required.")
    return uid


def user_home(uid: int, *, lookup: Callable[[int], Any] | None = None) -> Path:
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        _error("authorization_required", "Administrator authentication is required.")
    try:
        record = (lookup or pwd.getpwuid)(uid)
        home = Path(record.pw_dir)
    except (KeyError, OSError, TypeError, ValueError):
        _error("authorization_required", "Administrator authentication is required.")
    if not home.is_absolute() or str(home) in {"", "/"} or "\x00" in str(home):
        _error("unsafe_storage", "Developer artifact spool is unavailable")
    return home


def user_spool(uid: int, *, lookup: Callable[[int], Any] | None = None) -> Path:
    """The sole production artifact spool location for one authenticated UID."""

    # logind creates /run/user/<uid> with the caller's ownership.  Never
    # honor HOME or XDG_CACHE_HOME from a root helper.
    if lookup is not None:
        user_home(uid, lookup=lookup)
    return Path("/run/user") / str(uid) / "zeus-developer"


class ArtifactPaths:
    def __init__(self, directory: Path, manifest_path: Path, signature_path: Path, extension_path: Path):
        self.directory = directory
        self.manifest = manifest_path
        self.signature = signature_path
        self.extension = extension_path


class DeveloperRuntime:
    """Root-owned Developer Mode state machine.

    ``storage_check=False`` is intended only for isolated unit-test fixtures.
    All paths, runners, and the current base root remain injectable so failure
    and interruption behavior can be tested without a booted systemd host.
    """

    def __init__(
        self,
        state_root: str | os.PathLike[str] | None = None,
        *,
        root: str | os.PathLike[str] | None = None,
        os_root: str | os.PathLike[str] | None = None,
        artifact_root: str | os.PathLike[str] | None = None,
        extensions_root: str | os.PathLike[str] | None = None,
        extension_path: str | os.PathLike[str] | None = None,
        updater_lock: str | os.PathLike[str] | None = None,
        spool_root: str | os.PathLike[str] | None = None,
        signers: str | os.PathLike[str] = manifest.DEVELOPER_SIGNERS,
        component_policy: str | os.PathLike[str] | None = None,
        runner: Callable[..., Any] | None = None,
        signature_verifier: Callable[..., Mapping[str, Any]] | None = None,
        component_map: Mapping[str, Mapping[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        storage_check: bool = True,
        strict_confirmation: bool = True,
    ):
        if root is not None and os_root is None:
            os_root = root
        self.os_root = Path(os_root or "/")
        if not self.os_root.is_absolute():
            _error("unsafe_storage", "Developer Mode system root is not absolute")
        self.state_root = Path(state_root) if state_root is not None else _under_root(self.os_root, STATE_ROOT)
        self.artifact_root = Path(artifact_root) if artifact_root is not None else self.state_root / "artifacts"
        self.extensions_root = Path(extensions_root) if extensions_root is not None else _under_root(self.os_root, EXTENSIONS_ROOT)
        self.extension_path = Path(extension_path) if extension_path is not None else self.extensions_root / "zeus-developer.raw"
        self.updater_lock = Path(updater_lock) if updater_lock is not None else _under_root(self.os_root, UPDATER_LOCK)
        self.spool_root = Path(spool_root) if spool_root is not None else None
        signer_path = Path(signers)
        if signer_path == Path(manifest.DEVELOPER_SIGNERS) and self.os_root != Path("/"):
            signer_path = _under_root(self.os_root, signer_path)
        self.signers = signer_path
        self.runner = runner or subprocess.run
        self.signature_verifier = signature_verifier
        self.component_map = component_map
        self.component_policy = Path(component_policy) if component_policy is not None else _under_root(self.os_root, Path(manifest.COMPONENT_POLICY_PATH))
        self.now = now or _utc_now
        self.storage_check = bool(storage_check)
        self.strict_confirmation = bool(strict_confirmation)

    def _component_policy(self) -> Mapping[str, Mapping[str, Any]]:
        if self.component_map is None:
            self.component_map = manifest.load_component_map(self.component_policy)
        return self.component_map

    # ---- storage and references -------------------------------------------------

    def _dir(self, path: Path, *, mode: int, private: bool = False) -> None:
        if not path.is_absolute():
            _error("unsafe_storage", "Developer Mode storage path is not absolute")
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        _safe_path_components(path / ".placeholder")
        info = _lstat(path)
        assert info is not None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _error("unsafe_storage", "Developer Mode storage directory is unsafe")
        if private and info.st_mode & 0o077:
            _error("unsafe_storage", "Developer Mode private storage is not protected")
        if self.storage_check and (info.st_uid != 0 or info.st_mode & 0o022):
            _error("unsafe_storage", "Developer Mode storage is not root-owned")

    def prepare(self) -> None:
        self._dir(self.state_root, mode=0o755)
        self._dir(self.artifact_root, mode=0o700, private=True)
        self._dir(self.extensions_root, mode=0o755)
        self._dir(self.updater_lock.parent, mode=0o755)
        lock_info = _lstat(self.updater_lock, missing_ok=True)
        if lock_info is not None:
            if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
                _error("unsafe_storage", "Updater operation lock is unsafe")
            if lock_info.st_mode & 0o077 or (self.storage_check and lock_info.st_uid != 0):
                _error("unsafe_storage", "Updater operation lock is not protected")

    @contextlib.contextmanager
    def operation_lock(self, *, wait: bool = False) -> Iterator[None]:
        self.prepare()
        try:
            fd = os.open(self.updater_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            _error("unsafe_storage", "Updater operation lock is unavailable")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (self.storage_check and info.st_uid != 0):
                _error("unsafe_storage", "Updater operation lock is not protected")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if not wait else 0))
            except BlockingIOError:
                _error("busy", "Another update or Developer Mode operation is already running.")
            yield
        finally:
            os.close(fd)

    def _ref_path(self, name: str) -> Path:
        if name not in {ACTIVE_FILENAME, PREVIOUS_FILENAME}:
            raise ValueError(name)
        return self.state_root / name

    def _read_ref(self, name: str) -> str | None:
        path = self._ref_path(name)
        info = _lstat(path, missing_ok=True)
        if info is None:
            return None
        raw = _read_file(path, label=f"Developer Mode {name} reference", limit=MAX_REF_BYTES)
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            _error("state_invalid", "Developer Mode references are invalid")
        if value == "":
            return None
        if HEX64_RE.fullmatch(value) is None:
            _error("state_invalid", "Developer Mode references are invalid")
        return value

    def _write_ref(self, name: str, value: str | None) -> None:
        path = self._ref_path(name)
        if value is None:
            _remove_regular(path)
            return
        if HEX64_RE.fullmatch(value) is None:
            _error("state_invalid", "Developer Mode reference is invalid")
        _atomic_write(path, (value + "\n").encode("ascii"), 0o600)

    def _read_pending(self) -> dict[str, Any] | None:
        path = self.state_root / PENDING_FILENAME
        if _lstat(path, missing_ok=True) is None:
            return None
        value = _bounded_json_bytes(_read_file(path, label="Developer Mode transaction", limit=MAX_STATUS_BYTES), limit=MAX_STATUS_BYTES, label="Developer Mode transaction")
        if not isinstance(value, dict):
            _error("state_invalid", "Developer Mode transaction is invalid")
        allowed = {
            "schema_version", "new_digest", "old_digest", "old_previous",
            "enabled", "old_enabled", "actor_uid", "started_at",
        }
        if any(key not in allowed for key in value):
            _error("state_invalid", "Developer Mode transaction is invalid")
        if "schema_version" in value and value["schema_version"] != SCHEMA_VERSION:
            _error("state_invalid", "Developer Mode transaction is invalid")
        for key in ("new_digest", "old_digest", "old_previous", "enabled", "actor_uid"):
            if key not in value:
                _error("state_invalid", "Developer Mode transaction is invalid")
        for key in ("new_digest", "old_digest", "old_previous"):
            item = value[key]
            if item is not None and (not isinstance(item, str) or HEX64_RE.fullmatch(item) is None):
                _error("state_invalid", "Developer Mode transaction is invalid")
        if (
            not isinstance(value["enabled"], bool)
            or isinstance(value["actor_uid"], bool)
            or not isinstance(value["actor_uid"], int)
            or value["actor_uid"] < 0
            or value["actor_uid"] > 2**31 - 1
        ):
            _error("state_invalid", "Developer Mode transaction is invalid")
        if "old_enabled" in value and not isinstance(value["old_enabled"], bool):
            _error("state_invalid", "Developer Mode transaction is invalid")
        if "started_at" in value:
            started_at = value["started_at"]
            if not isinstance(started_at, str) or len(started_at) > 64:
                _error("state_invalid", "Developer Mode transaction is invalid")
        return value

    def _clear_pending(self) -> None:
        _remove_regular(self.state_root / PENDING_FILENAME)

    def _read_state_file(self) -> dict[str, Any] | None:
        path = self.state_root / STATUS_FILENAME
        if _lstat(path, missing_ok=True) is None:
            return None
        value = _bounded_json_bytes(_read_file(path, label="Developer Mode status", limit=MAX_STATUS_BYTES), limit=MAX_STATUS_BYTES, label="Developer Mode status")
        if not isinstance(value, dict):
            _error("state_invalid", "Developer Mode status is unavailable")
        return value

    # ---- spool and trust --------------------------------------------------------

    def _spool(self, uid: int) -> Path:
        path = self.spool_root if self.spool_root is not None else user_spool(uid)
        if self.spool_root is None and self.os_root != Path("/"):
            path = _under_root(self.os_root, path)
        if not path.is_absolute():
            _error("unsafe_spool", "Developer artifact spool is unavailable")
        _safe_path_components(path / ".placeholder")
        info = _lstat(path, missing_ok=True)
        if info is None:
            _error("unsafe_spool", "Developer artifact spool is unavailable")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _error("unsafe_spool", "Developer artifact spool is unavailable")
        if info.st_mode & 0o077:
            _error("unsafe_spool", "Developer artifact spool is not private")
        if self.storage_check and info.st_uid != uid:
            _error("unsafe_spool", "Developer artifact spool belongs to another user")
        return path

    @staticmethod
    def _candidate_paths(spool: Path, digest: str) -> list[ArtifactPaths]:
        # The digest directory and these three fixed names are the complete
        # installed interface.  In particular, there is no caller-supplied
        # path or flat filename fallback.
        directory = spool / digest
        candidates = [
            ArtifactPaths(directory, directory / "manifest.json", directory / "manifest.json.sig", directory / "extension.raw"),
        ]
        return candidates

    def _artifact_paths(self, uid: int, digest: str) -> ArtifactPaths:
        if HEX64_RE.fullmatch(digest) is None:
            _error("invalid_digest", "Developer artifact digest must be 64 lowercase hexadecimal characters.")
        spool = self._spool(uid)
        for candidate in self._candidate_paths(spool, digest):
            directory_info = _lstat(candidate.directory, missing_ok=True)
            if directory_info is not None:
                if (
                    stat.S_ISLNK(directory_info.st_mode)
                    or not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_mode & 0o077
                    or (self.storage_check and directory_info.st_uid != uid)
                ):
                    _error("unsafe_spool", "Developer artifact directory is not private")
            present = [_lstat(path, missing_ok=True) is not None for path in (candidate.manifest, candidate.signature, candidate.extension)]
            if any(present):
                if not all(present):
                    _error("artifact_incomplete", "Developer artifact is incomplete.")
                # Every candidate path is generated from the digest and fixed
                # names.  Check ancestors before opening any user-owned file.
                for path in (candidate.manifest, candidate.signature, candidate.extension):
                    _safe_path_components(path)
                # A digest directory is a bounded one-artifact spool.  Extra
                # names, links, and devices are rejected before any content is
                # copied, even though they are not selected by the helper.
                try:
                    children = list(candidate.directory.iterdir())
                except OSError:
                    _error("unsafe_spool", "Developer artifact spool is unavailable")
                allowed = {"extension.raw", "manifest.json", "manifest.json.sig"}
                if any(child.name not in allowed for child in children):
                    _error("unsafe_spool", "Developer artifact spool contains an unexpected file")
                for child in children:
                    child_info = _lstat(child)
                    if child_info is None or stat.S_ISLNK(child_info.st_mode) or not stat.S_ISREG(child_info.st_mode):
                        _error("unsafe_spool", "Developer artifact spool contains an unsafe file")
                return candidate
        _error("artifact_missing", "Developer artifact is unavailable.")

    def _prepared_digest(self, uid: int) -> str:
        """Read the one bounded, per-user selection used by UI ``apply``."""

        path = self._spool(uid) / "prepared.json"
        raw = _read_file(path, label="Developer prepared selection", limit=MAX_STATUS_BYTES, writable=True)
        value = _bounded_json_bytes(raw, limit=MAX_STATUS_BYTES, label="Developer prepared selection")
        if not isinstance(value, dict):
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        # The bundle worker may include descriptive paths and provenance in
        # this pointer.  They are accepted only when they name the canonical
        # digest directory; activation still derives all bytes from the UID
        # and digest below and never opens a pointer-supplied path.
        allowed = {
            "schema_version", "artifact_kind", "digest", "artifact_digest",
            "extension", "manifest", "signature", "source_commit",
        }
        if any(key not in allowed for key in value):
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        if "schema_version" in value and value["schema_version"] != SCHEMA_VERSION:
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        if "artifact_kind" in value and value["artifact_kind"] != manifest.ARTIFACT_KIND:
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        digest = value.get("digest")
        alternate = value.get("artifact_digest")
        if digest is None:
            digest = alternate
        elif alternate is not None and alternate != digest:
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        if not isinstance(digest, str) or HEX64_RE.fullmatch(digest) is None:
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        directory = self._spool(uid) / digest
        for field, filename in (("extension", "extension.raw"), ("manifest", "manifest.json"), ("signature", "manifest.json.sig")):
            selected = value.get(field)
            if selected is None:
                continue
            if not isinstance(selected, str) or len(selected.encode("utf-8", "ignore")) > 1024:
                _error("prepared_invalid", "Developer prepared selection is invalid.")
            if selected != os.fspath(directory / filename):
                _error("prepared_invalid", "Developer prepared selection is invalid.")
        source_commit = value.get("source_commit")
        if source_commit is not None and (not isinstance(source_commit, str) or manifest.HEX40_RE.fullmatch(source_commit) is None):
            _error("prepared_invalid", "Developer prepared selection is invalid.")
        return digest

    def _verify_signature(self, raw: bytes, signature: bytes) -> Mapping[str, Any]:
        try:
            if self.signature_verifier is not None:
                try:
                    value = self.signature_verifier(raw, signature, self.signers, self.runner, self.component_map)
                except TypeError:
                    try:
                        value = self.signature_verifier(raw, signature, self.signers, self.runner)
                    except TypeError:
                        value = self.signature_verifier(raw, signature, self.signers)
                try:
                    return manifest.validate_manifest(value, component_map=self._component_policy())
                except manifest.DeveloperError as error:
                    # Test/integration verifiers commonly return the already
                    # normalised manifest produced by ``validate_manifest``.
                    # Its runtime-only ``component_names`` field is never in
                    # the signed JSON and must not be persisted as a receipt.
                    # Revalidate that canonical result after removing only
                    # this derived field; production detached verification
                    # above still validates the original signed bytes.
                    if error.code == "manifest_unknown_field" and isinstance(value, Mapping) and "component_names" in value:
                        candidate = dict(value)
                        candidate.pop("component_names", None)
                        # Normalised manifests flatten grouped components to
                        # one file record. Re-wrap those records before the
                        # strict parser sees them; the signed JSON contract
                        # itself remains unchanged.
                        normalized_components = candidate.get("components")
                        if isinstance(normalized_components, list) and all(
                            isinstance(item, Mapping) and "files" not in item for item in normalized_components
                        ):
                            groups: list[dict[str, Any]] = []
                            for item in normalized_components:
                                file_value = {
                                    key: item[key]
                                    for key in item
                                    if key not in {"name", "activation"}
                                }
                                groups.append(
                                    {
                                        "name": item.get("name"),
                                        "activation": item.get("activation"),
                                        "files": [file_value],
                                    }
                                )
                            candidate["components"] = groups
                        return manifest.validate_manifest(candidate, component_map=self._component_policy())
                    raise
            value = manifest.verify_manifest_signature(raw, signature, self.signers, runner=self.runner, component_map=self._component_policy())
            return value
        except manifest.DeveloperError as error:
            if isinstance(error, DeveloperError):
                raise
            raise DeveloperError(error.code, str(error)) from error

    def _verify_component_policy_binding(self, value: Mapping[str, Any]) -> None:
        """Bind a signed policy digest to the installed, validated map.

        Component paths are checked against the parsed map independently.  If
        a bundle also carries the builder's policy digest, require that digest
        to identify the exact policy bytes used by this image.
        """

        declared = value.get("component_policy_sha256")
        if declared is None:
            return
        if not isinstance(declared, str) or manifest.HEX64_RE.fullmatch(declared) is None:
            _error("component_map_mismatch", "Developer component policy identity is invalid.")
        try:
            _size, actual = manifest.hash_file(self.component_policy, limit=512 * 1024)
        except manifest.DeveloperError as error:
            raise DeveloperError("component_map_invalid", "Developer component policy is unavailable.") from error
        if not hmac.compare_digest(actual, declared):
            _error("component_map_mismatch", "Developer artifact was built against a different component policy.")

    def _verify_spooled_artifact(self, uid: int, digest: str, base_level: str | None) -> tuple[dict[str, Any], ArtifactPaths]:
        paths = self._artifact_paths(uid, digest)
        raw = _read_file(paths.manifest, label="Developer manifest", limit=manifest.MAX_MANIFEST_BYTES, writable=True)
        signature = _read_file(paths.signature, label="Developer manifest signature", limit=manifest.MAX_SIGNATURE_BYTES, writable=True)
        verified = dict(self._verify_signature(raw, signature))
        self._verify_component_policy_binding(verified)
        # Keep the bytes authenticated at the first read attached to this
        # operation.  The per-user spool remains writable, so the copy step
        # must detect a manifest or signature replacement between verification
        # and storage just as it detects an extension replacement.
        verified["_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        verified["_signature_sha256"] = hashlib.sha256(signature).hexdigest()
        if verified["artifact_sha256"] != digest:
            _error("digest_mismatch", "Developer artifact digest does not match its signed manifest.")
        if base_level is None:
            _error("base_unavailable", "The installed base SYSEXT_LEVEL could not be verified.")
        # Verify the full identity before copying anything into the root-owned
        # content-addressed store.  ``base_level`` is retained as an explicit
        # argument for callers that inject a base fixture, while the helper
        # independently reads all fields published by the installed image.
        self._ensure_base_compatible(verified)
        try:
            manifest.verify_artifact(paths.extension, verified, digest=digest)
        except manifest.DeveloperError as error:
            if isinstance(error, DeveloperError):
                raise
            raise DeveloperError(error.code, str(error)) from error
        return verified, paths

    def _store_bundle(self, digest: str, paths: ArtifactPaths, verified: Mapping[str, Any]) -> Path:
        destination = self.artifact_root / digest
        self._dir(destination, mode=0o700, private=True)
        stored_manifest = destination / "manifest.json"
        stored_signature = destination / "manifest.json.sig"
        stored_extension = destination / "extension.raw"
        for source, target, mode, limit in (
            (paths.manifest, stored_manifest, 0o600, manifest.MAX_MANIFEST_BYTES),
            (paths.signature, stored_signature, 0o600, manifest.MAX_SIGNATURE_BYTES),
            (paths.extension, stored_extension, 0o444, manifest.MAX_ARTIFACT_SIZE),
        ):
            if _lstat(target, missing_ok=True) is None:
                size, actual = _copy_file(source, target, mode=mode, limit=limit)
                expected_source_digest = (
                    verified.get("_manifest_sha256") if target == stored_manifest else
                    verified.get("_signature_sha256") if target == stored_signature else
                    digest
                )
                if isinstance(expected_source_digest, str) and not hmac.compare_digest(actual, expected_source_digest):
                    _error("artifact_changed", "Developer artifact metadata changed while it was stored.")
                if target == stored_extension:
                    if size != verified["artifact"]["size"] or not hmac.compare_digest(actual, digest):
                        _error("artifact_hash_mismatch", "Developer artifact changed while it was stored.")
            else:
                existing = _lstat(target)
                if existing is None or stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                    _error("unsafe_storage", "Developer artifact storage is unsafe")
                # Existing content-addressed entries must be byte-identical;
                # never overwrite a root-owned artifact with a new payload.
                try:
                    source_size, source_digest = manifest.hash_file(source, limit=limit)
                    size, actual = manifest.hash_file(target, limit=limit)
                except manifest.DeveloperError as error:
                    raise DeveloperError(error.code, str(error)) from error
                expected_source_digest = (
                    verified.get("_manifest_sha256") if target == stored_manifest else
                    verified.get("_signature_sha256") if target == stored_signature else
                    digest
                )
                if (
                    size != source_size
                    or not hmac.compare_digest(actual, source_digest)
                    or (isinstance(expected_source_digest, str) and not hmac.compare_digest(source_digest, expected_source_digest))
                ):
                    _error("artifact_collision", "Developer artifact storage contains conflicting data.")
                if target == stored_extension and not hmac.compare_digest(actual, digest):
                    _error("artifact_collision", "Developer artifact storage contains conflicting data.")
        # Reinspect the copied image so a user spool race cannot affect the
        # bytes that will be activated.
        try:
            manifest.verify_artifact(stored_extension, verified, digest=digest)
        except manifest.DeveloperError as error:
            if isinstance(error, DeveloperError):
                raise
            raise DeveloperError(error.code, str(error)) from error
        return destination

    def _stored_manifest(self, digest: str) -> dict[str, Any]:
        if HEX64_RE.fullmatch(digest) is None:
            _error("state_invalid", "Developer Mode reference is invalid")
        path = self.artifact_root / digest / "manifest.json"
        signature_path = self.artifact_root / digest / "manifest.json.sig"
        raw = _read_file(path, label="stored Developer manifest", limit=manifest.MAX_MANIFEST_BYTES)
        signature = _read_file(signature_path, label="stored Developer manifest signature", limit=manifest.MAX_SIGNATURE_BYTES)
        try:
            # Stored metadata is root-owned, but it originated in a
            # user-writable spool. Re-run detached SSH verification on every
            # read so a race or later tamper cannot turn unsigned metadata into
            # trusted boot/status provenance.
            value = dict(self._verify_signature(raw, signature))
        except manifest.DeveloperError as error:
            if isinstance(error, DeveloperError):
                raise
            raise DeveloperError(error.code, str(error)) from error
        if value["artifact_sha256"] != digest:
            _error("state_invalid", "Stored Developer artifact identity is invalid")
        self._verify_component_policy_binding(value)
        extension = self.artifact_root / digest / "extension.raw"
        try:
            manifest.verify_artifact(extension, value, digest=digest)
        except manifest.DeveloperError as error:
            if isinstance(error, DeveloperError):
                raise
            raise DeveloperError(error.code, str(error)) from error
        return value

    # ---- public status ----------------------------------------------------------

    @staticmethod
    def _status_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
        components = value.get("component_names", [])
        if not isinstance(components, list):
            components = []
        components = [item for item in components if isinstance(item, str)][:manifest.MAX_COMPONENTS]
        tests: list[Any] = []
        for item in value.get("focused_tests", []):
            if isinstance(item, str):
                tests.append(item[:256])
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                tests.append({"name": item["name"][:256], "result": "passed"})
        base = value.get("base") if isinstance(value.get("base"), Mapping) else {}
        return {
            "source_commit": value.get("source_commit"),
            "components": sorted(set(components)),
            "required_activation": list(value.get("activation_actions", []))[:32],
            "focused_tests": tests[:manifest.MAX_TESTS],
            "base_id": base.get("id"),
            "base_version_id": base.get("version_id"),
            "base_architecture": base.get("architecture"),
            "base_image_digest": base.get("image_digest"),
            "base_sysext_level": base.get("sysext_level"),
        }

    def _write_status(
        self,
        state: str,
        *,
        enabled: bool,
        active_digest: str | None = None,
        previous_digest: str | None = None,
        value: Mapping[str, Any] | None = None,
        actor_uid: int | None = None,
        error: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        if state not in SAFE_STATES:
            _error("state_invalid", "Developer Mode state is invalid")
        if active_digest is not None and HEX64_RE.fullmatch(active_digest) is None:
            _error("state_invalid", "Developer Mode active reference is invalid")
        if previous_digest is not None and HEX64_RE.fullmatch(previous_digest) is None:
            _error("state_invalid", "Developer Mode previous reference is invalid")
        if actor_uid is not None and (isinstance(actor_uid, bool) or not isinstance(actor_uid, int) or actor_uid < 0):
            _error("state_invalid", "Developer Mode actor is invalid")
        metadata = self._status_metadata(value or {})
        current_identity = self._base_identity()
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "enabled": bool(enabled),
            "state": state,
            "base_id": current_identity.get("id"),
            "base_version_id": current_identity.get("version_id"),
            "base_architecture": current_identity.get("architecture"),
            "base_image_digest": current_identity.get("image_digest"),
            "base_sysext_level": current_identity.get("sysext_level"),
            "active_digest": active_digest,
            "previous_digest": previous_digest,
            "source_commit": metadata["source_commit"] if isinstance(metadata["source_commit"], str) else None,
            "components": metadata["components"],
            "required_activation": metadata["required_activation"],
            "focused_tests": metadata["focused_tests"],
            "applied_at": self.now() if state == "active" else None,
            "actor_uid": actor_uid,
            "error": error if isinstance(error, str) and len(error) <= 64 else None,
            "message": message if isinstance(message, str) and len(message) <= 256 else None,
        }
        # Stable aliases are intentionally duplicated and bounded for older
        # Settings/doctor clients.  They carry no extra authority.
        payload["base_build"] = payload["base_sysext_level"]
        payload["base_build_id"] = payload["base_sysext_level"]
        payload["active_commit"] = payload["source_commit"]
        payload["artifact_digest"] = payload["active_digest"]
        first_action = payload["required_activation"][0] if payload["required_activation"] else "none"
        payload["required_action"] = (
            "logout" if first_action in {"logout-login", "logout"} else
            "restart" if first_action.startswith("restart") else
            "reboot" if first_action in {"reboot", "restart-system"} else "none"
        )
        payload["focused_test_receipt"] = payload["focused_tests"]
        _atomic_json(self.state_root / STATUS_FILENAME, payload, 0o644)
        return payload

    def public_status(self, *, deep: bool | None = None) -> dict[str, Any]:
        """Return bounded status data without invoking a privileged command.

        A normal desktop process can read the public status receipt but cannot
        traverse the root-only artifact store or its 0600 references.  Root
        callers keep the deeper consistency checks by default; tests and
        callers that need the public boundary can explicitly pass ``deep=False``.
        """

        if deep is None:
            try:
                deep = os.geteuid() == 0
            except OSError:
                deep = False
        try:
            stored = self._read_state_file()
            base_identity = self._base_identity()
            base_level = base_identity.get("sysext_level")
            if stored is None:
                active = self._read_ref(ACTIVE_FILENAME) if deep else None
                previous = self._read_ref(PREVIOUS_FILENAME) if deep else None
                result = {
                    "ok": True,
                    "schema_version": SCHEMA_VERSION,
                    "enabled": False,
                    "state": "disabled",
                    "base_id": base_identity.get("id"),
                    "base_version_id": base_identity.get("version_id"),
                    "base_architecture": base_identity.get("architecture"),
                    "base_image_digest": base_identity.get("image_digest"),
                    "base_sysext_level": base_level,
                    "active_digest": active,
                    "previous_digest": previous,
                    "source_commit": None,
                    "components": [],
                    "required_activation": [],
                    "focused_tests": [],
                    "applied_at": None,
                    "actor_uid": None,
                    "error": None,
                    "message": "Developer Mode is off.",
                }
                result.update({
                    "base_build": base_level,
                    "base_build_id": base_level,
                    "active_commit": None,
                    "artifact_digest": active,
                    "required_action": "none",
                    "focused_test_receipt": [],
                })
                return result
            enabled = stored.get("enabled") is True
            state = stored.get("state") if stored.get("state") in SAFE_STATES else "needs_attention"
            if deep:
                active = self._read_ref(ACTIVE_FILENAME)
                previous = self._read_ref(PREVIOUS_FILENAME)
            else:
                active = stored.get("active_digest")
                previous = stored.get("previous_digest")
                for value in (active, previous):
                    if value is not None and (not isinstance(value, str) or HEX64_RE.fullmatch(value) is None):
                        _error("state_invalid", "Developer Mode status references are invalid")
            metadata = {
                "source_commit": stored.get("source_commit") if isinstance(stored.get("source_commit"), str) else None,
                "components": stored.get("components") if isinstance(stored.get("components"), list) else [],
                "required_activation": stored.get("required_activation") if isinstance(stored.get("required_activation"), list) else [],
                "focused_tests": stored.get("focused_tests") if isinstance(stored.get("focused_tests"), list) else [],
                "base_sysext_level": stored.get("base_sysext_level") if isinstance(stored.get("base_sysext_level"), str) else base_level,
                "base_id": stored.get("base_id") if isinstance(stored.get("base_id"), str) else base_identity.get("id"),
                "base_version_id": stored.get("base_version_id") if isinstance(stored.get("base_version_id"), str) else base_identity.get("version_id"),
                "base_architecture": stored.get("base_architecture") if isinstance(stored.get("base_architecture"), str) else base_identity.get("architecture"),
                "base_image_digest": stored.get("base_image_digest") if isinstance(stored.get("base_image_digest"), str) else base_identity.get("image_digest"),
            }
            if deep and active is not None:
                try:
                    value = self._stored_manifest(active)
                    metadata = self._status_metadata(value)
                    try:
                        self._ensure_base_compatible(value, identity=base_identity)
                    except DeveloperError as mismatch:
                        if mismatch.code != "base_mismatch":
                            raise
                        state = "incompatible"
                    else:
                        if state not in {"applying", "interrupted", "paused", "needs_attention"}:
                            state = "active"
                except DeveloperError:
                    state = "needs_attention"
            elif not deep:
                # The root-owned status file is the authority for public
                # display.  Do not replace its state with a private ref check.
                pass
            elif enabled and state == "active":
                state = "enabled"
            elif not enabled and state != "paused":
                state = "disabled"
            result = {
                "ok": state not in {"needs_attention"},
                "schema_version": SCHEMA_VERSION,
                "enabled": enabled,
                "state": state,
                "base_id": base_identity.get("id"),
                "base_version_id": base_identity.get("version_id"),
                "base_architecture": base_identity.get("architecture"),
                "base_image_digest": base_identity.get("image_digest"),
                "base_sysext_level": base_level,
                "active_digest": active,
                "previous_digest": previous,
                "source_commit": metadata["source_commit"] if isinstance(metadata["source_commit"], str) else None,
                "components": sorted({item for item in metadata["components"] if isinstance(item, str)})[:manifest.MAX_COMPONENTS],
                "required_activation": [item for item in metadata["required_activation"] if isinstance(item, str)][:32],
                "focused_tests": metadata["focused_tests"][:manifest.MAX_TESTS],
                "applied_at": stored.get("applied_at") if isinstance(stored.get("applied_at"), str) else None,
                "actor_uid": stored.get("actor_uid") if isinstance(stored.get("actor_uid"), int) and not isinstance(stored.get("actor_uid"), bool) else None,
                "error": stored.get("error") if isinstance(stored.get("error"), str) else None,
                "message": stored.get("message") if isinstance(stored.get("message"), str) else None,
            }
            result.update({
                "base_build": base_level,
                "base_build_id": base_level,
                "active_commit": result["source_commit"],
                "artifact_digest": active,
                "required_action": (
                    "logout" if result["required_activation"] and result["required_activation"][0] in {"logout-login", "logout"} else
                    "restart" if result["required_activation"] and result["required_activation"][0].startswith("restart") else
                    "reboot" if result["required_activation"] and result["required_activation"][0] in {"reboot", "restart-system"} else "none"
                ),
                "focused_test_receipt": result["focused_tests"],
            })
            return result
        except DeveloperError as error:
            result = {
                "ok": False,
                "schema_version": SCHEMA_VERSION,
                "enabled": False,
                "state": "needs_attention",
                "base_id": self._base_identity().get("id"),
                "base_version_id": self._base_identity().get("version_id"),
                "base_architecture": self._base_identity().get("architecture"),
                "base_image_digest": self._base_identity().get("image_digest"),
                "base_sysext_level": self._base_identity().get("sysext_level"),
                "active_digest": None,
                "previous_digest": None,
                "source_commit": None,
                "components": [],
                "required_activation": [],
                "focused_tests": [],
                "applied_at": None,
                "actor_uid": None,
                "error": error.code,
                "message": "Developer Mode status is unavailable.",
            }
            result.update({
                "base_build": result["base_sysext_level"],
                "base_build_id": result["base_sysext_level"],
                "active_commit": None,
                "artifact_digest": None,
                "required_action": "none",
                "focused_test_receipt": [],
            })
            return result

    # ---- systemd-sysext ---------------------------------------------------------

    def _command(self, args: list[str], *, timeout: int = 60) -> Any:
        try:
            return self.runner(args, capture_output=True, text=True, check=False, timeout=timeout, env=dict(SAFE_ENV))
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            _error("command_failed", "Developer Mode activation command failed.")

    @staticmethod
    def _output(result: Any) -> str:
        value = getattr(result, "stdout", "")
        if isinstance(value, bytes):
            value = value[: MAX_OUTPUT_BYTES + 1].decode("utf-8", "replace")
        if not isinstance(value, str) or len(value.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
            _error("command_failed", "Developer Mode activation command returned unsafe output.")
        return value

    def _confirm_merge(self, result: Any, expected_digest: str | None, expected_level: str | None) -> None:
        if getattr(result, "returncode", 1) != 0:
            _error("sysext_status_failed", "systemd-sysext did not confirm the Developer Mode state.")
        output = self._output(result).strip()
        if output:
            lowered = output.lower()
            try:
                decoded = json.loads(output)
            except (json.JSONDecodeError, TypeError, ValueError):
                decoded = None
            encoded = json.dumps(decoded, sort_keys=True).lower() if decoded is not None else lowered
            marker = "zeus-developer" in encoded
            failure_words = ("failed", "error", "inactive", "unmerged", "bad")
            absence_words = ("none", "inactive", "unmerged", "absent", "not found", "not_found")
            if expected_digest is not None:
                # systemd-sysext currently reports a list whose ``extensions``
                # value is often ``none`` or ``zeus-developer.raw``.  Require
                # the fixed extension marker itself; accepting a generic
                # successful command would leave activation unconfirmed.
                if not marker or any(word in encoded and marker for word in failure_words):
                    _error("merge_unconfirmed", "systemd-sysext did not confirm the Developer Mode merge.")
            elif marker and not any(word in encoded for word in absence_words):
                # A pause/disable operation must also prove that the fixed
                # extension disappeared from the merged hierarchy.
                _error("merge_unconfirmed", "systemd-sysext did not confirm the Developer Mode unmerge.")
        elif self.strict_confirmation and self.runner is subprocess.run:
            # Real systemd-sysext emits status output.  Empty output is only
            # tolerated for injected fixture runners used by unit tests.
            _error("merge_unconfirmed", "systemd-sysext did not confirm the Developer Mode merge.")
        release = self.os_root / "usr/lib/extension-release.d/extension-release.zeus-developer"
        if _lstat(release, missing_ok=True) is not None and expected_level is not None:
            raw = _read_file(release, label="Developer extension identity", limit=16 * 1024)
            found = None
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("SYSEXT_LEVEL="):
                    found = line.partition("=")[2].strip().strip("\"'")
                    break
            if found != expected_level:
                _error("base_mismatch", "The merged Developer Mode extension has the wrong base identity.")

    def _refresh(self, *, expected_digest: str | None, expected_level: str | None) -> None:
        refreshed = self._command([SYSTEMD_SYSEXT, "refresh"], timeout=120)
        if getattr(refreshed, "returncode", 1) != 0:
            _error("sysext_refresh_failed", "systemd-sysext could not refresh the Developer Mode extension.")
        status = self._command([SYSTEMD_SYSEXT, "status", "--json=short"], timeout=60)
        self._confirm_merge(status, expected_digest, expected_level)

    def _active_file_matches(self, digest: str) -> bool:
        info = _lstat(self.extension_path, missing_ok=True)
        if info is None:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _error("unsafe_storage", "Active Developer extension is unsafe")
        try:
            _size, actual = manifest.hash_file(self.extension_path, limit=manifest.MAX_ARTIFACT_SIZE)
        except manifest.DeveloperError as error:
            raise DeveloperError(error.code, str(error)) from error
        return hmac.compare_digest(actual, digest)

    def _activate_stored(self, digest: str) -> None:
        source = self.artifact_root / digest / "extension.raw"
        if _lstat(source, missing_ok=True) is None:
            _error("artifact_missing", "Stored Developer artifact is unavailable.")
        _copy_file(source, self.extension_path, mode=0o444, limit=manifest.MAX_ARTIFACT_SIZE)
        # _copy_file opens the destination atomically; verify its final bytes
        # before asking systemd to merge it.
        try:
            size, actual = manifest.hash_file(self.extension_path, limit=manifest.MAX_ARTIFACT_SIZE)
        except manifest.DeveloperError as error:
            raise DeveloperError(error.code, str(error)) from error
        if actual != digest:
            _error("artifact_hash_mismatch", "Active Developer extension has the wrong digest.")
        value = self._stored_manifest(digest)
        if size != value["artifact"]["size"]:
            _error("artifact_hash_mismatch", "Active Developer extension has the wrong size.")

    def _remove_active_extension(self) -> None:
        _remove_regular(self.extension_path)

    # ---- transactions -----------------------------------------------------------

    def _base_level(self) -> str | None:
        return manifest.read_base_sysext_level(self.os_root)

    def _base_identity(self) -> Mapping[str, str | None]:
        return manifest.read_base_identity(self.os_root)

    def _ensure_base_compatible(
        self,
        value: Mapping[str, Any],
        *,
        identity: Mapping[str, str | None] | None = None,
    ) -> None:
        """Require every identity field published by an artifact to match.

        ``SYSEXT_LEVEL`` is the systemd-sysext gate.  The other fields are
        checked when the installed base publishes them, and a declared image
        digest is intentionally fail-closed when the image cannot provide a
        corresponding value.  Test roots may contain only SYSEXT_LEVEL, so
        absent optional fields remain compatible for those isolated fixtures.
        """

        base = value.get("base")
        if not isinstance(base, Mapping):
            _error("base_mismatch", "Developer artifact has no valid base identity.")
        current = identity or self._base_identity()
        expected_level = base.get("sysext_level")
        current_level = current.get("sysext_level")
        if not isinstance(expected_level, str) or current_level is None or expected_level != current_level:
            _error("base_mismatch", "Developer artifact was built for a different base image.")

        comparisons = (
            ("id", base.get("id"), current.get("id")),
            ("version_id", base.get("version_id"), current.get("version_id")),
            ("architecture", base.get("architecture"), current.get("architecture")),
            ("image_digest", base.get("image_digest"), current.get("image_digest")),
        )
        for field, expected, observed in comparisons:
            if expected is None:
                continue
            if observed is None or expected != observed:
                _error("base_mismatch", "Developer artifact was built for a different base image.")

    @staticmethod
    def _pending_old_enabled(pending: Mapping[str, Any]) -> bool:
        """Return the enabled state that existed before a journaled action.

        ``old_enabled`` was added after the first transaction format shipped.
        Keep reading older pending files: an existing old digest proves that
        Developer Mode was enabled, and otherwise the legacy intended state is
        the only bounded value available.
        """

        old_digest = pending.get("old_digest")
        value = pending.get("old_enabled")
        if isinstance(value, bool):
            return True if old_digest is not None else value
        return old_digest is not None or bool(pending.get("enabled"))

    def _prior_enabled(self, old_digest: str | None) -> bool:
        """Capture the state to restore if a switch cannot complete."""

        # A validated active reference is the strongest indication: an active
        # extension means the mode was enabled before the transaction began.
        if old_digest is not None:
            return True
        state = self._read_state_file()
        if state is None:
            return False
        enabled = state.get("enabled")
        if not isinstance(enabled, bool):
            _error("state_invalid", "Developer Mode status is invalid")
        return enabled

    def _pending_payload(
        self,
        new_digest: str | None,
        old_digest: str | None,
        old_previous: str | None,
        enabled: bool,
        actor_uid: int,
        *,
        old_enabled: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "new_digest": new_digest,
            "old_digest": old_digest,
            "old_previous": old_previous,
            "enabled": enabled,
            # Keep ``enabled`` as the post-operation value for compatibility
            # with the original journal format, while recording the value that
            # must be restored if this transaction fails or is interrupted.
            "old_enabled": enabled if old_enabled is None else old_enabled,
            "actor_uid": actor_uid,
            "started_at": self.now(),
        }

    def _restore_locked(self, old_digest: str | None, old_level: str | None) -> None:
        if old_digest is None:
            self._remove_active_extension()
        else:
            self._activate_stored(old_digest)
        self._refresh(expected_digest=old_digest, expected_level=old_level)

    def _recover_pending_locked(self, pending: Mapping[str, Any]) -> dict[str, Any]:
        old_digest = pending.get("old_digest")
        old_previous = pending.get("old_previous")
        old_enabled = self._pending_old_enabled(pending)
        old_value = self._stored_manifest(old_digest) if old_digest is not None else None
        level = self._base_level()
        try:
            self._restore_locked(old_digest, level if old_value is None else old_value["base"]["sysext_level"])
            self._write_ref(ACTIVE_FILENAME, old_digest)
            self._write_ref(PREVIOUS_FILENAME, old_previous)
            restored_state = "active" if old_digest is not None else ("enabled" if old_enabled else "disabled")
            result = self._write_status(
                restored_state,
                enabled=old_enabled,
                active_digest=old_digest,
                previous_digest=old_previous,
                value=old_value,
                actor_uid=pending.get("actor_uid"),
                error="interrupted",
                message="The previous Developer Mode operation was interrupted; the last working state was restored.",
            )
            self._clear_pending()
            return result
        except DeveloperError:
            self._write_status(
                "needs_attention",
                enabled=old_enabled,
                active_digest=old_digest,
                previous_digest=old_previous,
                value=old_value,
                actor_uid=pending.get("actor_uid"),
                error="restore_failed",
                message="Developer Mode needs attention before another change can be applied.",
            )
            raise

    def _switch_locked(
        self,
        new_digest: str | None,
        *,
        enabled_after: bool,
        actor_uid: int,
        new_value: Mapping[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        old_digest = self._read_ref(ACTIVE_FILENAME)
        old_previous = self._read_ref(PREVIOUS_FILENAME)
        old_value = self._stored_manifest(old_digest) if old_digest is not None else None
        old_enabled = self._prior_enabled(old_digest)
        level = self._base_level()
        if new_value is not None:
            self._ensure_base_compatible(new_value)
        # A missing active reference does not prove that the fixed extension
        # path is absent (for example, a killed helper can leave the path
        # behind before publishing its reference).  Only skip the transaction
        # when the requested base state is actually present on disk.
        active_is_absent = new_digest is None and _lstat(self.extension_path, missing_ok=True) is None
        if old_digest == new_digest and active_is_absent:
            result = self._write_status(
                "active" if new_digest is not None else ("enabled" if enabled_after else "disabled"),
                enabled=enabled_after,
                active_digest=new_digest,
                previous_digest=old_previous,
                value=new_value or old_value,
                actor_uid=actor_uid,
                message="Developer Mode is active." if new_digest else "Developer Mode is disabled.",
            )
            self._clear_pending()
            return result
        pending = self._pending_payload(
            new_digest,
            old_digest,
            old_previous,
            enabled_after,
            actor_uid,
            old_enabled=old_enabled,
        )
        _atomic_json(self.state_root / PENDING_FILENAME, pending, 0o600)
        self._write_status(
            "applying",
            enabled=enabled_after,
            active_digest=old_digest,
            previous_digest=old_previous,
            value=old_value,
            actor_uid=actor_uid,
            message="Applying the verified Developer Mode extension.",
        )
        try:
            if new_digest is None:
                self._remove_active_extension()
            else:
                self._activate_stored(new_digest)
            expected_level = level if new_value is None else new_value["base"]["sysext_level"]
            self._refresh(expected_digest=new_digest, expected_level=expected_level)
            self._write_ref(ACTIVE_FILENAME, new_digest)
            # Re-applying the already active digest is a confirmation/retry,
            # not a new history entry.  Preserve the previous artifact so
            # undo still returns to the preceding working extension (or base).
            next_previous = old_previous if new_digest == old_digest else old_digest
            self._write_ref(PREVIOUS_FILENAME, next_previous)
            result = self._write_status(
                "active" if new_digest is not None else ("enabled" if enabled_after else "disabled"),
                enabled=enabled_after,
                active_digest=new_digest,
                previous_digest=next_previous,
                value=new_value,
                actor_uid=actor_uid,
                message=("Developer Mode is active." if new_digest else "Developer Mode is disabled."),
            )
            self._clear_pending()
            return result
        except BaseException as failure:
            # A caught interruption is restored immediately.  A hard process
            # kill leaves pending.json, which the boot oneshot reconciles.
            try:
                self._restore_locked(old_digest, level if old_value is None else old_value["base"]["sysext_level"])
                self._write_ref(ACTIVE_FILENAME, old_digest)
                self._write_ref(PREVIOUS_FILENAME, old_previous)
                self._write_status(
                    "active" if old_digest is not None else ("enabled" if old_enabled else "disabled"),
                    enabled=old_enabled,
                    active_digest=old_digest,
                    previous_digest=old_previous,
                    value=old_value,
                    actor_uid=actor_uid,
                    error=(failure.code if isinstance(failure, DeveloperError) else "interrupted"),
                    message="The Developer Mode change failed; the previous state was restored.",
                )
                self._clear_pending()
            except BaseException:
                try:
                    self._write_status(
                        "needs_attention",
                        enabled=old_enabled,
                        active_digest=old_digest,
                        previous_digest=old_previous,
                        value=old_value,
                        actor_uid=actor_uid,
                        error="restore_failed",
                        message="Developer Mode needs attention before another change can be applied.",
                    )
                except BaseException:
                    pass
            if isinstance(failure, DeveloperError):
                raise
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise
            raise DeveloperError("interrupted", "The Developer Mode operation was interrupted safely.") from failure

    def enable(self, actor_uid: int) -> dict[str, Any]:
        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            active = self._read_ref(ACTIVE_FILENAME)
            previous = self._read_ref(PREVIOUS_FILENAME)
            value = self._stored_manifest(active) if active else None
            if active is not None and value is not None:
                try:
                    self._ensure_base_compatible(value)
                except DeveloperError as mismatch:
                    if mismatch.code != "base_mismatch":
                        raise
                    return self._write_status(
                        "incompatible",
                        enabled=True,
                        active_digest=active,
                        previous_digest=previous,
                        value=value,
                        actor_uid=actor_uid,
                        error="base_mismatch",
                        message="Developer Mode needs a rebuild for this base image.",
                    )
            prior = self._read_state_file()
            if prior and prior.get("state") == "paused":
                # A normal OS update deliberately pauses the extension.  An
                # explicit enable records that the user wants it considered
                # again; the boot oneshot (or a later apply) performs merge.
                return self._write_status(
                    "enabled",
                    enabled=True,
                    active_digest=active,
                    previous_digest=previous,
                    value=value,
                    actor_uid=actor_uid,
                    message="Developer Mode is enabled; the extension will activate at the next boot.",
                )
            return self._write_status(
                "active" if active else "enabled",
                enabled=True,
                active_digest=active,
                previous_digest=previous,
                value=value,
                actor_uid=actor_uid,
                message="Developer Mode is enabled." if not active else "Developer Mode is active.",
            )

    def apply(self, digest: str, actor_uid: int) -> dict[str, Any]:
        if HEX64_RE.fullmatch(digest) is None:
            _error("invalid_digest", "Developer artifact digest must be 64 lowercase hexadecimal characters.")
        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            state = self._read_state_file()
            if not state or state.get("enabled") is not True:
                _error("mode_disabled", "Enable Developer Mode before applying an artifact.")
            level = self._base_level()
            value, paths = self._verify_spooled_artifact(actor_uid, digest, level)
            self._store_bundle(digest, paths, value)
            return self._switch_locked(digest, enabled_after=True, actor_uid=actor_uid, new_value=value, operation="apply")

    def apply_prepared(self, actor_uid: int) -> dict[str, Any]:
        """Apply the digest selected by the fixed per-user prepared file."""

        return self.apply(self._prepared_digest(actor_uid), actor_uid)

    def undo(self, actor_uid: int) -> dict[str, Any]:
        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            state = self._read_state_file()
            if not state or state.get("enabled") is not True:
                _error("mode_disabled", "Enable Developer Mode before undoing an artifact.")
            active = self._read_ref(ACTIVE_FILENAME)
            if active is None:
                _error("nothing_to_undo", "There is no applied Developer Mode artifact to undo.")
            previous = self._read_ref(PREVIOUS_FILENAME)
            value = self._stored_manifest(previous) if previous else None
            # The first applied artifact has no previous extension.  Undo is
            # still useful in that state: it must restore the untouched base
            # rather than reporting that there is nothing to undo.
            return self._switch_locked(previous, enabled_after=True, actor_uid=actor_uid, new_value=value, operation="undo")

    def disable(self, actor_uid: int) -> dict[str, Any]:
        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            active = self._read_ref(ACTIVE_FILENAME)
            previous = self._read_ref(PREVIOUS_FILENAME)
            old_value = self._stored_manifest(active) if active else None
            return self._switch_locked(None, enabled_after=False, actor_uid=actor_uid, new_value=None, operation="disable")

    def pause_for_update(self, actor_uid: int = 0) -> dict[str, Any]:
        """Temporarily unmerge the extension before a normal OS update.

        The verified artifact reference is retained for a later explicit
        enable/rebuild decision, while the boot oneshot is prevented from
        silently reapplying it to a newly selected base.
        """

        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            active = self._read_ref(ACTIVE_FILENAME)
            previous = self._read_ref(PREVIOUS_FILENAME)
            value = self._stored_manifest(active) if active else None
            if active is None:
                # There is normally no extension to unmerge.  A stale path
                # without an active reference is unsafe, however, so remove
                # it before recording the paused marker.
                if _lstat(self.extension_path, missing_ok=True) is not None:
                    self._remove_active_extension()
                    self._refresh(expected_digest=None, expected_level=self._base_level())
                return self._write_status(
                    "paused", enabled=False, active_digest=None,
                    previous_digest=previous, value=None, actor_uid=actor_uid,
                    message="Developer Mode is paused for the OS update.",
                )
            level = self._base_level()
            pending_payload = self._pending_payload(
                None,
                active,
                previous,
                False,
                actor_uid,
                old_enabled=True,
            )
            _atomic_json(self.state_root / PENDING_FILENAME, pending_payload, 0o600)
            self._write_status(
                "applying",
                enabled=False,
                active_digest=active,
                previous_digest=previous,
                value=value,
                actor_uid=actor_uid,
                message="Pausing Developer Mode for the OS update.",
            )
            try:
                self._remove_active_extension()
                self._refresh(expected_digest=None, expected_level=level)
                result = self._write_status(
                    "paused",
                    enabled=False,
                    active_digest=active,
                    previous_digest=previous,
                    value=value,
                    actor_uid=actor_uid,
                    message="Developer Mode is paused for the OS update.",
                )
                self._clear_pending()
                return result
            except BaseException as failure:
                try:
                    self._activate_stored(active)
                    self._refresh(expected_digest=active, expected_level=level if value is None else value["base"]["sysext_level"])
                    self._write_status(
                        "active",
                        enabled=True,
                        active_digest=active,
                        previous_digest=previous,
                        value=value,
                        actor_uid=actor_uid,
                        error=(failure.code if isinstance(failure, DeveloperError) else "interrupted"),
                        message="Developer Mode could not be paused; the previous state was restored.",
                    )
                    self._clear_pending()
                except BaseException:
                    self._write_status(
                        "needs_attention",
                        enabled=True,
                        active_digest=active,
                        previous_digest=previous,
                        value=value,
                        actor_uid=actor_uid,
                        error="restore_failed",
                        message="Developer Mode needs attention before the OS update.",
                    )
                if isinstance(failure, DeveloperError):
                    raise
                if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                    raise
                raise DeveloperError("interrupted", "The Developer Mode operation was interrupted safely.") from failure

    def recover_interrupted(self, actor_uid: int = 0) -> dict[str, Any]:
        with self.operation_lock():
            pending = self._read_pending()
            if pending is None:
                return self.public_status()
            return self._recover_pending_locked(pending)

    def boot_activate(self) -> dict[str, Any]:
        """Conditional oneshot: reconcile an interrupted transaction and exit."""

        with self.operation_lock():
            pending = self._read_pending()
            if pending is not None:
                self._recover_pending_locked(pending)
            state = self._read_state_file()
            if not state or state.get("enabled") is not True or state.get("state") == "paused":
                return self.public_status()
            # A previous boot already proved this artifact incompatible (or
            # left the state requiring manual recovery).  Do not retry it on
            # every boot; an authenticated apply/rebuild will publish a fresh
            # active state and re-enable this bounded oneshot.
            if state.get("state") in {"incompatible", "needs_attention"}:
                return self.public_status()
            active = self._read_ref(ACTIVE_FILENAME)
            if active is None:
                return self.public_status()
            level = self._base_level()
            value = self._stored_manifest(active)
            try:
                self._ensure_base_compatible(value)
            except DeveloperError as mismatch:
                if mismatch.code != "base_mismatch":
                    raise
                self._remove_active_extension()
                try:
                    self._refresh(expected_digest=None, expected_level=level)
                except DeveloperError:
                    self._write_status(
                        "needs_attention",
                        enabled=True,
                        active_digest=active,
                        previous_digest=self._read_ref(PREVIOUS_FILENAME),
                        value=value,
                        actor_uid=0,
                        error="base_mismatch",
                        message="Developer Mode needs a rebuild for this base image.",
                    )
                    raise
                result = self._write_status(
                    "incompatible",
                    enabled=True,
                    active_digest=active,
                    previous_digest=self._read_ref(PREVIOUS_FILENAME),
                    value=value,
                    actor_uid=0,
                    error="base_mismatch",
                    message="Developer Mode needs a rebuild for this base image.",
                )
                return result
            if not self._active_file_matches(active):
                self._activate_stored(active)
            self._refresh(expected_digest=active, expected_level=level)
            return self._write_status(
                "active",
                enabled=True,
                active_digest=active,
                previous_digest=self._read_ref(PREVIOUS_FILENAME),
                value=value,
                actor_uid=0,
                message="Developer Mode is active.",
            )


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _result_error(error: Exception) -> dict[str, Any]:
    if isinstance(error, manifest.DeveloperError):
        return {"ok": False, "state": "error", "error": error.code, "message": str(error)}
    return {"ok": False, "state": "error", "error": "operation_failed", "message": "Developer Mode operation failed safely."}


def client_main(argv: list[str] | None = None, *, runner: Callable[..., Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zeus Developer Mode")
    parser.add_argument("action", choices=["status", "enable", "apply", "apply-by-digest", "undo", "disable"])
    parser.add_argument("digest", nargs="?")
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action == "status":
        result = DeveloperRuntime().public_status()
        _print_json(result)
        return 0 if result.get("ok") else 1
    if args.action == "apply":
        if args.digest is not None:
            if HEX64_RE.fullmatch(args.digest) is None:
                _print_json({"ok": False, "state": "error", "error": "invalid_digest", "message": "Developer artifact digest must be 64 lowercase hexadecimal characters."})
                return 2
            command = [PKEXEC_COMMAND, ADMIN_COMMAND, "apply-by-digest", args.digest]
        else:
            command = [PKEXEC_COMMAND, ADMIN_COMMAND, "apply"]
    elif args.action == "apply-by-digest":
        if args.digest is None or HEX64_RE.fullmatch(args.digest) is None:
            _print_json({"ok": False, "state": "error", "error": "invalid_digest", "message": "Developer artifact digest must be 64 lowercase hexadecimal characters."})
            return 2
        command = [PKEXEC_COMMAND, ADMIN_COMMAND, "apply-by-digest", args.digest]
    else:
        if args.digest is not None:
            _print_json({"ok": False, "state": "error", "error": "invalid_arguments", "message": "This Developer Mode action does not accept an artifact digest."})
            return 2
        command = [PKEXEC_COMMAND, ADMIN_COMMAND, args.action]
    try:
        completed = (runner or subprocess.run)(command, capture_output=True, text=True, check=False, env=dict(SAFE_ENV), timeout=120)
        output = getattr(completed, "stdout", "")
        if not isinstance(output, str) or len(output.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
            raise ValueError
        try:
            result = json.loads(output)
        except (json.JSONDecodeError, TypeError, ValueError):
            result = {"ok": False, "state": "error", "error": "not_authorized", "message": "Administrator authentication is required."}
        if not isinstance(result, dict):
            result = {"ok": False, "state": "error", "error": "operation_failed", "message": "Developer Mode operation failed safely."}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        result = {"ok": False, "state": "error", "error": "not_authorized", "message": "Administrator authentication is required."}
    _print_json(result)
    return 0 if result.get("ok") else 1


def admin_main(argv: list[str] | None = None, *, runtime: DeveloperRuntime | None = None, environ: Mapping[str, str] | None = None) -> int:
    if os.geteuid() != 0:
        _print_json({"ok": False, "state": "error", "error": "authorization_required", "message": "Administrator authentication is required."})
        return 1
    parser = argparse.ArgumentParser(description="Privileged Zeus Developer Mode helper")
    parser.add_argument("action", choices=["enable", "apply", "apply-by-digest", "undo", "disable", "pause-for-update", "boot-activate", "recover"])
    parser.add_argument("digest", nargs="?")
    args = parser.parse_args(argv)
    if args.action == "apply-by-digest":
        if args.digest is None or HEX64_RE.fullmatch(args.digest) is None:
            _print_json({"ok": False, "state": "error", "error": "invalid_digest", "message": "Developer artifact digest must be 64 lowercase hexadecimal characters."})
            return 2
        action = "apply-by-digest"
    elif args.action == "apply":
        if args.digest is not None:
            if HEX64_RE.fullmatch(args.digest) is None:
                _print_json({"ok": False, "state": "error", "error": "invalid_digest", "message": "Developer artifact digest must be 64 lowercase hexadecimal characters."})
                return 2
            action = "apply-by-digest"
        else:
            action = "apply"
    elif args.digest is not None:
        _print_json({"ok": False, "state": "error", "error": "invalid_arguments", "message": "This Developer Mode action does not accept an artifact digest."})
        return 2
    else:
        action = args.action
    try:
        uid = resolve_calling_uid(environ)
        manager = runtime or DeveloperRuntime()
        if action == "enable":
            result = manager.enable(uid)
        elif action == "apply":
            result = manager.apply_prepared(uid)
        elif action == "apply-by-digest":
            result = manager.apply(args.digest, uid)
        elif action == "undo":
            result = manager.undo(uid)
        elif action == "disable":
            result = manager.disable(uid)
        elif action == "pause-for-update":
            result = manager.pause_for_update(uid)
        elif action == "recover":
            result = manager.recover_interrupted(uid)
        elif action == "boot-activate":
            result = manager.boot_activate()
        else:
            result = manager.public_status()
        result = {"ok": True, **result}
        _print_json(result)
        return 0
    except (manifest.DeveloperError, OSError, ValueError, KeyError, TypeError) as error:
        _print_json(_result_error(error))
        return 1


__all__ = [
    "ACTIVE_EXTENSION",
    "ADMIN_COMMAND",
    "ArtifactPaths",
    "DeveloperError",
    "DeveloperRuntime",
    "EXTENSIONS_ROOT",
    "HEX64_RE",
    "STATE_ROOT",
    "UPDATER_LOCK",
    "admin_main",
    "client_main",
    "resolve_calling_uid",
    "user_home",
    "user_spool",
]
