#!/usr/bin/env python3
"""Build a signed, repository-backed Zeus Developer Mode system extension.

The builder is deliberately self contained.  It proves the source checkout is
the exact clean tip of a pushed KanterLabs/zeusos branch, reads both the
component policy and payload bytes from that Git object, and then gives
``mksquashfs`` a newly-created staging tree containing only the fixed targets
from the policy.  The working tree is never used as extension input.

The normal invocation is:

    python3 scripts/build-developer-extension.py \
        --base-os-release /path/to/usr/lib/os-release \
        --base-build-id git-0123456789ab \
        --output-dir /tmp/zeus-developer \
        --signing-key /path/to/developer-key

The key is optional for local inspection.  When supplied, the detached
manifest is signed with ``ssh-keygen -Y sign -n zeusos-developer``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as _datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPONENTS = ROOT / "developer-mode" / "components.json"
CANONICAL_REPOSITORY = "https://github.com/KanterLabs/zeusos.git"
CANONICAL_REPOSITORY_NO_SUFFIX = "https://github.com/KanterLabs/zeusos"
SIGNING_NAMESPACE = "zeusos-developer"
EXTENSION_NAME = "zeus-developer"
EXTENSION_RELEASE = f"extension-release.{EXTENSION_NAME}"
DEFAULT_ARCHITECTURE = "x86_64"
DEFAULT_FEDORA_VERSION = "44"
# This is the same per-user spool consumed by the privileged runtime helper.
# Keep the path fixed and derive only the authenticated process UID; callers
# can still choose an explicit output directory for CI fixtures.
DEFAULT_OUTPUT_DIRECTORY = Path("/run/user") / str(os.getuid()) / "zeus-developer"
DEFAULT_MKSQUASHFS = "/usr/bin/mksquashfs"
DEFAULT_SSH_KEYGEN = "/usr/bin/ssh-keygen"

MAX_COMPONENTS = 32
MAX_FILES = 256
MAX_FILE_SIZE = 16 * 1024 * 1024
MAX_TOTAL_FILE_SIZE = 32 * 1024 * 1024
MAX_COMPONENTS_BYTES = 512 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_BASE_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_IMAGE_SIZE = 128 * 1024 * 1024
MAX_TEXT_FIELD = 256
MAX_TESTS = 256

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@/-]{0,127}$")
SAFE_COMPONENT = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SAFE_TEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,255}$")
# Activation is metadata interpreted by the desktop/runtime helpers.  Keep
# it an action label, never an arbitrary string that a later consumer might
# accidentally pass to a shell or service manager.  ``restart-*`` remains
# intentionally open-ended so a new presentation app can add its own focused
# restart action without changing the bundle schema.
SAFE_ACTIVATION = re.compile(
    r"^(?:restart-[a-z][a-z0-9._-]*|logout(?:-login)?|reboot)$"
)
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
OCTAL_MODE = re.compile(r"^(?:0o)?[0-7]{3,4}$")

FULL_IMAGE_LANE = "full-image"

# The component map is allowed to name presentation locations only.  Keeping
# this second, code-level boundary means a malformed map cannot turn an
# explicitly selected component into an arbitrary /usr replacement.
ALLOWED_SOURCE_PREFIXES = (
    "desktop/rootfs/usr/",
    "zeus/assets/",
)
DISALLOWED_SOURCE_PREFIXES = (
    "desktop/rootfs/usr/lib/systemd/",
    "desktop/rootfs/usr/lib/tmpfiles.d/",
    "desktop/rootfs/usr/share/polkit-1/",
    "desktop/rootfs/usr/share/zeus/",
    "desktop/rootfs/etc/",
    "desktop/rootfs/var/",
)
# These repository records never enter the image or the extension payload.
# They are allowed to accumulate after an immutable base build so publishing
# release metadata or recording measurements does not permanently disable the
# desktop fast lane.  Executable, policy, CI, package, image, and test changes
# remain subject to the component map or fail closed.
NON_PAYLOAD_SOURCE_PATHS = frozenset({"README.md"})
NON_PAYLOAD_SOURCE_PREFIXES = ("docs/", "updates/")
DISALLOWED_SOURCE_PATHS = {
    "desktop/rootfs/usr/libexec/zeus-browser-session",
    "desktop/rootfs/usr/libexec/zeus-desktop-safe",
    "desktop/rootfs/usr/libexec/zeus-developer",
    "desktop/rootfs/usr/libexec/zeus-developer-admin",
    "desktop/rootfs/usr/libexec/zeus-style-sync",
    "desktop/rootfs/usr/libexec/zeus-tailscale",
    "desktop/rootfs/usr/libexec/zeus-tailscale-admin",
    "desktop/rootfs/usr/libexec/zeus-temp",
    "desktop/rootfs/usr/libexec/zeus-temp-inspector",
    "desktop/rootfs/usr/libexec/zeus-temp-scheduler",
    "desktop/rootfs/usr/libexec/zeus-temp-session",
    "desktop/rootfs/usr/libexec/zeus-update",
    "desktop/rootfs/usr/libexec/zeus-update-admin",
    "desktop/rootfs/usr/lib/zeus/developer_manifest.py",
    "desktop/rootfs/usr/lib/zeus/tailscale_control.py",
    "desktop/rootfs/usr/lib/zeus/update_manifest.py",
    "desktop/rootfs/usr/lib/zeus/zeus_developer.py",
    "desktop/rootfs/usr/lib/zeus/zeus_temp.py",
    "desktop/rootfs/usr/lib/zeus/zeus_update.py",
}
ALLOWED_TARGET_PREFIXES = (
    "/usr/libexec/zeus-",
    "/usr/lib/zeus/",
    "/usr/share/applications/org.zeus.",
    "/usr/share/backgrounds/zeus/",
    "/usr/share/gnome-background-properties/zeus.",
    "/usr/share/gnome-shell/extensions/zeus-",
    "/usr/share/icons/Zeus/",
    "/usr/share/icons/hicolor/scalable/apps/org.zeus.",
    "/usr/share/themes/Zeus/",
)
RESERVED_TARGETS = {
    f"/usr/lib/extension-release.d/{EXTENSION_RELEASE}",
    "/usr/share/zeus/developer-provenance.json",
}
DISALLOWED_TARGETS = {
    "/usr/libexec/zeus-browser-session",
    "/usr/libexec/zeus-desktop-safe",
    "/usr/libexec/zeus-developer",
    "/usr/libexec/zeus-developer-admin",
    "/usr/libexec/zeus-style-sync",
    "/usr/libexec/zeus-tailscale",
    "/usr/libexec/zeus-tailscale-admin",
    "/usr/libexec/zeus-temp",
    "/usr/libexec/zeus-temp-inspector",
    "/usr/libexec/zeus-temp-scheduler",
    "/usr/libexec/zeus-temp-session",
    "/usr/libexec/zeus-update",
    "/usr/libexec/zeus-update-admin",
    "/usr/lib/zeus/developer_manifest.py",
    "/usr/lib/zeus/tailscale_control.py",
    "/usr/lib/zeus/update_manifest.py",
    "/usr/lib/zeus/zeus_developer.py",
    "/usr/lib/zeus/zeus_temp.py",
    "/usr/lib/zeus/zeus_update.py",
}


class DeveloperBundleError(Exception):
    """A bounded, user-facing builder failure."""

    def __init__(self, code: str, message: str, *, route: str | None = None):
        self.code = code
        self.message = message
        # Preserve the legacy error code while exposing the lane transition
        # required by orchestration.  A desktop bundle cannot safely
        # represent an unclassified source change.
        self.route = route
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PolicyFile:
    source: str
    target: str
    file_type: str
    mode: int
    source_mode: int
    max_size: int


@dataclass(frozen=True)
class PolicyComponent:
    name: str
    activation: str
    tests: tuple[str, ...]
    files: tuple[PolicyFile, ...]


@dataclass(frozen=True)
class ComponentPolicy:
    raw: bytes
    schema_version: int
    product: str
    artifact_kind: str
    extension_name: str
    repository: str
    architecture: str
    components: tuple[PolicyComponent, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True)
class BaseIdentity:
    fedora_id: str
    version_id: str
    sysext_level: str
    image_digest: str | None
    architecture: str


@dataclass(frozen=True)
class SourceProof:
    repository: str
    branch: str
    commit: str
    remote_name: str


@dataclass(frozen=True)
class SelectedFile:
    component: str
    policy: PolicyFile
    data: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def _fail(code: str, message: str, *, route: str | None = None) -> None:
    raise DeveloperBundleError(code, message, route=route)


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _fail("json_invalid", "metadata cannot be encoded as bounded JSON")


def _bounded_read(path: Path, limit: int, code: str, description: str) -> bytes:
    try:
        file_stat = os.lstat(path)
    except OSError:
        _fail(code, f"{description} is unavailable")
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        _fail(code, f"{description} is not a regular file")
    if file_stat.st_size < 0 or file_stat.st_size > limit:
        _fail(code, f"{description} is too large")
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        _fail(code, f"{description} could not be read")
    if len(data) != file_stat.st_size or len(data) > limit:
        _fail(code, f"{description} changed while being read")
    return data


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 30,
    code: str = "command_failed",
    description: str = "command",
    max_output: int = 1024 * 1024,
) -> tuple[bytes, bytes]:
    try:
        result = subprocess.run(
            list(command),
            cwd=os.fspath(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        _fail(code, f"{description} failed")
    stdout = bytes(result.stdout or b"")
    stderr = bytes(result.stderr or b"")
    if len(stdout) > max_output or len(stderr) > max_output:
        _fail(code, f"{description} output is too large")
    if result.returncode != 0:
        _fail(code, f"{description} failed")
    return stdout, stderr


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    timeout: float = 30,
    max_output: int = 1024 * 1024,
    allow_failure: bool = False,
) -> bytes:
    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=os.fspath(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        if allow_failure:
            return b""
        _fail("git_error", "git operation failed")
    stdout = bytes(result.stdout or b"")
    stderr = bytes(result.stderr or b"")
    if len(stdout) > max_output or len(stderr) > max_output:
        _fail("git_error", "git operation output is too large")
    if result.returncode != 0:
        if allow_failure:
            return b""
        _fail("git_error", "git operation failed")
    return stdout


def _git_text(repo: Path, args: Sequence[str], *, timeout: float = 30) -> str:
    raw = _git(repo, args, timeout=timeout)
    try:
        return raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        _fail("git_error", "git returned invalid text")


def _git_text_optional(repo: Path, args: Sequence[str], *, timeout: float = 30) -> str:
    raw = _git(repo, args, timeout=timeout, allow_failure=True)
    try:
        return raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        _fail("git_error", "git returned invalid text")


def _path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    """Make a lexical absolute path without following symlinked ancestors."""

    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        # ``abspath`` normalizes ``.`` and ``..`` lexically.  It does not call
        # realpath, so a later lstat walk can still reject symlink components.
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        _fail("path_invalid", "path is invalid")


def _repo_path(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        _fail("repository_invalid", "repository path is invalid")
    try:
        directory_stat = os.lstat(path)
    except OSError:
        _fail("repository_invalid", "repository path is unavailable")
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        _fail("repository_invalid", "repository path is not a directory")
    return path


def _normalize_repository(url: str) -> str | None:
    """Return the fixed GitHub repository identity for accepted Git URLs."""

    value = url.strip()
    if not value or any(char in value for char in "\r\n\x00"):
        return None
    # Git's scp-like SSH form has no URL scheme.
    if re.fullmatch(r"(?:[^@/:\s]+@)?github\.com:KanterLabs/zeusos(?:\.git)?", value, re.IGNORECASE):
        return CANONICAL_REPOSITORY
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"}:
        return None
    if parsed.hostname is None or parsed.hostname.lower() != "github.com":
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if path != "/KanterLabs/zeusos":
        return None
    if parsed.query or parsed.fragment:
        return None
    return CANONICAL_REPOSITORY


def _safe_branch(value: str) -> str:
    if not value or not SAFE_BRANCH.fullmatch(value) or value.startswith("/") or value.endswith("/"):
        _fail("detached_source", "source branch is invalid")
    if ".." in value or "//" in value or value.endswith("."):
        _fail("detached_source", "source branch is invalid")
    return value


def prove_source(repo_value: str | os.PathLike[str], commit_value: str | None = None, expected_branch: str | None = None) -> SourceProof:
    """Prove a clean branch is exactly the pushed canonical repository tip."""

    repo = _repo_path(repo_value)
    status = _git_text(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        _fail("dirty_source", "source checkout has uncommitted or untracked changes")

    branch_ref = _git_text_optional(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if not branch_ref:
        _fail("detached_source", "source checkout is detached")
    branch = _safe_branch(branch_ref)
    if expected_branch is not None and branch != _safe_branch(expected_branch):
        _fail("wrong_branch", "source branch does not match the requested branch")

    remote_name = _git_text_optional(repo, ["config", "--get", f"branch.{branch}.remote"])
    merge_ref = _git_text_optional(repo, ["config", "--get", f"branch.{branch}.merge"])
    if not remote_name or not merge_ref.startswith("refs/heads/"):
        _fail("unpushed_source", "source branch has no pushed upstream")
    remote_branch = merge_ref.removeprefix("refs/heads/")
    if remote_branch != branch:
        _fail("wrong_branch", "source upstream branch does not match the checked out branch")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", remote_name):
        _fail("wrong_remote", "source upstream remote is invalid")

    urls = _git_text_optional(repo, ["config", "--get-all", f"remote.{remote_name}.url"])
    if not urls:
        _fail("wrong_remote", "source upstream remote URL is unavailable")
    url_lines = tuple(line for line in urls.splitlines() if line)
    pushurls = _git_text_optional(repo, ["config", "--get-all", f"remote.{remote_name}.pushurl"])
    push_lines = tuple(line for line in pushurls.splitlines() if line)
    if any(_normalize_repository(line) != CANONICAL_REPOSITORY for line in (*url_lines, *push_lines)):
        _fail("wrong_remote", "source upstream is not KanterLabs/zeusos")

    resolved = _git_text(repo, ["rev-parse", "--verify", f"{commit_value or 'HEAD'}^{{commit}}"]).lower()
    if not HEX40.fullmatch(resolved):
        _fail("source_commit_invalid", "source commit is not a full Git commit")
    head = _git_text(repo, ["rev-parse", "--verify", "HEAD"]).lower()
    if resolved != head:
        _fail("source_not_head", "selected commit is not the checked out branch tip")
    local_upstream = _git_text_optional(repo, ["rev-parse", "--verify", f"{remote_name}/{remote_branch}"]).lower()
    if resolved != local_upstream:
        _fail("unpushed_source", "source commit differs from its local upstream tip")

    # The tracking ref can be stale after a remote rewrite.  Ask the configured
    # remote for the branch object as the final push proof.
    remote_output = _git_text_optional(repo, ["ls-remote", "--exit-code", remote_name, f"refs/heads/{remote_branch}"])
    remote_hash = remote_output.split()[0].lower() if remote_output.split() else ""
    if not HEX40.fullmatch(remote_hash) or remote_hash != resolved:
        _fail("unpushed_source", "source commit is not the exact remote branch tip")
    return SourceProof(CANONICAL_REPOSITORY, branch, resolved, remote_name)


def _git_succeeds(repo: Path, args: Sequence[str], *, timeout: float = 30) -> bool:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=os.fspath(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _read_source_commit_marker(root: Path) -> str:
    marker = root / "usr" / "share" / "zeus" / "source-commit"
    raw = _bounded_read(marker, 4096, "base_source_required", "installed base source commit")
    try:
        value = raw.decode("ascii", "strict").strip()
    except UnicodeDecodeError:
        _fail("base_source_invalid", "installed base source commit is not ASCII")
    if not HEX40.fullmatch(value):
        _fail("base_source_invalid", "installed base source commit is not a full Git commit")
    return value


def _installed_base_source_commit(
    base_root_value: str | os.PathLike[str] | None,
    base_os_release_value: str | os.PathLike[str] | None,
) -> str:
    """Resolve the source commit represented by the installed base image."""

    if base_root_value is not None:
        return _read_source_commit_marker(_repo_path(base_root_value))
    # ``--base-os-release`` may point at an extracted root for convenience.
    # Treat it as a base root only when it is explicitly a directory; a loose
    # os-release file does not prove which source commit is installed.
    if base_os_release_value is not None:
        candidate = _absolute_path(base_os_release_value)
        try:
            info = os.lstat(candidate)
        except OSError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            return _read_source_commit_marker(_repo_path(candidate))
    # A build executed on Zeus can use the fixed installed identity marker.
    # Missing or malformed host state is a hard failure; callers in CI must
    # provide --base-source-commit explicitly.
    return _read_source_commit_marker(Path("/"))


def prove_changed_paths(
    repo_value: str | os.PathLike[str],
    source: SourceProof,
    policy: ComponentPolicy,
    selected: Sequence[SelectedFile],
    base_commit_value: str | None = None,
) -> str:
    """Require the complete source diff to be covered by selected policy files.

    A caller cannot hide a system or policy change by asking for only one UI
    component.  The comparison base must identify the source commit represented
    by the installed image.  Callers may provide an explicit, fully-qualified
    base source commit; the build entrypoint derives it from the installed base
    marker when one is available.  A candidate's first parent is never a
    sufficient substitute because earlier committed UI changes would be omitted
    from the classification.
    """

    repo = _repo_path(repo_value)
    if base_commit_value is None:
        _fail("base_source_required", "installed base source commit or --base-source-commit is required")
    base_commit = _git_text_optional(repo, ["rev-parse", "--verify", f"{base_commit_value}^{{commit}}"]).lower()
    if not HEX40.fullmatch(base_commit) or base_commit == source.commit:
        _fail("base_source_invalid", "base source commit is unavailable or invalid")
    if not _git_succeeds(repo, ["merge-base", "--is-ancestor", base_commit, source.commit]):
        _fail("base_source_invalid", "base source commit is not an ancestor of the source")
    changed_text = _git_text(
        repo,
        ["diff", "--name-only", "--no-renames", "--diff-filter=ACDMRTUXB", base_commit, source.commit],
    )
    changed = [line for line in changed_text.splitlines() if line]
    if any(
        not line
        or "\\" in line
        or any(part in {"", ".", ".."} for part in PurePosixPath(line).parts)
        or PurePosixPath(line).is_absolute()
        for line in changed
    ):
        _fail("changed_path_invalid", "source diff contains an unsafe path")
    selected_component_names = {item.component for item in selected}
    selected_tests = {
        test
        for component in policy.components
        if component.name in selected_component_names
        for test in component.tests
    }
    allowlisted = {item.policy.source for item in selected} | selected_tests
    policy_sources = {item.source for component in policy.components for item in component.files}
    policy_tests = {test for component in policy.components for test in component.tests}
    non_payload = {
        path
        for path in changed
        if path in NON_PAYLOAD_SOURCE_PATHS
        or any(path.startswith(prefix) for prefix in NON_PAYLOAD_SOURCE_PREFIXES)
    }
    unknown = sorted(set(changed) - policy_sources - policy_tests - non_payload)
    if unknown:
        _fail(
            "changed_path_not_allowlisted",
            "source diff contains files outside the desktop allowlist; use the full image lane",
            route=FULL_IMAGE_LANE,
        )
    omitted = sorted(set(changed) - allowlisted - non_payload)
    if omitted:
        _fail(
            "component_selection_incomplete",
            "selected components do not cover the complete source diff; use the full image lane",
            route=FULL_IMAGE_LANE,
        )
    return base_commit


def _object_bytes(repo: Path, commit: str, path: str, *, limit: int, code: str, description: str) -> bytes:
    # ``git show`` addresses the object database directly.  A path beginning
    # with '-' cannot become an option because it is after the object-spec.
    try:
        raw = _git(repo, ["show", "--no-ext-diff", "--format=", f"{commit}:{path}"], max_output=limit + 1)
    except DeveloperBundleError:
        _fail(code, f"{description} is unavailable in the source commit")
    if len(raw) > limit:
        _fail(code, f"{description} is too large")
    return raw


def _parse_json_bytes(raw: bytes, *, code: str, description: str) -> Any:
    try:
        text = raw.decode("utf-8", "strict")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail(code, f"{description} is invalid JSON")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("json_duplicate_field", "metadata contains duplicate fields")
        result[key] = value
    return result


def _text_field(value: Any, *, field: str, pattern: re.Pattern[str] | None = None, max_length: int = MAX_TEXT_FIELD) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length or any(ord(char) < 0x20 for char in value):
        _fail("components_invalid", f"{field} is invalid")
    if pattern is not None and not pattern.fullmatch(value):
        _fail("components_invalid", f"{field} is invalid")
    return value


def _parse_mode(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        _fail("components_invalid", f"{field} is invalid")
    if isinstance(value, int):
        mode = value
    elif isinstance(value, str) and OCTAL_MODE.fullmatch(value):
        try:
            mode = int(value.removeprefix("0o"), 8)
        except ValueError:
            _fail("components_invalid", f"{field} is invalid")
    else:
        _fail("components_invalid", f"{field} is invalid")
    if mode < 0 or mode > 0o777 or mode & 0o022:
        _fail("components_invalid", f"{field} has unsafe permissions")
    return mode


def _validate_relative_source(value: Any) -> str:
    source = _text_field(value, field="source", max_length=512)
    if (
        source.startswith("/")
        or "\\" in source
        or not source.startswith(ALLOWED_SOURCE_PREFIXES)
        or source.startswith(DISALLOWED_SOURCE_PREFIXES)
        or source in DISALLOWED_SOURCE_PATHS
    ):
        _fail("components_invalid", "component source is outside the desktop source tree")
    path = PurePosixPath(source)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("components_invalid", "component source path is unsafe")
    return source


def _validate_target(value: Any) -> str:
    target = _text_field(value, field="target", max_length=512)
    if not target.startswith("/") or "\\" in target:
        _fail("components_invalid", "component target must be an absolute /usr path")
    path = PurePosixPath(target)
    if any(part in {"", ".", ".."} for part in path.parts) or not target.startswith("/usr/"):
        _fail("components_invalid", "component target path is unsafe")
    if target in RESERVED_TARGETS or target in DISALLOWED_TARGETS or not target.startswith(ALLOWED_TARGET_PREFIXES):
        _fail("components_invalid", "component target is outside the presentation allowlist")
    return target


def _component_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                _fail("components_invalid", "component entry is invalid")
            item = dict(value)
            item.setdefault("name", name)
            items.append(item)
    else:
        _fail("components_invalid", "components must be an array or object")
    if len(items) == 0 or len(items) > MAX_COMPONENTS:
        _fail("components_invalid", "component count is outside the supported range")
    return items


def load_policy(repo_value: str | os.PathLike[str], commit: str, policy_value: str | os.PathLike[str] | None = None) -> ComponentPolicy:
    """Load and validate the checked-in component policy."""

    repo = _repo_path(repo_value)
    # The default map is addressed relative to the named Git object.  Using
    # this checkout's absolute path here would accidentally read a mutable
    # policy from the builder host when the caller supplies an isolated repo.
    policy_path_value = os.fspath(policy_value) if policy_value is not None else "developer-mode/components.json"
    policy_path = Path(policy_path_value)
    if policy_path.is_absolute():
        policy_path = _absolute_path(policy_path)
        if not _path_is_within(policy_path, repo):
            _fail("components_invalid", "component policy must be checked in to the source repository")
    else:
        relative = policy_path
    if policy_path.is_absolute():
        relative = policy_path.relative_to(repo)
        relative_text = PurePosixPath(relative.as_posix()).as_posix()
    else:
        relative_text = PurePosixPath(relative.as_posix()).as_posix()
    if relative_text.startswith("../") or relative_text in {".", ""} or "\\" in relative_text or any(part in {"", ".", ".."} for part in PurePosixPath(relative_text).parts):
        _fail("components_invalid", "component policy path is unsafe")
    # The policy is itself part of the signed source object.  Reject a Git
    # symlink, submodule, or executable policy before parsing its bytes so a
    # mutable checkout cannot smuggle a different map into the bundle.
    policy_tree_entry = _git_tree_entries(repo, commit).get(relative_text)
    if policy_tree_entry is None or policy_tree_entry[1] != "blob" or policy_tree_entry[0] != 0o100644:
        _fail("components_invalid", "component policy is not a regular Git file")
    raw = _object_bytes(repo, commit, relative_text, limit=MAX_COMPONENTS_BYTES, code="components_invalid", description="component policy")
    parsed = _parse_json_bytes(raw, code="components_invalid", description="component policy")
    if not isinstance(parsed, dict):
        _fail("components_invalid", "component policy must be an object")
    allowed_top = {
        "schema_version",
        "product",
        "artifact_kind",
        "extension_name",
        "repository",
        "architecture",
        "components",
    }
    if set(parsed) - allowed_top:
        _fail("components_invalid", "component policy contains unknown fields")
    schema = parsed.get("schema_version", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
        _fail("components_invalid", "component policy schema must be 1")
    product = parsed.get("product", "zeusos")
    if product != "zeusos":
        _fail("components_invalid", "component policy product is not zeusos")
    artifact_kind = parsed.get("artifact_kind", "systemd-sysext")
    if artifact_kind not in {"systemd-sysext", "sysext"}:
        _fail("components_invalid", "component policy artifact kind is invalid")
    extension_name = _text_field(parsed.get("extension_name", EXTENSION_NAME), field="extension_name", pattern=SAFE_COMPONENT)
    if extension_name != EXTENSION_NAME:
        _fail("components_invalid", "component policy extension name is invalid")
    repository = parsed.get("repository", CANONICAL_REPOSITORY)
    if not isinstance(repository, str) or _normalize_repository(repository) != CANONICAL_REPOSITORY:
        _fail("components_invalid", "component policy repository is not KanterLabs/zeusos")
    architecture = parsed.get("architecture", DEFAULT_ARCHITECTURE)
    if architecture not in {"x86_64", "amd64", "x86-64"}:
        _fail("components_invalid", "component policy architecture is invalid")
    components: list[PolicyComponent] = []
    seen_names: set[str] = set()
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    total_files = 0
    total_max_size = 0
    for raw_component in _component_items(parsed.get("components")):
        if set(raw_component) - {"name", "activation", "tests", "files"}:
            _fail("components_invalid", "component contains unknown fields")
        name = _text_field(raw_component.get("name"), field="component name", pattern=SAFE_COMPONENT)
        if name in seen_names:
            _fail("components_invalid", "component names must be unique")
        seen_names.add(name)
        activation = _text_field(raw_component.get("activation"), field="activation")
        if SAFE_ACTIVATION.fullmatch(activation) is None:
            _fail("components_invalid", "component activation is not an approved action")
        raw_tests = raw_component.get("tests", [])
        if not isinstance(raw_tests, list) or len(raw_tests) > MAX_TESTS:
            _fail("components_invalid", "component test list is invalid")
        tests: list[str] = []
        for test in raw_tests:
            if not isinstance(test, str) or not SAFE_TEST.fullmatch(test) or not test.startswith("tests/"):
                _fail("components_invalid", "component test name is invalid")
            if test in tests:
                _fail("components_invalid", "component tests must be unique")
            tests.append(test)
        raw_files = raw_component.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            _fail("components_invalid", "component file list is invalid")
        files: list[PolicyFile] = []
        for raw_file in raw_files:
            if not isinstance(raw_file, dict) or set(raw_file) - {"source", "target", "type", "mode", "source_mode", "max_size"}:
                _fail("components_invalid", "component file entry is invalid")
            source = _validate_relative_source(raw_file.get("source"))
            target = _validate_target(raw_file.get("target"))
            if source in seen_sources:
                _fail("components_invalid", "each source must map to one fixed target")
            if target in seen_targets:
                _fail("components_invalid", "each target must be unique")
            seen_sources.add(source)
            seen_targets.add(target)
            file_type = raw_file.get("type", "regular")
            if file_type not in {"regular", "file"}:
                _fail("components_invalid", "component file type must be regular")
            mode = _parse_mode(raw_file.get("mode"), field="mode")
            source_mode = _parse_mode(raw_file.get("source_mode", raw_file.get("mode")), field="source_mode")
            max_size = raw_file.get("max_size")
            if isinstance(max_size, bool) or not isinstance(max_size, int) or not 0 < max_size <= MAX_FILE_SIZE:
                _fail("components_invalid", "component file size limit is invalid")
            total_files += 1
            total_max_size += max_size
            if total_files > MAX_FILES or total_max_size > MAX_TOTAL_FILE_SIZE:
                _fail("components_invalid", "component file limits are too large")
            files.append(PolicyFile(source, target, "regular", mode, source_mode, max_size))
        components.append(PolicyComponent(name, activation, tuple(sorted(tests)), tuple(sorted(files, key=lambda item: item.target))))
    return ComponentPolicy(
        raw,
        schema,
        "zeusos",
        "systemd-sysext",
        extension_name,
        CANONICAL_REPOSITORY,
        "x86_64" if architecture in {"amd64", "x86-64"} else architecture,
        tuple(sorted(components, key=lambda item: item.name)),
    )


def _archive_members_from_bytes(raw: bytes) -> dict[str, tarfile.TarInfo]:
    if len(raw) > MAX_ARCHIVE_BYTES:
        _fail("archive_invalid", "Git archive is too large")
    members: dict[str, tarfile.TarInfo] = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except (tarfile.TarError, OSError, ValueError):
        _fail("archive_invalid", "Git archive is invalid")
    try:
        for member in archive:
            name = member.name
            path = PurePosixPath(name)
            if not name or path.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in path.parts):
                _fail("archive_invalid", "Git archive contains an unsafe path")
            if name in members:
                _fail("archive_invalid", "Git archive contains duplicate paths")
            members[name] = member
    except (tarfile.TarError, OSError, ValueError):
        _fail("archive_invalid", "Git archive could not be inspected")
    finally:
        archive.close()
    return members


def _archive_bytes(repo_value: str | os.PathLike[str], commit: str) -> bytes:
    repo = _repo_path(repo_value)
    return _git(repo, ["archive", "--format=tar", commit], timeout=120, max_output=MAX_ARCHIVE_BYTES + 1)


def _archive_members(repo_value: str | os.PathLike[str], commit: str) -> dict[str, tarfile.TarInfo]:
    return _archive_members_from_bytes(_archive_bytes(repo_value, commit))


def _git_tree_entries(repo_value: str | os.PathLike[str], commit: str) -> dict[str, tuple[int, str]]:
    """Return object modes/types without trusting tar's umask-adjusted mode."""

    repo = _repo_path(repo_value)
    raw = _git(repo, ["ls-tree", "-r", "-z", commit], timeout=60, max_output=MAX_ARCHIVE_BYTES)
    entries: dict[str, tuple[int, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, path_raw = record.split(b"\t", 1)
            mode_raw, object_type, _object_id = header.split(b" ", 2)
            path = path_raw.decode("utf-8", "strict")
            mode = int(mode_raw, 8)
            object_type_text = object_type.decode("ascii", "strict")
        except (UnicodeDecodeError, ValueError):
            _fail("archive_invalid", "Git tree contains an invalid path or mode")
        if path in entries or not path or "\\" in path or PurePosixPath(path).is_absolute() or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts):
            _fail("archive_invalid", "Git tree contains an unsafe path")
        entries[path] = (mode, object_type_text)
    return entries


def select_files(repo_value: str | os.PathLike[str], commit: str, policy: ComponentPolicy, component_names: Iterable[str] | None = None) -> tuple[SelectedFile, ...]:
    """Select and hash policy files from a Git archive of ``commit``."""

    requested = None if component_names is None else tuple(component_names)
    if isinstance(component_names, str):
        requested = (component_names,)
    by_name = {component.name: component for component in policy.components}
    if requested is not None:
        if not requested:
            _fail("component_unknown", "at least one component must be selected")
        unknown = [name for name in requested if name not in by_name]
        if unknown:
            _fail("component_unknown", "requested component is not in the policy")
        if len(set(requested)) != len(requested):
            _fail("component_unknown", "requested components must be unique")
        components = tuple(by_name[name] for name in sorted(requested))
    else:
        components = policy.components
    archive_raw = _archive_bytes(repo_value, commit)
    members = _archive_members_from_bytes(archive_raw)
    tree_entries = _git_tree_entries(repo_value, commit)
    selected: list[SelectedFile] = []
    total_size = 0
    for component in components:
        for policy_file in component.files:
            member = members.get(policy_file.source)
            if member is None:
                _fail("source_missing", f"allowlisted source is absent from the selected commit: {policy_file.source}")
            if not member.isreg() or member.issym() or member.islnk() or member.isdev():
                _fail("source_type", f"allowlisted source is not a regular file: {policy_file.source}")
            tree_entry = tree_entries.get(policy_file.source)
            if tree_entry is None:
                _fail("source_missing", f"allowlisted source is absent from the Git object: {policy_file.source}")
            tree_mode, tree_type = tree_entry
            if tree_type != "blob" or tree_mode not in {0o100644, 0o100755}:
                _fail("source_type", f"allowlisted source is not a regular Git file: {policy_file.source}")
            mode = tree_mode & 0o7777
            if mode != policy_file.source_mode:
                _fail("source_mode", f"source mode is outside the policy: {policy_file.source}")
            if member.size < 0 or member.size > policy_file.max_size:
                _fail("source_size", f"source size is outside the policy: {policy_file.source}")
            try:
                with tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:") as archive:
                    member_data = archive.extractfile(member)
                    if member_data is None:
                        _fail("source_type", f"source cannot be read: {policy_file.source}")
                    data = member_data.read(policy_file.max_size + 1)
            except (tarfile.TarError, OSError, ValueError):
                _fail("archive_invalid", "Git archive could not be read")
            if len(data) != member.size or len(data) > policy_file.max_size:
                _fail("source_size", f"source changed while being read: {policy_file.source}")
            total_size += len(data)
            if total_size > MAX_TOTAL_FILE_SIZE:
                _fail("source_size", "selected component data is too large")
            selected.append(SelectedFile(component.name, policy_file, data, hashlib.sha256(data).hexdigest()))
    return tuple(sorted(selected, key=lambda item: (item.policy.target, item.component)))


def _parse_os_release(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("base_invalid", "base os-release is not UTF-8")
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _fail("base_invalid", "base os-release has an invalid line")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", key):
            _fail("base_invalid", "base os-release has an invalid key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
            if '"' == line.split("=", 1)[1][:1]:
                # os-release permits only a small escaped set in double quotes;
                # replacing these four forms avoids executing shell syntax.
                value = value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        if not value or any(ord(char) < 0x20 and char != "\t" for char in value):
            _fail("base_invalid", "base os-release has an invalid value")
        if key in values:
            _fail("base_invalid", "base os-release contains a duplicate key")
        values[key] = value
    return values


def _validate_digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(char in value for char in "\r\n\x00"):
        _fail("base_invalid", "base image digest is invalid")
    if HEX64.fullmatch(value):
        digest = "sha256:" + value
    elif value.startswith("sha256:"):
        digest = value
    elif "@sha256:" in value:
        digest = "sha256:" + value.rsplit("@sha256:", 1)[1]
    else:
        _fail("base_invalid", "base image digest is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        _fail("base_invalid", "base image digest is invalid")
    return digest


def _base_file_value(base_root: Path | None, relative: str) -> bytes | None:
    if base_root is None:
        return None
    path = base_root / relative
    try:
        path = path.resolve()
        if not _path_is_within(path, base_root):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        os.lstat(path)
    except OSError:
        return None
    return _bounded_read(path, MAX_BASE_BYTES, "base_invalid", f"base {relative}")


def load_base_identity(
    *,
    base_os_release_value: str | os.PathLike[str] | None = None,
    base_root_value: str | os.PathLike[str] | None = None,
    base_build_id: str | None = None,
    base_image_digest: str | None = None,
    fedora_version: str | None = None,
    architecture: str = DEFAULT_ARCHITECTURE,
    repo_value: str | os.PathLike[str] | None = None,
    commit: str | None = None,
) -> BaseIdentity:
    """Read and cross-check Fedora identity and the exact SYSEXT_LEVEL."""

    if architecture in {"amd64", "x86-64"}:
        architecture = "x86_64"
    if architecture != "x86_64":
        _fail("base_invalid", "only x86_64 Developer Mode artifacts are supported")
    base_root: Path | None = None
    if base_root_value is not None:
        base_root = _repo_path(base_root_value)
    os_release_path: Path | None = None
    if base_os_release_value is not None:
        candidate = Path(base_os_release_value).expanduser()
        try:
            candidate = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            _fail("base_invalid", "base os-release path is invalid")
        if candidate.is_dir():
            for relative in ("usr/lib/os-release", "etc/os-release"):
                probe = candidate / relative
                if probe.exists():
                    os_release_path = probe
                    break
        else:
            os_release_path = candidate
    elif base_root is not None:
        for relative in ("usr/lib/os-release", "etc/os-release"):
            probe = base_root / relative
            if probe.exists():
                os_release_path = probe
                break
    values: dict[str, str] = {}
    if os_release_path is not None:
        raw = _bounded_read(os_release_path, MAX_BASE_BYTES, "base_invalid", "base os-release")
        if raw.lstrip().startswith(b"{"):
            parsed = _parse_json_bytes(raw, code="base_invalid", description="base identity")
            if not isinstance(parsed, dict):
                _fail("base_invalid", "base identity is invalid")
            for key, value in parsed.items():
                if isinstance(key, str) and isinstance(value, (str, int)) and not isinstance(value, bool):
                    values[key.upper()] = str(value)
        else:
            values = _parse_os_release(raw)
    fedora_id = values.get("ID", "fedora")
    if fedora_id != "fedora":
        _fail("base_mismatch", "base ID is not Fedora")
    observed_version = values.get("VERSION_ID")
    if fedora_version is not None and observed_version is not None and fedora_version != observed_version:
        _fail("base_mismatch", "base VERSION_ID does not match the requested Fedora version")
    version = fedora_version or observed_version or DEFAULT_FEDORA_VERSION
    version = _text_field(version, field="base VERSION_ID", pattern=re.compile(r"^[0-9]+(?:\.[0-9]+)?$"), max_length=32)
    observed_architecture = values.get("ARCHITECTURE") or values.get("BUILD_ARCHITECTURE")
    if observed_architecture is not None:
        normalized_observed = "x86_64" if observed_architecture in {"amd64", "x86-64"} else observed_architecture
        if normalized_observed != architecture:
            _fail("base_mismatch", "base architecture does not match the requested architecture")
    release_level = values.get("SYSEXT_LEVEL") or values.get("BUILD_ID")
    if not release_level and base_root is not None:
        raw_level = _base_file_value(base_root, "usr/share/zeus/build-id")
        if raw_level:
            try:
                release_level = raw_level.decode("utf-8", "strict").strip()
            except UnicodeDecodeError:
                _fail("base_invalid", "base build ID is not UTF-8")
    if base_build_id is not None:
        base_build_id = _text_field(base_build_id, field="base build ID", pattern=SAFE_TOKEN)
        if release_level and release_level != base_build_id:
            _fail("base_mismatch", "base SYSEXT_LEVEL does not match the requested base ID")
        release_level = base_build_id
    if not release_level or not SAFE_TOKEN.fullmatch(release_level):
        _fail("base_invalid", "base SYSEXT_LEVEL is unavailable or invalid")
    discovered_digest_value: str | None = None
    if base_root is not None:
        raw_inputs = _base_file_value(base_root, "usr/share/zeus/inputs.json")
        if raw_inputs:
            parsed_inputs = _parse_json_bytes(raw_inputs, code="base_invalid", description="base inputs")
            if isinstance(parsed_inputs, dict) and isinstance(parsed_inputs.get("base"), str):
                discovered_digest_value = parsed_inputs["base"]
    if discovered_digest_value is None and repo_value is not None and commit is not None:
        try:
            raw_inputs = _object_bytes(_repo_path(repo_value), commit, "image/inputs.json", limit=MAX_BASE_BYTES, code="base_invalid", description="image inputs")
            parsed_inputs = _parse_json_bytes(raw_inputs, code="base_invalid", description="image inputs")
            if isinstance(parsed_inputs, dict) and isinstance(parsed_inputs.get("base"), str):
                discovered_digest_value = parsed_inputs["base"]
        except DeveloperBundleError:
            # The digest is supplementary provenance; identity still requires
            # the Fedora ID and exact level above.
            discovered_digest_value = None
    discovered_digest = _validate_digest(discovered_digest_value) if discovered_digest_value is not None else None
    requested_digest = _validate_digest(base_image_digest) if base_image_digest is not None else None
    if discovered_digest is not None and requested_digest is not None and discovered_digest != requested_digest:
        _fail("base_mismatch", "base image digest does not match the installed base identity")
    digest = requested_digest or discovered_digest
    return BaseIdentity("fedora", version, release_level, digest, architecture)


def _format_timestamp(value: str | None) -> str:
    if value is not None:
        if not RFC3339.fullmatch(value):
            _fail("metadata_invalid", "timestamp must be UTC RFC3339")
        try:
            _datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            _fail("metadata_invalid", "timestamp is invalid")
        return value
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _focused_tests(policy: ComponentPolicy, components: Sequence[PolicyComponent]) -> list[str]:
    return sorted({test for component in components for test in component.tests})


def _normalise_test_result(value: Any) -> str:
    if not isinstance(value, str):
        _fail("test_receipt_invalid", "test result is invalid")
    lowered = value.strip().lower()
    if lowered in {"pass", "passed", "ok", "success", "successful"}:
        return "passed"
    if lowered in {"fail", "failed", "error", "failure"}:
        return "failed"
    if lowered in {"skip", "skipped", "not-run", "not_run", "pending"}:
        return "not-run"
    _fail("test_receipt_invalid", "test result is invalid")


def load_test_receipt(
    receipt_value: str | os.PathLike[str] | None,
    *,
    source_commit: str,
    focused_tests: Sequence[str],
    created_at: str,
) -> dict[str, Any]:
    """Bound a caller's focused-test evidence to the selected source commit."""

    results: dict[str, dict[str, str]] = {}
    recorded_at = created_at
    if receipt_value is None and focused_tests:
        _fail("test_receipt_required", "focused tests require a receipt bound to the source commit")
    if receipt_value is not None:
        path = Path(receipt_value).expanduser().resolve()
        raw = _bounded_read(path, MAX_RECEIPT_BYTES, "test_receipt_invalid", "test receipt")
        parsed = _parse_json_bytes(raw, code="test_receipt_invalid", description="test receipt")
        if not isinstance(parsed, dict):
            _fail("test_receipt_invalid", "test receipt must be an object")
        receipt_schema = parsed.get("schema_version", 1)
        if isinstance(receipt_schema, bool) or not isinstance(receipt_schema, int) or receipt_schema != 1:
            _fail("test_receipt_invalid", "test receipt schema must be 1")
        receipt_commit = parsed.get("source_commit") or parsed.get("commit")
        if receipt_commit is not None and receipt_commit != source_commit:
            _fail("test_receipt_mismatch", "test receipt source commit does not match the artifact")
        supplied_time = parsed.get("recorded_at") or parsed.get("timestamp") or parsed.get("created_at")
        if supplied_time is not None:
            recorded_at = _format_timestamp(supplied_time)
        raw_results = parsed.get("results", parsed.get("tests", []))
        if isinstance(raw_results, dict):
            entries = []
            for name, result in raw_results.items():
                entries.append({"name": name, "result": result})
        elif isinstance(raw_results, list):
            entries = raw_results
        else:
            _fail("test_receipt_invalid", "test receipt results are invalid")
        if len(entries) > MAX_TESTS:
            _fail("test_receipt_invalid", "test receipt has too many results")
        for entry in entries:
            if isinstance(entry, str):
                name, result = entry, "passed"
                entry_time = recorded_at
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("test")
                result = entry.get("result", entry.get("status"))
                entry_time = entry.get("recorded_at") or entry.get("timestamp") or recorded_at
            else:
                _fail("test_receipt_invalid", "test receipt result entry is invalid")
            if not isinstance(name, str) or not SAFE_TEST.fullmatch(name) or not name.startswith("tests/"):
                _fail("test_receipt_invalid", "test receipt test name is invalid")
            if name in results:
                _fail("test_receipt_invalid", "test receipt contains duplicate tests")
            entry_time = _format_timestamp(entry_time)
            normalized = _normalise_test_result(result)
            results[name] = {"name": name, "result": normalized, "recorded_at": entry_time}
            if normalized == "failed":
                _fail("focused_tests_failed", f"focused test did not pass: {name}")
    for name in focused_tests:
        result = results.get(name)
        if result is None or result["result"] != "passed":
            _fail("focused_tests_incomplete", f"focused test did not pass: {name}")
    ordered = [results[name] for name in sorted(results)]
    receipt = {
        "schema_version": 1,
        "source_commit": source_commit,
        "recorded_at": recorded_at,
        "results": ordered,
    }
    if len(_canonical_json(receipt)) > MAX_RECEIPT_BYTES:
        _fail("test_receipt_invalid", "normalized test receipt is too large")
    return receipt


def _component_manifest(component: PolicyComponent, selected: Sequence[SelectedFile]) -> dict[str, Any]:
    return {
        "name": component.name,
        "activation": component.activation,
        "files": [
            {
                "source": item.policy.source,
                "target": item.policy.target,
                "type": item.policy.file_type,
                "mode": item.policy.mode,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in sorted(selected, key=lambda value: value.policy.target)
        ],
    }


def _write_file(path: Path, data: bytes, mode: int) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(path, (0, 0), follow_symlinks=False)
    except (OSError, ValueError):
        if "fd" in locals() and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                path.unlink()
        except OSError:
            pass
        _fail("staging_error", "could not write extension staging data")


def _write_prepared_pointer(path: Path, digest: str) -> None:
    """Atomically publish the runtime's bounded latest-digest pointer."""

    if not HEX64.fullmatch(digest):
        _fail("manifest_invalid", "prepared pointer digest is invalid")
    parent = _ensure_output_directory(path.parent)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    except OSError:
        _fail("output_exists", "prepared pointer cannot be inspected")
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        _fail("output_exists", "prepared pointer is not a regular file")
    data = _canonical_json({"schema_version": 1, "digest": digest})
    if len(data) > 16 * 1024:
        _fail("manifest_invalid", "prepared pointer is too large")
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".building", dir=parent)
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(temporary, (0, 0), follow_symlinks=False)
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError):
        _fail("output_error", "could not publish prepared pointer")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _stage_tree(
    selected: Sequence[SelectedFile],
    *,
    base: BaseIdentity,
    source: SourceProof,
    base_source_commit: str,
    policy: ComponentPolicy,
    stage: Path,
) -> tuple[dict[str, Any], str, str]:
    for item in selected:
        target = PurePosixPath(item.policy.target)
        path = stage.joinpath(*target.parts[1:])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o755)
        except OSError:
            _fail("staging_error", "could not create extension target directory")
        _write_file(path, item.data, item.policy.mode)

    extension_release_data = (
        f"ID={base.fedora_id}\n"
        f"VERSION_ID={base.version_id}\n"
        f"SYSEXT_LEVEL={base.sysext_level}\n"
        f"ARCHITECTURE=x86-64\n"
        "SYSEXT_SCOPE=system\n"
    ).encode("utf-8")
    extension_release_path = stage / "usr" / "lib" / "extension-release.d" / EXTENSION_RELEASE
    extension_release_path.parent.mkdir(parents=True, exist_ok=True)
    _write_file(extension_release_path, extension_release_data, 0o644)

    selected_components: list[PolicyComponent] = []
    by_name: dict[str, list[SelectedFile]] = {}
    for item in selected:
        by_name.setdefault(item.component, []).append(item)
    for component in policy.components:
        if component.name in by_name:
            selected_components.append(component)
    component_records = [_component_manifest(component, by_name[component.name]) for component in sorted(selected_components, key=lambda value: value.name)]
    provenance = {
        "schema_version": 1,
        "product": "zeusos",
        "artifact_kind": "systemd-sysext",
        "extension_name": EXTENSION_NAME,
        "repository": source.repository,
        "branch": source.branch,
        "base_source_commit": base_source_commit,
        "source_commit": source.commit,
        "base": {
            "id": base.fedora_id,
            "version_id": base.version_id,
            "sysext_level": base.sysext_level,
            "architecture": base.architecture,
            "image_digest": base.image_digest,
        },
        "component_policy_sha256": policy.sha256,
        "components": component_records,
    }
    provenance_data = _canonical_json(provenance)
    provenance_path = stage / "usr" / "share" / "zeus" / "developer-provenance.json"
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    _write_file(provenance_path, provenance_data, 0o644)
    internal_files = [
        {
            "target": f"/usr/lib/extension-release.d/{EXTENSION_RELEASE}",
            "type": "regular",
            "mode": 0o644,
            "size": len(extension_release_data),
            "sha256": hashlib.sha256(extension_release_data).hexdigest(),
        },
        {
            "target": "/usr/share/zeus/developer-provenance.json",
            "type": "regular",
            "mode": 0o644,
            "size": len(provenance_data),
            "sha256": hashlib.sha256(provenance_data).hexdigest(),
        },
    ]
    # Directory timestamps influence filesystem metadata in some mksquashfs
    # versions, so normalize every directory after all files exist.
    try:
        directories = sorted((path for path in stage.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            directory.chmod(0o755)
            os.utime(directory, (0, 0), follow_symlinks=False)
        os.utime(stage, (0, 0), follow_symlinks=False)
    except OSError:
        _fail("staging_error", "could not normalize extension staging timestamps")
    return {"components": component_records, "internal_files": internal_files}, hashlib.sha256(provenance_data).hexdigest(), provenance_data.decode("utf-8")


def _resolve_executable(value: str, *, code: str, description: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            file_stat = os.lstat(candidate)
        except OSError:
            _fail(code, f"{description} is unavailable")
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            _fail(code, f"{description} is invalid")
        return os.fspath(candidate)
    resolved = shutil.which(value)
    if not resolved:
        _fail(code, f"{description} is unavailable")
    return resolved


def _make_image(stage: Path, destination: Path, executable: str) -> None:
    _run(
        [
            executable,
            os.fspath(stage),
            os.fspath(destination),
            "-noappend",
            "-all-root",
            "-no-xattrs",
            "-no-exports",
            "-no-fragments",
            "-no-progress",
            "-quiet",
            "-processors",
            "1",
            "-comp",
            "xz",
            "-repro-time",
            "0",
        ],
        timeout=180,
        code="image_build_failed",
        description="mksquashfs",
        max_output=1024 * 1024,
    )
    try:
        file_stat = os.lstat(destination)
    except OSError:
        _fail("image_build_failed", "mksquashfs did not produce an image")
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0 or file_stat.st_size > MAX_IMAGE_SIZE:
        _fail("image_build_failed", "extension image is invalid")


def _key_fingerprint(key: Path, ssh_keygen: str) -> str | None:
    """Obtain a public fingerprint without ever reading private bytes aloud."""

    try:
        result = subprocess.run(
            [ssh_keygen, "-lf", os.fspath(key)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        fields = bytes(result.stdout or b"").decode("utf-8", "strict").split()
    except UnicodeDecodeError:
        return None
    for field in fields:
        if field.startswith("SHA256:") and len(field) <= 128:
            return field
    return None


def _sign_manifest(manifest_data: bytes, manifest_path: Path, key_value: str | os.PathLike[str], ssh_keygen_value: str) -> tuple[Path, str | None]:
    try:
        key = _absolute_path(key_value)
    except DeveloperBundleError:
        _fail("signing_key_invalid", "signing key is invalid")
    try:
        key_stat = os.lstat(key)
    except OSError:
        _fail("signing_key_invalid", "signing key is unavailable")
    if stat.S_ISLNK(key_stat.st_mode) or not stat.S_ISREG(key_stat.st_mode) or key_stat.st_size <= 0 or key_stat.st_size > 64 * 1024:
        _fail("signing_key_invalid", "signing key is invalid")
    temp_manifest = manifest_path.with_name(f".{manifest_path.name}.signing")
    temp_signature = Path(f"{temp_manifest}.sig")
    try:
        _write_file(temp_manifest, manifest_data, 0o600)
        _run(
            [ssh_keygen_value, "-Y", "sign", "-f", os.fspath(key), "-n", SIGNING_NAMESPACE, os.fspath(temp_manifest)],
            timeout=30,
            code="signing_failed",
            description="developer manifest signing",
            max_output=256 * 1024,
        )
        signature_data = _bounded_read(temp_signature, 256 * 1024, "signing_failed", "developer signature")
        try:
            _write_file(manifest_path, manifest_data, 0o644)
            _write_file(Path(f"{manifest_path}.sig"), signature_data, 0o644)
        except DeveloperBundleError:
            for path in (manifest_path, Path(f"{manifest_path}.sig")):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return Path(f"{manifest_path}.sig"), _key_fingerprint(key, ssh_keygen_value)
    finally:
        for path in (temp_manifest, temp_signature):
            try:
                path.unlink()
            except OSError:
                pass


def _ensure_output_directory(value: str | os.PathLike[str], *, private: bool = False) -> Path:
    path = _absolute_path(value)
    if not path.name:
        _fail("output_invalid", "output directory is invalid")
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                entry = os.lstat(current)
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700 if private else 0o755)
                except FileExistsError:
                    entry = os.lstat(current)
                else:
                    entry = os.lstat(current)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                _fail("output_invalid", "output directory contains an unsafe path")
        final_stat = os.lstat(path)
        if private and final_stat.st_mode & 0o077:
            _fail("output_invalid", "private output directory is not protected")
    except (OSError, RuntimeError, ValueError):
        _fail("output_invalid", "output directory is unavailable")
    return path


def _final_path(path: Path, description: str) -> None:
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        _fail("output_exists", f"{description} cannot be inspected")
    if stat.S_ISLNK(file_stat.st_mode) or stat.S_ISREG(file_stat.st_mode) or stat.S_ISDIR(file_stat.st_mode):
        _fail("output_exists", f"{description} already exists")
    _fail("output_exists", f"{description} already exists")


def _build_image_to_temporary(stage: Path, temporary: Path, executable: str) -> None:
    _final_path(temporary, "temporary extension image")
    try:
        _make_image(stage, temporary, executable)
        os.chmod(temporary, 0o644)
    except DeveloperBundleError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _publish_temporary_image(temporary: Path, image_path: Path) -> None:
    _final_path(image_path, "extension image")
    try:
        os.link(temporary, image_path)
    except FileExistsError:
        _fail("output_exists", "extension image already exists")
    except OSError:
        _fail("output_error", "could not publish extension image")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _file_digest(path: Path) -> tuple[int, str]:
    try:
        file_stat = os.lstat(path)
    except OSError:
        _fail("image_build_failed", "extension image is unavailable")
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        _fail("image_build_failed", "extension image is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > MAX_IMAGE_SIZE:
        _fail("image_build_failed", "extension image size is outside the supported range")
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
                if total > MAX_IMAGE_SIZE:
                    _fail("image_build_failed", "extension image is too large")
    except DeveloperBundleError:
        raise
    except (OSError, ValueError):
        _fail("image_build_failed", "extension image could not be read")
    if total != file_stat.st_size:
        _fail("image_build_failed", "extension image changed while being read")
    return total, digest.hexdigest()


def _product_version(repo_value: str | os.PathLike[str], commit: str, override: str | None) -> str:
    if override is not None:
        return _text_field(override, field="product version", pattern=re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"), max_length=64)
    try:
        raw = _object_bytes(_repo_path(repo_value), commit, "image/release-track.json", limit=64 * 1024, code="metadata_invalid", description="release track")
        parsed = _parse_json_bytes(raw, code="metadata_invalid", description="release track")
        if isinstance(parsed, dict) and isinstance(parsed.get("version"), str):
            return _text_field(parsed["version"], field="product version", pattern=re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"), max_length=64)
    except DeveloperBundleError:
        pass
    return "0.1.0-preview.2"


def build_bundle(
    *,
    repo: str | os.PathLike[str] = ".",
    commit: str | None = None,
    components: str | os.PathLike[str] | None = None,
    component_names: Iterable[str] | None = None,
    base_os_release: str | os.PathLike[str] | None = None,
    base_root: str | os.PathLike[str] | None = None,
    base_build_id: str | None = None,
    base_image_digest: str | None = None,
    fedora_version: str | None = None,
    architecture: str = DEFAULT_ARCHITECTURE,
    product_version: str | None = None,
    test_receipt: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] = DEFAULT_OUTPUT_DIRECTORY,
    output: str | os.PathLike[str] | None = None,
    manifest: str | os.PathLike[str] | None = None,
    signing_key: str | os.PathLike[str] | None = None,
    mksquashfs: str = DEFAULT_MKSQUASHFS,
    ssh_keygen: str = DEFAULT_SSH_KEYGEN,
    image_format: str = "raw",
    created_at: str | None = None,
    branch: str | None = None,
    base_source_commit: str | None = None,
    prepared: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if image_format not in {"raw", "squashfs"}:
        _fail("output_invalid", "image format must be raw or squashfs")
    repo_path = _repo_path(repo)
    source = prove_source(repo_path, commit, branch)
    policy = load_policy(repo_path, source.commit, components)
    selected = select_files(repo_path, source.commit, policy, component_names)
    comparison_base = base_source_commit
    if comparison_base is None:
        comparison_base = _installed_base_source_commit(base_root, base_os_release)
    resolved_base_source = prove_changed_paths(repo_path, source, policy, selected, comparison_base)
    selected_component_names = sorted({item.component for item in selected})
    selected_components = tuple(component for component in policy.components if component.name in selected_component_names)
    base = load_base_identity(
        base_os_release_value=base_os_release,
        base_root_value=base_root,
        base_build_id=base_build_id,
        base_image_digest=base_image_digest,
        fedora_version=fedora_version,
        architecture=architecture,
        repo_value=repo_path,
        commit=source.commit,
    )
    if policy.architecture != base.architecture:
        _fail("base_mismatch", "component architecture does not match the Fedora base")
    timestamp = _format_timestamp(created_at)
    focused_tests = _focused_tests(policy, selected_components)
    receipt = load_test_receipt(test_receipt, source_commit=source.commit, focused_tests=focused_tests, created_at=timestamp)
    version = _product_version(repo_path, source.commit, product_version)
    explicit_artifact = output is not None or manifest is not None
    output_directory = _ensure_output_directory(output_dir, private=not explicit_artifact)
    prepared_path: Path | None = None
    if prepared is not None:
        prepared_path = _absolute_path(prepared)
    elif not explicit_artifact:
        # The runtime's no-argument apply reads this optional latest digest.
        prepared_path = output_directory / "prepared.json"
    if explicit_artifact:
        image_path = _absolute_path(output) if output is not None else output_directory / f"extension.{image_format}"
        manifest_path = _absolute_path(manifest) if manifest is not None else image_path.parent / "manifest.json"
        _ensure_output_directory(image_path.parent)
        _ensure_output_directory(manifest_path.parent)
        _final_path(image_path, "extension image")
        _final_path(manifest_path, "developer manifest")
        _final_path(Path(f"{manifest_path}.sig"), "developer signature")
    else:
        # The runtime consumes a content-addressed prepared spool.  The image
        # is first made beside the spool root; after hashing, it is linked into
        # <root>/<sha256>/extension.raw and the manifest is written there.
        image_path = None
        manifest_path = None
    executable = _resolve_executable(mksquashfs, code="image_build_failed", description="mksquashfs")
    signer_executable = _resolve_executable(ssh_keygen, code="signing_failed", description="ssh-keygen") if signing_key is not None else ssh_keygen
    temporary_image = output_directory / f".{EXTENSION_NAME}-{os.getpid()}-{source.commit[:12]}.building"
    with tempfile.TemporaryDirectory(prefix="zeus-developer-stage-") as temporary:
        stage = Path(temporary)
        internal, provenance_sha256, _ = _stage_tree(
            selected,
            base=base,
            source=source,
            base_source_commit=resolved_base_source,
            policy=policy,
            stage=stage,
        )
        _build_image_to_temporary(stage, temporary_image, executable)
    try:
        image_size, image_sha256 = _file_digest(temporary_image)
    except DeveloperBundleError:
        try:
            temporary_image.unlink()
        except OSError:
            pass
        raise
    if not explicit_artifact:
        artifact_directory = _ensure_output_directory(output_directory / image_sha256, private=True)
        image_path = artifact_directory / "extension.raw" if image_format == "raw" else artifact_directory / "extension.squashfs"
        manifest_path = artifact_directory / "manifest.json"
        _final_path(image_path, "extension image")
        _final_path(manifest_path, "developer manifest")
        _final_path(Path(f"{manifest_path}.sig"), "developer signature")
    assert image_path is not None and manifest_path is not None
    if prepared_path is not None and prepared_path in {
        image_path,
        manifest_path,
        Path(f"{manifest_path}.sig"),
    }:
        _fail("output_invalid", "prepared pointer must have its own path")
    test_receipt_sha256 = hashlib.sha256(_canonical_json(receipt)).hexdigest()
    components_record = internal["components"]
    activation = sorted({component["activation"] for component in components_record})
    manifest_value: dict[str, Any] = {
        "schema_version": 1,
        "product": "zeusos",
        "artifact_kind": "systemd-sysext",
        "extension_name": EXTENSION_NAME,
        "format": image_format,
        "filesystem": "squashfs",
        "architecture": base.architecture,
        "version": version,
        "repository": source.repository,
        "branch": source.branch,
        "base_source_commit": resolved_base_source,
        "source_commit": source.commit,
        "base": {
            "id": base.fedora_id,
            "version_id": base.version_id,
            "sysext_level": base.sysext_level,
            "architecture": base.architecture,
            "image_digest": base.image_digest,
        },
        # These flat identity fields make the receipt convenient for the root
        # helper while the nested ``base`` object remains the source of truth.
        "base_build_id": base.sysext_level,
        "base_image_digest": base.image_digest,
        "component_policy_sha256": policy.sha256,
        "components": components_record,
        "required_activation": activation,
        "focused_tests": focused_tests,
        "test_receipt": receipt,
        "test_receipt_sha256": test_receipt_sha256,
        "provenance": {
            "inner_path": "/usr/share/zeus/developer-provenance.json",
            "inner_sha256": provenance_sha256,
            "extension_release_path": f"/usr/lib/extension-release.d/{EXTENSION_RELEASE}",
            "files": internal["internal_files"],
        },
        "artifact": {
            "name": image_path.name,
            "format": image_format,
            "filesystem": "squashfs",
            "size": image_size,
            "sha256": image_sha256,
        },
        "signature": {
            "algorithm": "ssh",
            "namespace": SIGNING_NAMESPACE,
            "required": signing_key is not None,
            "signed": False,
            "identity": SIGNING_NAMESPACE,
            "fingerprint": None,
        },
        "signer": SIGNING_NAMESPACE,
        "created_at": timestamp,
    }
    manifest_data = _canonical_json(manifest_value)
    if len(manifest_data) > MAX_MANIFEST_BYTES:
        _fail("manifest_invalid", "developer manifest is too large")
    signature_path: Path | None = None
    if signing_key is None:
        _write_file(manifest_path, manifest_data, 0o644)
    else:
        signature_path, identity = _sign_manifest(manifest_data, manifest_path, signing_key, signer_executable)
        manifest_value["signature"]["signed"] = True
        manifest_value["signature"]["fingerprint"] = identity
        # The identity is metadata in the signed bytes, so sign once more when
        # fingerprint discovery succeeded.  If a test double cannot report a
        # fingerprint, the initial signature remains valid and bounded.
        final_data = _canonical_json(manifest_value)
        if final_data != manifest_data:
            try:
                manifest_path.unlink()
                signature_path.unlink()
            except OSError:
                pass
            signature_path, _ = _sign_manifest(final_data, manifest_path, signing_key, signer_executable)
            manifest_data = final_data
        else:
            manifest_data = final_data
    # Ensure unsigned manifests also carry their final bytes and every output
    # remains a regular file after the atomic publication.
    if signing_key is None:
        try:
            os.chmod(manifest_path, 0o644)
        except OSError:
            _fail("output_error", "could not finalize developer manifest")
    # Publish the image only after its detached metadata is complete.  The
    # hard link is atomic within the private spool and leaves no partial image
    # if manifest creation or signing fails.
    _publish_temporary_image(temporary_image, image_path)
    result = {
        "artifact": os.fspath(image_path),
        "manifest": os.fspath(manifest_path),
        "signature": os.fspath(signature_path) if signature_path is not None else None,
        "source_commit": source.commit,
        "artifact_sha256": image_sha256,
    }
    if prepared_path is not None:
        _write_prepared_pointer(prepared_path, image_sha256)
        result["prepared"] = os.fspath(prepared_path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="clean ZeusOS checkout")
    parser.add_argument("--commit", help="full or symbolic commit; defaults to HEAD")
    parser.add_argument("--base-source-commit", help="commit to compare for the complete extension diff")
    parser.add_argument("--branch", help="require this checked-out branch")
    parser.add_argument("--components", "--policy", dest="components", help="checked-in component map")
    parser.add_argument("--component", dest="component_names", action="append", help="component to include (repeatable)")
    parser.add_argument("--base-os-release", "--base", dest="base_os_release", help="Fedora os-release file or root")
    parser.add_argument("--base-root", help="root of an extracted Fedora base")
    parser.add_argument("--base-build-id", "--base-id", dest="base_build_id", help="exact base SYSEXT_LEVEL")
    parser.add_argument("--base-image-digest", "--base-digest", dest="base_image_digest", help="base image digest or image@sha256:digest")
    parser.add_argument("--fedora-version", help="expected Fedora VERSION_ID")
    parser.add_argument("--architecture", default=DEFAULT_ARCHITECTURE)
    parser.add_argument("--version", "--product-version", dest="product_version")
    parser.add_argument("--test-receipt", "--tests-receipt", dest="test_receipt")
    parser.add_argument("--output-dir", default=os.fspath(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--output", "--image", dest="output")
    parser.add_argument("--manifest")
    parser.add_argument("--prepared", "--prepared-pointer", dest="prepared", help="write a bounded latest-artifact pointer")
    parser.add_argument("--signing-key")
    parser.add_argument("--mksquashfs", default=DEFAULT_MKSQUASHFS)
    parser.add_argument("--ssh-keygen", default=DEFAULT_SSH_KEYGEN)
    parser.add_argument("--format", dest="image_format", choices=("raw", "squashfs"), default="raw")
    parser.add_argument("--created-at", help="UTC timestamp, useful for reproducible receipts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_bundle(
            repo=args.repo,
            commit=args.commit,
            components=args.components,
            component_names=args.component_names,
            base_os_release=args.base_os_release,
            base_root=args.base_root,
            base_build_id=args.base_build_id,
            base_image_digest=args.base_image_digest,
            fedora_version=args.fedora_version,
            architecture=args.architecture,
            product_version=args.product_version,
            test_receipt=args.test_receipt,
            output_dir=args.output_dir,
            output=args.output,
            manifest=args.manifest,
            signing_key=args.signing_key,
            mksquashfs=args.mksquashfs,
            ssh_keygen=args.ssh_keygen,
            image_format=args.image_format,
            created_at=args.created_at,
            branch=args.branch,
            base_source_commit=args.base_source_commit,
            prepared=args.prepared,
        )
    except DeveloperBundleError as error:
        route = f" (route: {error.route})" if error.route else ""
        print(f"error: {error.code}: {error.message}{route}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
