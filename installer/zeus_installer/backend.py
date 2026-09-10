"""Journaled, fail-closed backend for the Fedora-launched Zeus installer.

The backend has two deliberately separate responsibilities:

* :meth:`InstallerBackend.prepare` retrieves the current signed release and
  leaves one fully verified OCI archive in a private staging directory.
* :meth:`InstallerBackend.install` can only call a separately qualified
  maintenance-boot executor.  No such executor is enabled by default, so an
  install request fails closed and never pretends that a disk was changed.

The preflight worker owns discovery and plan construction.  This module
consumes its schema rather than probing a disk by a conventional path.  A plan
must carry an opaque exact target ``fingerprint``; the value is durably bound
to the journal and is checked again immediately before a qualified executor
is allowed to run.

All filesystem state is written atomically and fsynced.  In-progress phases
are refused after a process interruption; they are not silently resumed or
rolled back.  Commands are fixed-allowlist argument arrays with ``shell=False``
and their argv is recorded in the journal without recording output.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime as _datetime
import fcntl
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import time
from typing import Any, Callable, Iterator, Mapping, Protocol
import uuid

from . import artifacts, validate_allocation


SCHEMA_VERSION = 1
PLAN_LIMIT = 256 * 1024
JOURNAL_LIMIT = 512 * 1024
DEFAULT_ROOT = Path("/var/lib/zeus/installer")
ARTIFACTS_DIRNAME = "artifacts"
JOURNAL_NAME = "journal.json"
LOCK_NAME = "operation.lock"
DEFAULT_ALLOCATION_GIB = 128

# The unprivileged GTK launcher reaches the root-owned backend only through
# these fixed paths.  The helper accepts an action and (for preflight/prepare)
# one numeric allocation; it never accepts a caller-selected plan file,
# executable, archive path, or credential.
PKEXEC_COMMAND = "/usr/bin/pkexec"
HELPER_COMMAND = "/usr/libexec/zeus-installer-helper"
_HELPER_OUTPUT_LIMIT = 512 * 1024
# A streamed preparation response reserves room for its final journal result
# (which may contain a near-limit, reviewed plan).  The helper suppresses
# later advisory events after this budget; it never truncates a line or emits
# output that the client cannot bound and parse.
_HELPER_PROGRESS_LIMIT = _HELPER_OUTPUT_LIMIT // 4
_HELPER_TIMEOUTS = {
    "preflight": 180.0,
    # Review first reads the local journal and only then runs the same
    # read-only preflight used by the standalone action.  Give the combined
    # root call room for both bounded operations while keeping it distinct
    # from the much longer archive preparation timeout.
    "review": 210.0,
    "status": 30.0,
    "prepare": 1800.0,
    "retry_prepare": 1800.0,
    "install": 3600.0,
    "continue": 3600.0,
    "restart": 30.0,
}

# Advisory progress stages are deliberately separate from byte progress.  A
# stage is a hint for the desktop UI and never becomes journal state, so a
# callback or transport failure cannot alter the operation's safety boundary.
_ADVISORY_STAGE_IDS = frozenset(
    {
        "checking_target",
        "waiting_for_operation",
        "checking_release",
        "connecting",
        "downloading",
        "verifying",
    }
)
_HELPER_STAGE_RESERVE = 16 * 1024
_HELPER_FINAL_PROGRESS_RESERVE = 4 * 1024
_HELPER_BYTE_PROGRESS_LIMIT = (
    _HELPER_PROGRESS_LIMIT - _HELPER_STAGE_RESERVE - _HELPER_FINAL_PROGRESS_RESERVE
)
_BYTE_PROGRESS_INTERVAL = 2.0

# The pinned installation route passed the disposable VM118 clean install and
# VM117 update/rollback checks. This is software qualification for a preview,
# not physical laptop qualification. Runtime configuration cannot change it;
# fresh target validation and the verified-backup requirement still gate writes.
QUALIFIED = True

# This is the only qualification receipt that the privileged service factory
# may pass to ``DualBootExecutor``.  It records the exact disposable VM route
# and image/tool versions that were reviewed; it is deliberately not loaded
# from an environment variable or a caller-owned file. The artifact and tool
# identities are checked again by the executor before any storage mutation.
_QUALIFICATION_RECEIPT = {
    "scope": "Fedora43 VM-tested preview; physical laptop untested",
    "physical": False,
    "vmid": 118,
    "lifecycle_vmid": 117,
    "evidence": "docs/iterations/installer-20260910/README.md",
    "build_id": "git-f080c2d9bc53",
    "manifest_digest": "sha256:8797860dc4c27c7e8e3f0034bfcf71f9876401df809509c6b49588752c9c1c18",
    "bootc_version": "1.16.10",
    "bootupd_version": "0.2.35",
}

PHASE_IDLE = "idle"
PHASE_PREPARING = "preparing"
PHASE_DOWNLOADING = "downloading"
PHASE_VERIFYING = "verifying"
PHASE_PREPARED = "prepared"
PHASE_INSTALLING = "installing"
PHASE_REBOOT_REQUIRED = "reboot_required"
PHASE_INSTALLED = "installed"
PHASE_ERROR = "error"
PHASE_INTERRUPTED = "interrupted"

IN_PROGRESS_PHASES = frozenset(
    {
        PHASE_PREPARING,
        PHASE_DOWNLOADING,
        PHASE_VERIFYING,
        PHASE_INSTALLING,
    }
)
KNOWN_PHASES = frozenset(
    {
        PHASE_IDLE,
        PHASE_PREPARING,
        PHASE_DOWNLOADING,
        PHASE_VERIFYING,
        PHASE_PREPARED,
        PHASE_INSTALLING,
        PHASE_REBOOT_REQUIRED,
        PHASE_INSTALLED,
        PHASE_ERROR,
        PHASE_INTERRUPTED,
    }
)

_FINGERPRINT_RE = re.compile(r"\A[^\x00-\x20\x7f]{1,256}\Z")
_SAFE_ARTIFACT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\Z")
_BOOT_ID_RE = re.compile(r"\A[0-9a-fA-F-]{8,128}\Z")


class InstallError(RuntimeError):
    """An expected, user-safe installer failure with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(str(message))


def _emit_stage(
    progress: Callable[[Mapping[str, Any]], Any] | None,
    stage: str,
) -> None:
    """Deliver one advisory stage without making it part of operation state."""

    if progress is None or stage not in _ADVISORY_STAGE_IDS:
        return
    try:
        progress({"stage": stage})
    except Exception:
        # Stage updates are UX hints.  A broken callback must never interrupt
        # the lock, download, verification, or their durable journal writes.
        pass


class MaintenanceExecutor(Protocol):
    """Interface implemented only after the maintenance boot is qualified."""

    qualified: bool

    def execute(
        self,
        *,
        plan: Mapping[str, Any],
        artifact: Mapping[str, Any],
        runner: "CommandRunner",
        journal: "Journal",
    ) -> Mapping[str, Any]:
        """Perform the qualified maintenance operation and return its phase."""

    def verify_resume(
        self,
        *,
        plan: Mapping[str, Any],
        record: Mapping[str, Any],
        inventory: Mapping[str, Any],
    ) -> bool:
        """Validate the recorded post-reboot end-only layout before resume.

        A rebooted maintenance flow may intentionally change the original
        target fingerprint (for example, after shrinking Fedora's partition
        end).  The executor owns the exact expected-change proof; the backend
        must never accept an arbitrary changed inventory merely because the
        operation is in ``reboot_required``.
        """


def utc_now() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def current_boot_id() -> str:
    """Read the kernel boot identity used to explain interrupted operations."""

    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        return "unavailable"
    if not value or not _BOOT_ID_RE.fullmatch(value):
        return "unavailable"
    return value


def _fingerprint_text(value: Any) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise InstallError(
            "invalid_plan", "The installer plan has no valid target fingerprint."
        )
    # Accept the storage worker's bare SHA-256 spelling while using one exact
    # spelling in journal records and comparisons.
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return f"sha256:{value}"
    return value


def _json_clone(value: Any, *, limit: int, code: str, message: str) -> Any:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > limit:
            raise InstallError(code, message)
        return json.loads(raw.decode("utf-8"))
    except InstallError:
        raise
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError) as error:
        raise InstallError(code, message) from error


def _extract_fingerprint(value: Mapping[str, Any]) -> str | None:
    for key in ("fingerprint", "target_fingerprint", "table_fingerprint"):
        candidate = value.get(key)
        if candidate is not None:
            return _fingerprint_text(candidate)
    target = value.get("target")
    if isinstance(target, Mapping):
        for key in ("fingerprint", "target_fingerprint", "table_fingerprint"):
            candidate = target.get(key)
            if candidate is not None:
                return _fingerprint_text(candidate)
    return None


def fingerprint_inventory(inventory: Mapping[str, Any]) -> str:
    """Return the stable fingerprint used by a preflight inventory.

    Preflight may provide a digest directly.  If it does not, this helper
    hashes the complete JSON inventory, which gives callers a deterministic
    value they can place in the plan.  A supplied fingerprint is treated as an
    assertion and is never recomputed or silently normalized beyond the bare
    SHA-256 spelling accepted by the storage planner.
    """

    if not isinstance(inventory, Mapping):
        raise InstallError("invalid_inventory", "The target inventory is invalid.")
    supplied = _extract_fingerprint(inventory)
    if supplied is not None:
        return supplied
    # Keep target revalidation on the same canonical identity function as the
    # preflight worker.  Its digest deliberately excludes volatile values such
    # as free space, battery state and collection time.  Import through the
    # module loader because ``zeus_installer.__init__`` exports a historical
    # ``preflight`` function alias with the same name.
    try:
        module_name = f"{__package__}.preflight" if __package__ else "preflight"
        preflight = importlib.import_module(module_name)
        function = getattr(preflight, "fingerprint", None)
    except (ImportError, ModuleNotFoundError, AttributeError):
        function = None
    if callable(function):
        try:
            result = function(inventory)
        except (OSError, TypeError, ValueError, KeyError, RecursionError, MemoryError) as error:
            raise InstallError(
                "invalid_inventory", "The target inventory is too large or invalid."
            ) from error
        except Exception as error:
            # A preflight implementation is an injected read-only boundary;
            # implementation failures must still become a safe refusal rather
            # than escaping into the privileged helper.
            raise InstallError(
                "invalid_inventory", "The target inventory could not be fingerprinted safely."
            ) from error
        if isinstance(result, str):
            return _fingerprint_text(result)
        raise InstallError("invalid_inventory", "The target inventory fingerprint is invalid.")

    # The fallback is retained for isolated integrations that intentionally
    # omit the preflight module.  Production launcher packages include it.
    clone = _json_clone(
        dict(inventory),
        limit=PLAN_LIMIT,
        code="invalid_inventory",
        message="The target inventory is too large or invalid.",
    )
    raw = json.dumps(
        clone, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# Names used by the preflight worker and by external launcher code.
target_fingerprint = fingerprint_inventory
compute_fingerprint = fingerprint_inventory


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach the shared preflight plan schema.

    Required fields are ``schema_version``, ``supported``, ``blockers`` and an
    exact target ``fingerprint``.  Other preflight data is retained so the
    maintenance executor can record the original GPT table and filesystem
    bounds.  The plan is JSON-only and bounded before it reaches privileged
    code.
    """

    if not isinstance(plan, Mapping):
        raise InstallError("invalid_plan", "The installer plan must be a JSON object.")
    detached = _json_clone(
        dict(plan),
        limit=PLAN_LIMIT,
        code="invalid_plan",
        message="The installer plan is too large or invalid.",
    )
    if not isinstance(detached, dict):
        raise InstallError("invalid_plan", "The installer plan must be a JSON object.")
    if type(detached.get("schema_version")) is not int or detached.get("schema_version") != SCHEMA_VERSION:
        raise InstallError("invalid_plan", "The installer plan schema is unsupported.")
    if type(detached.get("supported")) is not bool:
        raise InstallError("invalid_plan", "The installer plan support flag is invalid.")

    blockers = detached.get("blockers", [])
    if not isinstance(blockers, list) or any(
        not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024
        for value in blockers
    ):
        raise InstallError("invalid_plan", "The installer plan blockers are invalid.")
    if len(blockers) > 256:
        raise InstallError("invalid_plan", "The installer plan has too many blockers.")

    fingerprint = _extract_fingerprint(detached)
    if fingerprint is None:
        raise InstallError("invalid_plan", "The installer plan has no target fingerprint.")
    nested_target = detached.get("target")
    if nested_target is not None and not isinstance(nested_target, Mapping):
        raise InstallError("invalid_plan", "The installer plan target is invalid.")
    if isinstance(nested_target, Mapping):
        nested_fingerprint = _extract_fingerprint(nested_target)
        if nested_fingerprint is not None and nested_fingerprint != fingerprint:
            raise InstallError(
                "target_mismatch", "The installer plan contains conflicting target fingerprints."
            )
    detached["fingerprint"] = fingerprint
    detached["blockers"] = list(blockers)
    detached.setdefault("target", {})

    allocation = detached.get("allocation_gib", DEFAULT_ALLOCATION_GIB)
    if type(allocation) is not int or not 1 <= allocation <= 1024 * 1024:
        raise InstallError("invalid_plan", "The installer allocation is invalid.")
    detached["allocation_gib"] = allocation
    return detached


def validate_target(plan: Mapping[str, Any], inventory: Mapping[str, Any]) -> str:
    """Require an inventory to match the plan's exact target fingerprint."""

    validated = validate_plan(plan)
    expected = validated["fingerprint"]
    observed = fingerprint_inventory(inventory)
    if not hmac.compare_digest(expected, observed):
        raise InstallError(
            "target_mismatch",
            "The selected disk and partition layout changed after preflight.",
        )
    return observed


def _ensure_absolute(path: str | os.PathLike[str], *, code: str, message: str) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as error:
        raise InstallError(code, message) from error
    if not candidate.is_absolute() or candidate.name in {"", ".", ".."}:
        raise InstallError(code, message)
    return candidate


def _mkdir_secure(path: Path, *, mode: int, strict_root: bool) -> None:
    """Create path one component at a time and reject ancestor symlinks."""

    if not path.is_absolute():
        raise InstallError("unsafe_storage", "Installer storage must use an absolute path.")
    current = Path(path.anchor)
    for component in path.parts:
        if component == path.anchor:
            continue
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, mode)
            except FileExistsError:
                pass
            except OSError as error:
                raise InstallError(
                    "unsafe_storage", "Installer storage could not be created."
                ) from error
            try:
                metadata = os.lstat(current)
            except OSError as error:
                raise InstallError(
                    "unsafe_storage", "Installer storage could not be checked."
                ) from error
        except OSError as error:
            raise InstallError("unsafe_storage", "Installer storage could not be checked.") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise InstallError("unsafe_storage", "Installer storage contains an unsafe path.")

    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise InstallError("unsafe_storage", "Installer storage could not be checked.") from error
    expected_owner = 0 if strict_root else os.geteuid()
    if metadata.st_uid != expected_owner or metadata.st_mode & 0o077:
        raise InstallError("unsafe_storage", "Installer storage is not private and protected.")


def _check_private_file(path: Path, *, strict_root: bool, code: str = "unsafe_storage") -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise InstallError(code, "Installer state is unavailable.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError(code, "Installer state is not a regular file.")
    expected_owner = 0 if strict_root else os.geteuid()
    if metadata.st_uid != expected_owner or metadata.st_mode & 0o077:
        raise InstallError(code, "Installer state is not protected.")
    return metadata


def _atomic_json(path: Path, value: Mapping[str, Any], *, mode: int, limit: int) -> None:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError) as error:
        raise InstallError("invalid_state", "Installer state could not be serialized.") from error
    if len(raw) > limit:
        raise InstallError("invalid_state", "Installer state is too large.")

    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise InstallError("unsafe_storage", "Installer state could not be checked.") from error
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise InstallError("unsafe_storage", "Installer state is not a regular file.")

    parent = path.parent
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode)
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short state write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        # The storage directory is root-owned/private, and checking the target
        # above ensures a pre-existing symlink is never followed.
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except InstallError:
        raise
    except (OSError, ValueError) as error:
        raise InstallError("state_write_failed", "Installer state could not be saved durably.") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _read_json_file(path: Path, *, limit: int, strict_root: bool) -> Any:
    metadata = _check_private_file(path, strict_root=strict_root, code="invalid_state")
    if metadata.st_size > limit:
        raise InstallError("invalid_state", "Installer state is too large.")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise InstallError("unsafe_storage", "Installer state changed while it was read.")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit - len(raw) + 1))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > limit:
                raise InstallError("invalid_state", "Installer state is too large.")
        if len(raw) != metadata.st_size:
            raise InstallError("invalid_state", "Installer state changed while it was read.")
    except InstallError:
        raise
    except (OSError, ValueError) as error:
        raise InstallError("invalid_state", "Installer state could not be read.") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError, MemoryError) as error:
        raise InstallError("invalid_state", "Installer state is not valid JSON.") from error


class Journal:
    """Root-owned durable journal and lock for one installer operation."""

    def __init__(
        self,
        root: str | os.PathLike[str] = DEFAULT_ROOT,
        *,
        require_root: bool = True,
        boot_id: Callable[[], str] = current_boot_id,
    ):
        self.root = _ensure_absolute(root, code="unsafe_storage", message="Installer storage is invalid.")
        self.require_root = bool(require_root)
        self.boot_id = boot_id
        self.path = self.root / JOURNAL_NAME
        self.lock_path = self.root / LOCK_NAME
        self.artifacts_path = self.root / ARTIFACTS_DIRNAME

    def ensure_storage(self) -> None:
        _mkdir_secure(self.root, mode=0o700, strict_root=self.require_root)
        if self.root.exists() and (self.root.stat().st_mode & 0o077):
            raise InstallError("unsafe_storage", "Installer storage is not private and protected.")
        _mkdir_secure(self.artifacts_path, mode=0o700, strict_root=self.require_root)
        try:
            metadata = os.lstat(self.artifacts_path)
        except OSError as error:
            raise InstallError("unsafe_storage", "Artifact storage is unavailable.") from error
        if metadata.st_mode & 0o077:
            raise InstallError("unsafe_storage", "Artifact storage is not private.")

    @contextmanager
    def lock(self, *, wait: bool = False) -> Iterator[None]:
        self.ensure_storage()
        descriptor: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except (OSError, ValueError) as error:
                raise InstallError("unsafe_storage", "Installer lock is unavailable.") from error
            metadata = os.fstat(descriptor)
            expected_owner = 0 if self.require_root else os.geteuid()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != expected_owner
                or metadata.st_mode & 0o077
            ):
                raise InstallError("unsafe_storage", "Installer lock is not protected.")
            operation = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as error:
                raise InstallError("busy", "Another installer operation is already running.") from error
            yield
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def is_locked(self) -> bool:
        """Return whether another process currently owns the operation lock."""

        self.ensure_storage()
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except (OSError, ValueError) as error:
                raise InstallError("unsafe_storage", "Installer lock is unavailable.") from error
            metadata = os.fstat(descriptor)
            expected_owner = 0 if self.require_root else os.geteuid()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != expected_owner
                or metadata.st_mode & 0o077
            ):
                raise InstallError("unsafe_storage", "Installer lock is not protected.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def load(self) -> dict[str, Any] | None:
        self.ensure_storage()
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InstallError("invalid_state", "Installer journal is unavailable.") from error
        value = _read_json_file(self.path, limit=JOURNAL_LIMIT, strict_root=self.require_root)
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != SCHEMA_VERSION
        ):
            raise InstallError("invalid_state", "Installer journal schema is unsupported.")
        phase = value.get("phase")
        if phase not in KNOWN_PHASES:
            raise InstallError("invalid_state", "Installer journal phase is invalid.")
        fingerprint = value.get("fingerprint")
        if fingerprint is not None:
            value["fingerprint"] = _fingerprint_text(fingerprint)
        history = value.get("history", [])
        if not isinstance(history, list) or len(history) > 256:
            raise InstallError("invalid_state", "Installer journal history is invalid.")
        return value

    def write(self, value: Mapping[str, Any]) -> None:
        self.ensure_storage()
        _atomic_json(self.path, value, mode=0o600, limit=JOURNAL_LIMIT)
        _check_private_file(self.path, strict_root=self.require_root)

    def begin(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_plan(plan)
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": str(uuid.uuid4()),
            "phase": PHASE_PREPARING,
            "fingerprint": validated["fingerprint"],
            "plan": validated,
            "allocation_gib": validated["allocation_gib"],
            "artifact": None,
            "release": None,
            "progress": None,
            "error": None,
            "message": "Preparing the selected Zeus installation.",
            "boot_id": self.boot_id(),
            "updated_at": utc_now(),
            "history": [],
        }
        self._append(value, PHASE_PREPARING)
        self.write(value)
        return value

    def transition(
        self,
        value: Mapping[str, Any],
        phase: str,
        *,
        message: str | None = None,
        error: str | None = None,
        progress: Mapping[str, Any] | None = None,
        artifact: Mapping[str, Any] | None = None,
        release: Mapping[str, Any] | None = None,
        executor_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase not in KNOWN_PHASES:
            raise InstallError("invalid_state", "Installer journal phase is invalid.")
        updated = copy.deepcopy(dict(value))
        updated["schema_version"] = SCHEMA_VERSION
        updated["phase"] = phase
        updated["updated_at"] = utc_now()
        updated["boot_id"] = self.boot_id()
        if message is not None:
            updated["message"] = str(message)[:2048]
        updated["error"] = str(error)[:256] if error is not None else None
        if progress is not None:
            updated["progress"] = copy.deepcopy(dict(progress))
        if artifact is not None:
            updated["artifact"] = copy.deepcopy(dict(artifact))
        if release is not None:
            updated["release"] = copy.deepcopy(dict(release))
        if executor_result is not None:
            updated["executor_result"] = _json_clone(
                dict(executor_result),
                limit=32 * 1024,
                code="invalid_state",
                message="Maintenance executor result is too large or invalid.",
            )
        self._append(updated, phase)
        self.write(updated)
        return updated

    @staticmethod
    def _append(value: dict[str, Any], phase: str) -> None:
        history = value.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            value["history"] = history
        history.append(
            {
                "phase": phase,
                "boot_id": value.get("boot_id", "unavailable"),
                "at": value.get("updated_at", utc_now()),
            }
        )
        if len(history) > 256:
            del history[:-256]


class CommandRunner:
    """Fixed-policy subprocess runner for a qualified executor.

    The command set covers read-only inventory tools and the eventual
    maintenance primitives.  A caller cannot supply an arbitrary executable
    through the helper CLI, and ``shell`` is always false.
    """

    ALLOWED_EXECUTABLES = frozenset(
        {
            "/usr/bin/bootc",
            "/usr/sbin/btrfs",
            "/usr/bin/findmnt",
            "/usr/sbin/blkid",
            "/usr/bin/lsblk",
            "/usr/sbin/blockdev",
            "/usr/sbin/partx",
            "/usr/bin/mount",
            "/usr/bin/umount",
            "/usr/sbin/udevadm",
            "/usr/sbin/sfdisk",
            "/usr/sbin/mkfs.fat",
            "/usr/sbin/mkfs.ext4",
            "/usr/bin/podman",
            "/usr/bin/bootupctl",
            "/usr/sbin/grub2-mkconfig",
            "/usr/bin/systemd-inhibit",
            "/usr/bin/cat",
            "/usr/bin/systemctl",
        }
    )
    SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin", "LANG": "C.UTF-8", "HOME": "/root"}

    def __init__(
        self,
        *,
        runner: Callable[..., Any] | None = None,
        recorder: Callable[[Mapping[str, Any]], Any] | None = None,
    ):
        self._runner = runner or subprocess.run
        self._recorder = recorder
        self.commands: list[list[str]] = []

    @staticmethod
    def _argv(argv: Any) -> list[str]:
        if isinstance(argv, (str, bytes, bytearray)) or not isinstance(argv, (list, tuple)):
            raise InstallError("invalid_command", "Privileged commands require an argument array.")
        if not argv or len(argv) > 128:
            raise InstallError("invalid_command", "Privileged command arguments are invalid.")
        result: list[str] = []
        for value in argv:
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                raise InstallError("invalid_command", "Privileged command arguments are invalid.")
            result.append(value)
        if result[0] not in CommandRunner.ALLOWED_EXECUTABLES:
            raise InstallError("command_not_allowed", "The maintenance command is not allowed.")
        return result

    def run(
        self,
        argv: Any,
        *,
        timeout: float = 30.0,
        input: str | None = None,
        accepted_returncodes: tuple[int, ...] = (0,),
    ) -> Any:
        args = self._argv(argv)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 1800:
            raise InstallError("invalid_command", "Privileged command timeout is invalid.")
        # A non-zero status is an error by default.  The only exception is an
        # explicit, small tuple supplied by the fixed executor for a command
        # whose tool contract documents another non-error status (for example
        # ``blkid --probe`` returns 2 for an unformatted partition).  Keep this
        # boundary typed and bounded so callers cannot smuggle a broad
        # "ignore failures" policy into the privileged runner.
        if (
            not isinstance(accepted_returncodes, tuple)
            or not accepted_returncodes
            or len(accepted_returncodes) > 8
            or any(type(code) is not int or code < 0 or code > 255 for code in accepted_returncodes)
        ):
            raise InstallError("invalid_command", "Privileged command return codes are invalid.")
        if accepted_returncodes != (0,):
            # Keep the exception tied to the one fixed probe whose utility
            # contracts document another non-error status.  A future command
            # must add its own reviewed policy instead of turning the runner
            # into a generic non-zero-status escape hatch.  ``blkid --probe``
            # returns 2 for an unformatted partition; ``findmnt --mountpoint
            # /target`` returns 1 when the fixed target is not mounted.
            blkid_empty = (
                args[0] == "/usr/sbin/blkid"
                and accepted_returncodes == (0, 2)
                and "--probe" in args
            )
            findmnt_unmounted = (
                args[0] == "/usr/bin/findmnt"
                and accepted_returncodes == (0, 1)
                and any(
                    args[index : index + 2] == ["--mountpoint", "/target"]
                    for index in range(len(args) - 1)
                )
            )
            if not (blkid_empty or findmnt_unmounted):
                raise InstallError("invalid_command", "Privileged command return codes are invalid.")
        if input is not None and (
            not isinstance(input, str) or len(input.encode("utf-8")) > 4 * 1024 * 1024 or "\x00" in input
        ):
            raise InstallError("invalid_command", "Privileged command input is invalid.")
        self.commands.append(list(args))
        started = time.monotonic()
        result: Any = None
        failure: str | None = None
        try:
            options: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "shell": False,
                "env": dict(self.SAFE_ENV),
                "check": False,
            }
            if input is not None:
                options["input"] = input
            result = self._runner(args, **options)
        except subprocess.TimeoutExpired:
            failure = "timeout"
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            failure = "os_error"
        finally:
            if self._recorder is not None:
                self._recorder(
                    {
                        "argv": list(args),
                        "returncode": getattr(result, "returncode", None),
                        "error": failure,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                    }
                )
        if failure is not None:
            raise InstallError("command_failed", "A maintenance command failed or timed out.")
        returncode = getattr(result, "returncode", None)
        # ``subprocess.run`` always supplies an integer return code.  Treat a
        # thin or malformed runner result as failure too; accepting ``None``
        # here would turn an unverified privileged command into success.
        if type(returncode) is not int or returncode not in accepted_returncodes:
            raise InstallError("command_failed", "A maintenance command failed or timed out.")
        return result


def _safe_artifact_name(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_ARTIFACT_NAME_RE.fullmatch(value) is None:
        raise InstallError("release_invalid", "The signed release archive name is invalid.")
    return value


class InstallerBackend:
    """Read-only preflight consumer and journaled artifact backend."""

    def __init__(
        self,
        root: str | os.PathLike[str] = DEFAULT_ROOT,
        *,
        release_fetch: Callable[[], Mapping[str, Any]] | None = None,
        artifact_download: Callable[..., Any] | None = None,
        artifact_verify: Callable[..., Mapping[str, Any]] | None = None,
        inventory_provider: Callable[[], Mapping[str, Any]] | Any | None = None,
        maintenance_executor: MaintenanceExecutor | Any | None = None,
        resume_verifier: Callable[..., Any] | None = None,
        runner: Callable[..., Any] | None = None,
        boot_id: Callable[[], str] = current_boot_id,
        require_root: bool = True,
    ):
        self.journal = Journal(root, require_root=require_root, boot_id=boot_id)
        self.require_root = bool(require_root)
        self.release_fetch = release_fetch or artifacts.fetch_current_release
        self.artifact_download = artifact_download or artifacts.download_release
        self.artifact_verify = artifact_verify or artifacts.verify_archive
        self.inventory_provider = inventory_provider
        self.maintenance_executor = maintenance_executor
        self.resume_verifier = resume_verifier
        self.command_runner = CommandRunner(runner=runner, recorder=self._record_command)
        self._current_record: dict[str, Any] | None = None

    def _require_privilege(self) -> None:
        if self.require_root and os.geteuid() != 0:
            raise InstallError(
                "authorization_required", "Administrator authentication is required."
            )

    def _record_command(self, command: Mapping[str, Any]) -> None:
        if self._current_record is None:
            return
        # The qualified executor persists its own state directly between
        # backend phase transitions.  Refresh first so recording a command
        # cannot overwrite that state with the backend's older snapshot.
        try:
            latest = self.journal.load()
        except InstallError:
            raise
        if latest is None:
            raise InstallError("invalid_state", "Installer journal disappeared during maintenance.")
        self._current_record = latest
        history = self._current_record.setdefault("commands", [])
        if not isinstance(history, list):
            raise InstallError("invalid_state", "Installer command history is invalid.")
        history.append(copy.deepcopy(dict(command)))
        if len(history) > 128:
            del history[:-128]
        # A command event must survive a process that continues after the
        # command.  If this durable write fails, fail the operation closed
        # rather than allowing a successful command to become untracked.
        self.journal.write(self._current_record)

    @contextmanager
    def _operation(self, *, wait: bool = True) -> Iterator[None]:
        self._require_privilege()
        with self.journal.lock(wait=wait):
            yield

    @staticmethod
    def _same_fingerprint(record: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
        recorded = record.get("fingerprint")
        expected = validate_plan(plan)["fingerprint"]
        try:
            return isinstance(recorded, str) and hmac.compare_digest(
                _fingerprint_text(recorded), expected
            )
        except InstallError:
            return False

    def _check_record_binding(self, record: Mapping[str, Any] | None, plan: Mapping[str, Any]) -> None:
        if record is None:
            return
        if not self._same_fingerprint(record, plan):
            raise InstallError(
                "target_mismatch", "The installer journal belongs to a different target."
            )

    def _reload_record(self) -> dict[str, Any]:
        """Refresh a transition base after executor-owned journal writes."""

        try:
            current = self.journal.load()
        except InstallError:
            raise
        if current is None:
            raise InstallError("invalid_state", "Installer journal disappeared during maintenance.")
        refreshed = copy.deepcopy(dict(current))
        self._current_record = refreshed
        return refreshed

    @staticmethod
    def _record_plan(record: Mapping[str, Any]) -> dict[str, Any]:
        """Recover the original preflight plan from a durable journal."""

        candidate = record.get("plan")
        if not isinstance(candidate, Mapping):
            raise InstallError("invalid_state", "The installer journal has no original plan.")
        try:
            validated = validate_plan(candidate)
        except InstallError as error:
            raise InstallError("invalid_state", "The installer journal plan is invalid.") from error
        recorded = record.get("fingerprint")
        try:
            consistent = isinstance(recorded, str) and hmac.compare_digest(
                _fingerprint_text(recorded), validated["fingerprint"]
            )
        except InstallError as error:
            raise InstallError("invalid_state", "The installer journal plan is inconsistent.") from error
        if not consistent:
            raise InstallError("invalid_state", "The installer journal plan is inconsistent.")
        return validated

    @staticmethod
    def _executor_state(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return the durable executor state without importing the executor."""

        for key in ("executor_state", "dualboot_state"):
            value = record.get(key)
            if isinstance(value, Mapping):
                return value
        result = record.get("executor_result")
        if isinstance(result, Mapping):
            for key in ("executor_state", "dualboot_state"):
                value = result.get(key)
                if isinstance(value, Mapping):
                    return value
        return None

    def _refuse_interrupted(self, record: Mapping[str, Any] | None) -> None:
        if record is None:
            return
        phase = record.get("phase")
        if phase in IN_PROGRESS_PHASES:
            raise InstallError(
                "interrupted",
                "An earlier installer operation stopped before a safe boundary; review it before retrying.",
            )
        if phase == PHASE_INTERRUPTED or record.get("error") == "interrupted":
            raise InstallError(
                "interrupted",
                "An earlier installer operation was interrupted and cannot be resumed automatically.",
            )

    def _provider_inventory(self) -> Mapping[str, Any]:
        provider = self.inventory_provider
        if provider is None:
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before writes."
            )
        try:
            if callable(provider):
                value = provider()
            elif hasattr(provider, "collect") and callable(provider.collect):
                value = provider.collect()
            else:
                value = provider
        except InstallError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before writes."
            ) from None
        except Exception as error:
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before writes."
            ) from error
        if not isinstance(value, Mapping):
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before writes."
            )
        return value

    @staticmethod
    def _public_release(manifest: Mapping[str, Any]) -> dict[str, Any]:
        # The release was validated by artifacts; copying preserves the exact
        # signed identity while avoiding a mutable caller object in the journal.
        return _json_clone(
            dict(manifest),
            limit=64 * 1024,
            code="release_invalid",
            message="The signed release metadata is too large or invalid.",
        )

    def _load_release(self) -> dict[str, Any]:
        try:
            value = self.release_fetch()
        except artifacts.ArtifactError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise InstallError(
                "release_fetch_failed", "The signed release could not be fetched."
            ) from error
        if not isinstance(value, Mapping):
            raise InstallError("release_invalid", "The signed release metadata is invalid.")
        try:
            return artifacts.validate_release(value)
        except artifacts.ArtifactError:
            raise

    def _artifact_path(self, manifest: Mapping[str, Any]) -> tuple[Path, Path]:
        try:
            archive = manifest["archive"]
            name = _safe_artifact_name(archive["name"])
        except (KeyError, TypeError):
            raise InstallError("release_invalid", "The signed archive metadata is invalid.") from None
        final = self.journal.artifacts_path / name
        partial = self.journal.artifacts_path / f".{name}.part"
        # Both names are generated from trusted schema data; recheck the
        # parent and final paths to protect future changes to that schema.
        for candidate in (final, partial):
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstallError("unsafe_storage", "Artifact storage is unavailable.") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise InstallError("unsafe_storage", "Artifact storage contains an unsafe path.")
        return final, partial

    def _verify_path(self, path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
        # The cryptographic verifier proves the bytes and OCI identity.  The
        # backend additionally requires the journal-bound staging file to stay
        # a private file owned by the service, including on later status and
        # install calls after the initial download commit.
        _check_private_file(path, strict_root=self.require_root, code="unsafe_storage")
        try:
            value = self.artifact_verify(path, manifest)
        except artifacts.ArtifactError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise InstallError(
                "artifact_invalid", "The staged release archive could not be verified."
            ) from error
        if not isinstance(value, Mapping):
            raise InstallError("artifact_invalid", "The staged release archive identity is invalid.")
        detached = _json_clone(
            dict(value),
            limit=32 * 1024,
            code="artifact_invalid",
            message="The staged release archive identity is invalid.",
        )
        detached["path"] = str(path)
        return detached

    def _progress_callback(
        self,
        record: dict[str, Any],
        manifest: Mapping[str, Any],
        external: Callable[[Mapping[str, Any]], Any] | None = None,
    ):
        expected = manifest["archive"]["size"]
        downloading_announced = False

        def callback(done: Any, total: Any) -> None:
            nonlocal downloading_announced
            if type(done) is not int or type(total) is not int or total != expected:
                raise InstallError("download_invalid", "The release download progress is invalid.")
            if done < 0 or done > total:
                raise InstallError("download_invalid", "The release download progress is invalid.")
            if done > 0 and not downloading_announced:
                downloading_announced = True
                _emit_stage(external, "downloading")
            self._current_record = record
            self._current_record = self.journal.transition(
                record,
                PHASE_DOWNLOADING,
                message="Downloading the signed Zeus release.",
                progress={"bytes": done, "total": total},
            )
            record.clear()
            record.update(self._current_record)
            if external is not None:
                try:
                    external({"bytes": done, "total": total})
                except Exception:
                    # Progress is advisory UI state.  A broken callback must
                    # never turn a verified download into an unsafe partial
                    # operation or interrupt durable journal transitions.
                    pass

        return callback

    def _commit_download(self, partial: Path, final: Path) -> None:
        try:
            metadata = os.lstat(partial)
        except OSError as error:
            raise InstallError("download_failed", "The release archive was not downloaded safely.") from error
        expected_owner = 0 if self.require_root else os.geteuid()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_owner
            or metadata.st_mode & 0o077
        ):
            raise InstallError("unsafe_storage", "The downloaded archive is not protected.")
        try:
            os.lstat(final)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise InstallError("unsafe_storage", "The artifact destination is unavailable.") from error
        else:
            raise InstallError("destination_exists", "The artifact destination already exists.")
        try:
            # A hard-link commit cannot overwrite a destination that appeared
            # after the lstat check.  It also preserves the inode verified by
            # the download helper.
            os.link(partial, final, follow_symlinks=False)
            os.unlink(partial)
            descriptor = os.open(
                self.journal.artifacts_path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError as error:
            raise InstallError("destination_exists", "The artifact destination already exists.") from error
        except OSError as error:
            raise InstallError("download_failed", "The downloaded archive could not be committed safely.") from error

    def _clear_retry_staging(self, record: Mapping[str, Any]) -> None:
        """Remove only files bound to a failed pre-executor download.

        A failed download leaves a journal entry and, commonly, a ``.part``
        file.  Retrying must not require deleting the root-owned journal by
        hand, but it must also never become a general cleanup primitive.  The
        journal phase/history and the absence of executor state prove that no
        maintenance operation has crossed a storage boundary.  The only
        paths eligible for removal are the canonical archive and partial path
        derived from the journal's authenticated release metadata.
        """

        allowed_history = {
            PHASE_PREPARING,
            PHASE_DOWNLOADING,
            PHASE_VERIFYING,
            PHASE_ERROR,
        }
        history = record.get("history", [])
        if history is not None:
            if not isinstance(history, list):
                raise InstallError("invalid_state", "The installer journal history is invalid.")
            for entry in history:
                if not isinstance(entry, Mapping) or entry.get("phase") not in allowed_history:
                    raise InstallError(
                        "retry_not_allowed",
                        "This installer operation has crossed a storage boundary and cannot be retried.",
                    )
        for key in (
            "commands",
            "executor_result",
            "executor_state",
            "dualboot_state",
            "storage_result",
            "storage",
        ):
            value = record.get(key)
            if value not in (None, [], {}):
                raise InstallError(
                    "retry_not_allowed",
                    "This installer operation has crossed a storage boundary and cannot be retried.",
                )
        if record.get("artifact") not in (None, {}, []):
            raise InstallError(
                "retry_not_allowed",
                "This installer operation has crossed a storage boundary and cannot be retried.",
            )

        release = record.get("release")
        if release is None:
            # A crash before the signed release was recorded has no
            # journal-bound filename to remove.  Leave every staging file in
            # place; the next prepare attempt will apply its normal exact
            # path checks.
            return
        if not isinstance(release, Mapping):
            raise InstallError("invalid_state", "The installer journal release is invalid.")
        try:
            validated_release = artifacts.validate_release(release)
            final, partial = self._artifact_path(validated_release)
        except artifacts.ArtifactError as error:
            raise InstallError("invalid_state", "The installer journal release is invalid.") from error
        candidates = (final, partial)
        expected_owner = 0 if self.require_root else os.geteuid()
        removed = False
        for candidate in candidates:
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstallError("unsafe_storage", "Artifact storage is unavailable.") from error
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != expected_owner
                or metadata.st_mode & 0o077
            ):
                raise InstallError(
                    "unsafe_storage", "The failed archive staging path is not protected."
                )
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstallError(
                    "unsafe_storage", "The failed archive staging path could not be cleared."
                ) from error
            removed = True
        if removed:
            try:
                descriptor = os.open(
                    self.journal.artifacts_path,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
            except OSError as error:
                raise InstallError(
                    "state_write_failed", "Artifact staging could not be cleared durably."
                ) from error
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _prepared_result(self, record: Mapping[str, Any], *, ok: bool = True) -> dict[str, Any]:
        phase = str(record.get("phase", PHASE_ERROR))
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ok": ok,
            "state": phase,
            "phase": phase,
            "fingerprint": record.get("fingerprint"),
            "allocation_gib": record.get("allocation_gib"),
            "artifact": copy.deepcopy(record.get("artifact")),
            "release": copy.deepcopy(record.get("release")),
            "progress": copy.deepcopy(record.get("progress")),
            "error": record.get("error"),
            "message": record.get("message", ""),
            "operation_id": record.get("operation_id"),
            "updated_at": record.get("updated_at"),
        }
        # The root journal is authoritative after the reboot boundary.  Keep
        # the reviewed plan in status so the launcher can display the original
        # target and allocation without collecting a new (potentially smaller)
        # Fedora layout.  A small detached target copy is convenient for
        # clients; cap it so a malformed-but-bounded journal cannot make the
        # helper response grow without limit through duplication.
        journal_plan = record.get("plan")
        if isinstance(journal_plan, Mapping):
            result["plan"] = copy.deepcopy(dict(journal_plan))
            target = journal_plan.get("target")
            if isinstance(target, Mapping):
                try:
                    result["target"] = _json_clone(
                        dict(target),
                        limit=64 * 1024,
                        code="invalid_state",
                        message="The installer journal target is too large or invalid.",
                    )
                except InstallError:
                    # The complete validated plan remains available for an
                    # owner-facing review; omit only the redundant shortcut.
                    pass
        return result

    def _resume_status(
        self,
        record: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Report post-reboot proof only after the executor verifies it.

        A different boot ID by itself is insufficient: the executor must also
        prove the expected end-only table (and its other resume invariants)
        against a fresh inventory.  Returning ``False`` for the same boot
        keeps the pre-reboot state explicit and prevents a custom verifier
        from accidentally making a restart look like a completed reboot.
        """

        result: dict[str, Any] = {
            "current_boot_changed": False,
            "resume_verified": False,
        }
        state = self._executor_state(record)
        old_boot = state.get("old_boot_id") if isinstance(state, Mapping) else None
        if not isinstance(old_boot, str) or not old_boot:
            # Older fixture journals may have only the top-level boot marker.
            # It is safe to use it as the pre-reboot identity, but only when
            # the executor state is otherwise unavailable do we fall back.
            old_boot = record.get("boot_id")
        try:
            current_boot = self.journal.boot_id()
        except Exception:
            current_boot = None
        if not isinstance(old_boot, str) or not old_boot or not isinstance(current_boot, str) or not current_boot:
            result["resume_verification"] = "unavailable"
            result["resume_error"] = "target_unverified"
            return result
        if hmac.compare_digest(old_boot, current_boot):
            result["resume_verification"] = "pending_reboot"
            return result

        result["boot_id_changed"] = True
        verifier = self.resume_verifier
        if verifier is None:
            verifier = getattr(self.maintenance_executor, "verify_resume", None)
        if verifier is None:
            verifier = getattr(self.maintenance_executor, "verify_after_reboot", None)
        if not callable(verifier):
            result["resume_verification"] = "unavailable"
            result["resume_error"] = "target_unverified"
            return result
        try:
            inventory = self._provider_inventory()
            proof = verifier(plan=plan, record=record, inventory=inventory)
        except InstallError as error:
            result["resume_verification"] = "failed"
            result["resume_error"] = error.code
            result["resume_message"] = str(error)[:256]
            return result
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["resume_verification"] = "failed"
            result["resume_error"] = "target_unverified"
            result["resume_message"] = str(error)[:256]
            return result
        except Exception as error:
            result["resume_verification"] = "failed"
            result["resume_error"] = "target_unverified"
            result["resume_message"] = str(error)[:256]
            return result
        if proof is True:
            result["current_boot_changed"] = True
            result["resume_verified"] = True
            result["resume_verification"] = "passed"
        else:
            result["resume_verification"] = "failed"
            result["resume_error"] = "target_mismatch"
        return result

    def status(self, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return local state without fetching or writing disks.

        When ``plan`` is omitted, a prepared journal supplies the original
        preflight plan.  This is required after the maintenance boot has
        changed Fedora's layout: rebuilding a fresh plan at that point would
        describe a different target and could incorrectly request another
        allocation.
        """

        self._require_privilege()
        record = self.journal.load()
        if record is None:
            if plan is None:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "ok": True,
                    "state": PHASE_IDLE,
                    "phase": PHASE_IDLE,
                    "fingerprint": None,
                    "allocation_gib": None,
                    "artifact": None,
                    "release": None,
                    "progress": None,
                    "error": None,
                    "message": "No Zeus installation has been prepared.",
                }
            validated = validate_plan(plan)
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "state": PHASE_IDLE,
                "phase": PHASE_IDLE,
                "fingerprint": validated["fingerprint"],
                "allocation_gib": validated["allocation_gib"],
                "artifact": None,
                "release": None,
                "progress": None,
                "error": None,
                "message": "No Zeus installation has been prepared.",
            }
        validated = self._record_plan(record) if plan is None else validate_plan(plan)
        if not self._same_fingerprint(record, validated):
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "state": PHASE_ERROR,
                "phase": PHASE_ERROR,
                "fingerprint": record.get("fingerprint"),
                "error": "target_mismatch",
                "message": "The installer journal belongs to a different target.",
            }
        phase = record.get("phase")
        if phase in IN_PROGRESS_PHASES and not self.journal.is_locked():
            return {
                **self._prepared_result(record, ok=False),
                "state": PHASE_INTERRUPTED,
                "phase": PHASE_INTERRUPTED,
                "error": "interrupted",
                "message": "The previous installer operation stopped before a safe boundary.",
            }
        if phase in {PHASE_PREPARED, PHASE_REBOOT_REQUIRED} and record.get("artifact"):
            try:
                release = record.get("release")
                artifact = record.get("artifact")
                if isinstance(release, Mapping) and isinstance(artifact, Mapping):
                    path = Path(str(artifact.get("path", "")))
                    if path != self.journal.artifacts_path / _safe_artifact_name(release["archive"]["name"]):
                        raise InstallError("artifact_tampered", "The staged archive path is not owned by the installer.")
                    self._verify_path(path, release)
            except (InstallError, artifacts.ArtifactError, KeyError, TypeError, ValueError):
                return {
                    **self._prepared_result(record, ok=False),
                    "state": PHASE_ERROR,
                    "phase": PHASE_ERROR,
                    "error": "artifact_tampered",
                    "message": "The prepared release archive no longer matches its signed identity.",
                }
        result = self._prepared_result(record, ok=phase not in {PHASE_ERROR, PHASE_INTERRUPTED})
        if phase == PHASE_REBOOT_REQUIRED:
            result.update(self._resume_status(record, validated))
        return result

    def prepare(
        self,
        plan: Mapping[str, Any],
        progress: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Download and stage one verified signed artifact for a plan."""

        self._require_privilege()
        validated = validate_plan(plan)
        if progress is not None and not callable(progress):
            raise InstallError("download_invalid", "The release progress callback is invalid.")
        if not validated["supported"] or validated["blockers"]:
            raise InstallError(
                "unsupported_plan", "The preflight plan contains blockers and cannot be prepared."
            )
        _emit_stage(progress, "waiting_for_operation")
        with self._operation(wait=True):
            _emit_stage(progress, "checking_target")
            record = self.journal.load()
            self._check_record_binding(record, validated)
            self._refuse_interrupted(record)
            if record is not None and record.get("phase") == PHASE_INSTALLED:
                raise InstallError("already_installed", "The selected Zeus installation is complete.")
            if record is not None and record.get("phase") in {PHASE_PREPARED, PHASE_REBOOT_REQUIRED}:
                # Reverify an existing durable artifact rather than redownload
                # over it.  Tampering is evidence for a human review.
                release = record.get("release")
                artifact = record.get("artifact")
                if not isinstance(release, Mapping) or not isinstance(artifact, Mapping):
                    raise InstallError("invalid_state", "The prepared installer state is incomplete.")
                try:
                    archive = release["archive"]
                    if not isinstance(archive, Mapping):
                        raise TypeError
                    expected = self.journal.artifacts_path / _safe_artifact_name(archive["name"])
                    path_value = artifact.get("path")
                    if not isinstance(path_value, str) or not path_value:
                        raise ValueError
                    path = Path(path_value)
                except (KeyError, TypeError, ValueError) as error:
                    raise InstallError("invalid_state", "The prepared installer state is incomplete.") from error
                if path != expected:
                    raise InstallError("artifact_tampered", "The prepared archive path is not owned by the installer.")
                try:
                    _emit_stage(progress, "verifying")
                    verified = self._verify_path(path, release)
                except (artifacts.ArtifactError, InstallError) as error:
                    raise InstallError("artifact_tampered", "The prepared archive no longer matches its signed identity.") from error
                record["artifact"] = verified
                return self._prepared_result(record)

            if self.inventory_provider is not None:
                validate_target(validated, self._provider_inventory())
            record = self.journal.begin(validated)
            self._current_record = record
            try:
                _emit_stage(progress, "checking_release")
                manifest = self._load_release()
                record = self.journal.transition(
                    record,
                    PHASE_PREPARING,
                    message="The signed release metadata is authenticated.",
                    release=self._public_release(manifest),
                )
                self._current_record = record
                final, partial = self._artifact_path(manifest)
                record = self.journal.transition(
                    record,
                    PHASE_DOWNLOADING,
                    message="Downloading the signed Zeus release.",
                    progress={"bytes": 0, "total": manifest["archive"]["size"]},
                )
                self._current_record = record
                try:
                    os.lstat(final)
                except FileNotFoundError:
                    # A stale partial is never resumed automatically.  This
                    # keeps an interrupted download from becoming an implicit
                    # trusted input on the next invocation.
                    try:
                        os.lstat(partial)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        raise InstallError("unsafe_storage", "The partial archive path is unavailable.") from error
                    else:
                        raise InstallError("interrupted", "A partial release archive requires explicit review before retrying.")
                    callback = self._progress_callback(record, manifest, progress)
                    try:
                        _emit_stage(progress, "connecting")
                        self.artifact_download(manifest, partial, progress=callback)
                    except artifacts.ArtifactError:
                        raise
                    except InstallError:
                        raise
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        raise InstallError("download_failed", "The signed release archive could not be downloaded safely.") from error
                    self._commit_download(partial, final)
                except OSError as error:
                    raise InstallError("unsafe_storage", "The artifact destination is unavailable.") from error
                else:
                    raise InstallError("destination_exists", "The artifact destination already exists.")

                record = self.journal.transition(
                    record,
                    PHASE_VERIFYING,
                    message="Verifying the signed archive and Zeus image identity.",
                    progress=None,
                )
                self._current_record = record
                _emit_stage(progress, "verifying")
                verified = self._verify_path(final, manifest)
                record = self.journal.transition(
                    record,
                    PHASE_PREPARED,
                    message="The verified Zeus build is ready to install.",
                    artifact=verified,
                    release=self._public_release(manifest),
                    progress={"bytes": manifest["archive"]["size"], "total": manifest["archive"]["size"]},
                )
                self._current_record = record
                return self._prepared_result(record)
            except (artifacts.ArtifactError, InstallError) as error:
                code = getattr(error, "code", "prepare_failed")
                try:
                    record = self.journal.transition(
                        record,
                        PHASE_ERROR,
                        message="The Zeus artifact could not be prepared safely.",
                        error=code,
                    )
                    self._current_record = record
                except InstallError:
                    pass
                raise InstallError(code, str(error)) from error
            except (OSError, ValueError, TypeError, KeyError) as error:
                try:
                    record = self.journal.transition(
                        record,
                        PHASE_ERROR,
                        message="The Zeus artifact could not be prepared safely.",
                        error="prepare_failed",
                    )
                    self._current_record = record
                except InstallError:
                    pass
                raise InstallError("prepare_failed", "The Zeus artifact could not be prepared safely.") from error
            except Exception as error:
                try:
                    record = self.journal.transition(
                        record,
                        PHASE_ERROR,
                        message="The Zeus artifact could not be prepared safely.",
                        error="prepare_failed",
                    )
                    self._current_record = record
                except InstallError:
                    pass
                raise InstallError("prepare_failed", "The Zeus artifact could not be prepared safely.") from error
            finally:
                self._current_record = None

    def retry_prepare(
        self,
        plan: Mapping[str, Any] | None = None,
        progress: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Retry a pre-executor preparation after clearing owned staging.

        This is the recovery path for a failed or interrupted network/archive
        preparation.  It accepts only preparation phases, validates the exact
        original plan and current target first, and removes only the archive
        paths named by the journal's signed release.  An operation with any
        executor command/result or storage-phase history is permanently
        ineligible for this cleanup path and must be reviewed instead.
        """

        self._require_privilege()
        validated: dict[str, Any] | None = None
        if progress is not None and not callable(progress):
            raise InstallError("download_invalid", "The release progress callback is invalid.")
        if plan is not None:
            validated = validate_plan(plan)
            if not validated["supported"] or validated["blockers"]:
                raise InstallError(
                    "unsupported_plan", "The preflight plan contains blockers and cannot be prepared."
                )
        _emit_stage(progress, "waiting_for_operation")
        with self._operation(wait=True):
            _emit_stage(progress, "checking_target")
            record = self.journal.load()
            if record is None:
                raise InstallError(
                    "retry_not_allowed", "There is no failed preparation to retry."
                )
            if validated is None:
                validated = self._record_plan(record)
            self._check_record_binding(record, validated)
            phase = record.get("phase")
            if phase not in {PHASE_PREPARING, PHASE_DOWNLOADING, PHASE_VERIFYING, PHASE_ERROR}:
                raise InstallError(
                    "retry_not_allowed",
                    "This installer operation is beyond the safe preparation retry boundary.",
                )
            # Revalidation happens while the original journal is still
            # authoritative and before deleting any staging file.
            validate_target(validated, self._provider_inventory())
            self._clear_retry_staging(record)
            # Leave a durable error boundary while the lock is released for
            # the normal prepare implementation.  If this process stops in
            # that small handoff window, the next retry remains safe and
            # recoverable; no executor state or storage history is erased.
            self.journal.transition(
                record,
                PHASE_ERROR,
                message="The failed preparation staging was cleared; retrying download.",
                error="retry_requested",
                artifact=None,
            )

        # ``prepare`` owns the complete fresh signed fetch and verification
        # sequence, including a new journal operation and cryptographic check.
        # Calling it after releasing the lock avoids nested lock ownership and
        # keeps all normal crash handling in one implementation.
        assert validated is not None
        return self.prepare(validated, progress=progress)

    def _qualified_executor(self) -> Any:
        executor = self.maintenance_executor
        if executor is None or getattr(executor, "qualified", False) is not True:
            raise InstallError(
                "executor_unavailable",
                "The maintenance boot executor is not qualified; no disk change was attempted.",
            )
        method = getattr(executor, "execute", None)
        if not callable(method):
            method = getattr(executor, "run", None)
        if not callable(method):
            method = getattr(executor, "install", None)
        if not callable(method):
            raise InstallError(
                "executor_unavailable",
                "The maintenance boot executor is not qualified; no disk change was attempted.",
            )
        return method

    @staticmethod
    def _inventory_partition_table(inventory: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Find the complete GPT table in a fresh preflight inventory."""

        owners: list[Mapping[str, Any]] = [inventory]
        nested = inventory.get("inventory")
        if isinstance(nested, Mapping):
            owners.append(nested)
        plan = inventory.get("plan")
        if isinstance(plan, Mapping):
            owners.append(plan)
            nested_plan = plan.get("inventory")
            if isinstance(nested_plan, Mapping):
                owners.append(nested_plan)
        for owner in owners:
            for key in ("partition_table", "partitiontable", "sfdisk", "table"):
                candidate = owner.get(key)
                if not isinstance(candidate, Mapping):
                    continue
                for nested_key in ("partitiontable", "partition_table"):
                    nested_candidate = candidate.get(nested_key)
                    if isinstance(nested_candidate, Mapping):
                        return nested_candidate
                if "partitions" in candidate or "partition" in candidate:
                    return candidate
        return None

    def _verify_installed_restart_boundary(self, record: Mapping[str, Any]) -> None:
        """Prove a completed install before allowing an owner-requested reboot.

        Restarting after installation never replays the executor, but a stale
        or hand-edited terminal journal must not turn ``systemctl reboot`` into
        an unconditional command.  A qualified executor may provide an exact
        installed-boundary verifier.  Otherwise, when the durable stage state
        carries the full allocated GPT table, compare it with a fresh table;
        fixtures and older journals without that optional table rely on the
        strict stage/result markers checked by :meth:`restart`.
        """

        executor = self.maintenance_executor
        verifier = None
        for name in ("verify_installed", "verify_installed_boundary"):
            candidate = getattr(executor, name, None) if executor is not None else None
            if callable(candidate):
                verifier = candidate
                break
        if verifier is not None:
            try:
                inventory = self._provider_inventory()
                plan = self._record_plan(record)
                proof = verifier(plan=plan, record=record, inventory=inventory)
            except InstallError as error:
                raise InstallError(error.code, str(error)) from error
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                raise InstallError(
                    "target_unverified",
                    "The installed target could not be verified before restart.",
                ) from error
            except Exception as error:
                raise InstallError(
                    "target_unverified",
                    "The installed target could not be verified before restart.",
                ) from error
            if proof is not True:
                raise InstallError(
                    "target_mismatch",
                    "The installed target no longer matches its completed layout.",
                )
            return

        state = self._executor_state(record)
        expected = None
        if isinstance(state, Mapping):
            expected = state.get("allocated_table")
            if expected is None:
                expected = state.get("expected_table")
        if expected is None:
            return
        if not isinstance(expected, Mapping):
            raise InstallError(
                "target_unverified", "The completed installed layout record is invalid."
            )
        inventory = self._provider_inventory()
        observed = self._inventory_partition_table(inventory)
        if observed is None:
            raise InstallError(
                "target_unverified",
                "The current partition table could not be verified before restart.",
            )
        if dict(observed) != dict(expected):
            raise InstallError(
                "target_mismatch",
                "The installed target no longer matches its completed layout.",
            )

    def _verify_install_target(
        self,
        plan: Mapping[str, Any],
        inventory: Mapping[str, Any],
        record: Mapping[str, Any],
        executor: Any,
    ) -> None:
        """Revalidate the target, including an explicit reboot resume proof."""

        try:
            validate_target(plan, inventory)
            return
        except InstallError as mismatch:
            if mismatch.code != "target_mismatch" or record.get("phase") != PHASE_REBOOT_REQUIRED:
                raise

        verifier = self.resume_verifier
        if verifier is None:
            verifier = getattr(executor, "verify_resume", None)
        if verifier is None:
            verifier = getattr(executor, "verify_after_reboot", None)
        if not callable(verifier):
            raise InstallError(
                "target_mismatch",
                "The target changed after the recorded reboot boundary and has no qualified resume proof.",
            )
        try:
            result = verifier(plan=plan, record=record, inventory=inventory)
        except InstallError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before installation."
            ) from error
        except Exception as error:
            raise InstallError(
                "target_unverified", "The target layout could not be revalidated before installation."
            ) from error
        if result is not True:
            raise InstallError(
                "target_mismatch",
                "The changed target layout did not match the recorded reboot boundary.",
            )

    def install(self, plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run a qualified maintenance executor, or fail closed.

        Omitting ``plan`` continues the exact plan persisted by ``prepare``.
        That path is used after a reboot, when the live Fedora geometry may
        legitimately differ at the planned partition end.
        """

        self._require_privilege()
        validated: dict[str, Any] | None = None
        if plan is not None:
            validated = validate_plan(plan)
            if not validated["supported"] or validated["blockers"]:
                raise InstallError(
                    "unsupported_plan", "The preflight plan contains blockers and cannot be installed."
                )
        # Resolve qualification before taking any installation phase or
        # invoking an external command.  The default CLI has no executor.
        method = self._qualified_executor()
        with self._operation(wait=True):
            record = self.journal.load()
            if validated is None:
                if record is None:
                    raise InstallError(
                        "not_prepared", "Prepare a verified Zeus artifact before installation."
                    )
                validated = self._record_plan(record)
            self._check_record_binding(record, validated)
            if not validated["supported"] or validated["blockers"]:
                raise InstallError(
                    "unsupported_plan", "The preflight plan contains blockers and cannot be installed."
                )
            self._refuse_interrupted(record)
            if record is None or record.get("phase") not in {PHASE_PREPARED, PHASE_REBOOT_REQUIRED}:
                raise InstallError(
                    "not_prepared", "Prepare a verified Zeus artifact before installation."
                )
            try:
                release = record.get("release")
                artifact = record.get("artifact")
                if not isinstance(release, Mapping) or not isinstance(artifact, Mapping):
                    raise InstallError("invalid_state", "The prepared installer state is incomplete.")
                archive = release.get("archive")
                if not isinstance(archive, Mapping):
                    raise InstallError("invalid_state", "The prepared installer state is incomplete.")
                expected = self.journal.artifacts_path / _safe_artifact_name(archive["name"])
                path_value = artifact.get("path")
                if not isinstance(path_value, str) or not path_value:
                    raise InstallError("invalid_state", "The prepared installer state is incomplete.")
                path = Path(path_value)
                if path != expected:
                    raise InstallError("artifact_tampered", "The prepared archive path is not owned by the installer.")
                verified = self._verify_path(path, release)
                if verified.get("sha256") != archive.get("sha256"):
                    raise InstallError("artifact_tampered", "The prepared archive no longer matches its signed identity.")
                inventory = self._provider_inventory()
                self._verify_install_target(validated, inventory, record, self.maintenance_executor)
                record = self.journal.transition(
                    record,
                    PHASE_INSTALLING,
                    message="Running the qualified Zeus maintenance executor.",
                    artifact=verified,
                )
                self._current_record = record
                result = method(
                    plan=validated,
                    artifact=verified,
                    runner=self.command_runner,
                    journal=self.journal,
                )
                if not isinstance(result, Mapping):
                    raise InstallError("executor_invalid", "The maintenance executor returned no safe phase.")
                phase = result.get("phase", result.get("state"))
                if phase not in {PHASE_REBOOT_REQUIRED, PHASE_INSTALLED}:
                    raise InstallError("executor_invalid", "The maintenance executor returned no safe phase.")
                # The executor persists its detailed stage state directly in
                # the journal.  Transition from that durable snapshot so the
                # final backend phase cannot erase its reboot proof.
                record = self._reload_record()
                record = self.journal.transition(
                    record,
                    phase,
                    message=(
                        "Restart Fedora, then reopen Zeus Installer to continue."
                        if phase == PHASE_REBOOT_REQUIRED
                        else "The Zeus installation completed."
                    ),
                    artifact=verified,
                    executor_result=result,
                )
                self._current_record = record
                return self._prepared_result(record)
            except (artifacts.ArtifactError, InstallError, OSError, ValueError, TypeError, KeyError) as error:
                code = getattr(error, "code", "executor_failed")
                if isinstance(error, artifacts.ArtifactError):
                    code = "artifact_tampered"
                try:
                    # Preserve any executor state that was durably written
                    # before the failure.  If the journal cannot be read, do
                    # not overwrite an ambiguous storage record with a stale
                    # in-memory snapshot.
                    record = self._reload_record()
                    record = self.journal.transition(
                        record,
                        PHASE_ERROR,
                        message="The maintenance operation did not complete safely.",
                        error=code,
                    )
                    self._current_record = record
                except InstallError:
                    pass
                raise InstallError(code, str(error)) from error
            except Exception as error:
                try:
                    record = self._reload_record()
                    record = self.journal.transition(
                        record,
                        PHASE_ERROR,
                        message="The maintenance operation did not complete safely.",
                        error="executor_failed",
                    )
                    self._current_record = record
                except InstallError:
                    pass
                raise InstallError(
                    "executor_failed", "The maintenance operation did not complete safely."
                ) from error
            finally:
                self._current_record = None

    def restart(self) -> dict[str, Any]:
        """Request the explicit reboot at the recorded safe boundary.

        A reboot is available after the qualified executor has durably
        recorded either its completed stage-one boundary or its completed
        installation.  Keeping this check in the root-owned backend prevents
        a stale GUI flag or a hand-edited request from turning the helper into
        an unconditional reboot command.
        """

        self._require_privilege()
        with self._operation(wait=True):
            record = self.journal.load()
            phase = record.get("phase") if isinstance(record, Mapping) else None
            if phase not in {PHASE_REBOOT_REQUIRED, PHASE_INSTALLED}:
                raise InstallError(
                    "reboot_not_ready",
                    "Restart Fedora, then reopen Zeus Installer to continue.",
                )
            executor_result = record.get("executor_result")
            executor_state = self._executor_state(record)
            if phase == PHASE_REBOOT_REQUIRED:
                if (
                    not isinstance(executor_result, Mapping)
                    or executor_result.get("phase", executor_result.get("state"))
                    != PHASE_REBOOT_REQUIRED
                    or not isinstance(executor_state, Mapping)
                    or executor_state.get("stage") != 1
                    or executor_state.get("phase") != PHASE_REBOOT_REQUIRED
                    or executor_state.get("status") != "complete"
                ):
                    raise InstallError(
                        "reboot_not_ready",
                        "The maintenance executor has not recorded a complete reboot boundary.",
                    )
                # Once the machine has booted again, the safe restart boundary
                # is no longer pending.  Refuse a second reboot even if a
                # stale GUI or hand-edited journal still advertises stage one
                # complete.
                executor_state = self._executor_state(record)
                old_boot = executor_state.get("old_boot_id") if isinstance(executor_state, Mapping) else None
                if isinstance(old_boot, str) and old_boot:
                    try:
                        current_boot = self.journal.boot_id()
                    except Exception as error:
                        raise InstallError(
                            "reboot_not_ready", "The current boot identity is unavailable."
                        ) from error
                    if not isinstance(current_boot, str) or not current_boot:
                        raise InstallError(
                            "reboot_not_ready", "The current boot identity is unavailable."
                        )
                    if not hmac.compare_digest(old_boot, current_boot):
                        raise InstallError(
                            "reboot_not_ready",
                            "The maintenance reboot boundary has already been crossed.",
                        )
            else:
                if (
                    not isinstance(executor_result, Mapping)
                    or executor_result.get("phase", executor_result.get("state"))
                    != PHASE_INSTALLED
                    or not isinstance(executor_state, Mapping)
                    or executor_state.get("stage") != 2
                    or executor_state.get("phase") != PHASE_INSTALLED
                    or executor_state.get("status") != "complete"
                ):
                    raise InstallError(
                        "reboot_not_ready",
                        "The completed installation boundary is not recorded safely.",
                    )
                self._verify_installed_restart_boundary(record)
            # Keep the direct root helper fail closed as well as the unprivileged
            # module facade.  A fabricated boundary must never become an
            # unconditional systemctl reboot command.
            self._qualified_executor()
            self._current_record = record
            try:
                self.command_runner.run(["/usr/bin/systemctl", "reboot"], timeout=30.0)
            finally:
                self._current_record = None
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "state": "restart_requested",
                "phase": phase,
                "operation_id": record.get("operation_id"),
                "message": "Restart Fedora, then reopen Zeus Installer to continue.",
            }

    def reconcile(self) -> dict[str, Any]:
        """Expose interrupted state without attempting an automatic resume."""

        self._require_privilege()
        with self._operation(wait=True):
            record = self.journal.load()
            if record is None:
                return {"schema_version": SCHEMA_VERSION, "ok": True, "state": PHASE_IDLE}
            if record.get("phase") in IN_PROGRESS_PHASES:
                self._current_record = record
                try:
                    record = self.journal.transition(
                        record,
                        PHASE_INTERRUPTED,
                        message="The previous installer operation was interrupted; review before retrying.",
                        error="interrupted",
                    )
                finally:
                    self._current_record = None
            return self._prepared_result(record, ok=record.get("phase") not in {PHASE_ERROR, PHASE_INTERRUPTED})


def load_plan(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a plan file for the helper without following a symlink."""

    candidate = _ensure_absolute(path, code="invalid_plan", message="The plan path must be absolute.")
    try:
        metadata = os.lstat(candidate)
    except OSError as error:
        raise InstallError("invalid_plan", "The installer plan is unavailable.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("invalid_plan", "The installer plan is not a regular file.")
    if metadata.st_size > PLAN_LIMIT:
        raise InstallError("invalid_plan", "The installer plan is too large.")
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise InstallError("invalid_plan", "The installer plan changed while it was read.")
        raw = os.read(descriptor, PLAN_LIMIT + 1)
    except InstallError:
        raise
    except (OSError, ValueError) as error:
        raise InstallError("invalid_plan", "The installer plan could not be read.") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(raw) != metadata.st_size or len(raw) > PLAN_LIMIT:
        raise InstallError("invalid_plan", "The installer plan changed while it was read.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError, MemoryError) as error:
        raise InstallError("invalid_plan", "The installer plan is not valid JSON.") from error
    return validate_plan(value)


def _consume_helper_line(
    line: Any,
    *,
    final: dict[str, Any] | None,
    progress: Callable[[Mapping[str, Any]], Any] | None,
) -> dict[str, Any] | None:
    """Parse one complete helper protocol line and return its final result.

    The streaming transport calls this as soon as a newline arrives.  Keeping
    the validation shared with the non-streaming ``run`` path ensures that a
    callback never receives unchecked values and that both transports have the
    same exactly-one-final-response contract.
    """

    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeError as error:
            raise InstallError(
                "helper_invalid_response",
                "The privileged installer returned an invalid response.",
            ) from error
    if not isinstance(line, str):
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        )
    if not line.strip():
        return final
    try:
        value = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned invalid JSON."
        ) from error
    if not isinstance(value, Mapping):
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        )
    if value.get("event") == "stage":
        stage = value.get("stage")
        if type(stage) is not str or stage not in _ADVISORY_STAGE_IDS:
            raise InstallError(
                "helper_invalid_response", "The privileged installer returned an invalid stage."
            )
        if progress is not None:
            try:
                progress({"stage": stage})
            except Exception:
                # Stage is advisory UI state.  A broken callback must not
                # change the operation result or interrupt the helper.
                pass
        return final
    if value.get("event") == "progress":
        update = value.get("progress", value)
        if (
            not isinstance(update, Mapping)
            or type(update.get("bytes")) is not int
            or type(update.get("total")) is not int
            or update["total"] <= 0
            or update["bytes"] < 0
            or update["bytes"] > update["total"]
        ):
            raise InstallError(
                "helper_invalid_response", "The privileged installer returned invalid progress."
            )
        if progress is not None:
            try:
                progress({"bytes": update["bytes"], "total": update["total"]})
            except Exception:
                # Progress is advisory UI state.  A broken callback must not
                # change the operation result or interrupt the helper.
                pass
        return final
    if final is not None:
        raise InstallError(
            "helper_invalid_response",
            "The privileged installer returned multiple final responses.",
        )
    return dict(value)


def _finish_helper_result(final: dict[str, Any] | None) -> dict[str, Any]:
    if final is None or type(final.get("ok")) is not bool:
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned no safe result."
        )
    return final


def _helper_json_result(
    stdout: Any,
    *,
    action: str,
    progress: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Decode one bounded helper response, including optional progress lines.

    The root helper emits one final JSON object and may emit explicit JSON
    progress events when requested.  Arbitrary stdout text, duplicate final
    objects, and an absent final result are hard failures.
    """

    del action  # Kept in the signature for callers and future per-action policy.
    if not isinstance(stdout, str):
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        )
    try:
        raw = stdout.encode("utf-8")
    except UnicodeError as error:
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        ) from error
    if len(raw) > _HELPER_OUTPUT_LIMIT:
        raise InstallError(
            "helper_invalid_response", "The privileged installer response is too large."
        )

    final: dict[str, Any] | None = None
    for line in stdout.splitlines():
        final = _consume_helper_line(line, final=final, progress=progress)
    return _finish_helper_result(final)


def _terminate_helper(process: Any) -> None:
    """Stop a streamed helper after a transport or protocol failure."""

    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except (OSError, ValueError, TypeError):
            pass
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            result = wait(timeout=0.25)
            if result is not None:
                return
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            pass
    kill = getattr(process, "kill", None)
    if callable(kill):
        try:
            kill()
        except (OSError, ValueError, TypeError):
            pass
    if callable(wait):
        try:
            wait(timeout=0.25)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            pass


def _invoke_helper_stream(
    argv: list[str],
    *,
    action: str,
    progress: Callable[[Mapping[str, Any]], Any],
) -> dict[str, Any]:
    """Run a progress-enabled helper with a bounded, timeout-aware pipe.

    ``readline`` on a text pipe can block forever on a malicious partial line.
    Read the fixed stdout pipe with ``os.read`` and parse only complete lines,
    checking the absolute deadline on every select.  Stderr is discarded so a
    helper cannot deadlock the operation by filling an unconsumed diagnostic
    pipe.  The final JSON result is still required even when progress arrived.
    """

    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            bufsize=0,
            shell=False,
            env=dict(CommandRunner.SAFE_ENV),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        raise InstallError("helper_unavailable", "The privileged installer is unavailable.") from error

    stream = getattr(process, "stdout", None)
    if stream is None:
        _terminate_helper(process)
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        )
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError, AttributeError, TypeError) as error:
        _terminate_helper(process)
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        ) from error

    selector: selectors.BaseSelector | None = None
    registered = False
    buffer = b""
    output_size = 0
    final: dict[str, Any] | None = None
    deadline = time.monotonic() + float(_HELPER_TIMEOUTS[action])
    try:
        selector = selectors.DefaultSelector()
        selector.register(stream, selectors.EVENT_READ)
        registered = True
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallError(
                    "helper_timeout", "The privileged installer did not finish safely."
                )
            try:
                ready = selector.select(remaining)
            except (OSError, ValueError) as error:
                raise InstallError(
                    "helper_invalid_response", "The privileged installer returned an invalid response."
                ) from error
            if not ready:
                # A child that has exited should have closed stdout; if it
                # did not, treat the missing EOF as an invalid transport rather
                # than blocking beyond the absolute deadline.
                if getattr(process, "poll", lambda: None)() is not None:
                    break
                raise InstallError(
                    "helper_timeout", "The privileged installer did not finish safely."
                )
            try:
                chunk = os.read(descriptor, min(64 * 1024, _HELPER_OUTPUT_LIMIT - output_size + 1))
            except (OSError, ValueError) as error:
                raise InstallError(
                    "helper_invalid_response", "The privileged installer returned an invalid response."
                ) from error
            if not chunk:
                eof = True
                break
            output_size += len(chunk)
            if output_size > _HELPER_OUTPUT_LIMIT:
                raise InstallError(
                    "helper_invalid_response", "The privileged installer response is too large."
                )
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                final = _consume_helper_line(line, final=final, progress=progress)
        if buffer.strip():
            final = _consume_helper_line(buffer, final=final, progress=progress)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("helper_timeout", "The privileged installer did not finish safely.")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise InstallError(
                "helper_timeout", "The privileged installer did not finish safely."
            ) from error
        if type(returncode) is not int:
            raise InstallError(
                "helper_invalid_response", "The privileged installer returned no exit status."
            )
        if returncode != 0 and final is None:
            if returncode in {126, 127}:
                raise InstallError(
                    "authorization_required",
                    "Administrator authentication is required for the installer operation.",
                )
            raise InstallError(
                "helper_failed", "The privileged installer did not complete safely."
            )
        return _finish_helper_result(final)
    except InstallError:
        _terminate_helper(process)
        raise
    except (OSError, ValueError, TypeError, AttributeError, MemoryError) as error:
        _terminate_helper(process)
        raise InstallError(
            "helper_invalid_response", "The privileged installer returned an invalid response."
        ) from error
    finally:
        if selector is not None:
            if registered:
                try:
                    selector.unregister(stream)
                except (KeyError, OSError, ValueError):
                    pass
            try:
                selector.close()
            except (OSError, ValueError):
                pass
        try:
            stream.close()
        except (OSError, ValueError, AttributeError):
            pass


def _invoke_helper(
    action: str,
    *,
    allocation_gib: int | None = None,
    expected_fingerprint: str | None = None,
    progress: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Invoke the fixed root helper with no caller-selected path or command."""

    if action not in _HELPER_TIMEOUTS:
        raise InstallError("invalid_action", "The installer action is not supported.")
    if progress is not None:
        if action not in {"prepare", "retry_prepare"}:
            raise InstallError(
                "invalid_action", "Progress streaming is supported only for preparation."
            )
        if not callable(progress):
            raise InstallError("download_invalid", "The release progress callback is invalid.")
    if allocation_gib is not None:
        allocation_gib = _helper_allocation(allocation_gib)
        if action not in {"preflight", "review", "prepare"}:
            raise InstallError("invalid_action", "This installer action does not accept an allocation.")
    if expected_fingerprint is not None:
        expected_fingerprint = _fingerprint_text(expected_fingerprint)
        if action != "prepare":
            raise InstallError("invalid_action", "This installer action does not accept a target fingerprint.")
    argv = [PKEXEC_COMMAND, HELPER_COMMAND, action]
    if allocation_gib is not None:
        argv.extend(("--allocation-gib", str(allocation_gib)))
    if expected_fingerprint is not None:
        argv.extend(("--expected-fingerprint", expected_fingerprint))
    if progress is not None:
        # This is the sole opt-in transport switch.  The root helper accepts
        # it only for prepare/retry_prepare, and never accepts a stream/path
        # selected by the unprivileged caller.
        argv.append("--progress-json")
        return _invoke_helper_stream(argv, action=action, progress=progress)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUTS[action],
            shell=False,
            check=False,
            env=dict(CommandRunner.SAFE_ENV),
        )
    except subprocess.TimeoutExpired as error:
        raise InstallError("helper_timeout", "The privileged installer did not finish safely.") from error
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        raise InstallError("helper_unavailable", "The privileged installer is unavailable.") from error
    returncode = getattr(result, "returncode", None)
    if type(returncode) is not int:
        raise InstallError("helper_invalid_response", "The privileged installer returned no exit status.")
    stdout = getattr(result, "stdout", None)
    if returncode != 0 and (not isinstance(stdout, str) or not stdout.strip()):
        if returncode in {126, 127}:
            raise InstallError(
                "authorization_required",
                "Administrator authentication is required for the installer operation.",
            )
        raise InstallError(
            "helper_failed", "The privileged installer did not complete safely."
        )
    # A helper error is still a structured response and is returned intact so
    # the GUI can display its stable code, blockers, and safe message.  Only a
    # missing/invalid response turns into a local transport error.
    return _helper_json_result(stdout, action=action, progress=progress)


def qualification() -> dict[str, Any]:
    """Return the code-level preview route qualification and its exact scope.

    The disposable Fedora VM and scoped EFI servicing gates were accepted for
    this pinned image. Each host must still pass preflight and the protected
    backup receipt guard. Install and restart check this marker again.
    """

    if QUALIFIED is True:
        return {
            "qualified": True,
            "status": "qualified",
            "scope": _QUALIFICATION_RECEIPT["scope"],
            "physical": False,
        }
    return {
        "qualified": False,
        "status": "unqualified",
        "reason": "This installer build has not completed dual-boot testing.",
    }


def preflight(*, allocation_gib: int = DEFAULT_ALLOCATION_GIB) -> dict[str, Any]:
    """Run the fixed root-side read-only preflight for one numeric allocation."""

    return _invoke_helper("preflight", allocation_gib=_helper_allocation(allocation_gib))


def review(*, allocation_gib: int = DEFAULT_ALLOCATION_GIB) -> dict[str, Any]:
    """Read current installer state, then preflight only while the target is idle."""

    return _invoke_helper("review", allocation_gib=_helper_allocation(allocation_gib))


def prepare(
    plan: Mapping[str, Any],
    progress: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Stage a signed release through the root helper using fixed scalars.

    The supplied review plan is validated locally for its allocation, support
    result, and target fingerprint, but it is never serialized into a
    privileged command.  The helper recollects and replans the current host,
    compares that exact fingerprint, and only then downloads bytes.
    """

    validated = validate_plan(plan)
    if progress is not None and not callable(progress):
        raise InstallError("download_invalid", "The release progress callback is invalid.")
    if not validated["supported"] or validated["blockers"]:
        raise InstallError(
            "unsupported_plan", "The preflight plan contains blockers and cannot be prepared."
        )
    return _invoke_helper(
        "prepare",
        allocation_gib=validated["allocation_gib"],
        expected_fingerprint=validated["fingerprint"],
        progress=progress,
    )


def status() -> dict[str, Any]:
    """Read the root-owned installer journal without accepting a plan path."""

    return _invoke_helper("status")


def retry_prepare(
    progress: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Retry only the root journal's pre-executor download staging."""

    if progress is not None and not callable(progress):
        raise InstallError("download_invalid", "The release progress callback is invalid.")
    return _invoke_helper("retry_prepare", progress=progress)


def install(plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Continue the qualified executor through the fixed root helper.

    The current package has no qualified physical executor, so this function
    fails closed.  If a separately reviewed deployment publishes the marker,
    the helper still derives its original journal plan and never accepts a
    desktop-supplied plan file.
    """

    if QUALIFIED is not True:
        raise InstallError(
            "executor_unavailable",
            "The maintenance boot executor is not qualified; no disk change was attempted.",
        )
    if plan is not None:
        validated = validate_plan(plan)
        if not validated["supported"] or validated["blockers"]:
            raise InstallError(
                "unsupported_plan", "The preflight plan contains blockers and cannot be installed."
            )
    return _invoke_helper("install")


def restart() -> dict[str, Any]:
    """Request reboot only after a qualified executor's durable boundary."""

    if QUALIFIED is not True:
        raise InstallError(
            "executor_unavailable",
            "The maintenance boot executor is not qualified; restart is unavailable.",
        )
    return _invoke_helper("restart")


def _root_preflight_plan(allocation_gib: int) -> dict[str, Any]:
    """Collect and plan the current host inside the privileged helper.

    The desktop process may show a review plan, but the helper must never
    trust a caller-selected JSON path for a privileged operation.  Recollect
    through the installed preflight worker and validate the resulting plan at
    this boundary.  Both worker calls are read-only.
    """

    allocation_gib = _helper_allocation(allocation_gib)
    try:
        module_name = f"{__package__}.preflight" if __package__ else "preflight"
        preflight = importlib.import_module(module_name)
        collect = getattr(preflight, "collect", None)
        plan_function = getattr(preflight, "plan", None)
        if not callable(collect) or not callable(plan_function):
            raise InstallError(
                "preflight_unavailable", "The privileged Fedora preflight worker is unavailable."
            )
        inventory = collect()
        if not isinstance(inventory, Mapping):
            raise InstallError(
                "preflight_failed", "The privileged Fedora preflight returned an invalid inventory."
            )
        result = plan_function(inventory, allocation_gib=allocation_gib)
    except InstallError:
        raise
    except (ImportError, ModuleNotFoundError, AttributeError) as error:
        raise InstallError(
            "preflight_unavailable", "The privileged Fedora preflight worker is unavailable."
        ) from error
    except (OSError, TypeError, ValueError, KeyError, RecursionError, MemoryError) as error:
        raise InstallError(
            "preflight_failed", "The privileged Fedora preflight could not complete safely."
        ) from error
    except Exception as error:
        raise InstallError(
            "preflight_failed", "The privileged Fedora preflight could not complete safely."
        ) from error
    if not isinstance(result, Mapping):
        raise InstallError(
            "preflight_failed", "The privileged Fedora preflight returned an invalid plan."
        )
    try:
        return validate_plan(result)
    except InstallError as error:
        raise InstallError(
            "preflight_failed", "The privileged Fedora preflight returned an invalid plan."
        ) from error


def _root_preflight_module() -> Any:
    """Load the installed preflight worker without package-level aliases."""

    module_name = f"{__package__}.preflight" if __package__ else "preflight"
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError, AttributeError) as error:
        raise InstallError(
            "preflight_unavailable", "The privileged Fedora preflight worker is unavailable."
        ) from error


def make_service() -> InstallerBackend:
    """Construct the fixed root-owned backend service.

    The helper must use one code-defined service graph for every action.  In
    particular, installing through a bare ``InstallerBackend`` would omit the
    executor entirely, while accepting an environment or user-supplied
    executor would make the privileged boundary mutable.  The executor stays
    unqualified while :data:`QUALIFIED` is false; constructing it here still
    gives the qualification harness one exact integration to exercise.
    """

    try:
        preflight = _root_preflight_module()
        collect = getattr(preflight, "collect", None)
        if not callable(collect):
            raise InstallError(
                "preflight_unavailable", "The privileged Fedora preflight worker is unavailable."
            )
        from . import efi_update
        from .executor import DualBootExecutor
    except InstallError:
        raise
    except (ImportError, ModuleNotFoundError, AttributeError) as error:
        raise InstallError(
            "executor_unavailable", "The fixed dual-boot installer service is unavailable."
        ) from error
    except Exception as error:
        raise InstallError(
            "executor_unavailable", "The fixed dual-boot installer service is unavailable."
        ) from error

    try:
        executor = DualBootExecutor(
            qualified=QUALIFIED,
            efi_update=efi_update.run_update,
            qualification_receipt=copy.deepcopy(_QUALIFICATION_RECEIPT),
            inventory_provider=collect,
        )
    except Exception as error:
        raise InstallError(
            "executor_unavailable", "The fixed dual-boot installer service is unavailable."
        ) from error
    return InstallerBackend(
        inventory_provider=collect,
        maintenance_executor=executor,
        require_root=True,
    )


def _helper_allocation(value: Any) -> int:
    """Parse the helper's fixed numeric allocation argument."""

    try:
        allocation = validate_allocation(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("allocation must be a positive whole number of GiB") from error
    if allocation > 1024 * 1024:
        raise ValueError("allocation is outside the supported range")
    return allocation


def _helper_fingerprint(value: Any) -> str:
    """Parse the reviewed plan fingerprint accepted by the fixed helper."""

    try:
        return _fingerprint_text(value)
    except InstallError as error:
        raise ValueError(str(error)) from error


def _root_inventory() -> Mapping[str, Any]:
    """Collect a fresh root-side inventory for backend revalidation."""

    try:
        module_name = f"{__package__}.preflight" if __package__ else "preflight"
        preflight = importlib.import_module(module_name)
        collect = getattr(preflight, "collect", None)
        if not callable(collect):
            raise InstallError(
                "target_unverified", "The privileged Fedora preflight worker is unavailable."
            )
        value = collect()
    except InstallError:
        raise
    except (
        ImportError,
        ModuleNotFoundError,
        AttributeError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        raise InstallError(
            "target_unverified", "The target layout could not be revalidated before writes."
        ) from error
    except Exception as error:
        raise InstallError(
            "target_unverified", "The target layout could not be revalidated before writes."
        ) from error
    if not isinstance(value, Mapping):
        raise InstallError(
            "target_unverified", "The target layout could not be revalidated before writes."
        )
    return value


def helper_main(
    argv: list[str] | None = None,
    *,
    backend: InstallerBackend | None = None,
) -> int:
    """Narrow privileged helper CLI used by the Fedora launcher."""

    import argparse

    if os.geteuid() != 0:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "state": PHASE_ERROR,
                    "error": "authorization_required",
                    "message": "Administrator authentication is required.",
                }
            )
        )
        return 1
    parser = argparse.ArgumentParser(description="Journaled Zeus dual-boot installer helper")
    parser.add_argument(
        "action",
        choices=("preflight", "review", "status", "prepare", "retry_prepare", "install", "continue", "restart"),
    )
    parser.add_argument(
        "--allocation-gib",
        "--allocation",
        type=lambda value: _helper_allocation(value),
        default=DEFAULT_ALLOCATION_GIB,
        metavar="GIB",
        help="requested total Zeus allocation in GiB for root-side preflight/prepare (default: 128)",
    )
    parser.add_argument(
        "--expected-fingerprint",
        type=_helper_fingerprint,
        default=None,
        metavar="FINGERPRINT",
        help="exact desktop-reviewed target fingerprint for root-side prepare",
    )
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="stream bounded JSON stage and byte-progress events (prepare/retry_prepare only)",
    )
    args = parser.parse_args(argv)
    try:
        if args.expected_fingerprint is not None and args.action != "prepare":
            raise InstallError(
                "invalid_action", "A target fingerprint is accepted only for prepare."
            )
        if args.progress_json and args.action not in {"prepare", "retry_prepare"}:
            raise InstallError(
                "invalid_action", "Progress streaming is supported only for preparation."
            )

        progress_callback: Callable[[Mapping[str, Any]], Any] | None = None
        if args.progress_json:
            progress_output_size = 0
            stage_output_size = 0
            byte_progress_size = 0
            last_byte_progress_at: float | None = None

            def emit_progress(update: Mapping[str, Any]) -> None:
                """Write only validated, bounded advisory events to stdout."""

                nonlocal progress_output_size, stage_output_size, byte_progress_size
                nonlocal last_byte_progress_at
                if not isinstance(update, Mapping):
                    return
                is_stage = "stage" in update
                if is_stage:
                    stage = update.get("stage")
                    if type(stage) is not str or stage not in _ADVISORY_STAGE_IDS:
                        return
                else:
                    if (
                        type(update.get("bytes")) is not int
                        or type(update.get("total")) is not int
                        or update["total"] <= 0
                        or update["bytes"] < 0
                        or update["bytes"] > update["total"]
                    ):
                        return
                    # Download helpers can report roughly once per second for
                    # a long archive.  Coalesce intermediate byte events so a
                    # bounded stream still has room for late bytes and stage
                    # events; the initial and terminal byte values are always
                    # retained.
                    now = time.monotonic()
                    done = update["bytes"]
                    total = update["total"]
                    terminal = done in {0, total}
                    if (
                        last_byte_progress_at is not None
                        and not terminal
                        and now - last_byte_progress_at < _BYTE_PROGRESS_INTERVAL
                    ):
                        return
                try:
                    event = (
                        {"event": "stage", "stage": update["stage"]}
                        if is_stage
                        else {
                            "event": "progress",
                            "progress": {
                                "bytes": update["bytes"],
                                "total": update["total"],
                            },
                        }
                    )
                    line = (
                        json.dumps(
                            event,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                except (TypeError, ValueError, OverflowError, UnicodeError, MemoryError):
                    return
                if is_stage:
                    # Reserve a separate advisory budget so a long stream of
                    # byte events cannot suppress the important stage near
                    # archive verification.
                    if stage_output_size + len(line) > _HELPER_STAGE_RESERVE:
                        return
                elif not terminal and byte_progress_size + len(line) > _HELPER_BYTE_PROGRESS_LIMIT:
                    return
                # Keep enough headroom for the final structured journal result.
                if progress_output_size + len(line) > _HELPER_PROGRESS_LIMIT:
                    return
                try:
                    print(line.decode("utf-8"), end="", flush=True)
                except (OSError, ValueError):
                    return
                progress_output_size += len(line)
                if is_stage:
                    stage_output_size += len(line)
                else:
                    byte_progress_size += len(line)
                    last_byte_progress_at = now

            progress_callback = emit_progress
        if args.action == "prepare":
            if progress_callback is not None:
                progress_callback({"stage": "checking_target"})
            selected = _root_preflight_plan(args.allocation_gib)
        elif args.action == "review":
            service = backend if backend is not None else make_service()
            # Review is one fixed privileged boundary for the launcher: read
            # the durable local state first, and only collect fresh target
            # geometry while the journal is truly idle.  Active, prepared,
            # failed, or interrupted records are returned unchanged so a
            # refresh cannot silently replace their authoritative state with a
            # new preflight plan.
            status_result = service.status()
            if status_result.get("ok") is True and status_result.get("phase") == PHASE_IDLE:
                result = dict(_root_preflight_plan(args.allocation_gib))
                result["ok"] = True
                result["state"] = "preflight"
            else:
                result = status_result
        else:
            selected = None
        if args.action == "preflight":
            result = dict(_root_preflight_plan(args.allocation_gib))
            result["ok"] = True
            result["state"] = "preflight"
        elif args.action == "prepare":
            if (
                args.expected_fingerprint is not None
                and not hmac.compare_digest(selected["fingerprint"], args.expected_fingerprint)
            ):
                result = dict(selected)
                result.update(
                    {
                        "ok": False,
                        "state": PHASE_ERROR,
                        "phase": PHASE_ERROR,
                        "error": "target_mismatch",
                        "reviewed_fingerprint": args.expected_fingerprint,
                        "message": "The target layout changed after review; run preflight again before preparing.",
                    }
                )
            elif not selected["supported"] or selected["blockers"]:
                result = dict(selected)
                result.update(
                    {
                        "ok": False,
                        "state": PHASE_ERROR,
                        "phase": PHASE_ERROR,
                        "error": "unsupported_plan",
                        "message": "The privileged preflight contains blockers; no download was attempted.",
                    }
                )
            else:
                service = backend if backend is not None else make_service()
                if progress_callback is None:
                    result = service.prepare(selected)
                else:
                    result = service.prepare(selected, progress=progress_callback)
        elif args.action == "review":
            pass
        else:
            # A default helper backend always re-collects through the installed
            # preflight worker before target validation.  Injected fixture
            # backends remain available to tests and qualification harnesses.
            service = backend if backend is not None else make_service()
            if args.action == "status":
                result = service.status()
            elif args.action == "retry_prepare":
                if progress_callback is None:
                    result = service.retry_prepare()
                else:
                    result = service.retry_prepare(progress=progress_callback)
            elif args.action == "restart":
                result = service.restart()
            else:
                result = service.install()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if result.get("ok") else 1
    except InstallError as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "state": PHASE_ERROR,
                    "error": error.code,
                    "message": str(error),
                }
            )
        )
        return 1
    except artifacts.ArtifactError as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "state": PHASE_ERROR,
                    "error": getattr(error, "code", "operation_failed"),
                    "message": "The installer operation could not finish safely.",
                }
            )
        )
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "state": PHASE_ERROR,
                    "error": "operation_failed",
                    "message": "The installer operation could not finish safely.",
                }
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "state": PHASE_ERROR,
                    "error": "operation_failed",
                    "message": "The installer operation could not finish safely.",
                }
            )
        )
        return 1


main = helper_main


__all__ = [
    "CommandRunner",
    "DEFAULT_ALLOCATION_GIB",
    "DEFAULT_ROOT",
    "HELPER_COMMAND",
    "InstallError",
    "InstallerBackend",
    "Journal",
    "MaintenanceExecutor",
    "PKEXEC_COMMAND",
    "QUALIFIED",
    "SCHEMA_VERSION",
    "compute_fingerprint",
    "current_boot_id",
    "fingerprint_inventory",
    "helper_main",
    "install",
    "load_plan",
    "make_service",
    "main",
    "preflight",
    "prepare",
    "qualification",
    "review",
    "restart",
    "retry_prepare",
    "status",
    "target_fingerprint",
    "utc_now",
    "validate_plan",
    "validate_target",
]
