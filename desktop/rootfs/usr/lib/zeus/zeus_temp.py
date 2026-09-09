#!/usr/bin/env python3
"""Owner-scoped temporary download storage and cleanup.

The module deliberately keeps the policy and the destructive operation in one
small, dependency-free implementation.  ``HomeManager`` accepts an explicit
home directory so tests can use disposable fixtures.  The module-level API
and the installed command use the real owner's home directory.

Cleanup never accepts a caller supplied root.  A provisioned ``~/Temp`` root
is identified by its device/inode pair in the state file and is opened with
``O_NOFOLLOW``.  Children are walked and removed through directory file
descriptors, so a path replacement cannot turn a cleanup into an arbitrary
directory delete.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import errno
import fcntl
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
POLICY_VERSION = 1
STATE_DIR = Path(".local") / "state" / "zeus"
STATE_FILENAME = "temp-policy.json"
LOCK_FILENAME = "temp.lock"
MARKER_FILENAME = ".zeus-temp-root"
DEFAULT_DESTINATION = "Documents"
MIN_CUSTOM_INTERVAL = 15 * 60
PRESET_INTERVALS = {
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}
POLICY_MODES = {"boot", "hourly", "daily", "weekly", "custom", "never"}
TIMED_POLICY_MODES = frozenset(PRESET_INTERVALS) | {"custom"}
MIN_RETRY_INTERVAL = MIN_CUSTOM_INTERVAL
_DEFAULT_PROC_ROOT = object()

# These are the names used by the common browser download implementations.
# A marker is a reason to preserve an entry during automatic cleanup; it is
# not an age policy and it does not make arbitrary filename suffixes active.
PARTIAL_SUFFIXES = (
    ".part",
    ".crdownload",
    ".download",
    ".partial",
    ".filepart",
    ".opdownload",
    ".aria2",
)
PARTIAL_NAMES = {".tmp", ".temp", "download.tmp", "download.part"}


class TempError(RuntimeError):
    """Base class for safe, user-facing Temp failures."""


class SetupError(TempError):
    pass


class TempCollisionError(SetupError):
    pass


class UnsafeTempError(TempError):
    pass


class NotEnrolledError(TempError):
    pass


class UnsupportedStateError(TempError):
    pass


class PolicyError(TempError):
    pass


class DestinationError(TempError):
    pass


class ActiveFileError(TempError):
    pass


class InspectionError(TempError):
    pass


@dataclass(frozen=True)
class _Candidate:
    """A snapshot entry, addressed by relative path components."""

    parts: tuple[str, ...]
    kind: str
    st_dev: int
    st_ino: int
    st_mode: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int

    @property
    def rel(self) -> str:
        return "/".join(self.parts)

    @property
    def identity(self) -> tuple[int, int]:
        return (self.st_dev, self.st_ino)


@dataclass(frozen=True)
class _Root:
    fd: int
    st_dev: int
    st_ino: int
    st_uid: int
    st_mode: int


def _jsonable_time(value: Any) -> float:
    """Convert test clocks and normal time values to a finite timestamp."""

    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_datetime.timezone.utc)
        return float(value.timestamp())
    if isinstance(value, _datetime.date):
        return float(
            _datetime.datetime.combine(
                value, _datetime.time(), tzinfo=_datetime.timezone.utc
            ).timestamp()
        )
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError("clock returned a non-finite timestamp")
    return result


def _iso_time(value: float | None) -> str | None:
    if value is None:
        return None
    return (
        _datetime.datetime.fromtimestamp(value, tz=_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _uid() -> int:
    return os.getuid()


def _flags(*names: str) -> int:
    result = 0
    for name in names:
        result |= int(getattr(os, name, 0))
    return result


_DIR_FLAGS = _flags("O_RDONLY", "O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW")
_ROOT_FLAGS = _DIR_FLAGS
_READ_FLAGS = _flags("O_RDONLY", "O_CLOEXEC", "O_NOFOLLOW")


def _is_missing(error: OSError) -> bool:
    return error.errno in (errno.ENOENT, errno.ESTALE)


def _path_parts(path: Path) -> tuple[str, ...]:
    """Return safe relative POSIX path components, rejecting traversal."""

    # Paths are used on Linux.  Explicitly reject backslashes too so a value
    # coming from another platform cannot be interpreted inconsistently.
    raw = os.fspath(path)
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("path must be a non-empty relative path")
    p = Path(raw)
    if p.is_absolute():
        raise ValueError("absolute paths are not allowed")
    pieces = tuple(p.parts)
    if not pieces or any(piece in ("", ".", "..") for piece in pieces):
        raise ValueError("path traversal is not allowed")
    return pieces


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_uid == right.st_uid
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _matches_candidate(value: os.stat_result, candidate: _Candidate) -> bool:
    """Compare a live stat to the integer metadata captured in a snapshot."""

    return (
        value.st_dev == candidate.st_dev
        and value.st_ino == candidate.st_ino
        and value.st_uid == _uid()
        and stat.S_IFMT(value.st_mode) == stat.S_IFMT(candidate.st_mode)
        and value.st_size == candidate.st_size
        and value.st_mtime_ns == candidate.st_mtime_ns
        and value.st_ctime_ns == candidate.st_ctime_ns
    )


def _same_content_stat(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare fields relevant to a stable file read, excluding link count."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_uid == right.st_uid
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _matches_for_remove(value: os.stat_result, candidate: _Candidate) -> bool:
    """Check identity/type before removal, allowing directory metadata changes."""

    matches = (
        value.st_dev == candidate.st_dev
        and value.st_ino == candidate.st_ino
        and value.st_uid == _uid()
        and stat.S_IFMT(value.st_mode) == stat.S_IFMT(candidate.st_mode)
    )
    if not matches:
        return False
    # Removing a child necessarily changes its parent's size and timestamps;
    # require the full snapshot match for files, but let rmdir perform the
    # final no-new-children check for directories.
    return candidate.kind == "dir" or (
        value.st_size == candidate.st_size
        and value.st_mtime_ns == candidate.st_mtime_ns
        and value.st_ctime_ns == candidate.st_ctime_ns
    )


def _stat_fields(value: os.stat_result) -> dict[str, int]:
    return {
        "st_dev": int(value.st_dev),
        "st_ino": int(value.st_ino),
        "st_uid": int(value.st_uid),
        "st_mode": int(value.st_mode),
        "st_size": int(value.st_size),
        "st_mtime_ns": int(value.st_mtime_ns),
        "st_ctime_ns": int(value.st_ctime_ns),
    }


def _mode_is_private(mode: int) -> bool:
    return stat.S_IMODE(mode) & 0o077 == 0


def _normalize_mode(mode: Any) -> str:
    if not isinstance(mode, str):
        raise PolicyError("policy mode must be a string")
    value = mode.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "on-boot": "boot",
        "onboot": "boot",
        "boot-once": "boot",
        "bootonce": "boot",
        "every-hour": "hourly",
        "hour": "hourly",
        "every-day": "daily",
        "day": "daily",
        "every-week": "weekly",
        "week": "weekly",
        "off": "never",
        "disabled": "never",
    }
    value = aliases.get(value, value)
    if value not in POLICY_MODES:
        raise PolicyError(
            "unsupported policy mode; choose boot, hourly, daily, weekly, "
            "custom, or never"
        )
    return value


def _policy(mode: Any, interval_seconds: Any = None) -> dict[str, Any]:
    mode = _normalize_mode(mode)
    if mode in PRESET_INTERVALS:
        if interval_seconds is not None and int(interval_seconds) != PRESET_INTERVALS[mode]:
            raise PolicyError(f"{mode} policy has a fixed interval")
        interval = PRESET_INTERVALS[mode]
    elif mode == "custom":
        if isinstance(interval_seconds, bool):
            raise PolicyError("custom interval must be at least 15 minutes")
        try:
            interval = int(interval_seconds)
        except (TypeError, ValueError, OverflowError):
            raise PolicyError("custom interval must be at least 15 minutes") from None
        if interval < MIN_CUSTOM_INTERVAL:
            raise PolicyError("custom interval must be at least 15 minutes")
    else:
        if interval_seconds is not None:
            raise PolicyError(f"{mode} policy does not accept an interval")
        interval = None
    return {"mode": mode, "interval_seconds": interval}


def _current_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
            value = stream.read().strip()
    except OSError as error:
        raise InspectionError(f"cannot read the OS boot identifier: {error}") from error
    if not value:
        raise InspectionError("the OS boot identifier is empty")
    return value


class HomeManager:
    """Manage the private Temp directory belonging to one home directory.

    ``home`` is intentionally explicit for disposable tests.  Production code
    should use the module-level functions, which construct this class from
    the passwd owner home and have no environment-controlled home override.
    """

    def __init__(
        self,
        home: os.PathLike[str] | str,
        *,
        now: Callable[[], Any] | Any | None = None,
        boot_id: Callable[[], str] | str | None = None,
        proc_root: os.PathLike[str] | str | None | object = _DEFAULT_PROC_ROOT,
        state_path: os.PathLike[str] | str | None = None,
        lock_path: os.PathLike[str] | str | None = None,
        clock: Any | None = None,
    ) -> None:
        home_path = Path(home)
        if not home_path.is_absolute():
            home_path = Path.cwd() / home_path
        self.home = Path(os.path.abspath(os.fspath(home_path)))
        self.temp_path = self.home / "Temp"
        self.downloads_path = self.home / "Downloads"
        if proc_root is _DEFAULT_PROC_ROOT:
            # The production default is always the brokered /proc inspector.
            # A disposable fixture must explicitly pass proc_root=None (or a
            # fake proc tree); a custom home alone must never silently disable
            # active-file protection.
            self.proc_root = Path("/proc")
        else:
            self.proc_root = Path(proc_root) if proc_root is not None else None

        if clock is not None:
            if now is None:
                now = getattr(clock, "now", getattr(clock, "time", None))
            if boot_id is None:
                boot_id = getattr(clock, "boot_id", None)
        self._now_source = now
        self._boot_id_source = boot_id

        self.state_path = Path(state_path) if state_path is not None else self.home / STATE_DIR / STATE_FILENAME
        self.lock_path = Path(lock_path) if lock_path is not None else self.home / STATE_DIR / LOCK_FILENAME
        self.state_path = Path(os.path.abspath(os.fspath(self.state_path)))
        self.lock_path = Path(os.path.abspath(os.fspath(self.lock_path)))
        try:
            self.state_path.relative_to(self.home)
            self.lock_path.relative_to(self.home)
        except ValueError as error:
            raise ValueError("state and lock paths must be inside the home") from error
        if self.state_path.parent != self.lock_path.parent:
            raise ValueError("state and lock files must share a directory")
        self.state_dir = self.state_path.parent
        self._state_relative = self.state_path.relative_to(self.home)
        self._lock_relative = self.lock_path.relative_to(self.home)

    # ------------------------------------------------------------------
    # Clock and ownership helpers
    # ------------------------------------------------------------------
    def _now(self) -> float:
        source = self._now_source
        if source is None:
            return time.time()
        if callable(source):
            source = source()
        return _jsonable_time(source)

    def _boot_id(self) -> str:
        source = self._boot_id_source
        if source is None:
            return _current_boot_id()
        if callable(source):
            source = source()
        if not isinstance(source, str) or not source.strip():
            raise InspectionError("the OS boot identifier is empty")
        return source.strip()

    def _ensure_home(self) -> None:
        try:
            value = os.lstat(self.home)
        except OSError as error:
            raise SetupError(f"home directory is unavailable: {error}") from error
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise SetupError("home is not a real directory")
        if value.st_uid != _uid():
            raise SetupError("home directory is not owned by the current user")

    def _open_home(self) -> int:
        self._ensure_home()
        try:
            fd = os.open(self.home, _ROOT_FLAGS)
        except OSError as error:
            raise SetupError(f"cannot open home directory: {error}") from error
        try:
            value = os.fstat(fd)
            if not stat.S_ISDIR(value.st_mode) or value.st_uid != _uid():
                raise SetupError("home directory changed or is not owner-owned")
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _open_child_dir(parent_fd: int, name: str, *, create: bool = False) -> int:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise SetupError(f"cannot open directory {name!r}: {error}") from error
        try:
            value = os.fstat(fd)
            if not stat.S_ISDIR(value.st_mode) or value.st_uid != _uid():
                raise SetupError(f"directory {name!r} is not owner-owned")
            return fd
        except Exception:
            os.close(fd)
            raise

    def _ensure_state_dir(self) -> int:
        home_fd = self._open_home()
        current_fd = home_fd
        try:
            # State paths are relative to home and were validated in __init__.
            for component in self._state_relative.parent.parts:
                next_fd = self._open_child_dir(current_fd, component, create=True)
                if current_fd != home_fd:
                    os.close(current_fd)
                current_fd = next_fd
            # Use the already validated descriptor.  A path-based chmod here
            # would re-resolve components after the no-follow opens and could
            # touch a replacement path during a concurrent rename.
            if current_fd != home_fd:
                os.fchmod(current_fd, 0o700)
            if current_fd != home_fd:
                os.close(home_fd)
            return current_fd
        except Exception:
            if current_fd != home_fd:
                os.close(current_fd)
            os.close(home_fd)
            raise

    def _ensure_state_file_parent(self) -> int:
        return self._ensure_state_dir()

    @contextlib.contextmanager
    def _lock(self) -> Iterator[int]:
        state_dir_fd = self._ensure_state_file_parent()
        lock_name = self._lock_relative.name
        flags = _flags("O_RDWR", "O_CREAT", "O_CLOEXEC", "O_NOFOLLOW")
        try:
            fd = os.open(lock_name, flags, 0o600, dir_fd=state_dir_fd)
        except OSError as error:
            os.close(state_dir_fd)
            raise TempError(f"cannot open Temp state lock: {error}") from error
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != _uid():
                raise TempError("Temp state lock is not owner-owned")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield state_dir_fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            os.close(state_dir_fd)

    # ------------------------------------------------------------------
    # Versioned policy state
    # ------------------------------------------------------------------
    def _read_state(self, state_dir_fd: int) -> dict[str, Any] | None:
        state_name = self._state_relative.name
        flags = _flags("O_RDONLY", "O_CLOEXEC", "O_NOFOLLOW")
        try:
            fd = os.open(state_name, flags, dir_fd=state_dir_fd)
        except OSError as error:
            if _is_missing(error):
                return None
            raise TempError(f"cannot read Temp state: {error}") from error
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != _uid():
                raise UnsupportedStateError("Temp state is not an owner-owned regular file")
            if value.st_size > 1024 * 1024:
                raise UnsupportedStateError("Temp state is unexpectedly large")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            try:
                data = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise UnsupportedStateError("Temp state is not valid JSON") from error
        finally:
            os.close(fd)
        return self._validate_state(data)

    def _validate_state(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise UnsupportedStateError("Temp state has an invalid schema")
        version = data.get("schema_version", data.get("version"))
        if version != SCHEMA_VERSION:
            raise UnsupportedStateError(
                f"unsupported Temp state version {version!r}; cleanup is disabled"
            )
        if data.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            raise UnsupportedStateError("unsupported Temp policy version; cleanup is disabled")
        root = data.get("root")
        if not isinstance(root, dict):
            raise UnsupportedStateError("Temp state has no root identity")
        if "relative_path" in root and root.get("relative_path") != "Temp":
            raise UnsupportedStateError("Temp state names an unsupported cleanup root")
        for key in ("st_dev", "st_ino", "st_uid"):
            if not isinstance(root.get(key), int) or root[key] < 0:
                raise UnsupportedStateError("Temp state has an invalid root identity")
        policy_data = data.get("policy")
        if not isinstance(policy_data, dict):
            raise UnsupportedStateError("Temp state has no policy")
        try:
            policy = _policy(policy_data.get("mode"), policy_data.get("interval_seconds"))
        except PolicyError as error:
            raise UnsupportedStateError(str(error)) from error
        # Stored policy values are canonical.  This prevents an old binary
        # from treating an unknown mode as permission to delete.
        if policy_data.get("mode") != policy["mode"] or policy_data.get("interval_seconds") != policy["interval_seconds"]:
            raise UnsupportedStateError("Temp state policy is not canonical")
        for key in ("last_boot_id",):
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                raise UnsupportedStateError(f"Temp state field {key} is invalid")
        for key in ("last_sweep_at", "next_cleanup_at", "policy_changed_at", "manual_clear_at"):
            value = data.get(key)
            if value is not None and (not isinstance(value, (int, float)) or value != value):
                raise UnsupportedStateError(f"Temp state field {key} is invalid")
        if data.get("in_progress") not in (None, False, True):
            raise UnsupportedStateError("Temp state in_progress field is invalid")
        result = dict(data)
        result["schema_version"] = SCHEMA_VERSION
        result["policy_version"] = POLICY_VERSION
        result["policy"] = policy
        result["in_progress"] = bool(data.get("in_progress", False))
        return result

    def _write_state(self, state_dir_fd: int, state: Mapping[str, Any]) -> None:
        # Validate before replacing the old state.  If validation fails, the
        # previous state remains intact and cleanup cannot become authorized.
        value = self._validate_state(dict(state))
        payload = json.dumps(value, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
        temporary_name = f".{self._state_relative.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = _flags("O_WRONLY", "O_CREAT", "O_EXCL", "O_CLOEXEC", "O_NOFOLLOW")
        fd = os.open(temporary_name, flags, 0o600, dir_fd=state_dir_fd)
        try:
            os.fchmod(fd, 0o600)
            data = payload.encode("utf-8")
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(
                temporary_name,
                self._state_relative.name,
                src_dir_fd=state_dir_fd,
                dst_dir_fd=state_dir_fd,
            )
            with contextlib.suppress(OSError):
                os.fsync(state_dir_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=state_dir_fd)

    @staticmethod
    def _new_state(root: os.stat_result, now: float) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "root": {
                "relative_path": "Temp",
                "st_dev": int(root.st_dev),
                "st_ino": int(root.st_ino),
                "st_uid": int(root.st_uid),
            },
            "policy": {"mode": "boot", "interval_seconds": None},
            "last_boot_id": None,
            "last_sweep_at": None,
            "next_cleanup_at": None,
            "policy_changed_at": now,
            "manual_clear_at": None,
            "in_progress": False,
            "last_result": None,
            "last_error": None,
        }

    # ------------------------------------------------------------------
    # Temp enrollment and root containment
    # ------------------------------------------------------------------
    def _open_temp_root(
        self,
        *,
        expected: Mapping[str, Any] | None = None,
        require_marker: bool = True,
    ) -> _Root:
        home_fd = self._open_home()
        try:
            flags = _ROOT_FLAGS
            fd = os.open("Temp", flags, dir_fd=home_fd)
        except OSError as error:
            os.close(home_fd)
            if _is_missing(error):
                raise NotEnrolledError("~/Temp has not been provisioned") from error
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafeTempError("~/Temp must be a real directory, not a symlink") from error
            raise UnsafeTempError(f"cannot open ~/Temp safely: {error}") from error
        finally:
            # The opened root remains valid after the home descriptor closes.
            pass
        os.close(home_fd)
        try:
            value = os.fstat(fd)
            if not stat.S_ISDIR(value.st_mode):
                raise UnsafeTempError("~/Temp is not a directory")
            if value.st_uid != _uid() or not _mode_is_private(value.st_mode):
                raise UnsafeTempError("~/Temp must be private and owner-owned")
            try:
                home_value = os.stat(self.home, follow_symlinks=False)
            except OSError as error:
                raise UnsafeTempError("home changed while opening ~/Temp") from error
            if value.st_dev != home_value.st_dev:
                raise UnsafeTempError("~/Temp is on a different filesystem")
            mount_points, mount_issues = self._mount_points()
            if mount_issues:
                raise UnsafeTempError("cannot inspect mount boundaries for ~/Temp")
            if () in mount_points:
                raise UnsafeTempError("~/Temp is a mount point")
            # Check the path and the descriptor agree.  The descriptor is what
            # cleanup uses; this check turns a replacement into an actionable
            # status instead of silently accepting a new root.
            try:
                path_value = os.stat(self.temp_path, follow_symlinks=False)
            except OSError as error:
                raise UnsafeTempError("cannot verify ~/Temp after opening") from error
            if not _same_stat(value, path_value):
                raise UnsafeTempError("~/Temp changed while it was opened")
            if expected is not None:
                if value.st_dev != int(expected.get("st_dev", -1)) or value.st_ino != int(expected.get("st_ino", -1)):
                    raise UnsafeTempError("~/Temp no longer matches its enrolled identity")
            if require_marker:
                try:
                    marker_fd = os.open(MARKER_FILENAME, _READ_FLAGS, dir_fd=fd)
                except OSError as error:
                    raise UnsafeTempError("Temp enrollment marker is missing or unsafe") from error
                try:
                    marker_value = os.fstat(marker_fd)
                    if not stat.S_ISREG(marker_value.st_mode) or marker_value.st_uid != _uid():
                        raise UnsafeTempError("Temp enrollment marker is unsafe")
                    if not _mode_is_private(marker_value.st_mode):
                        raise UnsafeTempError("Temp enrollment marker is not private")
                    if marker_value.st_size > 64 * 1024:
                        raise UnsafeTempError("Temp enrollment marker is unexpectedly large")
                    marker_bytes = bytearray()
                    try:
                        while True:
                            chunk = os.read(marker_fd, 16 * 1024)
                            if not chunk:
                                break
                            marker_bytes.extend(chunk)
                    except OSError as error:
                        raise UnsafeTempError("cannot read Temp enrollment marker") from error
                    try:
                        marker_data = json.loads(bytes(marker_bytes).decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise UnsafeTempError("Temp enrollment marker is invalid") from error
                    marker_root = marker_data.get("root") if isinstance(marker_data, dict) else None
                    if (
                        not isinstance(marker_data, dict)
                        or marker_data.get("schema_version") != SCHEMA_VERSION
                        or not isinstance(marker_root, dict)
                        or marker_root.get("st_dev") != int(value.st_dev)
                        or marker_root.get("st_ino") != int(value.st_ino)
                        or marker_root.get("st_uid") != _uid()
                    ):
                        raise UnsafeTempError("Temp enrollment marker does not match ~/Temp")
                finally:
                    os.close(marker_fd)
            return _Root(fd, int(value.st_dev), int(value.st_ino), int(value.st_uid), int(value.st_mode))
        except Exception:
            os.close(fd)
            raise

    def _create_marker(self, root_fd: int, root: os.stat_result) -> None:
        flags = _flags("O_WRONLY", "O_CREAT", "O_EXCL", "O_CLOEXEC", "O_NOFOLLOW")
        try:
            fd = os.open(MARKER_FILENAME, flags, 0o600, dir_fd=root_fd)
        except FileExistsError:
            try:
                marker_fd = os.open(MARKER_FILENAME, _READ_FLAGS, dir_fd=root_fd)
            except OSError as error:
                raise UnsafeTempError("Temp enrollment marker is unsafe") from error
            try:
                marker = os.fstat(marker_fd)
                if not stat.S_ISREG(marker.st_mode) or marker.st_uid != _uid() or not _mode_is_private(marker.st_mode):
                    raise UnsafeTempError("Temp enrollment marker is unsafe")
            finally:
                os.close(marker_fd)
            return
        except OSError as error:
            raise SetupError(f"cannot create Temp enrollment marker: {error}") from error
        payload = {
            "schema_version": SCHEMA_VERSION,
            "root": {"st_dev": int(root.st_dev), "st_ino": int(root.st_ino), "st_uid": int(root.st_uid)},
        }
        try:
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _make_temp(self, home_fd: int) -> _Root:
        try:
            os.mkdir("Temp", 0o700, dir_fd=home_fd)
        except FileExistsError as error:
            raise TempCollisionError("~/Temp already exists; explicit setup adoption is required") from error
        except OSError as error:
            raise SetupError(f"cannot provision ~/Temp: {error}") from error
        fd = os.open("Temp", _ROOT_FLAGS, dir_fd=home_fd)
        try:
            root = os.fstat(fd)
            if not stat.S_ISDIR(root.st_mode) or root.st_uid != _uid() or not _mode_is_private(root.st_mode):
                raise UnsafeTempError("new ~/Temp is not private and owner-owned")
            self._create_marker(fd, root)
            return _Root(fd, int(root.st_dev), int(root.st_ino), int(root.st_uid), int(root.st_mode))
        except Exception:
            os.close(fd)
            raise

    def setup(
        self,
        *,
        adopt: bool = False,
        collision: str | None = None,
        include_usage: bool = True,
    ) -> dict[str, Any]:
        """Provision Temp and the default boot policy.

        Existing ``~/Temp`` is adopted only when ``adopt=True`` (or the
        explicit ``collision='adopt'`` choice is supplied).  Existing files,
        including ``Downloads``, are never moved or deleted.
        """

        if collision is not None:
            if collision.strip().lower() in ("adopt", "use-existing", "enroll"):
                adopt = True
            elif collision.strip().lower() in ("refuse", "cancel"):
                adopt = False
            else:
                raise SetupError("collision choice must be adopt or refuse")
        now = self._now()
        with self._lock() as state_dir_fd:
            self._ensure_home()
            state = self._read_state(state_dir_fd)
            home_fd = self._open_home()
            root: _Root | None = None
            created = False
            try:
                try:
                    value = os.lstat(self.temp_path)
                except OSError as error:
                    if not _is_missing(error):
                        raise SetupError(f"cannot inspect ~/Temp: {error}") from error
                    value = None
                if value is None:
                    root = self._make_temp(home_fd)
                    created = True
                else:
                    if stat.S_ISLNK(value.st_mode):
                        raise UnsafeTempError("~/Temp is a symlink; it will not be adopted")
                    if not stat.S_ISDIR(value.st_mode):
                        raise TempCollisionError("~/Temp exists and is not a directory")
                    if value.st_uid != _uid():
                        raise TempCollisionError("~/Temp exists but is not owned by the current user")
                    enrolled_identity = (
                        state is not None
                        and value.st_dev == int(state["root"]["st_dev"])
                        and value.st_ino == int(state["root"]["st_ino"])
                    )
                    marker_exists = False
                    try:
                        marker = os.lstat(self.temp_path / MARKER_FILENAME)
                        marker_exists = stat.S_ISREG(marker.st_mode) and marker.st_uid == _uid() and _mode_is_private(marker.st_mode)
                    except OSError as error:
                        if not _is_missing(error):
                            raise UnsafeTempError(f"cannot inspect Temp enrollment marker: {error}") from error
                    if not enrolled_identity and not marker_exists and not adopt:
                        raise TempCollisionError(
                            "~/Temp already exists; choose explicit adoption before enabling cleanup"
                        )
                    if not _mode_is_private(value.st_mode):
                        if not adopt:
                            raise UnsafeTempError("existing ~/Temp is not private; explicit adoption is required")
                        os.chmod(self.temp_path, 0o700, follow_symlinks=False)
                    root = self._open_temp_root(
                        expected=state["root"] if enrolled_identity else None,
                        require_marker=marker_exists,
                    )
                    if not marker_exists:
                        self._create_marker(root.fd, os.fstat(root.fd))
                assert root is not None
                root_stat = os.fstat(root.fd)
                if state is None:
                    state = self._new_state(root_stat, now)
                    self._write_state(state_dir_fd, state)
                else:
                    if state["root"]["st_dev"] != root.st_dev or state["root"]["st_ino"] != root.st_ino:
                        raise UnsafeTempError("Temp root does not match the enrolled state")
                    # Preserve the policy and bookkeeping across idempotent
                    # setup calls and image updates.
                result = self._status_locked(
                    state,
                    root=root,
                    setup_created=created,
                    include_usage=bool(include_usage),
                )
                result.update({"setup": True, "created": created, "adopted": bool(not created and adopt)})
                return result
            finally:
                if root is not None:
                    os.close(root.fd)
                os.close(home_fd)

    # ------------------------------------------------------------------
    # Safe fd-relative traversal and active-file inspection
    # ------------------------------------------------------------------
    def _open_relative_parent(self, root_fd: int, parts: Sequence[str], root_dev: int) -> tuple[int, str]:
        if not parts:
            raise InspectionError("an entry path is empty")
        current_fd = os.dup(root_fd)
        try:
            for component in parts[:-1]:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                value = os.fstat(current_fd)
                if not stat.S_ISDIR(value.st_mode) or value.st_uid != _uid() or value.st_dev != root_dev:
                    raise InspectionError("entry parent escaped the Temp filesystem")
            return current_fd, parts[-1]
        except Exception:
            os.close(current_fd)
            raise

    def _stat_relative(self, root_fd: int, parts: Sequence[str], root_dev: int) -> os.stat_result:
        parent_fd, name = self._open_relative_parent(root_fd, parts, root_dev)
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(parent_fd)

    def _mount_points(self) -> tuple[set[tuple[str, ...]], list[str]]:
        """Read Linux mount points so same-device bind mounts are bounded.

        Device checks catch ordinary mounts.  Linux bind mounts can retain the
        same ``st_dev`` as Temp, so mountinfo supplies the additional boundary
        check.  A test manager may pass ``proc_root=None`` to use a disposable
        fixture without a proc filesystem.
        """

        if self.proc_root is None:
            return set(), []
        mountinfo = self.proc_root / "self" / "mountinfo"
        try:
            content = mountinfo.read_text(encoding="utf-8")
        except OSError as error:
            return set(), [f"cannot inspect mount boundaries: {error}"]
        points: set[tuple[str, ...]] = set()
        for line in content.splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            # mountinfo escapes spaces, tabs, and backslashes as octal values.
            mountpoint = fields[4]
            mountpoint = mountpoint.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
            try:
                point = Path(mountpoint).resolve(strict=False)
                relative = point.relative_to(self.temp_path.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
            if relative == Path("."):
                # Keep the root mount marker too.  A same-device bind mount
                # cannot be identified from st_dev alone and must not become
                # an enrolled cleanup root.
                points.add(())
                continue
            try:
                parts = _path_parts(relative)
            except ValueError:
                continue
            points.add(parts)
        return points, []

    def _snapshot(self, root: _Root) -> tuple[list[_Candidate], list[str]]:
        candidates: list[_Candidate] = []
        issues: list[str] = []
        mount_points, mount_issues = self._mount_points()
        issues.extend(mount_issues)

        def walk(dir_fd: int, prefix: tuple[str, ...]) -> None:
            try:
                names = os.listdir(dir_fd)
            except OSError as error:
                issues.append(f"cannot inspect Temp/{'/'.join(prefix)}: {error}")
                return
            for name in names:
                if not isinstance(name, str) or not name or name in (".", "..") or "\x00" in name:
                    issues.append("Temp contains an invalid entry name")
                    continue
                parts = prefix + (name,)
                if parts == (MARKER_FILENAME,):
                    continue
                try:
                    value = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError as error:
                    if not _is_missing(error):
                        issues.append(f"cannot inspect Temp/{'/'.join(parts)}: {error}")
                    continue
                mode_type = stat.S_IFMT(value.st_mode)
                if value.st_dev != root.st_dev or parts in mount_points:
                    # A different device can be a mounted child.  Do not
                    # inspect or remove anything below it.
                    candidates.append(_Candidate(parts, "mount", int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns)))
                    continue
                if mode_type == stat.S_IFLNK:
                    candidates.append(_Candidate(parts, "symlink", int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns)))
                    continue
                if mode_type == stat.S_IFDIR:
                    candidate = _Candidate(parts, "dir", int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns))
                    candidates.append(candidate)
                    try:
                        child_fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                    except OSError as error:
                        issues.append(f"cannot inspect Temp/{candidate.rel}: {error}")
                        continue
                    try:
                        child_value = os.fstat(child_fd)
                        if not _same_stat(value, child_value):
                            issues.append(f"Temp/{candidate.rel} changed during inspection")
                            continue
                        walk(child_fd, parts)
                    finally:
                        os.close(child_fd)
                    continue
                if mode_type == stat.S_IFREG:
                    kind = "file"
                else:
                    kind = "special"
                candidates.append(_Candidate(parts, kind, int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns)))

        walk(root.fd, ())
        # A root replacement is harmless to the held descriptor but must be
        # surfaced as a failed inspection rather than silently accepted.
        try:
            path_value = os.stat(self.temp_path, follow_symlinks=False)
            fd_value = os.fstat(root.fd)
            if not _same_stat(path_value, fd_value):
                issues.append("~/Temp changed during snapshot")
        except OSError as error:
            issues.append(f"cannot verify ~/Temp after snapshot: {error}")
        return candidates, issues

    def _scan_active(self, candidates: Iterable[_Candidate]) -> tuple[set[tuple[int, int]], bool, list[str]]:
        """Find same-user fds and report whether inspection was complete."""

        active: set[tuple[int, int]] = set()
        issues: list[str] = []
        wanted = {candidate.identity for candidate in candidates}
        if not wanted:
            # No entry can be deleted, so an active-file inspection is not
            # needed for an empty snapshot.
            return active, True, []
        if self.proc_root is None:
            # An explicit ``proc_root=None`` is a test-only fixture seam.  The
            # production default is always /proc and therefore fail-closed.
            return active, True, []
        if self.proc_root == Path("/proc"):
            return self._scan_active_inspector(wanted)
        proc_fd: int | None = None
        try:
            proc_fd = os.open(self.proc_root, _DIR_FLAGS)
        except OSError as error:
            return active, False, [f"cannot inspect /proc for active files: {error}"]
        try:
            try:
                process_names = os.listdir(proc_fd)
            except OSError as error:
                return active, False, [f"cannot enumerate /proc: {error}"]
            for process_name in process_names:
                if not isinstance(process_name, str) or not process_name.isdigit():
                    continue
                pid_fd: int | None = None
                try:
                    pid_fd = os.open(process_name, _DIR_FLAGS, dir_fd=proc_fd)
                    process_stat = os.fstat(pid_fd)
                    if process_stat.st_uid != _uid():
                        continue
                    fd_dir_fd = os.open("fd", _DIR_FLAGS, dir_fd=pid_fd)
                    try:
                        fd_names = os.listdir(fd_dir_fd)
                    except OSError as error:
                        if _is_missing(error):
                            continue
                        issues.append(f"cannot inspect /proc/{process_name}/fd: {error}")
                        continue
                    try:
                        for fd_name in fd_names:
                            if not isinstance(fd_name, str) or not fd_name.isdigit():
                                continue
                            try:
                                value = os.stat(fd_name, dir_fd=fd_dir_fd, follow_symlinks=True)
                            except OSError as error:
                                # A process can close an fd while it is being
                                # inspected.  That is a known, safe race.
                                if _is_missing(error):
                                    continue
                                issues.append(f"cannot inspect /proc/{process_name}/fd/{fd_name}: {error}")
                                continue
                            identity = (int(value.st_dev), int(value.st_ino))
                            if identity in wanted:
                                active.add(identity)
                    finally:
                        os.close(fd_dir_fd)
                except OSError as error:
                    # Process exit between directory operations is expected;
                    # permission or malformed proc entries are uncertainty.
                    if not _is_missing(error):
                        issues.append(f"cannot inspect /proc/{process_name}: {error}")
                finally:
                    if pid_fd is not None:
                        os.close(pid_fd)
        finally:
            os.close(proc_fd)
        return active, not issues, issues

    @staticmethod
    def _scan_active_inspector(
        wanted: set[tuple[int, int]],
    ) -> tuple[set[tuple[int, int]], bool, list[str]]:
        """Query the privileged read-only fd inspector used in production.

        Reading another same-user process's ``/proc/<pid>/fd`` can be blocked
        by ptrace restrictions even when the process is owned by the desktop
        user.  The inspector has only read authority and authenticates this
        client with ``SO_PEERCRED``; it returns device/inode identities, never
        paths or deletion authority.  A missing or malformed response is a
        hard inspection failure.
        """

        socket_path = "/run/zeus-temp-inspector.sock"
        active: set[tuple[int, int]] = set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5.0)
                client.connect(socket_path)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = client.recv(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 2 * 1024 * 1024:
                        return set(), False, ["active-file inspector response is too large"]
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, socket.timeout) as error:
            return set(), False, [f"cannot query active-file inspector: {error}"]
        try:
            response_payload = b"".join(chunks).split(b"\n", 1)[0]
            response = json.loads(response_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return set(), False, [f"active-file inspector returned invalid JSON: {error}"]
        if not isinstance(response, dict) or not isinstance(response.get("reliable"), bool):
            return set(), False, ["active-file inspector returned an invalid response"]
        raw_active = response.get("active")
        if not isinstance(raw_active, list):
            return set(), False, ["active-file inspector omitted active identities"]
        for item in raw_active:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return set(), False, ["active-file inspector returned an invalid identity"]
            if not all(isinstance(value, int) and value >= 0 for value in item):
                return set(), False, ["active-file inspector returned an invalid identity"]
            active.add((int(item[0]), int(item[1])))
        if not response["reliable"]:
            detail = response.get("error")
            return set(), False, [str(detail) if isinstance(detail, str) and detail else "active-file inspector was uncertain"]
        return active & wanted, True, []

    @staticmethod
    def _partial(parts: Sequence[str]) -> bool:
        for component in parts:
            lower = component.lower()
            if lower in PARTIAL_NAMES or any(lower.endswith(suffix) for suffix in PARTIAL_SUFFIXES):
                return True
        return False

    def _candidate_current(self, root: _Root, candidate: _Candidate) -> os.stat_result | None:
        try:
            value = self._stat_relative(root.fd, candidate.parts, root.st_dev)
        except OSError as error:
            if _is_missing(error):
                return None
            raise InspectionError(f"cannot inspect Temp/{candidate.rel}: {error}") from error
        return value

    def _remove_candidate(self, root: _Root, candidate: _Candidate) -> tuple[bool, str | None]:
        current = self._candidate_current(root, candidate)
        if current is None:
            return False, "already-gone"
        if not _matches_for_remove(current, candidate):
            return False, "changed"
        parent_fd, name = self._open_relative_parent(root.fd, candidate.parts, root.st_dev)
        try:
            # Re-check against the parent fd immediately before the unlink or
            # rmdir.  New entries are not in the snapshot and are never
            # selected; directories containing one remain non-empty.
            latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _matches_for_remove(latest, candidate):
                return False, "changed"
            if candidate.kind == "file":
                os.unlink(name, dir_fd=parent_fd)
            elif candidate.kind == "dir":
                os.rmdir(name, dir_fd=parent_fd)
            else:
                return False, "ineligible"
            return True, None
        except OSError as error:
            if _is_missing(error):
                return False, "already-gone"
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return False, "new-entry"
            raise
        finally:
            os.close(parent_fd)

    # ------------------------------------------------------------------
    # Status and scheduling
    # ------------------------------------------------------------------
    def _due(self, state: Mapping[str, Any], now: float) -> tuple[bool, str]:
        policy = state["policy"]
        mode = policy["mode"]
        if mode == "never":
            return False, "never"
        if mode == "boot":
            boot = self._boot_id()
            # The boot identifier is recorded before an attempt starts.  A
            # process interrupted halfway through therefore does not retry
            # against files created later in the same boot; the remaining
            # entries are reported and handled on the next OS boot.
            if state.get("last_boot_id") == boot:
                return False, "boot-complete"
            return True, "boot"
        next_at = state.get("next_cleanup_at")
        if state.get("in_progress"):
            return True, "interrupted"
        if next_at is None:
            return False, "not-scheduled"
        if now >= float(next_at):
            return True, "due"
        return False, "not-due"

    @staticmethod
    def _retry_deadline(state: Mapping[str, Any], now: float) -> float | None:
        """Return a bounded retry time after an uncertain timed sweep.

        The old implementation left failed timed sweeps due immediately.  A
        one-shot timer would then be rearmed for ``now`` and could wake the
        machine in a tight loop while the same inspection problem persisted.
        Keep retries at least as far apart as the smallest supported policy;
        normal policies use their own interval when it is longer.
        """

        policy = state.get("policy")
        if not isinstance(policy, Mapping) or policy.get("mode") not in TIMED_POLICY_MODES:
            return None
        interval = policy.get("interval_seconds")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool):
            return None
        return now + max(MIN_RETRY_INTERVAL, int(interval))

    def schedule(self) -> dict[str, Any]:
        """Return the persisted timer plan without scanning Temp contents.

        This read-only seam is intentionally separate from :meth:`status`.
        A scheduler needs the policy deadline and root enrollment checks, but
        it must not walk the Temp tree just to decide whether a timer should
        exist.  The returned fields are additive to the established status
        contract and are consumed by the systemd scheduler wrapper.
        """

        try:
            with self._lock() as state_dir_fd:
                state = self._read_state(state_dir_fd)
                if state is None:
                    return {
                        "ok": True,
                        "enabled": False,
                        "enrolled": False,
                        "scheduled": False,
                        "mode": None,
                        "interval_seconds": None,
                        "next_cleanup_at": None,
                        "due": False,
                        "due_reason": "not-enrolled",
                    }
                root = self._open_temp_root(expected=state["root"])
                try:
                    now = self._now()
                    due, due_reason = self._due(state, now)
                    mode = state["policy"]["mode"]
                    next_at = state.get("next_cleanup_at")
                    scheduled = mode in TIMED_POLICY_MODES and next_at is not None
                    return {
                        "ok": True,
                        "enabled": True,
                        "enrolled": True,
                        "scheduled": scheduled,
                        "mode": mode,
                        "interval_seconds": state["policy"]["interval_seconds"],
                        "next_cleanup_at": float(next_at) if next_at is not None else None,
                        "due": due,
                        "due_reason": due_reason,
                    }
                finally:
                    os.close(root.fd)
        except TempError as error:
            return {
                "ok": False,
                "enabled": False,
                "enrolled": False,
                "scheduled": False,
                "mode": None,
                "interval_seconds": None,
                "next_cleanup_at": None,
                "due": False,
                "due_reason": "error",
                "error": str(error),
                "error_type": type(error).__name__,
            }

    def _usage(self, root: _Root) -> tuple[int, int, list[str], list[dict[str, Any]]]:
        candidates, issues = self._snapshot(root)
        total = 0
        count = 0
        files: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.kind == "file":
                total += max(0, candidate.st_size)
                count += 1
                files.append(
                    {
                        "name": candidate.rel,
                        "size_bytes": candidate.st_size if candidate.kind == "file" else 0,
                        "is_directory": candidate.kind == "dir",
                        "directory": candidate.kind == "dir",
                        "kind": candidate.kind,
                        "partial": self._partial(candidate.parts),
                        "modified": _iso_time(candidate.st_mtime_ns / 1_000_000_000),
                    }
                )
        return total, count, issues, files

    def _status_locked(
        self,
        state: Mapping[str, Any] | None,
        *,
        root: _Root | None = None,
        setup_created: bool = False,
        state_error: str | None = None,
        include_usage: bool = True,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": state_error is None,
            "enabled": False,
            "enrolled": state is not None and state_error is None,
            "temp": str(self.temp_path),
            "temp_path": str(self.temp_path),
            "downloads": str(self.downloads_path),
            "download_destination": str(self.temp_path),
            "state_path": str(self.state_path),
            "configured": state is not None and state_error is None,
            "setup_required": state is None and state_error is None,
            "collision": False,
            "setup_created": setup_created,
            "policy": None,
            "policy_mode": None,
            "mode": None,
            "interval_seconds": None,
            "next_cleanup": None,
            "next_cleanup_at": None,
            "next_cleanup_label": None,
            "due": False,
            "space_used": 0,
            "space_used_bytes": 0,
            "file_count": 0,
            "files": [],
            "last_cleanup_at": None,
            "last_boot_id": None,
            "last_result": None,
            "errors": [],
        }
        if state_error is not None:
            result["errors"] = [state_error]
            result["disabled_reason"] = state_error
            return result
        if state is None:
            result["disabled_reason"] = "not_enrolled"
            try:
                value = os.lstat(self.temp_path)
                if stat.S_ISLNK(value.st_mode):
                    result["disabled_reason"] = "temp_is_symlink"
                elif value.st_uid != _uid():
                    result["disabled_reason"] = "temp_not_owner_owned"
                else:
                    result["disabled_reason"] = "temp_collision_requires_adoption"
                    result["collision"] = True
            except OSError as error:
                if not _is_missing(error):
                    result["errors"] = [str(error)]
            return result
        result["policy"] = dict(state["policy"])
        result["mode"] = state["policy"]["mode"]
        result["policy_mode"] = state["policy"]["mode"]
        result["interval_seconds"] = state["policy"]["interval_seconds"]
        result["last_cleanup_at"] = _iso_time(state.get("last_sweep_at"))
        result["last_boot_id"] = state.get("last_boot_id")
        result["last_result"] = state.get("last_result")
        try:
            if include_usage:
                if root is None:
                    root = self._open_temp_root(expected=state["root"])
                    owns_root = True
                else:
                    owns_root = False
            result["enabled"] = True
            if include_usage:
                assert root is not None
                used, count, issues, files = self._usage(root)
                result["space_used"] = used
                result["space_used_bytes"] = used
                result["file_count"] = count
                result["files"] = files
                result["errors"].extend(issues)
            now = self._now()
            due, reason = self._due(state, now)
            result["due"] = due
            result["due_reason"] = reason
            next_at = state.get("next_cleanup_at")
            if state["policy"]["mode"] == "boot":
                result["next_cleanup"] = "Next boot"
                result["next_cleanup_label"] = "Next boot"
            elif state["policy"]["mode"] == "never":
                result["next_cleanup"] = "Never"
                result["next_cleanup_label"] = "Never"
            elif next_at is not None:
                result["next_cleanup_at"] = float(next_at)
                result["next_cleanup"] = _iso_time(float(next_at))
                result["next_cleanup_label"] = result["next_cleanup"]
            if state.get("last_error"):
                result["errors"].append(state["last_error"])
            if result["errors"]:
                result["ok"] = False
            return result
        except TempError as error:
            result["ok"] = False
            result["enabled"] = False
            result["errors"] = [str(error)]
            result["disabled_reason"] = str(error)
            return result
        finally:
            if root is not None and 'owns_root' in locals() and owns_root:
                os.close(root.fd)

    def status(self) -> dict[str, Any]:
        try:
            with self._lock() as state_dir_fd:
                try:
                    state = self._read_state(state_dir_fd)
                except TempError as error:
                    return self._status_locked(None, state_error=str(error))
                return self._status_locked(state)
        except TempError as error:
            return {
                "ok": False,
                "enabled": False,
                "enrolled": False,
                "temp": str(self.temp_path),
                "temp_path": str(self.temp_path),
                "downloads": str(self.downloads_path),
                "download_destination": str(self.temp_path),
                "state_path": str(self.state_path),
                "configured": False,
                "policy": None,
                "policy_mode": None,
                "mode": None,
                "interval_seconds": None,
                "next_cleanup": None,
                "next_cleanup_at": None,
                "due": False,
                "space_used": 0,
                "space_used_bytes": 0,
                "file_count": 0,
                "files": [],
                "errors": [str(error)],
                "disabled_reason": str(error),
            }

    # ------------------------------------------------------------------
    # Policy mutation and sweep execution
    # ------------------------------------------------------------------
    def _load_enrolled(self, state_dir_fd: int) -> tuple[dict[str, Any], _Root]:
        state = self._read_state(state_dir_fd)
        if state is None:
            raise NotEnrolledError("Temp is not enrolled; run setup first")
        root = self._open_temp_root(expected=state["root"])
        return state, root

    def set_policy(self, mode: str, interval_seconds: int | None = None) -> dict[str, Any]:
        chosen = _policy(mode, interval_seconds)
        now = self._now()
        with self._lock() as state_dir_fd:
            state, root = self._load_enrolled(state_dir_fd)
            try:
                state["policy"] = chosen
                state["policy_version"] = POLICY_VERSION
                state["policy_changed_at"] = now
                state["last_error"] = None
                state["in_progress"] = False
                if chosen["mode"] in PRESET_INTERVALS or chosen["mode"] == "custom":
                    state["next_cleanup_at"] = now + int(chosen["interval_seconds"])
                else:
                    state["next_cleanup_at"] = None
                if chosen["mode"] == "boot":
                    # Selecting On boot schedules the next OS boot. A stale
                    # last_boot_id from time spent in Never must not authorize
                    # a same-boot service restart to clear newly downloaded files.
                    state["last_boot_id"] = self._boot_id()
                self._write_state(state_dir_fd, state)
                result = self._status_locked(state, root=root)
                result["policy_changed"] = True
                return result
            finally:
                os.close(root.fd)

    def _sweep_locked(self, state_dir_fd: int, state: dict[str, Any], root: _Root, *, force: bool) -> dict[str, Any]:
        now = self._now()
        mode = state["policy"]["mode"]
        due = True
        due_reason = "forced" if force else ""
        if not force:
            due, due_reason = self._due(state, now)
            if not due:
                result = {
                    "ok": True,
                    "swept": False,
                    "force": False,
                    "reason": due_reason,
                    "deleted": 0,
                    "deleted_bytes": 0,
                    "removed": 0,
                    "removed_bytes": 0,
                    "snapshot_count": 0,
                    "skipped": [],
                    "errors": [],
                }
                return result

        # Persist an in-progress marker before touching any entry.  Boot mode
        # also records the current boot before deletion.  This makes a retry
        # after an interruption wait for the next OS boot and protects files
        # downloaded later during this boot.
        state["in_progress"] = True
        state["last_error"] = None
        if mode == "boot":
            # A manual clear is still the boot's one cleanup attempt.  This
            # prevents a delayed session service from deleting files created
            # after the user explicitly cleared Temp.
            state["last_boot_id"] = self._boot_id()
        self._write_state(state_dir_fd, state)

        candidates, snapshot_issues = self._snapshot(root)
        active, active_reliable, active_issues = self._scan_active(candidates)
        result: dict[str, Any] = {
            "ok": True,
            "swept": False,
            "force": bool(force),
            "reason": due_reason,
            "deleted": 0,
            "deleted_bytes": 0,
            "removed": 0,
            "removed_bytes": 0,
            "snapshot_count": len(candidates),
            "skipped": [],
            "errors": list(snapshot_issues),
        }
        if snapshot_issues or not active_reliable:
            # Inspection uncertainty is a hard stop.  This protects against
            # deleting through a partially observed tree or /proc scan.
            result["ok"] = False
            result["errors"].extend(active_issues)
            if not active_reliable and not active_issues:
                result["errors"].append("active-file inspection was incomplete")
            result["skipped"] = [{"name": c.rel, "reason": "inspection-uncertain"} for c in candidates]
            state["last_error"] = "; ".join(result["errors"]) or "inspection uncertainty"
            state["in_progress"] = False
            state["last_result"] = dict(result)
            # Keep timed operations retryable, but leave enough space between
            # attempts that a persistent inspection failure cannot create a
            # one-shot timer busy loop.  Boot remains recorded as attempted
            # and is intentionally deferred to the next OS boot.
            retry_at = self._retry_deadline(state, now)
            if retry_at is not None:
                state["next_cleanup_at"] = retry_at
            self._write_state(state_dir_fd, state)
            result["retry_required"] = True
            return result

        partial_entries = [candidate for candidate in candidates if self._partial(candidate.parts)]
        if partial_entries:
            # A browser's sidecar marker (for example ``file.part`` beside
            # ``file``) means the pair may be changing.  Deferring the whole
            # snapshot is deliberately conservative and protects the
            # completed-looking companion from being removed mid-download.
            result["swept"] = False
            result["reason"] = "partial-download"
            result["skipped"] = [
                {"name": candidate.rel, "reason": "partial-download"}
                for candidate in candidates
            ]
            state["in_progress"] = False
            state["last_sweep_at"] = now
            state["last_result"] = dict(result)
            state["last_error"] = None
            if mode in TIMED_POLICY_MODES:
                state["next_cleanup_at"] = now + int(state["policy"]["interval_seconds"])
            self._write_state(state_dir_fd, state)
            return result

        active_directories = {
            candidate.parts
            for candidate in candidates
            if candidate.kind == "dir" and candidate.identity in active
        }
        # Remove deepest entries first.  Parent directory metadata changes as
        # children leave, and rmdir itself rejects any new child created after
        # the snapshot.
        ordered_candidates = sorted(candidates, key=lambda item: len(item.parts), reverse=True)
        for candidate in ordered_candidates:
            if candidate.kind in ("symlink", "mount", "special"):
                reason = "symlink" if candidate.kind == "symlink" else candidate.kind
                result["skipped"].append({"name": candidate.rel, "reason": reason})
                continue
            if self._partial(candidate.parts):
                result["skipped"].append({"name": candidate.rel, "reason": "partial-marker"})
                continue
            if candidate.identity in active:
                result["skipped"].append({"name": candidate.rel, "reason": "active-file"})
                continue
            if any(
                len(directory) < len(candidate.parts)
                and candidate.parts[: len(directory)] == directory
                for directory in active_directories
            ):
                result["skipped"].append({"name": candidate.rel, "reason": "active-directory"})
                continue
            try:
                removed, reason = self._remove_candidate(root, candidate)
            except OSError as error:
                result["ok"] = False
                result["errors"].append(f"Temp/{candidate.rel}: {error}")
                result["skipped"].append({"name": candidate.rel, "reason": "delete-failed"})
                continue
            except TempError as error:
                result["ok"] = False
                result["errors"].append(str(error))
                result["skipped"].append({"name": candidate.rel, "reason": "inspection-failed"})
                continue
            if removed:
                result["deleted"] += 1
                result["deleted_bytes"] += candidate.st_size if candidate.kind == "file" else 0
            elif reason not in (None, "already-gone"):
                result["skipped"].append({"name": candidate.rel, "reason": reason})

        result["removed"] = result["deleted"]
        result["removed_bytes"] = result["deleted_bytes"]
        result["swept"] = True
        state["in_progress"] = False
        state["last_sweep_at"] = now
        # Keep a detached copy: the public result receives a status object
        # below, and retaining the same dictionary would create a JSON cycle.
        state["last_result"] = dict(result)
        state["last_error"] = "; ".join(result["errors"]) if result["errors"] else None
        if mode in TIMED_POLICY_MODES:
            if result["ok"]:
                # Schedule from the actual completed run.  This collapses all
                # missed intervals after sleep/shutdown into one sweep.
                state["next_cleanup_at"] = now + int(state["policy"]["interval_seconds"])
            else:
                retry_at = self._retry_deadline(state, now)
                if retry_at is not None:
                    state["next_cleanup_at"] = retry_at
        self._write_state(state_dir_fd, state)
        return result

    def sweep(self, force: bool = False, *, include_usage: bool = True) -> dict[str, Any]:
        """Run one due sweep, or an explicit clear when ``force`` is true."""

        with self._lock() as state_dir_fd:
            state, root = self._load_enrolled(state_dir_fd)
            try:
                result = self._sweep_locked(state_dir_fd, state, root, force=bool(force))
                # A one-shot timer may still invoke this command after a
                # deadline was cancelled or while a clock is before the next
                # deadline.  Preserve the established result/status shape,
                # but do not walk Temp merely to report that no sweep ran.
                no_op_reasons = {
                    "never",
                    "boot-complete",
                    "not-scheduled",
                    "not-due",
                }
                report_usage = bool(include_usage) and (
                    bool(result.get("swept")) or result.get("reason") not in no_op_reasons
                )
                status = self._status_locked(state, root=root, include_usage=report_usage)
                result["status"] = status
                return result
            finally:
                os.close(root.fd)

    # ------------------------------------------------------------------
    # Conflict-safe Keep operation
    # ------------------------------------------------------------------
    def _destination_parts(self, destination: os.PathLike[str] | str | None) -> tuple[str, ...]:
        if destination is None:
            destination = DEFAULT_DESTINATION
        raw = os.fspath(destination)
        if not isinstance(raw, str):
            raw = os.fsdecode(raw)
        if raw.startswith("~/"):
            raw = os.fspath(self.home / raw[2:])
        path = Path(raw)
        if any(component in (".", "..") for component in path.parts):
            raise DestinationError("Keep destination path traversal is not allowed")
        if path.is_absolute():
            absolute = Path(os.path.abspath(os.fspath(path)))
            try:
                relative = absolute.relative_to(self.home)
            except ValueError as error:
                raise DestinationError("Keep destination must be inside the owner home") from error
        else:
            relative = path
        parts = _path_parts(relative)
        if parts == ("Temp",) or parts[:1] == ("Temp",):
            raise DestinationError("Keep destination cannot be inside ~/Temp")
        return parts

    def _open_destination(self, parts: Sequence[str]) -> int:
        home_fd = self._open_home()
        current_fd = home_fd
        try:
            for component in parts:
                try:
                    next_fd = self._open_child_dir(current_fd, component, create=True)
                except SetupError as error:
                    raise DestinationError(str(error)) from error
                if current_fd != home_fd:
                    os.close(current_fd)
                current_fd = next_fd
            if current_fd == home_fd:
                # Keep's destination may be the home directory.  Duplicate it
                # so ownership of the returned fd is unambiguous.
                current_fd = os.dup(home_fd)
                os.close(home_fd)
            else:
                os.close(home_fd)
            return current_fd
        except Exception:
            if current_fd != home_fd:
                os.close(current_fd)
            os.close(home_fd)
            raise

    @staticmethod
    def _destination_name(base: str, destination_fd: int) -> str:
        stem, suffix = os.path.splitext(base)
        for index in range(0, 10000):
            if index == 0:
                candidate = base
            else:
                candidate = f"{stem} ({index}){suffix}"
            try:
                os.lstat(candidate, dir_fd=destination_fd)
            except FileNotFoundError:
                return candidate
            except OSError as error:
                raise DestinationError(f"cannot inspect Keep destination: {error}") from error
        raise DestinationError("too many files with the same Keep name")

    def _copy_file(self, source_fd: int, destination_fd: int, name: str, source_stat: os.stat_result) -> None:
        flags = _flags("O_WRONLY", "O_CREAT", "O_EXCL", "O_CLOEXEC", "O_NOFOLLOW")
        destination_file_fd = os.open(name, flags, stat.S_IMODE(source_stat.st_mode), dir_fd=destination_fd)
        try:
            while True:
                data = os.read(source_fd, 1024 * 1024)
                if not data:
                    break
                view = memoryview(data)
                while view:
                    count = os.write(destination_file_fd, view)
                    view = view[count:]
            os.fchmod(destination_file_fd, stat.S_IMODE(source_stat.st_mode))
            os.fsync(destination_file_fd)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=destination_fd)
            raise
        finally:
            os.close(destination_file_fd)

    def keep(self, names: Iterable[os.PathLike[str] | str] | os.PathLike[str] | str, destination: os.PathLike[str] | str | None = None) -> dict[str, Any]:
        """Move selected regular files to an owner-selected permanent folder.

        Destination names are allocated using ``link(2)`` or ``O_EXCL`` copy
        creation, so an existing permanent file is never overwritten.
        """

        if isinstance(names, (str, bytes, os.PathLike)):
            names = [names]
        selected = list(names)
        if not selected:
            raise DestinationError("Keep requires at least one file")
        normalized: list[tuple[str, ...]] = []
        for value in selected:
            try:
                parts = _path_parts(Path(value))
            except (TypeError, ValueError) as error:
                raise DestinationError(f"invalid Temp file name: {value!r}") from error
            if parts == (MARKER_FILENAME,):
                raise DestinationError("the Temp enrollment marker cannot be kept")
            if HomeManager._partial(parts):
                raise ActiveFileError(f"Temp/{'/'.join(parts)} is a partial download")
            normalized.append(parts)

        destination_parts = self._destination_parts(destination)
        with self._lock() as state_dir_fd:
            state, root = self._load_enrolled(state_dir_fd)
            destination_fd: int | None = None
            try:
                snapshot, snapshot_issues = self._snapshot(root)
                if snapshot_issues:
                    raise ActiveFileError("cannot inspect Temp safely for Keep")
                partial_entries = [candidate for candidate in snapshot if self._partial(candidate.parts)]
                if partial_entries:
                    raise ActiveFileError("Keep is deferred while Temp contains a partial download")
                mounted_entries = {
                    candidate.parts
                    for candidate in snapshot
                    if candidate.kind == "mount"
                }
                destination_fd = self._open_destination(destination_parts)
                # Obtain identities after opening the root and before opening
                # source files, then scan same-user fds against those exact
                # entries.
                actual_candidates: list[_Candidate] = []
                for parts in normalized:
                    if any(
                        tuple(parts[:index]) in mounted_entries
                        for index in range(1, len(parts) + 1)
                    ):
                        raise DestinationError(f"Keep cannot enter a mounted Temp child: {'/'.join(parts)}")
                    try:
                        value = self._stat_relative(root.fd, parts, root.st_dev)
                    except OSError as error:
                        raise DestinationError(f"cannot inspect Temp/{'/'.join(parts)}: {error}") from error
                    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
                        raise DestinationError(f"Keep only accepts regular files: Temp/{'/'.join(parts)}")
                    actual_candidates.append(_Candidate(parts, "file", int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns)))
                active, reliable, issues = self._scan_active(actual_candidates)
                if not reliable:
                    raise ActiveFileError("cannot determine whether selected files are in use")
                if issues:
                    raise ActiveFileError("active-file inspection was incomplete")
                moved: list[dict[str, Any]] = []
                for candidate in actual_candidates:
                    if candidate.identity in active:
                        raise ActiveFileError(f"Temp/{candidate.rel} is in use")
                    source_parent_fd, source_name = self._open_relative_parent(root.fd, candidate.parts, root.st_dev)
                    source_fd: int | None = None
                    linked = False
                    destination_name = self._destination_name(candidate.parts[-1], destination_fd)
                    try:
                        source_fd = os.open(source_name, _READ_FLAGS, dir_fd=source_parent_fd)
                        initial = os.fstat(source_fd)
                        if not stat.S_ISREG(initial.st_mode) or not _matches_candidate(initial, candidate):
                            raise ActiveFileError(f"Temp/{candidate.rel} changed during Keep")
                        while True:
                            try:
                                os.link(
                                    source_name,
                                    destination_name,
                                    src_dir_fd=source_parent_fd,
                                    dst_dir_fd=destination_fd,
                                    follow_symlinks=False,
                                )
                                linked = True
                                break
                            except FileExistsError:
                                destination_name = self._destination_name(candidate.parts[-1], destination_fd)
                            except OSError as error:
                                if error.errno != errno.EXDEV:
                                    raise DestinationError(f"cannot keep Temp/{candidate.rel}: {error}") from error
                                self._copy_file(source_fd, destination_fd, destination_name, initial)
                                break
                        final_source = os.fstat(source_fd)
                        if not _same_content_stat(initial, final_source):
                            with contextlib.suppress(OSError):
                                os.unlink(destination_name, dir_fd=destination_fd)
                            raise ActiveFileError(f"Temp/{candidate.rel} changed during Keep")
                        try:
                            source_path_stat = os.stat(
                                source_name,
                                dir_fd=source_parent_fd,
                                follow_symlinks=False,
                            )
                        except OSError as error:
                            with contextlib.suppress(OSError):
                                os.unlink(destination_name, dir_fd=destination_fd)
                            raise DestinationError(
                                f"cannot recheck Temp/{candidate.rel} before Keep: {error}"
                            ) from error
                        if not _same_content_stat(initial, source_path_stat):
                            with contextlib.suppress(OSError):
                                os.unlink(destination_name, dir_fd=destination_fd)
                            raise ActiveFileError(f"Temp/{candidate.rel} changed during Keep")
                        os.unlink(source_name, dir_fd=source_parent_fd)
                        moved.append({
                            "source": candidate.rel,
                            "destination": str(self.home / Path(*destination_parts) / destination_name),
                            "name": destination_name,
                            "bytes": candidate.st_size,
                        })
                    except Exception:
                        if linked:
                            with contextlib.suppress(OSError):
                                os.unlink(destination_name, dir_fd=destination_fd)
                        raise
                    finally:
                        if source_fd is not None:
                            os.close(source_fd)
                        os.close(source_parent_fd)
                state["last_error"] = None
                state["last_result"] = {"kept": moved}
                self._write_state(state_dir_fd, state)
                return {
                    "ok": True,
                    "kept": moved,
                    "destination": str(self.home / Path(*destination_parts)),
                    "status": self._status_locked(state, root=root),
                }
            finally:
                if destination_fd is not None:
                    os.close(destination_fd)
                os.close(root.fd)

# ----------------------------------------------------------------------
# Production-facing convenience API
# ----------------------------------------------------------------------
_DEFAULT_MANAGER: HomeManager | None = None


def _owner_home() -> Path:
    """Resolve the real passwd home, independent of a HOME override."""

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=False)
    except (KeyError, OSError):
        # A normal installed account always has a passwd entry.  This fallback
        # keeps the helper usable in a minimal test image without introducing
        # an environment-controlled production root.
        return Path.home().resolve(strict=False)


def _manager() -> HomeManager:
    global _DEFAULT_MANAGER
    # The passwd database is authoritative.  There is no ZEUS_HOME or similar
    # environment override in the installed API.
    owner_home = _owner_home()
    if _DEFAULT_MANAGER is None or _DEFAULT_MANAGER.home != owner_home:
        _DEFAULT_MANAGER = HomeManager(owner_home)
    return _DEFAULT_MANAGER


def setup(**kwargs: Any) -> dict[str, Any]:
    return _manager().setup(**kwargs)


def status() -> dict[str, Any]:
    return _manager().status()


def set_policy(mode: str, interval_seconds: int | None = None) -> dict[str, Any]:
    result = _manager().set_policy(mode, interval_seconds)
    # Persistence releases the engine lock before taking the scheduler lock.
    # The scheduler then reads the latest policy, including concurrent edits.
    result["policy_saved"] = True
    return _synchronize_timer(result)


def schedule() -> dict[str, Any]:
    """Return the deadline plan used by the systemd scheduler wrapper."""

    return _manager().schedule()


def _synchronize_timer(result: dict[str, Any]) -> dict[str, Any]:
    """Synchronize installed scheduling after a completed owner operation."""

    try:
        completed = subprocess.run(
            [sys.executable, "/usr/libexec/zeus-temp-scheduler", "arm"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        plan = json.loads(completed.stdout)
        if not isinstance(plan, dict):
            raise ValueError("invalid scheduler response")
        result["scheduler"] = plan
        if completed.returncode == 0 and plan.get("ok") is True:
            return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    result["ok"] = False
    result["scheduler_error"] = True
    result["error"] = (
        "Policy saved, but cleanup scheduling could not be updated. Retry Apply."
        if result.get("policy_saved") else
        "Cleanup finished, but its next schedule could not be updated. Reapply the cleanup policy."
    )
    return result


def sweep(
    force: bool = False, *, include_usage: bool = True, synchronize_timer: bool = True,
) -> dict[str, Any]:
    result = _manager().sweep(force=force, include_usage=include_usage)
    # TimerScheduler.run already owns its lock and rearms after this call.
    return _synchronize_timer(result) if synchronize_timer else result


def keep(names: Iterable[os.PathLike[str] | str] | os.PathLike[str] | str, destination: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    return _manager().keep(names, destination)


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Zeus temporary downloads")
    parser.add_argument("--json", action="store_true", help="emit JSON (the default output format)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="show policy, schedule, and Temp usage")
    status_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    setup_parser = subparsers.add_parser("setup", help="provision private ~/Temp")
    setup_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument(
        "--adopt",
        action="store_true",
        help="explicitly enroll an existing owner-owned ~/Temp directory",
    )
    policy_parser = subparsers.add_parser("policy", help="set the cleanup policy")
    policy_parser.add_argument("mode", choices=sorted(POLICY_MODES))
    policy_parser.add_argument("interval_seconds", nargs="?", type=int)
    policy_parser.add_argument("--interval-seconds", dest="interval_option", type=int)
    policy_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    schedule_parser = subparsers.add_parser(
        "schedule",
        help="show the persisted cleanup deadline without scanning Temp",
    )
    schedule_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sweep_parser = subparsers.add_parser("sweep", help="run a due sweep or explicit clear")
    sweep_parser.add_argument("--force", action="store_true", help="clear now regardless of schedule")
    sweep_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    keep_parser = subparsers.add_parser("keep", help="move files to permanent storage")
    keep_parser.add_argument("names", nargs="+", help="file names relative to ~/Temp")
    keep_parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    keep_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            value = status()
        elif args.command == "setup":
            value = setup(adopt=bool(args.adopt))
        elif args.command == "policy":
            interval = args.interval_option if args.interval_option is not None else args.interval_seconds
            value = set_policy(args.mode, interval)
        elif args.command == "schedule":
            value = schedule()
        elif args.command == "sweep":
            value = sweep(force=bool(args.force))
        elif args.command == "keep":
            value = keep(args.names, args.destination)
        else:  # pragma: no cover - argparse enforces this
            raise TempError("unknown command")
    except (TempError, OSError, ValueError) as error:
        _print_json({"ok": False, "error": str(error), "error_type": type(error).__name__})
        return 1
    _print_json(value)
    return 0 if value.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
