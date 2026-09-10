"""Run bootupd's EFI update path against the Zeus ESP only.

bootupd 0.2.35 has no option for selecting one ESP when a disk contains more
than one EFI System Partition.  Its update implementation does, however,
reuse an already-mounted VFAT ESP before mounting the ESP it discovered.  The
runtime wrapper in this module pins that mount to the UUID recorded during
installation, checks the identity immediately before invoking ``bootupctl
update``, and refuses to touch an existing mount with another UUID. It also
requires exactly one backing root device and the audited bootupctl version;
bootupd's update loop unmounts after each root, so a multi-root deployment
could otherwise reach another ESP on a later iteration.

The wrapper deliberately delegates the file update to bootupd.  In
particular, it does not copy EFI files itself or invoke the ``backend install``
command for ongoing updates; bootupd retains responsibility for its file-tree
diff, state journal, static UUID configuration, fsync/freeze cycle, and EFI
vendor paths.  The bootloader-update systemd unit must retain
``MountFlags=slave`` so bootupd's cleanup unmount is contained by the service's
mount namespace.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("/etc/zeus/efi-update.json")
DEFAULT_MOUNTPOINT = "/boot/efi"
SUPPORTED_BOOTUPCTL_VERSION = "0.2.35"

# Keep these paths fixed.  The updater must not be able to turn a config-file
# change into execution of an arbitrary helper or mount operation.
BOOTUPCTL_COMMAND = "/usr/bin/bootupctl"
FINDMNT_COMMAND = "/usr/bin/findmnt"
LSBLK_COMMAND = "/usr/bin/lsblk"
MOUNT_COMMAND = "/usr/bin/mount"
UMOUNT_COMMAND = "/usr/bin/umount"

_COMMAND_TIMEOUT_SECONDS = 300
_BOOTUPCTL_VERSION_RE = re.compile(r"^bootupctl\s+([0-9]+\.[0-9]+\.[0-9]+)$")
_FAT_UUID_RE = re.compile(r"^[0-9a-fA-F]{4}-[0-9a-fA-F]{4}$")


Runner = Callable[..., Any]


class EfiUpdateError(RuntimeError):
    """A fail-closed scoped EFI update error."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


@dataclass(frozen=True)
class EfiScope:
    """The immutable part of the installed Zeus EFI update contract."""

    esp_uuid: str
    mountpoint: str = DEFAULT_MOUNTPOINT

    def __post_init__(self) -> None:
        normalized = _normalize_uuid(self.esp_uuid)
        if normalized is None:
            raise EfiUpdateError(
                "invalid_esp_uuid",
                "The configured Zeus ESP UUID is not a canonical FAT volume ID (XXXX-XXXX)",
            )
        if self.mountpoint != DEFAULT_MOUNTPOINT:
            raise EfiUpdateError(
                "invalid_mountpoint",
                f"The EFI update mountpoint must be {DEFAULT_MOUNTPOINT}",
            )
        object.__setattr__(self, "esp_uuid", normalized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "esp_uuid": self.esp_uuid,
            "mountpoint": self.mountpoint,
        }


@dataclass(frozen=True)
class MountedEsp:
    """The identity returned by findmnt for the guarded mountpoint."""

    target: str
    source: str
    fstype: str
    esp_uuid: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _normalize_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _FAT_UUID_RE.fullmatch(value):
        return None
    # util-linux/blkid reports FAT volume IDs as four hexadecimal digits,
    # hyphen, four hexadecimal digits (for example ``1234-ABCD``).  Keep the
    # static config canonical and reject GPT-style 36-character UUIDs.
    return value.upper()


def _short_text(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _command_name(command: Sequence[str]) -> str:
    return Path(str(command[0])).name if command else "unknown"


def _result_from_value(value: Any) -> CommandResult:
    """Normalize subprocess results and small fixture-runner return values."""

    if isinstance(value, (str, bytes)):
        stdout = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return CommandResult(0, stdout=stdout)
    if isinstance(value, (tuple, list)):
        return CommandResult(
            int(value[0]) if value else 0,
            str(value[1] if len(value) > 1 and value[1] is not None else ""),
            str(value[2] if len(value) > 2 and value[2] is not None else ""),
        )
    if isinstance(value, Mapping):
        return CommandResult(
            int(value.get("returncode", value.get("code", 0))),
            str(value.get("stdout", value.get("output", "")) or ""),
            str(value.get("stderr", value.get("error", "")) or ""),
        )
    return CommandResult(
        int(getattr(value, "returncode", 0)),
        str(getattr(value, "stdout", "") or ""),
        str(getattr(value, "stderr", "") or ""),
    )


def _run(command: Sequence[str], runner: Runner | None = None) -> CommandResult:
    """Run one fixed argument vector and normalize test doubles.

    The fallback call is intentionally only for fixture runners accepting a
    single argument vector.  Production subprocess calls always use
    ``shell=False`` and a timeout.
    """

    execute = runner or subprocess.run
    args = list(command)
    try:
        try:
            value = execute(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                shell=False,
            )
        except TypeError:
            value = execute(args)
    except FileNotFoundError as error:
        raise EfiUpdateError(
            "command_unavailable",
            f"Required command {_command_name(args)!r} is unavailable",
            detail=_short_text(error),
        ) from error
    except PermissionError as error:
        raise EfiUpdateError(
            "command_permission_denied",
            f"Cannot execute required command {_command_name(args)!r}",
            detail=_short_text(error),
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise EfiUpdateError(
            "command_failed",
            f"Required command {_command_name(args)!r} could not run",
            detail=_short_text(error),
        ) from error
    try:
        return _result_from_value(value)
    except (TypeError, ValueError) as error:
        raise EfiUpdateError(
            "invalid_command_result",
            f"Required command {_command_name(args)!r} returned an invalid result",
            detail=_short_text(error),
        ) from error


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as error:
        raise EfiUpdateError("config_missing", f"{label} does not exist") from error
    except OSError as error:
        raise EfiUpdateError("config_unreadable", f"Cannot inspect {label}", detail=_short_text(error)) from error


def _check_secure_file(path: Path, *, require_root: bool) -> None:
    st = _lstat(path, label="EFI update config")
    if not stat_is_regular(st.st_mode):
        raise EfiUpdateError("unsafe_config", "EFI update config must be a regular file")
    if require_root and st.st_uid != 0:
        raise EfiUpdateError("unsafe_config_owner", "EFI update config must be owned by root")
    if st.st_mode & 0o022:
        raise EfiUpdateError("unsafe_config_mode", "EFI update config must not be group or world writable")


def stat_is_regular(mode: int) -> bool:
    # Avoid importing stat solely for one predicate while keeping this helper
    # easy to exercise with synthetic stat results in tests.
    import stat

    return stat.S_ISREG(mode)


def _check_secure_parent(path: Path, *, require_root: bool) -> None:
    """Reject a writable or symlinked config directory in production."""

    current = path.parent
    direct_parent = True
    while True:
        try:
            st = current.lstat()
        except FileNotFoundError as error:
            raise EfiUpdateError(
                "unsafe_config_parent", "EFI update config directory does not exist"
            ) from error
        except OSError as error:
            raise EfiUpdateError(
                "unsafe_config_parent",
                "Cannot inspect EFI update config directory",
                detail=_short_text(error),
            ) from error
        if not stat_is_directory(st.st_mode):
            raise EfiUpdateError("unsafe_config_parent", "EFI update config parent must be a directory")
        # A production config must have a root-owned, non-writable chain all
        # the way to /.  The direct-parent check remains active for fixture
        # paths; skipping shared ancestors such as /tmp keeps tests portable.
        if direct_parent or require_root:
            if st.st_mode & 0o022:
                raise EfiUpdateError(
                    "unsafe_config_mode",
                    "EFI update config directory must not be group or world writable",
                )
            if require_root and st.st_uid != 0:
                raise EfiUpdateError("unsafe_config_owner", "EFI update config directory must be owned by root")
        if current.parent == current:
            break
        current = current.parent
        direct_parent = False


def stat_is_directory(mode: int) -> bool:
    import stat

    return stat.S_ISDIR(mode)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key {key!r}")
        value[key] = item
    return value


def _scope_from_mapping(value: Any) -> EfiScope:
    if not isinstance(value, dict):
        raise EfiUpdateError("invalid_config", "EFI update config must contain a JSON object")
    if set(value) != {"schema_version", "esp_uuid", "mountpoint"}:
        raise EfiUpdateError(
            "invalid_config",
            "EFI update config must contain only schema_version, esp_uuid, and mountpoint",
        )
    version = value.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise EfiUpdateError("unsupported_config", f"Unsupported EFI update config schema: {version!r}")
    try:
        return EfiScope(str(value["esp_uuid"]), str(value["mountpoint"]))
    except EfiUpdateError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise EfiUpdateError(
            "invalid_config",
            "EFI update config has invalid values",
            detail=_short_text(error),
        ) from error


def load_scope(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH, *, require_root: bool = True) -> EfiScope:
    """Load and validate a root-owned static EFI scope config."""

    config_path = Path(path)
    _check_secure_parent(config_path, require_root=require_root)
    _check_secure_file(config_path, require_root=require_root)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeError) as error:
        raise EfiUpdateError(
            "config_unreadable", "Cannot read EFI update config", detail=_short_text(error)
        ) from error
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise EfiUpdateError(
            "invalid_config", "EFI update config is not valid JSON", detail=_short_text(error)
        ) from error
    return _scope_from_mapping(value)


def _ensure_parent(path: Path, *, require_root: bool) -> None:
    """Create the final config directory without following symlinks."""

    parent = path.parent
    missing: list[Path] = []
    current = parent
    while True:
        try:
            st = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise EfiUpdateError("unsafe_config_parent", "EFI update config path has no existing root")
            current = current.parent
            continue
        except OSError as error:
            raise EfiUpdateError(
                "unsafe_config_parent", "Cannot inspect EFI update config path", detail=_short_text(error)
            ) from error
        if not stat_is_directory(st.st_mode):
            raise EfiUpdateError("unsafe_config_parent", "EFI update config parent must be a directory")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            pass
        except OSError as error:
            raise EfiUpdateError(
                "config_write_failed", "Cannot create EFI update config directory", detail=_short_text(error)
            ) from error
        try:
            st = directory.lstat()
        except OSError as error:
            raise EfiUpdateError(
                "unsafe_config_parent", "Cannot inspect created EFI update config directory", detail=_short_text(error)
            ) from error
        if not stat_is_directory(st.st_mode) or (st.st_mode & 0o022):
            raise EfiUpdateError("unsafe_config_parent", "Created EFI update config directory is unsafe")
        if require_root and st.st_uid != 0:
            raise EfiUpdateError("unsafe_config_owner", "EFI update config directory must be owned by root")
    _check_secure_parent(path, require_root=require_root)


def write_scope_config(
    path: str | os.PathLike[str],
    esp_uuid: str,
    *,
    require_root: bool = True,
) -> Path:
    """Atomically write the static scope produced during initial install.

    ``path`` is the destination config file.  The installer should pass the
    target-root path (for example ``/target/etc/zeus/efi-update.json``) after
    it has allocated the Zeus ESP and before rebooting into the installed OS.
    """

    if require_root:
        _require_root()
    config_path = Path(path)
    scope = EfiScope(esp_uuid)
    _ensure_parent(config_path, require_root=require_root)
    data = (json.dumps(scope.as_dict(), sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        temporary = None
        directory_fd = os.open(config_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise EfiUpdateError(
            "config_write_failed",
            "Cannot atomically write EFI update config",
            detail=_short_text(error),
        ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    _check_secure_file(config_path, require_root=require_root)
    return config_path


def _mount_target(path: str) -> str:
    # The config currently fixes this value, but normalize only the harmless
    # trailing slash before comparing findmnt output.
    return path.rstrip("/") or "/"


def _parse_mount_output(scope: EfiScope, stdout: str) -> MountedEsp | None:
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError) as error:
        raise EfiUpdateError(
            "mount_probe_invalid", "findmnt returned invalid JSON", detail=_short_text(error)
        ) from error
    if not isinstance(payload, dict):
        raise EfiUpdateError("mount_probe_invalid", "findmnt returned a non-object JSON value")
    filesystems = payload.get("filesystems")
    if not isinstance(filesystems, list):
        raise EfiUpdateError("mount_probe_invalid", "findmnt JSON has no filesystems array")
    target = _mount_target(scope.mountpoint)
    matches = [
        item
        for item in filesystems
        if isinstance(item, dict) and _mount_target(str(item.get("target", ""))) == target
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise EfiUpdateError("mount_probe_ambiguous", "findmnt returned multiple records for /boot/efi")
    item = matches[0]
    source = item.get("source")
    fstype = item.get("fstype")
    mounted_uuid = _normalize_uuid(item.get("uuid"))
    if not isinstance(source, str) or not source.strip():
        raise EfiUpdateError("mount_probe_invalid", "findmnt did not report an ESP source")
    if not isinstance(fstype, str) or fstype.lower() != "vfat":
        raise EfiUpdateError("mount_probe_wrong_type", "The guarded EFI mount is not vfat")
    if mounted_uuid is None:
        raise EfiUpdateError("mount_probe_missing_uuid", "findmnt did not report the mounted ESP UUID")
    return MountedEsp(target=target, source=source, fstype=fstype.lower(), esp_uuid=mounted_uuid)


def probe_mounted_esp(scope: EfiScope, *, runner: Runner | None = None) -> MountedEsp | None:
    """Read the exact ``/boot/efi`` mount identity without mutating mounts."""

    command = [
        FINDMNT_COMMAND,
        "--json",
        "--mountpoint",
        scope.mountpoint,
        "--output",
        "TARGET,SOURCE,FSTYPE,UUID",
    ]
    result = _run(command, runner)
    # findmnt uses status 1 for an unmounted mountpoint.  Empty output is
    # required as well, so a malformed diagnostic cannot be treated as safe.
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise EfiUpdateError(
            "mount_probe_failed",
            "Cannot determine the /boot/efi mount identity",
            detail=_short_text(result.stderr),
        )
    return _parse_mount_output(scope, result.stdout)


def _expect_scope_mount(scope: EfiScope, mounted: MountedEsp | None) -> MountedEsp:
    if mounted is None:
        raise EfiUpdateError("esp_not_mounted", "The Zeus ESP is not mounted at /boot/efi")
    if mounted.esp_uuid != scope.esp_uuid:
        raise EfiUpdateError(
            "wrong_esp_mounted",
            "Refusing EFI update because /boot/efi is not the configured Zeus ESP",
            detail=f"expected {scope.esp_uuid}, found {mounted.esp_uuid}",
        )
    return mounted


def _mount_scope(scope: EfiScope, *, runner: Runner | None = None) -> MountedEsp:
    result = _run([MOUNT_COMMAND, "--uuid", scope.esp_uuid, scope.mountpoint], runner)
    if result.returncode != 0:
        raise EfiUpdateError(
            "esp_mount_failed",
            "Cannot mount the configured Zeus ESP at /boot/efi",
            detail=_short_text(result.stderr),
        )
    mounted = probe_mounted_esp(scope, runner=runner)
    if mounted is None:
        raise EfiUpdateError("esp_mount_unverified", "The Zeus ESP mount could not be verified after mount")
    return _expect_scope_mount(scope, mounted)


def _unmount_scope(scope: EfiScope, *, runner: Runner | None = None) -> None:
    result = _run([UMOUNT_COMMAND, scope.mountpoint], runner)
    if result.returncode != 0:
        raise EfiUpdateError(
            "esp_unmount_failed",
            "Cannot clean up the temporary Zeus ESP mount",
            detail=_short_text(result.stderr),
        )


def _require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise EfiUpdateError("root_required", "Scoped EFI servicing requires root privileges")


def verify_bootupctl_version(*, runner: Runner | None = None) -> None:
    """Require the bootupd client version whose mount semantics we audited."""

    result = _run([BOOTUPCTL_COMMAND, "--version"], runner)
    if result.returncode != 0:
        raise EfiUpdateError(
            "bootupd_version_unavailable",
            "Cannot determine the installed bootupctl version",
            detail=_short_text(result.stderr),
        )
    version_text = result.stdout.strip()
    match = _BOOTUPCTL_VERSION_RE.fullmatch(version_text)
    if match is None or match.group(1) != SUPPORTED_BOOTUPCTL_VERSION:
        found = version_text or "unknown"
        raise EfiUpdateError(
            "unsupported_bootupd_version",
            f"Scoped EFI servicing requires bootupctl {SUPPORTED_BOOTUPCTL_VERSION}",
            detail=f"found {found}",
        )


def _findmnt_source(path: str, *, runner: Runner | None = None) -> str | None:
    """Return the source covering a path, matching bootupd's root lookup."""

    command = [
        FINDMNT_COMMAND,
        "--json",
        "--target",
        path,
        "--output",
        "SOURCE",
    ]
    result = _run(command, runner)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise EfiUpdateError(
            "root_probe_failed",
            f"Cannot determine the filesystem backing {path}",
            detail=_short_text(result.stderr),
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise EfiUpdateError(
            "root_probe_invalid", f"findmnt returned invalid JSON for {path}", detail=_short_text(error)
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("filesystems"), list):
        raise EfiUpdateError("root_probe_invalid", f"findmnt returned no filesystem data for {path}")
    filesystems = payload["filesystems"]
    if len(filesystems) != 1 or not isinstance(filesystems[0], dict):
        raise EfiUpdateError("root_probe_ambiguous", f"findmnt returned multiple sources for {path}")
    source = filesystems[0].get("source")
    if not isinstance(source, str) or not source.strip():
        raise EfiUpdateError("root_probe_invalid", f"findmnt returned no source for {path}")
    source = source.strip()
    if not source.startswith("/dev/"):
        raise EfiUpdateError(
            "root_probe_unsupported",
            f"The filesystem backing {path} is not a block device",
            detail=source,
        )
    return source


def _root_names(value: Any) -> set[str]:
    """Mirror ``Device::find_all_roots`` over lsblk's inverse tree."""

    if not isinstance(value, dict):
        raise EfiUpdateError("root_probe_invalid", "lsblk returned a malformed device record")
    children = value.get("children")
    if children is None or children == []:
        name = value.get("name") or value.get("path")
        if not isinstance(name, str) or not name.strip():
            raise EfiUpdateError("root_probe_invalid", "lsblk returned a root without a device name")
        return {name.strip()}
    if not isinstance(children, list):
        raise EfiUpdateError("root_probe_invalid", "lsblk returned malformed parent devices")
    roots: set[str] = set()
    for child in children:
        roots.update(_root_names(child))
    return roots


def verify_single_backing_disk(*, runner: Runner | None = None) -> str:
    """Require exactly one physical root in bootupd's inverse device tree.

    bootupd's EFI updater unmounts its selected ESP after each root. With more
    than one root, its next iteration can discover and update another ESP;
    refusing multi-root topologies prevents that behavior from expanding the
    configured scope when bootupd changes or a deployment gains another disk.
    """

    probe_errors: list[EfiUpdateError] = []
    for path in ("/boot", "/sysroot"):
        try:
            source = _findmnt_source(path, runner=runner)
        except EfiUpdateError as error:
            # bootupd tries /boot and then /sysroot when the first path is a
            # virtual or otherwise uninspectable filesystem. Preserve that
            # fallback while still failing closed if neither path works.
            probe_errors.append(error)
            continue
        if source is None:
            continue

        command = [LSBLK_COMMAND, "-J", "-b", "-O", "--inverse", source]
        try:
            result = _run(command, runner)
            if result.returncode != 0:
                raise EfiUpdateError(
                    "root_probe_failed",
                    "Cannot enumerate the devices backing the installed root",
                    detail=_short_text(result.stderr),
                )
            try:
                payload = json.loads(result.stdout)
            except (TypeError, ValueError) as error:
                raise EfiUpdateError(
                    "root_probe_invalid", "lsblk returned invalid JSON", detail=_short_text(error)
                ) from error
            if not isinstance(payload, dict) or not isinstance(payload.get("blockdevices"), list):
                raise EfiUpdateError("root_probe_invalid", "lsblk returned no blockdevices array")
            blockdevices = payload["blockdevices"]
            if not blockdevices:
                raise EfiUpdateError("root_probe_invalid", "lsblk returned no backing device")
            roots: set[str] = set()
            for device in blockdevices:
                roots.update(_root_names(device))
        except EfiUpdateError as error:
            probe_errors.append(error)
            continue
        if len(roots) != 1:
            raise EfiUpdateError(
                "multiple_root_devices",
                "Scoped EFI servicing requires exactly one backing disk",
                detail=f"found {len(roots)} roots",
            )
        return next(iter(roots))

    raise EfiUpdateError(
        "root_probe_unavailable",
        "Cannot determine a block device backing /boot or /sysroot",
        detail=probe_errors[-1].detail if probe_errors else None,
    )


def run_update(
    config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    *,
    runner: Runner | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """Guard the Zeus ESP and run the tested bootupd update algorithm.

    If the configured ESP is already mounted at ``/boot/efi``, it is reused
    and left mounted.  If no exact mount exists, the wrapper mounts the UUID,
    verifies it, invokes bootupd, and removes only the mount it created.  A
    wrong existing mount is never unmounted or overwritten.
    """

    if require_root:
        _require_root()
    scope = load_scope(config_path, require_root=require_root)
    verify_bootupctl_version(runner=runner)
    verify_single_backing_disk(runner=runner)
    mounted = probe_mounted_esp(scope, runner=runner)
    mounted_by_wrapper = False
    if mounted is None:
        mounted = _mount_scope(scope, runner=runner)
        mounted_by_wrapper = True
    else:
        mounted = _expect_scope_mount(scope, mounted)

    update_error: EfiUpdateError | None = None
    result: CommandResult | None = None
    cleanup_error: EfiUpdateError | None = None
    try:
        result = _run([BOOTUPCTL_COMMAND, "update"], runner)
        if result.returncode != 0:
            update_error = EfiUpdateError(
                "bootupd_failed",
                "bootupctl update failed",
                detail=_short_text(result.stderr),
            )
    except EfiUpdateError as error:
        update_error = error
    finally:
        try:
            after = probe_mounted_esp(scope, runner=runner)
            if after is None:
                if not mounted_by_wrapper:
                    # bootupd should unmount only inside the service's slave
                    # namespace.  Restore an externally supplied mount if a
                    # misconfigured unit allowed that cleanup to escape.
                    _mount_scope(scope, runner=runner)
            else:
                after = _expect_scope_mount(scope, after)
                if mounted_by_wrapper:
                    _unmount_scope(scope, runner=runner)
        except EfiUpdateError as error:
            cleanup_error = error

    if cleanup_error is not None:
        if update_error is not None:
            raise cleanup_error from update_error
        raise cleanup_error
    if update_error is not None:
        raise update_error
    assert result is not None
    return {
        "ok": True,
        "esp_uuid": scope.esp_uuid,
        "mountpoint": scope.mountpoint,
        "mounted_for_update": mounted_by_wrapper,
        "bootupctl_returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeus-efi-update",
        description="Update Zeus EFI payloads through bootupd using the pinned ESP UUID.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="root-owned EFI scope config")
    parser.add_argument("--json", action="store_true", help="print a machine-readable result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run_update(arguments.config)
    except EfiUpdateError as error:
        if arguments.json:
            print(json.dumps({"ok": False, "error": error.code, "message": error.message}))
        else:
            detail = f": {error.detail}" if error.detail else ""
            print(f"zeus-efi-update: {error.code}: {error.message}{detail}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(result, sort_keys=True))
    else:
        if result["stdout"]:
            print(result["stdout"], end="")
        if result["stderr"]:
            print(result["stderr"], end="", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the installed unit
    raise SystemExit(main())
