"""Trust and content validation for Zeus Developer Mode extensions.

The Developer Mode command is deliberately a small, boring file boundary.  A
desktop process supplies one hexadecimal digest; the privileged helper finds
the corresponding files in a fixed per-user spool and verifies every part of
the bundle before it can be made visible through ``systemd-sysext``.

This module has no third-party dependencies.  It is kept separate from the
runtime so that the unprivileged status/UI code can use the bounded manifest
normaliser without importing any privileged operations.
"""

from __future__ import annotations

import datetime as _datetime
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
PRODUCT = "zeusos"
ARTIFACT_KIND = "systemd-sysext"
ARCHITECTURE = "x86_64"
REPOSITORY = "https://github.com/KanterLabs/zeusos"

DEVELOPER_SIGNERS = "/usr/share/zeus/developer-allowed-signers"
SIGNER_IDENTITY = "zeusos-developer"
SIGNATURE_NAMESPACE = "zeusos-developer"
SSH_KEYGEN = "/usr/bin/ssh-keygen"

# All limits are intentionally modest for a desktop extension.  A larger
# package belongs in the complete developer-image lane.
MAX_MANIFEST_BYTES = 128 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_ARTIFACT_SIZE = 512 * 1024 * 1024
MAX_FILE_SIZE = 16 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 256 * 1024 * 1024
MAX_COMPONENTS = 32
MAX_FILES = 256
MAX_TESTS = 128
MAX_TEXT_BYTES = 512
MAX_TAR_MEMBERS = 4096
MAX_TAR_PATH_BYTES = 512
SIGNATURE_TIMEOUT = 30
MOUNT_TIMEOUT = 30
MOUNT = "/usr/bin/mount"
UMOUNT = "/usr/bin/umount"
MOUNT_ENV = {"PATH": "/usr/sbin:/usr/bin", "LANG": "C.UTF-8", "HOME": "/root"}

HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
HEX40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SAFE_BRANCH_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
SAFE_LEVEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
RFC3339_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,9})?)?(?:Z|\+00:00)\Z"
)


class DeveloperError(Exception):
    """An expected, user-safe Developer Mode failure."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(str(message))


class _DuplicateKey(ValueError):
    pass


class _InvalidJSON(ValueError):
    pass


def _error(code: str, message: str) -> None:
    raise DeveloperError(code, message)


def _bounded_text(value: Any, *, field: str, limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value or any(ord(ch) < 0x20 for ch in value):
        _error("manifest_invalid", f"{field} is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _error("manifest_invalid", f"{field} is invalid")
    if len(encoded) > limit:
        _error("manifest_too_large", f"{field} is too large")
    return value


def _strict_json(raw: bytes, *, label: str, limit: int = MAX_MANIFEST_BYTES) -> Any:
    if not isinstance(raw, bytes):
        _error("manifest_invalid", f"{label} must be bytes")
    if len(raw) == 0:
        _error("manifest_invalid", f"{label} is empty")
    if len(raw) > limit:
        _error("manifest_too_large", f"{label} is too large")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _error("manifest_invalid", f"{label} is not UTF-8")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise _InvalidJSON(value)

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except _DuplicateKey:
        _error("manifest_duplicate_field", f"{label} contains a duplicate field")
    except (
        _InvalidJSON,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        OverflowError,
        TypeError,
        MemoryError,
    ):
        _error("manifest_invalid", f"{label} is not valid JSON")


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        _error("manifest_invalid", f"{field} must be an object")
    return value


def _hex(value: Any, *, field: str, length: int = 64) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        _error("manifest_invalid", f"{field} is not a lowercase SHA-256 value")
    return value


def _digest(value: Any, *, field: str) -> str:
    """Accept raw hex and OCI-style ``sha256:`` values, return raw hex."""

    if isinstance(value, str) and value.startswith("sha256:"):
        value = value[7:]
    return _hex(value, field=field)


def _safe_source(value: Any, *, field: str) -> str:
    value = _bounded_text(value, field=field, limit=1024)
    if "\\" in value or value.startswith("/") or "\x00" in value:
        _error("path_unsafe", f"{field} is not a repository-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _error("path_unsafe", f"{field} is not a repository-relative path")
    # A source is a checked-in path, rather than a URI or a filesystem
    # expression.  This also prevents a colon-prefixed pseudo path.
    if ":" in parts[0] or any(part.startswith("~") for part in parts):
        _error("path_unsafe", f"{field} is not a repository-relative path")
    return "/".join(parts)


def _safe_target(value: Any, *, field: str) -> str:
    value = _bounded_text(value, field=field, limit=1024)
    if not value.startswith("/usr/") or "\\" in value or "\x00" in value or "//" in value or value.endswith("/"):
        _error("path_unsafe", f"{field} is outside the extension namespace")
    parts = PurePosixPath(value).parts
    if not parts or parts[0] != "/" or any(part in {"", ".", ".."} for part in parts[1:]):
        _error("path_unsafe", f"{field} is outside the extension namespace")
    return value


def _mode(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0o777:
        _error("manifest_invalid", f"{field} is not a regular file mode")
    if value & 0o6000:
        _error("path_unsafe", f"{field} contains special permission bits")
    return value


def _size(value: Any, *, field: str, limit: int = MAX_FILE_SIZE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > limit:
        _error("manifest_invalid", f"{field} is outside the supported size range")
    return value


def _safe_name(value: Any, *, field: str) -> str:
    value = _bounded_text(value, field=field, limit=128)
    if SAFE_ID_RE.fullmatch(value) is None:
        _error("manifest_invalid", f"{field} is invalid")
    return value


def _timestamp(value: Any, *, field: str = "created_at") -> str:
    value = _bounded_text(value, field=field, limit=64)
    if RFC3339_RE.fullmatch(value) is None:
        _error("manifest_invalid", f"{field} is not a UTC timestamp")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error("manifest_invalid", f"{field} is not a valid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != _datetime.timedelta(0):
        _error("manifest_invalid", f"{field} must use UTC")
    return value


COMPONENT_POLICY_PATH = "/usr/share/zeus/developer-components.json"


def _policy_mode(value: Any, *, field: str) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-7]{3,4}", value):
        value = int(value, 8)
    return _mode(value, field=field)


def load_component_map(path: str | os.PathLike[str] = COMPONENT_POLICY_PATH) -> dict[str, dict[str, Any]]:
    """Read and strictly validate the image-installed component policy."""

    try:
        info = os.lstat(path)
    except OSError:
        _error("component_map_invalid", "developer component policy is unavailable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
        _error("component_map_invalid", "developer component policy is unavailable")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        _error("component_map_invalid", "developer component policy is unavailable")
    parsed = _strict_json(raw, label="component policy", limit=512 * 1024)
    parsed = _mapping(parsed, field="component policy")
    allowed_top = {"schema_version", "product", "artifact_kind", "extension_name", "repository", "architecture", "components"}
    if any(key not in allowed_top for key in parsed):
        _error("component_map_invalid", "component policy contains unknown fields")
    if parsed.get("schema_version") != SCHEMA_VERSION or parsed.get("product") != PRODUCT:
        _error("component_map_invalid", "component policy identity is invalid")
    if parsed.get("artifact_kind") != "systemd-sysext" or parsed.get("extension_name") != "zeus-developer":
        _error("component_map_invalid", "component policy identity is invalid")
    repository = parsed.get("repository")
    if repository not in {REPOSITORY, REPOSITORY + ".git"}:
        _error("component_map_invalid", "component policy repository is invalid")
    if parsed.get("architecture") not in {"x86_64", "amd64"}:
        _error("component_map_invalid", "component policy architecture is invalid")
    components = parsed.get("components")
    if not isinstance(components, list) or not components or len(components) > MAX_COMPONENTS:
        _error("component_map_invalid", "component policy component list is invalid")
    result: dict[str, dict[str, Any]] = {}
    component_names: set[str] = set()
    target_names: set[str] = set()
    total_max = 0
    for index, raw_component in enumerate(components):
        component = _mapping(raw_component, field=f"component policy components[{index}]")
        if any(key not in {"name", "activation", "tests", "files"} for key in component):
            _error("component_map_invalid", "component policy contains unknown fields")
        name = _safe_name(component.get("name"), field="component policy name")
        if name in component_names:
            _error("component_map_invalid", "component policy component names are not unique")
        component_names.add(name)
        activation = component.get("activation")
        if isinstance(activation, str):
            activation_values = [_safe_name(activation, field="component policy activation")]
        elif isinstance(activation, list) and activation:
            activation_values = [_safe_name(item, field="component policy activation") for item in activation]
        else:
            _error("component_map_invalid", "component policy activation is invalid")
        tests = component.get("tests", [])
        if not isinstance(tests, list) or len(tests) > MAX_TESTS:
            _error("component_map_invalid", "component policy tests are invalid")
        for test in tests:
            _bounded_text(test, field="component policy test", limit=256)
        files = component.get("files")
        if not isinstance(files, list) or not files:
            _error("component_map_invalid", "component policy files are invalid")
        for file_index, raw_file in enumerate(files):
            entry = _mapping(raw_file, field=f"component policy files[{file_index}]")
            if any(key not in {"source", "target", "type", "mode", "source_mode", "max_size"} for key in entry):
                _error("component_map_invalid", "component policy file contains unknown fields")
            source = _safe_source(entry.get("source"), field="component policy source")
            target = _safe_target(entry.get("target"), field="component policy target")
            if source in result or target in target_names:
                _error("component_map_invalid", "component policy paths are not unique")
            target_names.add(target)
            if entry.get("type") not in {"regular", "file"}:
                _error("component_map_invalid", "component policy file type is invalid")
            mode = _policy_mode(entry.get("mode"), field="component policy mode")
            source_mode = _policy_mode(entry.get("source_mode", entry.get("mode")), field="component policy source mode")
            maximum = entry.get("max_size")
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 0 < maximum <= MAX_FILE_SIZE:
                _error("component_map_invalid", "component policy file size is invalid")
            total_max += maximum
            if total_max > MAX_TOTAL_FILE_BYTES:
                _error("component_map_invalid", "component policy files are too large")
            result[source] = {
                "component": name,
                "target": target,
                "mode": mode,
                "source_mode": source_mode,
                "max_size": maximum,
                "activation": activation_values,
            }
    return result


# Kept as an injectable/test-visible name.  Production callers load the
# image-installed policy through ``load_component_map`` rather than trusting a
# divergent Python copy.
APPROVED_COMPONENTS: dict[str, dict[str, Any]] = {}


def _component_entries(value: Any, *, field: str) -> list[Mapping[str, Any]]:
    """Expand both the compact one-file and grouped manifest spellings."""

    if not isinstance(value, list) or not value or len(value) > MAX_COMPONENTS:
        _error("manifest_invalid", f"{field} must contain a bounded component list")
    entries: list[Mapping[str, Any]] = []
    for index, component_value in enumerate(value):
        component = _mapping(component_value, field=f"{field}[{index}]")
        if any(
            key not in {
                "name", "id", "component", "activation", "activation_actions",
                "files", "source", "source_path", "target", "target_path",
                "sha256", "hash", "size", "mode", "type",
            }
            for key in component
        ):
            _error("manifest_unknown_field", "developer component contains an unknown field")
        files = component.get("files")
        if files is None:
            files = [component]
        if not isinstance(files, list) or not files:
            _error("manifest_invalid", f"{field}[{index}].files is invalid")
        name = component.get("name", component.get("id", component.get("component")))
        if name is not None:
            name = _safe_name(name, field=f"{field}[{index}].name")
        activation = component.get("activation", component.get("activation_actions"))
        if activation is not None:
            if isinstance(activation, str):
                activation = [activation]
            if not isinstance(activation, list) or not activation or len(activation) > 16:
                _error("manifest_invalid", f"{field}[{index}].activation is invalid")
            activation = [_safe_name(item, field="activation") for item in activation]
        for file_index, file_value in enumerate(files):
            item = _mapping(file_value, field=f"{field}[{index}].files[{file_index}]")
            if any(
                key not in {
                    "source", "source_path", "target", "target_path", "sha256",
                    "hash", "size", "mode", "type",
                }
                for key in item
            ):
                _error("manifest_unknown_field", "developer component file contains an unknown field")
            source = item.get("source", item.get("source_path"))
            target = item.get("target", item.get("target_path"))
            if source is None or target is None:
                _error("manifest_missing_field", "component source and target are required")
            normalized: dict[str, Any] = {
                "name": name,
                "activation": activation,
                "source": _safe_source(source, field="component.source"),
                "target": _safe_target(target, field="component.target"),
                "sha256": _digest(item.get("sha256", item.get("hash")), field="component.sha256"),
                "size": _size(item.get("size"), field="component.size"),
                "mode": _policy_mode(item.get("mode"), field="component.mode"),
                "type": item.get("type", "file"),
            }
            if normalized["type"] not in {"file", "regular"}:
                _error("path_unsafe", "developer extensions may contain regular files only")
            entries.append(normalized)
    if len(entries) > MAX_FILES:
        _error("manifest_too_large", "developer extension contains too many files")
    return entries


def _source_metadata(data: Mapping[str, Any]) -> dict[str, str | None]:
    source = data.get("source")
    if source is not None:
        source = _mapping(source, field="source")
    else:
        source = {}
    repository = source.get("repository", data.get("repository", REPOSITORY))
    branch = source.get("branch", data.get("branch"))
    commit = source.get("commit", data.get("source_commit"))
    repository = _bounded_text(repository, field="source.repository", limit=256)
    if repository not in {REPOSITORY, REPOSITORY + ".git"}:
        _error("manifest_invalid", "manifest repository is not the Zeus repository")
    repository = REPOSITORY
    if branch is not None:
        branch = _bounded_text(branch, field="source.branch", limit=128)
        if (
            SAFE_BRANCH_RE.fullmatch(branch) is None
            or branch.startswith("/")
            or branch.endswith("/")
            or branch.endswith(".")
            or ".." in branch
            or "//" in branch
        ):
            _error("manifest_invalid", "source branch is invalid")
    if not isinstance(commit, str) or HEX40_RE.fullmatch(commit) is None:
        _error("manifest_invalid", "source commit is invalid")
    return {"repository": repository, "branch": branch, "commit": commit}


def validate_manifest(
    data: Mapping[str, Any],
    *,
    component_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and normalise a signed Developer Mode manifest.

    The result contains only bounded, canonical values.  A caller must still
    verify the detached signature and the artifact bytes; this function does
    not treat a JSON object supplied by a desktop process as trusted.
    """

    data = _mapping(data, field="manifest")
    allowed_top = {
        "schema_version", "schema", "product", "artifact_kind", "kind", "extension_name",
        "format", "filesystem", "architecture", "version", "repository", "branch",
        "source", "source_commit", "base_source_commit", "base", "base_build_id", "base_sysext_level",
        "base_image_digest", "components", "files", "component_policy_sha256",
        "required_activation", "activation_actions", "focused_tests", "tests",
        "test_receipt", "test_receipt_sha256", "provenance", "artifact",
        "artifact_sha256", "artifact_size", "extension_sha256", "extension_size", "sha256",
        "signature", "signer", "signer_identity", "created_at", "created", "artifact_name",
    }
    if any(key not in allowed_top for key in data):
        _error("manifest_unknown_field", "developer manifest contains an unknown field")
    schema = data.get("schema_version", data.get("schema"))
    if schema != SCHEMA_VERSION:
        _error("manifest_invalid", "unsupported developer manifest schema")
    product = data.get("product", PRODUCT)
    if product != PRODUCT:
        _error("manifest_invalid", "manifest product is not Zeus OS")
    kind = data.get("artifact_kind", data.get("kind", ARTIFACT_KIND))
    if kind not in {ARTIFACT_KIND, "sysext"}:
        _error("manifest_invalid", "manifest artifact kind is unsupported")
    architecture = data.get("architecture", ARCHITECTURE)
    if architecture not in {ARCHITECTURE, "amd64", "x86-64"}:
        _error("manifest_invalid", "manifest architecture is unsupported")
    if data.get("extension_name", "zeus-developer") != "zeus-developer":
        _error("manifest_invalid", "manifest extension name is unsupported")
    if "version" in data:
        _bounded_text(data["version"], field="version", limit=64)
    if "format" in data and data["format"] not in {"raw", "squashfs"}:
        _error("manifest_invalid", "manifest extension format is unsupported")
    if "filesystem" in data and data["filesystem"] not in {"squashfs", "directory", "raw"}:
        _error("manifest_invalid", "manifest extension filesystem is unsupported")

    base_value = data.get("base")
    base = _mapping(base_value, field="base") if base_value is not None else {}
    if any(key not in {"id", "version_id", "sysext_level", "SYSEXT_LEVEL", "architecture", "image_digest", "digest"} for key in base):
        _error("manifest_unknown_field", "developer base identity contains an unknown field")
    sysext_level = base.get("sysext_level", base.get("SYSEXT_LEVEL", data.get("base_sysext_level", data.get("sysext_level"))))
    if not isinstance(sysext_level, str) or SAFE_LEVEL_RE.fullmatch(sysext_level) is None:
        _error("base_invalid", "manifest base SYSEXT_LEVEL is invalid")
    image_digest = base.get("image_digest", base.get("digest", data.get("base_image_digest")))
    if image_digest is not None:
        image_digest = "sha256:" + _digest(image_digest, field="base.image_digest")
    flat_image_digest = data.get("base_image_digest")
    if flat_image_digest is not None:
        flat_image_digest = "sha256:" + _digest(flat_image_digest, field="base_image_digest")
        if image_digest is not None and image_digest != flat_image_digest:
            _error("base_invalid", "manifest base image digest fields do not match")
        image_digest = flat_image_digest
    base_architecture = base.get("architecture")
    if base_architecture is not None and base_architecture not in {"x86_64", "amd64", "x86-64"}:
        _error("base_invalid", "manifest base architecture is unsupported")
    if base_architecture in {"amd64", "x86-64"}:
        base_architecture = ARCHITECTURE
    if base_architecture is not None and base_architecture != ARCHITECTURE:
        _error("base_invalid", "manifest base architecture does not match the artifact")
    base_id = base.get("id")
    if base_id is not None:
        base_id = _safe_name(base_id, field="base.id")
        if base_id != "fedora":
            _error("base_invalid", "manifest base ID is unsupported")
    base_version_id = base.get("version_id")
    if base_version_id is not None:
        base_version_id = _bounded_text(base_version_id, field="base.version_id", limit=32)
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", base_version_id) is None:
            _error("base_invalid", "manifest base VERSION_ID is invalid")
    base_build_id = data.get("base_build_id")
    if base_build_id is not None:
        _bounded_text(base_build_id, field="base_build_id", limit=128)
        if base_build_id != sysext_level:
            _error("base_invalid", "manifest base build ID does not match SYSEXT_LEVEL")

    source = _source_metadata(data)

    # The builder records the exact source commit used as the comparison base.
    # Keep this field part of the signed contract even though activation only
    # needs the selected source commit.
    base_source_commit = data.get("base_source_commit")
    if base_source_commit is not None and (
        not isinstance(base_source_commit, str) or HEX40_RE.fullmatch(base_source_commit) is None
    ):
        _error("manifest_invalid", "base source commit is invalid")

    artifact_value = data.get("artifact")
    artifact = _mapping(artifact_value, field="artifact") if artifact_value is not None else {}
    if any(key not in {"name", "format", "filesystem", "size", "sha256"} for key in artifact):
        _error("manifest_unknown_field", "developer artifact identity contains an unknown field")
    artifact_sha = artifact.get(
        "sha256",
        data.get("artifact_sha256", data.get("extension_sha256", data.get("sha256"))),
    )
    artifact_size = artifact.get(
        "size",
        data.get("artifact_size", data.get("extension_size", data.get("size"))),
    )
    artifact_sha = _digest(artifact_sha, field="artifact.sha256")
    artifact_size = _size(artifact_size, field="artifact.size", limit=MAX_ARTIFACT_SIZE)
    if artifact_size == 0:
        _error("manifest_invalid", "developer extension artifact is empty")
    artifact_name = artifact.get("name", data.get("artifact_name"))
    if artifact_name is not None:
        artifact_name = _bounded_text(artifact_name, field="artifact.name", limit=128)
        if (
            PurePosixPath(artifact_name).is_absolute()
            or "/" in artifact_name
            or "\\" in artifact_name
            or artifact_name in {".", ".."}
        ):
            _error("path_unsafe", "developer artifact name is invalid")
    artifact_format = artifact.get("format", data.get("format"))
    artifact_filesystem = artifact.get("filesystem", data.get("filesystem"))
    if artifact_format is not None and artifact_format not in {"raw", "squashfs"}:
        _error("manifest_invalid", "manifest extension format is unsupported")
    if artifact_filesystem is not None and artifact_filesystem not in {"squashfs", "directory", "raw"}:
        _error("manifest_invalid", "manifest extension filesystem is unsupported")
    if "format" in artifact and data.get("format") is not None and artifact["format"] != data["format"]:
        _error("manifest_invalid", "artifact format does not match the manifest")
    if "filesystem" in artifact and data.get("filesystem") is not None and artifact["filesystem"] != data["filesystem"]:
        _error("manifest_invalid", "artifact filesystem does not match the manifest")

    raw_components = data.get("components", data.get("files"))
    entries = _component_entries(raw_components, field="components")
    known_map = component_map if component_map is not None else APPROVED_COMPONENTS
    if not isinstance(known_map, Mapping):
        _error("component_map_invalid", "developer component policy is unavailable")
    component_policy_sha256 = data.get("component_policy_sha256")
    if component_policy_sha256 is not None:
        component_policy_sha256 = _digest(component_policy_sha256, field="component_policy_sha256")

    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    total_size = 0
    normalized_files: list[dict[str, Any]] = []
    component_names: set[str] = set()
    activation_actions: list[str] = []
    for entry in entries:
        source_path = entry["source"]
        target_path = entry["target"]
        if source_path in seen_sources or target_path in seen_targets:
            _error("component_duplicate", "developer component paths must be unique")
        seen_sources.add(source_path)
        seen_targets.add(target_path)
        policy = known_map.get(source_path)
        if policy is None:
            _error("component_not_allowed", "developer component is not approved")
        policy = _mapping(policy, field="component policy")
        expected_target = policy.get("target")
        if expected_target != target_path:
            _error("component_target_invalid", "developer component target is not approved")
        expected_name = policy.get("component", policy.get("name"))
        if expected_name is not None and entry.get("name") not in {None, expected_name}:
            _error("component_invalid", "developer component name is not approved")
        expected_mode = policy.get("mode")
        if expected_mode is not None and entry["mode"] != expected_mode:
            _error("component_mode_invalid", "developer component mode is not approved")
        maximum = policy.get("max_size", MAX_FILE_SIZE)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            _error("component_map_invalid", "developer component size policy is invalid")
        if entry["size"] > min(maximum, MAX_FILE_SIZE):
            _error("component_size_invalid", "developer component is too large")
        expected_activation = policy.get("activation", [])
        if isinstance(expected_activation, str):
            expected_activation = [expected_activation]
        if not isinstance(expected_activation, list):
            _error("component_map_invalid", "developer activation policy is invalid")
        requested_activation = entry.get("activation")
        if requested_activation is None:
            requested_activation = list(expected_activation)
        if any(item not in expected_activation for item in requested_activation):
            _error("activation_invalid", "developer activation action is not approved")
        name = entry.get("name") or expected_name or source_path.rsplit("/", 1)[-1]
        name = _safe_name(name, field="component.name")
        component_names.add(name)
        for action in requested_activation:
            if action not in activation_actions:
                activation_actions.append(action)
        total_size += entry["size"]
        if total_size > MAX_TOTAL_FILE_BYTES:
            _error("manifest_too_large", "developer extension files are too large")
        normalized_files.append(
            {
                "name": name,
                "source": source_path,
                "target": target_path,
                "sha256": entry["sha256"],
                "size": entry["size"],
                "mode": entry["mode"],
                "type": "file",
                "activation": list(requested_activation),
            }
        )

    focused_tests = data.get("focused_tests", data.get("tests", []))
    if isinstance(focused_tests, str):
        focused_tests = [focused_tests]
    if not isinstance(focused_tests, list) or len(focused_tests) > MAX_TESTS:
        _error("manifest_invalid", "focused tests are invalid")
    normalized_tests: list[Any] = []
    focused_test_names: set[str] = set()
    focused_test_re = re.compile(r"\Atests/[A-Za-z0-9._/+\-]{1,255}\Z")
    for test in focused_tests:
        if isinstance(test, str):
            test_name = _bounded_text(test, field="focused_test", limit=256)
            if focused_test_re.fullmatch(test_name) is None:
                _error("manifest_invalid", "focused test name is invalid")
            focused_test_names.add(test_name)
            normalized_tests.append(test_name)
        elif isinstance(test, Mapping):
            test = _mapping(test, field="focused_test")
            name = _bounded_text(test.get("name"), field="focused_test.name", limit=256)
            if focused_test_re.fullmatch(name) is None:
                _error("manifest_invalid", "focused test name is invalid")
            result = test.get("result", test.get("status", "passed"))
            if result not in {"passed", "ok", "success"}:
                _error("tests_failed", "developer focused tests did not pass")
            focused_test_names.add(name)
            normalized_tests.append({"name": name, "result": "passed"})
        else:
            _error("manifest_invalid", "focused test entry is invalid")

    # The bundle builder includes a bounded receipt and its canonical digest.
    # Validate both before exposing test metadata to status consumers.  The
    # receipt is evidence only; it cannot grant component or activation access.
    receipt_value = data.get("test_receipt")
    receipt_digest = data.get("test_receipt_sha256")
    normalized_receipt: dict[str, Any] | None = None
    if focused_test_names and receipt_value is None:
        _error("test_receipt_missing", "developer focused tests require a signed test receipt")
    if receipt_value is not None:
        receipt = _mapping(receipt_value, field="test_receipt")
        if any(key not in {"schema_version", "source_commit", "recorded_at", "results"} for key in receipt):
            _error("manifest_unknown_field", "developer test receipt contains an unknown field")
        if receipt.get("schema_version") != SCHEMA_VERSION:
            _error("manifest_invalid", "developer test receipt schema is unsupported")
        receipt_commit = receipt.get("source_commit")
        if receipt_commit != source["commit"]:
            _error("manifest_invalid", "developer test receipt source commit does not match the manifest")
        recorded_at = _timestamp(receipt.get("recorded_at"), field="test_receipt.recorded_at")
        results = receipt.get("results")
        if not isinstance(results, list) or len(results) > MAX_TESTS:
            _error("manifest_invalid", "developer test receipt results are invalid")
        normalized_results: list[dict[str, str]] = []
        receipt_names: set[str] = set()
        for result_value in results:
            result = _mapping(result_value, field="test_receipt.result")
            if any(key not in {"name", "result", "recorded_at"} for key in result):
                _error("manifest_unknown_field", "developer test receipt result contains an unknown field")
            name = result.get("name")
            if not isinstance(name, str) or focused_test_re.fullmatch(name) is None:
                _error("manifest_invalid", "developer test receipt test name is invalid")
            if name in receipt_names:
                _error("component_duplicate", "developer test receipt contains duplicate tests")
            receipt_names.add(name)
            outcome = result.get("result")
            if outcome not in {"passed", "ok", "success"}:
                _error("tests_failed", "developer test receipt contains a failed test")
            result_time = _timestamp(result.get("recorded_at", recorded_at), field="test_receipt.result.recorded_at")
            normalized_results.append(
                {
                    "name": name,
                    "result": "passed",
                    "recorded_at": result_time,
                }
            )
        receipt_result_names = {item["name"] for item in normalized_results}
        if not focused_test_names.issubset(receipt_result_names):
            _error("tests_failed", "developer test receipt does not cover every focused test")
        normalized_receipt = {
            "schema_version": SCHEMA_VERSION,
            "source_commit": source["commit"],
            "recorded_at": recorded_at,
            "results": normalized_results,
        }
        if receipt_digest is None:
            _error("test_receipt_invalid", "developer test receipt digest is required")
        receipt_digest = _digest(receipt_digest, field="test_receipt_sha256")
        canonical_receipt = (
            json.dumps(
                normalized_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if not hmac.compare_digest(hashlib.sha256(canonical_receipt).hexdigest(), receipt_digest):
            _error("manifest_hash_mismatch", "developer test receipt digest does not match its contents")
    elif receipt_digest is not None:
        _error("manifest_invalid", "developer test receipt digest has no receipt")

    created_at = data.get("created_at", data.get("created", "1970-01-01T00:00:00Z"))
    created_at = _timestamp(created_at)
    signer = data.get("signer", data.get("signer_identity"))
    signature = data.get("signature")
    if signer is None and isinstance(signature, Mapping):
        signer = signature.get("identity")
    # The builder records a null identity before signing and the SSH signature
    # itself carries the authoritative identity.  A non-null value is still
    # checked when present so release/update signers cannot be substituted.
    if signer is not None and signer != SIGNER_IDENTITY:
        _error("signature_identity", "developer manifest signer identity is invalid")
    if signature is not None:
        signature = _mapping(signature, field="signature")
        if any(key not in {"algorithm", "namespace", "required", "signed", "identity", "fingerprint"} for key in signature):
            _error("manifest_unknown_field", "developer signature metadata contains an unknown field")
        if signature.get("namespace", SIGNATURE_NAMESPACE) != SIGNATURE_NAMESPACE:
            _error("signature_identity", "developer manifest signature namespace is invalid")
        if signature.get("algorithm", "ssh") != "ssh":
            _error("signature_identity", "developer manifest signature algorithm is invalid")
        if "required" in signature and not isinstance(signature["required"], bool):
            _error("manifest_invalid", "developer signature requirement is invalid")
        if "signed" in signature and not isinstance(signature["signed"], bool):
            _error("manifest_invalid", "developer signature state is invalid")
        if signature.get("fingerprint") is not None:
            _bounded_text(signature["fingerprint"], field="signature.fingerprint", limit=256)

    provenance = data.get("provenance")
    normalized_provenance: dict[str, Any] | None = None
    if provenance is not None:
        provenance = _mapping(provenance, field="provenance")
        if any(key not in {"inner_path", "inner_sha256", "extension_release_path", "files"} for key in provenance):
            _error("manifest_unknown_field", "developer provenance contains an unknown field")
        inner_path = provenance.get("inner_path", "/usr/share/zeus/developer-provenance.json")
        release_path = provenance.get("extension_release_path", "/usr/lib/extension-release.d/extension-release.zeus-developer")
        if inner_path != "/usr/share/zeus/developer-provenance.json" or release_path != "/usr/lib/extension-release.d/extension-release.zeus-developer":
            _error("path_unsafe", "developer provenance path is not approved")
        internal_files = provenance.get("files", [])
        if not isinstance(internal_files, list) or len(internal_files) > 2:
            _error("manifest_invalid", "developer provenance files are invalid")
        normalized_internal: list[dict[str, Any]] = []
        internal_targets = {
            "/usr/share/zeus/developer-provenance.json",
            "/usr/lib/extension-release.d/extension-release.zeus-developer",
        }
        for internal in internal_files:
            internal = _mapping(internal, field="provenance file")
            if any(key not in {"target", "type", "mode", "size", "sha256"} for key in internal):
                _error("manifest_unknown_field", "developer provenance file contains an unknown field")
            target = _safe_target(internal.get("target"), field="provenance target")
            if target not in internal_targets:
                _error("path_unsafe", "developer provenance target is not approved")
            normalized_internal.append(
                {
                    "target": target,
                    "type": "file",
                    "mode": _policy_mode(internal.get("mode"), field="provenance mode"),
                    "size": _size(internal.get("size"), field="provenance size"),
                    "sha256": _digest(internal.get("sha256"), field="provenance sha256"),
                }
            )
        if len({item["target"] for item in normalized_internal}) != len(normalized_internal):
            _error("component_duplicate", "developer provenance paths are not unique")
        normalized_provenance = {
            "inner_path": inner_path,
            "inner_sha256": _digest(provenance.get("inner_sha256"), field="provenance.inner_sha256") if provenance.get("inner_sha256") is not None else None,
            "extension_release_path": release_path,
            "files": normalized_internal,
        }

    # Top-level activation declarations are advisory metadata but are checked
    # against the finite map so a UI cannot turn one into a shell command.
    declared_actions = data.get("activation_actions", data.get("required_activation", activation_actions))
    if isinstance(declared_actions, str):
        declared_actions = [declared_actions]
    if not isinstance(declared_actions, list) or len(declared_actions) > 32:
        _error("activation_invalid", "developer activation actions are invalid")
    declared_actions = [_safe_name(item, field="activation_actions") for item in declared_actions]
    if any(item not in activation_actions for item in declared_actions):
        _error("activation_invalid", "developer activation action is not approved")
    for item in activation_actions:
        if item not in declared_actions:
            declared_actions.append(item)

    return {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT,
        "artifact_kind": ARTIFACT_KIND,
        "architecture": ARCHITECTURE,
        "base": {
            "id": base_id,
            "version_id": base_version_id,
            "sysext_level": sysext_level,
            "architecture": base_architecture,
            "image_digest": image_digest,
        },
        "source": source,
        "source_commit": source["commit"],
        "base_source_commit": base_source_commit,
        "base_build_id": base_build_id if base_build_id is not None else sysext_level,
        "base_image_digest": image_digest,
        "component_policy_sha256": component_policy_sha256,
        "components": normalized_files,
        "component_names": sorted(component_names),
        "artifact": {"sha256": artifact_sha, "size": artifact_size},
        "artifact_sha256": artifact_sha,
        "artifact_size": artifact_size,
        "activation_actions": declared_actions,
        "focused_tests": normalized_tests,
        "provenance": normalized_provenance,
        "created_at": created_at,
        "test_receipt": normalized_receipt,
        "test_receipt_sha256": receipt_digest,
        "signer": SIGNER_IDENTITY,
    }


def parse_manifest(
    raw: bytes,
    *,
    component_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Parse bounded strict JSON without performing trust checks."""

    return validate_manifest(_strict_json(raw, label="manifest"), component_map=component_map)


def _write_signature_file(directory: str, data: bytes) -> str:
    try:
        fd, name = tempfile.mkstemp(prefix=".zeus-developer-", suffix=".sig", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return name
    except OSError:
        _error("signature_error", "could not prepare signature verification")


def verify_manifest_signature(
    raw: bytes,
    signature: bytes,
    allowed_signers: str | os.PathLike[str] = DEVELOPER_SIGNERS,
    *,
    runner: Callable[..., Any] | None = None,
    component_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify an SSH detached signature and then validate its manifest.

    Signature verification precedes JSON parsing.  The helper therefore does
    not expose a parser oracle to an untrusted spool, and release/update keys
    cannot be substituted for the dedicated developer identity/namespace.
    """

    if not isinstance(raw, bytes) or not isinstance(signature, bytes):
        _error("signature_invalid", "manifest and signature must be bytes")
    if len(raw) == 0 or len(raw) > MAX_MANIFEST_BYTES:
        _error("manifest_too_large", "developer manifest is too large or empty")
    if len(signature) == 0 or len(signature) > MAX_SIGNATURE_BYTES:
        _error("signature_invalid", "developer manifest signature is invalid")
    signer_path = os.fspath(allowed_signers)
    try:
        signer_stat = os.stat(signer_path, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        _error("signature_unavailable", "developer signing policy is unavailable")
    if not stat.S_ISREG(signer_stat.st_mode) or signer_stat.st_mode & 0o022:
        _error("signature_unavailable", "developer signing policy is unavailable")

    run = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix="zeus-developer-verify-") as directory:
        signature_path = _write_signature_file(directory, signature)
        try:
            completed = run(
                [
                    SSH_KEYGEN,
                    "-Y",
                    "verify",
                    "-f",
                    signer_path,
                    "-I",
                    SIGNER_IDENTITY,
                    "-n",
                    SIGNATURE_NAMESPACE,
                    "-s",
                    signature_path,
                ],
                input=raw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=SIGNATURE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _error("signature_timeout", "developer manifest signature verification timed out")
        except FileNotFoundError:
            _error("signature_tool_missing", "developer signature verification is unavailable")
        except (OSError, ValueError, TypeError):
            _error("signature_error", "developer manifest signature verification failed")
        if getattr(completed, "returncode", 1) != 0:
            _error("signature_invalid", "developer manifest signature is not trusted")
    # Parse only after signature verification.  The map is injectable for
    # isolated fixture tests; production callers use the constant map above.
    return validate_manifest(_strict_json(raw, label="manifest"), component_map=component_map)


def hash_file(path: str | os.PathLike[str], *, limit: int = MAX_ARTIFACT_SIZE) -> tuple[int, str]:
    """Hash one bounded regular file without following its final symlink."""

    try:
        info = os.lstat(path)
    except OSError:
        _error("artifact_missing", "developer extension artifact is unavailable")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _error("artifact_type", "developer extension artifact must be a regular file")
    if info.st_size > limit:
        _error("artifact_too_large", "developer extension artifact is too large")
    digest = hashlib.sha256()
    total = 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                total += len(block)
                if total > limit:
                    _error("artifact_too_large", "developer extension artifact is too large")
                digest.update(block)
    except DeveloperError:
        raise
    except (OSError, ValueError):
        _error("artifact_unavailable", "developer extension artifact could not be read")
    return total, digest.hexdigest()


def _tar_member_path(name: str) -> str:
    if not isinstance(name, str) or not name or len(name.encode("utf-8", "ignore")) > MAX_TAR_PATH_BYTES:
        _error("path_unsafe", "developer extension contains an unsafe path")
    if "\\" in name or name.startswith("/") or "\x00" in name:
        _error("path_unsafe", "developer extension contains an unsafe path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _error("path_unsafe", "developer extension contains an unsafe path")
    target = "/" + name
    if target != "/usr" and not target.startswith("/usr/"):
        _error("path_unsafe", "developer extension may only contain /usr paths")
    return target


def _expected_extension_files(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    expected = {entry["target"]: entry for entry in manifest["components"]}
    provenance = manifest.get("provenance")
    if isinstance(provenance, Mapping):
        for entry in provenance.get("files", []):
            expected[entry["target"]] = entry
    return expected


def _inspect_mounted_tree(
    root: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    *,
    require_root_owner: bool = True,
) -> None:
    """Match every mounted extension entry to the signed file manifest."""

    root_path = Path(root)
    expected = _expected_extension_files(manifest)
    allowed_directories: set[str] = set()
    for target in expected:
        parts = PurePosixPath(target).parts
        for index in range(2, len(parts)):
            allowed_directories.add("/" + "/".join(parts[1:index]))

    seen: set[str] = set()
    total = 0
    members = 0
    stack = [root_path]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            _error("artifact_invalid", "developer extension contents could not be inspected")
        for entry in entries:
            members += 1
            if members > MAX_TAR_MEMBERS:
                _error("artifact_too_large", "developer extension contains too many members")
            try:
                relative = Path(entry.path).relative_to(root_path).as_posix()
                target = _tar_member_path(relative)
                info = entry.stat(follow_symlinks=False)
                try:
                    xattrs = os.listxattr(entry.path, follow_symlinks=False)
                except OSError as error:
                    if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                        raise
                    xattrs = []
            except DeveloperError:
                raise
            except (OSError, ValueError, UnicodeError):
                _error("artifact_invalid", "developer extension contents could not be inspected")
            # SELinux may synthesize the path's expected security label even
            # for a SquashFS built with ``-no-xattrs``.  Other attributes,
            # especially file capabilities, are outside the artifact contract.
            if any(name != "security.selinux" for name in xattrs):
                _error("path_unsafe", "developer extensions may not contain extended attributes")
            if require_root_owner and (info.st_uid != 0 or info.st_gid != 0):
                _error("path_unsafe", "developer extension contents must be owned by root")
            if stat.S_ISDIR(info.st_mode):
                if target not in allowed_directories or stat.S_IMODE(info.st_mode) != 0o755:
                    _error("component_not_allowed", "developer extension contains an unapproved directory")
                stack.append(Path(entry.path))
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or target not in expected
            ):
                _error("component_not_allowed", "developer extension contains an unapproved file")
            if target in seen:
                _error("component_duplicate", "developer extension contains duplicate paths")
            seen.add(target)
            declared = expected[target]
            if info.st_size != declared["size"] or stat.S_IMODE(info.st_mode) != declared["mode"]:
                _error("artifact_metadata_mismatch", "developer extension file metadata does not match its manifest")
            size, digest = hash_file(entry.path, limit=MAX_FILE_SIZE)
            total += size
            if total > MAX_TOTAL_FILE_BYTES:
                _error("artifact_too_large", "developer extension files are too large")
            if size != declared["size"] or not hmac.compare_digest(digest, declared["sha256"]):
                _error("artifact_hash_mismatch", "developer extension file hash does not match its manifest")
    if seen != set(expected):
        _error("artifact_missing_file", "developer extension is missing an approved file")


def _inspect_squashfs_extension(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> None:
    """Mount one SquashFS read-only and inspect its complete namespace."""

    mount_root = Path("/run")
    try:
        root_info = os.lstat(mount_root)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != 0
            or root_info.st_mode & 0o022
        ):
            _error("artifact_inspection_unavailable", "Developer extension inspection storage is unsafe")
        mountpoint = Path(tempfile.mkdtemp(prefix=".zeus-developer-inspect-", dir=mount_root))
        os.chmod(mountpoint, 0o700)
    except DeveloperError:
        raise
    except OSError:
        _error("artifact_inspection_unavailable", "Developer extension inspection storage is unavailable")

    mounted = False
    inspection_error: BaseException | None = None
    try:
        completed = subprocess.run(
            [MOUNT, "-t", "squashfs", "-o", "loop,ro,nodev,nosuid,noexec", "--source", os.fspath(path), "--target", os.fspath(mountpoint)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=MOUNT_TIMEOUT,
            env=MOUNT_ENV,
        )
        if completed.returncode != 0:
            _error("artifact_invalid", "developer SquashFS extension could not be mounted safely")
        mounted = True
        _inspect_mounted_tree(mountpoint, manifest)
    except BaseException as error:
        inspection_error = error
    finally:
        if mounted:
            try:
                completed = subprocess.run(
                    [UMOUNT, "--", os.fspath(mountpoint)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=MOUNT_TIMEOUT,
                    env=MOUNT_ENV,
                )
                if completed.returncode != 0:
                    subprocess.run(
                        [UMOUNT, "-l", "--", os.fspath(mountpoint)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=MOUNT_TIMEOUT,
                        env=MOUNT_ENV,
                    )
                    inspection_error = DeveloperError(
                        "artifact_inspection_unavailable",
                        "developer extension inspection could not be cleaned up safely",
                    )
            except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired):
                inspection_error = DeveloperError(
                    "artifact_inspection_unavailable",
                    "developer extension inspection could not be cleaned up safely",
                )
        try:
            shutil.rmtree(mountpoint)
        except OSError:
            inspection_error = DeveloperError(
                "artifact_inspection_unavailable",
                "developer extension inspection could not be cleaned up safely",
            )
    if inspection_error is not None:
        if isinstance(inspection_error, DeveloperError):
            raise inspection_error
        if isinstance(inspection_error, (KeyboardInterrupt, SystemExit)):
            raise inspection_error
        _error("artifact_inspection_unavailable", "developer extension contents could not be inspected")


def inspect_tar_extension(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> None:
    """Validate every extension file against the signed manifest."""

    expected = _expected_extension_files(manifest)
    try:
        with open(path, "rb") as stream:
            magic = stream.read(4)
    except OSError:
        _error("artifact_unavailable", "developer extension artifact could not be read")
    if magic == b"hsqs":
        _inspect_squashfs_extension(path, manifest)
        return
    try:
        archive = tarfile.open(path, mode="r:*")
    except (OSError, EOFError, tarfile.TarError, ValueError):
        _error("artifact_invalid", "developer extension format is unsupported")
    seen: set[str] = set()
    total = 0
    with archive:
        try:
            members = archive.getmembers()
        except (OSError, EOFError, tarfile.TarError, ValueError):
            _error("artifact_invalid", "developer extension archive could not be read")
        if len(members) > MAX_TAR_MEMBERS:
            _error("artifact_too_large", "developer extension contains too many members")
        for member in members:
            target = _tar_member_path(member.name.rstrip("/") if member.isdir() else member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo() or member.ischr() or member.isblk():
                _error("path_unsafe", "developer extensions may not contain links or devices")
            if member.isdir():
                continue
            if not member.isfile() or target not in expected:
                _error("component_not_allowed", "developer extension contains an unapproved file")
            if target in seen:
                _error("component_duplicate", "developer extension contains duplicate paths")
            seen.add(target)
            entry = expected[target]
            if member.size != entry["size"] or stat.S_IMODE(member.mode) != entry["mode"]:
                _error("artifact_metadata_mismatch", "developer extension file metadata does not match its manifest")
            if member.size > MAX_FILE_SIZE:
                _error("artifact_too_large", "developer extension file is too large")
            extracted = archive.extractfile(member)
            if extracted is None:
                _error("artifact_invalid", "developer extension file could not be read")
            digest = hashlib.sha256()
            bytes_read = 0
            for block in iter(lambda: extracted.read(1024 * 1024), b""):
                bytes_read += len(block)
                total += len(block)
                if bytes_read > MAX_FILE_SIZE or total > MAX_TOTAL_FILE_BYTES:
                    _error("artifact_too_large", "developer extension files are too large")
                digest.update(block)
            if bytes_read != entry["size"] or not hmac.compare_digest(digest.hexdigest(), entry["sha256"]):
                _error("artifact_hash_mismatch", "developer extension file hash does not match its manifest")
    if seen != set(expected):
        _error("artifact_missing_file", "developer extension is missing an approved file")


def verify_artifact(
    path: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    *,
    digest: str | None = None,
) -> tuple[int, str]:
    """Verify bytes, declared artifact hash/size, and any inspectable members."""

    size, actual = hash_file(path)
    expected_size = manifest["artifact"]["size"]
    expected_hash = manifest["artifact"]["sha256"]
    if size != expected_size or not hmac.compare_digest(actual, expected_hash):
        _error("artifact_hash_mismatch", "developer extension hash or size does not match its manifest")
    if digest is not None:
        if HEX64_RE.fullmatch(digest) is None or not hmac.compare_digest(actual, digest):
            _error("digest_mismatch", "developer artifact digest does not match its content")
    inspect_tar_extension(path, manifest)
    return size, actual


def read_base_sysext_level(root: str | os.PathLike[str] = "/") -> str | None:
    """Read a bounded ``SYSEXT_LEVEL`` assignment from the base identity."""

    return read_base_identity(root).get("sysext_level")


def read_base_identity(root: str | os.PathLike[str] = "/") -> dict[str, str | None]:
    """Read the immutable base identity used by Developer Mode.

    ``SYSEXT_LEVEL`` is the primary systemd-sysext compatibility key, but the
    extension-release contract also binds the Fedora ID, VERSION_ID and
    architecture.  Keep those values available to the runtime so a forged
    manifest with a coincidentally matching build ID cannot cross a base
    boundary.  Missing optional values remain ``None`` for older fixture
    roots; callers compare only fields the installed image publishes.
    """

    root_path = Path(root)
    values: dict[str, str] = {}
    for relative in ("usr/lib/os-release", "etc/os-release"):
        path = root_path / relative
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
                continue
            raw = path.read_bytes()
        except (OSError, ValueError):
            continue
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return {
                "id": None,
                "version_id": None,
                "architecture": None,
                "sysext_level": None,
                "image_digest": None,
            }
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, encoded = line.split("=", 1)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]+", key) or key in values:
                continue
            try:
                parsed = shlex.split(encoded, comments=False, posix=True)
            except ValueError:
                continue
            if parsed and "\x00" not in parsed[0] and not any(ord(char) < 0x20 for char in parsed[0]):
                values[key] = parsed[0]
        break

    architecture = values.get("ARCHITECTURE") or values.get("BUILD_ARCHITECTURE")
    if architecture in {"amd64", "x86-64"}:
        architecture = "x86_64"
    if architecture is None and root_path == Path("/"):
        try:
            machine = os.uname().machine
        except OSError:
            machine = ""
        architecture = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "arm64",
            "armv7l": "arm",
        }.get(machine)
    level = values.get("SYSEXT_LEVEL") or values.get("BUILD_ID")

    image_digest: str | None = None
    inputs_path = root_path / "usr/share/zeus/inputs.json"
    try:
        info = os.lstat(inputs_path)
        if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024:
            parsed = json.loads(inputs_path.read_text(encoding="utf-8"))
            candidate = parsed.get("base") if isinstance(parsed, Mapping) else None
            if isinstance(candidate, str):
                if re.fullmatch(r"[0-9a-f]{64}", candidate):
                    image_digest = "sha256:" + candidate
                elif re.fullmatch(r"sha256:[0-9a-f]{64}", candidate):
                    image_digest = candidate
                elif re.search(r"@sha256:[0-9a-f]{64}\Z", candidate):
                    image_digest = "sha256:" + candidate.rsplit("@sha256:", 1)[1]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        image_digest = None

    return {
        "id": values.get("ID"),
        "version_id": values.get("VERSION_ID"),
        "architecture": architecture,
        "sysext_level": level if level is not None and SAFE_LEVEL_RE.fullmatch(level) else None,
        "image_digest": image_digest,
    }


__all__ = [
    "APPROVED_COMPONENTS",
    "ARCHITECTURE",
    "ARTIFACT_KIND",
    "DEVELOPER_SIGNERS",
    "DeveloperError",
    "HEX64_RE",
    "MAX_ARTIFACT_SIZE",
    "MAX_FILE_SIZE",
    "COMPONENT_POLICY_PATH",
    "PRODUCT",
    "REPOSITORY",
    "SCHEMA_VERSION",
    "SIGNATURE_NAMESPACE",
    "SIGNER_IDENTITY",
    "hash_file",
    "inspect_tar_extension",
    "load_component_map",
    "parse_manifest",
    "read_base_identity",
    "read_base_sysext_level",
    "validate_manifest",
    "verify_artifact",
    "verify_manifest_signature",
]
