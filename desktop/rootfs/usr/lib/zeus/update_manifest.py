"""Verification helpers for the signed Zeus OS preview update feed.

The module deliberately has no third-party dependencies.  It is used by both
the unprivileged Updates application and the root update helper, so all input
from the network and from an OCI archive is treated as hostile.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from contextlib import closing
from typing import Any, Callable, Mapping
from urllib import error as _urlerror
from urllib import parse as _urlparse
from urllib import request as _urlrequest


DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/KanterLabs/zeusos/main/updates/preview.json"
)
DEFAULT_SIGNERS = "/usr/share/zeus/update-allowed-signers"

SSH_KEYGEN = "/usr/bin/ssh-keygen"

MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_NOTES_BYTES = 4 * 1024
MAX_ARCHIVE_SIZE = 8 * 1024**3
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_OCI_LAYOUT_BYTES = 4 * 1024
MAX_TAR_MEMBERS = 10_000
MAX_NETWORK_CHUNK = 1024 * 1024
NETWORK_TIMEOUT = 30
MAX_REDIRECTS = 5

_VERSION_RE = re.compile(
    r"\A"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"\Z"
)
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_HEX40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_BUILD_ID_RE = re.compile(r"\Agit-[0-9a-f]{12}\Z")
_SEQUENCE_RE = re.compile(r"\A[1-9][0-9]*\Z")
_RFC3339_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|\+00:00)\Z"
)

_MANIFEST_KEYS = {
    "schema_version",
    "product",
    "channel",
    "architecture",
    "version",
    "build_id",
    "source_commit",
    "sequence",
    "published_at",
    "notes",
    "archive",
}
_ARCHIVE_KEYS = {"name", "url", "size", "sha256", "manifest_digest"}

# GitHub's release endpoint redirects to one of these fixed hosts.  Keep this
# list deliberately finite: accepting arbitrary *.githubusercontent.com or
# *.amazonaws.com names would make a signed archive URL less useful as a
# policy boundary.
_RELEASE_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_FEED_HOSTS = frozenset({"raw.githubusercontent.com"})

_OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_OCI_LAYER_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
    }
)


class UpdateError(Exception):
    """An expected, user-safe update failure.

    ``code`` is stable for callers that need to turn failures into UI or job
    state.  Messages intentionally contain no URLs, command output, or other
    untrusted network data.
    """

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(str(message))


class _DuplicateKey(ValueError):
    pass


class _InvalidJSON(ValueError):
    pass


class _UnsafeRedirect(Exception):
    pass


def _error(code: str, message: str) -> None:
    raise UpdateError(code, message)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _bounded_text(value: Any, *, field: str, limit: int, plain: bool = False) -> str:
    if not isinstance(value, str):
        _error("manifest_invalid", f"{field} must be text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _error("manifest_invalid", f"{field} is not valid UTF-8 text")
    if len(encoded) > limit:
        _error("manifest_too_large", f"{field} is too large")
    if plain:
        for character in value:
            number = ord(character)
            if (number < 0x20 and character not in "\t\n\r") or number == 0x7F:
                _error("manifest_invalid", f"{field} contains control characters")
    return value


def _validate_version(value: Any) -> str:
    value = _bounded_text(value, field="version", limit=128)
    if not _VERSION_RE.fullmatch(value):
        _error("manifest_invalid", "version is not a supported release version")
    return value


def _validate_source_commit(value: Any) -> str:
    value = _bounded_text(value, field="source_commit", limit=40)
    if _HEX40_RE.fullmatch(value) is None:
        _error("manifest_invalid", "source_commit is invalid")
    return value


def _validate_build_id(value: Any, source_commit: str) -> str:
    value = _bounded_text(value, field="build_id", limit=64)
    if _BUILD_ID_RE.fullmatch(value) is None:
        _error("manifest_invalid", "build_id is invalid")
    if value != f"git-{source_commit[:12]}":
        _error("manifest_invalid", "build_id does not match source_commit")
    return value


def _validate_published_at(value: Any) -> str:
    value = _bounded_text(value, field="published_at", limit=64)
    if _RFC3339_RE.fullmatch(value) is None:
        _error("manifest_invalid", "published_at must be a UTC RFC3339 timestamp")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        _error("manifest_invalid", "published_at is not a valid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != _datetime.timedelta(0):
        _error("manifest_invalid", "published_at must use UTC")
    return value


def _validate_https_url(url: Any, *, version: str, name: str) -> str:
    url = _bounded_text(url, field="archive.url", limit=512)
    expected = (
        f"https://github.com/KanterLabs/zeusos/releases/download/v{version}/{name}"
    )
    # The signed URL is intentionally canonical.  This also rejects encoded
    # separators, query strings, fragments, alternate ports, and userinfo.
    if url != expected:
        _error("manifest_invalid", "archive URL is not the canonical GitHub release URL")
    try:
        parsed = _urlparse.urlsplit(url)
        port = parsed.port
    except ValueError:
        _error("manifest_invalid", "archive URL is invalid")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/KanterLabs/zeusos/releases/download/v{version}/{name}"
    ):
        _error("manifest_invalid", "archive URL is not safe")
    return url


def _validate_manifest_shape(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        _error("manifest_invalid", "manifest must be a JSON object")
    if any(not isinstance(key, str) for key in data):
        _error("manifest_invalid", "manifest keys must be text")
    unknown = set(data) - _MANIFEST_KEYS
    missing = _MANIFEST_KEYS - set(data)
    if unknown:
        _error("manifest_unknown_field", "manifest contains an unknown field")
    if missing:
        _error("manifest_missing_field", "manifest is missing a required field")

    if not _is_int(data["schema_version"]) or data["schema_version"] != 1:
        _error("manifest_invalid", "unsupported manifest schema")
    if data["product"] != "zeusos" or not isinstance(data["product"], str):
        _error("manifest_invalid", "manifest product is not Zeus OS")
    if data["channel"] != "preview" or not isinstance(data["channel"], str):
        _error("manifest_invalid", "manifest channel is not preview")
    if data["architecture"] != "amd64" or not isinstance(data["architecture"], str):
        _error("manifest_invalid", "manifest architecture is not amd64")

    version = _validate_version(data["version"])
    source_commit = _validate_source_commit(data["source_commit"])
    build_id = _validate_build_id(data["build_id"], source_commit)

    sequence = data["sequence"]
    if not _is_int(sequence) or sequence <= 0 or sequence > 2**63 - 1:
        _error("manifest_invalid", "sequence must be a positive integer")
    published_at = _validate_published_at(data["published_at"])
    notes = _bounded_text(data["notes"], field="notes", limit=MAX_NOTES_BYTES, plain=True)

    archive = data["archive"]
    if not isinstance(archive, dict):
        _error("manifest_invalid", "archive must be an object")
    if any(not isinstance(key, str) for key in archive):
        _error("manifest_invalid", "archive keys must be text")
    unknown_archive = set(archive) - _ARCHIVE_KEYS
    missing_archive = _ARCHIVE_KEYS - set(archive)
    if unknown_archive:
        _error("manifest_unknown_field", "archive contains an unknown field")
    if missing_archive:
        _error("manifest_missing_field", "archive is missing a required field")

    name = _bounded_text(archive["name"], field="archive.name", limit=256)
    expected_name = f"zeusos-{version}-{build_id}.oci"
    if name != expected_name or "/" in name or "\\" in name or name in {".", ".."}:
        _error("manifest_invalid", "archive name is not safe")

    url = _validate_https_url(archive["url"], version=version, name=name)
    size = archive["size"]
    if not _is_int(size) or size <= 0 or size > MAX_ARCHIVE_SIZE:
        _error("manifest_invalid", "archive size is outside the supported range")
    archive_sha256 = _bounded_text(archive["sha256"], field="archive.sha256", limit=64)
    if _HEX64_RE.fullmatch(archive_sha256) is None:
        _error("manifest_invalid", "archive SHA-256 is invalid")
    manifest_digest = _bounded_text(
        archive["manifest_digest"], field="archive.manifest_digest", limit=71
    )
    if _SHA256_RE.fullmatch(manifest_digest) is None:
        _error("manifest_invalid", "OCI manifest digest is invalid")

    # Return a detached, exact-schema copy.  Validation has no side effects and
    # callers cannot mutate the input object through the result.
    return {
        "schema_version": 1,
        "product": "zeusos",
        "channel": "preview",
        "architecture": "amd64",
        "version": version,
        "build_id": build_id,
        "source_commit": source_commit,
        "sequence": sequence,
        "published_at": published_at,
        "notes": notes,
        "archive": {
            "name": name,
            "url": url,
            "size": size,
            "sha256": archive_sha256,
            "manifest_digest": manifest_digest,
        },
    }


def validate_manifest(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a decoded schema-1 preview manifest."""

    return _validate_manifest_shape(data)


def _strict_json(raw: bytes, *, label: str, limit: int) -> Any:
    if not isinstance(raw, bytes):
        _error("manifest_invalid", f"{label} must be bytes")
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


def _write_private_file(directory: str, data: bytes, *, suffix: str) -> str:
    try:
        fd, name = tempfile.mkstemp(prefix=".zeus-update-", suffix=suffix, dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return name
    except OSError:
        _error("signature_error", "could not prepare signature verification")


def verify_manifest(
    raw: bytes,
    signature: bytes,
    allowed_signers: str | os.PathLike[str] = DEFAULT_SIGNERS,
) -> dict[str, Any]:
    """Verify an SSH detached signature, then strictly validate its JSON.

    Signature verification intentionally happens before JSON parsing.  A
    malformed payload with no valid signer therefore cannot be used as a JSON
    parser oracle by the network-facing client.
    """

    if not isinstance(raw, bytes) or not isinstance(signature, bytes):
        _error("signature_invalid", "manifest and signature must be bytes")
    if len(raw) > MAX_MANIFEST_BYTES:
        _error("manifest_too_large", "manifest is too large")
    if len(signature) == 0 or len(signature) > MAX_SIGNATURE_BYTES:
        _error("signature_invalid", "signature is invalid")

    signer_path = os.fspath(allowed_signers)
    try:
        signer_stat = os.stat(signer_path, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        _error("signature_unavailable", "trusted update signers are unavailable")
    if not stat.S_ISREG(signer_stat.st_mode):
        _error("signature_unavailable", "trusted update signers are unavailable")

    with tempfile.TemporaryDirectory(prefix="zeus-update-verify-") as directory:
        signature_path = _write_private_file(directory, signature, suffix=".sig")
        try:
            completed = subprocess.run(
                [
                    SSH_KEYGEN,
                    "-Y",
                    "verify",
                    "-f",
                    signer_path,
                    "-I",
                    "zeusos-preview",
                    "-n",
                    "zeusos-update",
                    "-s",
                    signature_path,
                ],
                input=raw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=NETWORK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _error("signature_timeout", "signature verification timed out")
        except FileNotFoundError:
            _error("signature_tool_missing", "signature verification is unavailable")
        except (OSError, ValueError):
            _error("signature_error", "signature verification failed")
        if completed.returncode != 0:
            _error("signature_invalid", "manifest signature is not trusted")

    # This is deliberately after the cryptographic verification above.
    if len(raw) == 0:
        _error("manifest_invalid", "manifest is empty")
    decoded = _strict_json(raw, label="manifest", limit=MAX_MANIFEST_BYTES)
    return validate_manifest(decoded)


def _network_url_is_safe(url: str, hosts: frozenset[str], *, canonical: str | None = None) -> bool:
    if not isinstance(url, str) or any(ord(ch) < 0x20 for ch in url):
        return False
    try:
        parsed = _urlparse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or parsed.fragment
    ):
        return False
    if canonical is not None and url != canonical:
        return False
    return True


class _SafeRedirectHandler(_urlrequest.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]):
        super().__init__()
        self._hosts = hosts
        self._redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if self._redirect_count >= MAX_REDIRECTS or not _network_url_is_safe(
            newurl, self._hosts
        ):
            raise _UnsafeRedirect
        self._redirect_count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_network(url: str, *, hosts: frozenset[str], accept: str):
    if not _network_url_is_safe(url, hosts):
        _error("network_url_rejected", "update URL is not allowed")
    opener = _urlrequest.build_opener(_SafeRedirectHandler(hosts))
    request = _urlrequest.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "ZeusOS-updater/1",
        },
        method="GET",
    )
    try:
        response = opener.open(request, timeout=NETWORK_TIMEOUT)
    except _UnsafeRedirect:
        _error("network_redirect_rejected", "update server redirect is not allowed")
    except (_urlerror.URLError, TimeoutError, OSError, ValueError):
        _error("network_error", "could not fetch update data")
    try:
        final_url = response.geturl()
    except (AttributeError, OSError):
        final_url = url
    if not _network_url_is_safe(final_url, hosts):
        try:
            response.close()
        except (AttributeError, OSError):
            pass
        _error("network_redirect_rejected", "update server redirect is not allowed")
    try:
        status = response.getcode()
    except (AttributeError, OSError):
        status = 200
    if status not in (None, 200):
        try:
            response.close()
        except (AttributeError, OSError):
            pass
        _error("network_error", "update server returned an unexpected response")
    return response


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    return str(value)


def _content_length(response: Any) -> int | None:
    value = _header(response, "Content-Length")
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value.strip()):
        _error("network_error", "update server returned an invalid length")
    try:
        return int(value, 10)
    except ValueError:
        _error("network_error", "update server returned an invalid length")


def _read_bounded_response(response: Any, limit: int) -> bytes:
    content_length = _content_length(response)
    if content_length is not None and content_length > limit:
        _error("network_too_large", "update metadata is too large")
    result = bytearray()
    try:
        while len(result) <= limit:
            request_size = min(MAX_NETWORK_CHUNK, limit - len(result) + 1)
            chunk = response.read(request_size)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                _error("network_error", "update server returned invalid data")
            if len(result) + len(chunk) > limit:
                _error("network_too_large", "update metadata is too large")
            result.extend(chunk)
    except UpdateError:
        raise
    except (OSError, ValueError, TypeError):
        _error("network_error", "could not read update metadata")
    if content_length is not None and content_length != len(result):
        _error("network_error", "update metadata was truncated")
    return bytes(result)


def fetch_manifest() -> dict[str, Any]:
    """Fetch and verify the immutable preview feed and its detached signature."""

    # This function intentionally has no URL or signer arguments.  The desktop
    # process cannot redirect it to an arbitrary endpoint or trust a caller
    # supplied key; tests can mock _open_network at this narrow boundary.
    if not _network_url_is_safe(DEFAULT_FEED_URL, _FEED_HOSTS, canonical=DEFAULT_FEED_URL):
        _error("network_url_rejected", "default update feed URL is not allowed")
    signature_url = f"{DEFAULT_FEED_URL}.sig"
    with closing(
        _open_network(
            DEFAULT_FEED_URL,
            hosts=_FEED_HOSTS,
            accept="application/json",
        )
    ) as response:
        raw = _read_bounded_response(response, MAX_MANIFEST_BYTES)
    with closing(
        _open_network(
            signature_url,
            hosts=_FEED_HOSTS,
            accept="application/octet-stream",
        )
    ) as response:
        signature = _read_bounded_response(response, MAX_SIGNATURE_BYTES)
    # DEFAULT_SIGNERS is evaluated here, not as an import-time mutable path,
    # so image configuration can install it before this call while callers
    # still cannot substitute a key through fetch_manifest().
    return verify_manifest(raw, signature, DEFAULT_SIGNERS)


def _safe_archive_path(path: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(path)
        stat_result = os.lstat(candidate)
    except (OSError, TypeError, ValueError):
        _error("archive_invalid", "OCI archive is unavailable")
    if not stat.S_ISREG(stat_result.st_mode) or stat.S_ISLNK(stat_result.st_mode):
        _error("archive_invalid", "OCI archive is not a regular file")
    if stat_result.st_size > MAX_ARCHIVE_SIZE:
        _error("archive_too_large", "OCI archive is too large")
    return candidate


def _read_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    if member.size < 0 or member.size > limit:
        _error("archive_invalid", "OCI metadata member is too large")
    try:
        stream = archive.extractfile(member)
        if stream is None:
            _error("archive_invalid", "OCI metadata member is unreadable")
        with closing(stream):
            chunks: list[bytes] = []
            remaining = member.size
            while remaining:
                chunk = stream.read(min(MAX_NETWORK_CHUNK, remaining))
                if not chunk or not isinstance(chunk, bytes):
                    _error("archive_invalid", "OCI metadata member is truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            # A regular tar member's declared size is authoritative for the
            # stream.  A non-empty extra read catches malformed test fixtures
            # and sparse members without touching any output path.
            if stream.read(1):
                _error("archive_invalid", "OCI metadata member is oversized")
            return b"".join(chunks)
    except UpdateError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError, AttributeError, KeyError):
        _error("archive_invalid", "OCI metadata member is unreadable")


def _descriptor(value: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error("archive_invalid", f"OCI {kind} descriptor is invalid")
    digest = value.get("digest")
    size = value.get("size")
    media_type = value.get("mediaType")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        _error("archive_invalid", f"OCI {kind} digest is invalid")
    if not _is_int(size) or size <= 0 or size > MAX_ARCHIVE_SIZE:
        _error("archive_invalid", f"OCI {kind} size is invalid")
    if not isinstance(media_type, str):
        _error("archive_invalid", f"OCI {kind} media type is invalid")
    return {"digest": digest, "size": size, "mediaType": media_type, **value}


def _blob_member(
    members: Mapping[str, tarfile.TarInfo], descriptor: Mapping[str, Any], *, kind: str
) -> tarfile.TarInfo:
    digest = descriptor["digest"]
    member = members.get(f"blobs/sha256/{digest.split(':', 1)[1]}")
    if member is None or not member.isreg() or member.size != descriptor["size"]:
        _error("archive_invalid", f"OCI {kind} blob is missing or has the wrong size")
    return member


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    # Tar writers differ on whether directory entries carry a trailing slash.
    # Accept both spellings while keeping the directory topology exact.
    allowed_directories = {"blobs", "blobs/", "blobs/sha256", "blobs/sha256/"}
    try:
        for number, member in enumerate(archive):
            if number >= MAX_TAR_MEMBERS:
                _error("archive_too_many_members", "OCI archive has too many members")
            name = member.name
            if not isinstance(name, str) or "\x00" in name:
                _error("archive_invalid", "OCI archive contains an unsafe member name")
            if name in members:
                _error("archive_duplicate_member", "OCI archive contains duplicate members")
            if name in allowed_directories:
                if not member.isdir():
                    _error("archive_invalid", "OCI archive directory is invalid")
            elif name in {"index.json", "oci-layout"} or re.fullmatch(
                r"blobs/sha256/[0-9a-f]{64}", name
            ):
                if not member.isreg() or getattr(member, "sparse", None):
                    _error("archive_invalid", "OCI archive member is not a regular file")
                if member.size < 0 or member.size > MAX_ARCHIVE_SIZE:
                    _error("archive_invalid", "OCI archive member is too large")
            else:
                # Requiring the exact OCI layout names rejects absolute paths,
                # parent traversal, links, and arbitrary payload files before
                # any archive content is read.
                _error("archive_unsafe_member", "OCI archive contains an unsafe member")
            members[name] = member
    except UpdateError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError, AttributeError, KeyError):
        _error("archive_invalid", "OCI archive could not be read")
    return members


def inspect_archive(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect OCI metadata and verify its index, manifest, and config links.

    Layer bytes are not extracted or hashed here.  Their referenced blob names
    and declared sizes are checked, while the small index/manifest/config JSON
    objects are read and cryptographically linked.  The caller can perform a
    full archive hash separately against the signed feed's SHA-256.
    """

    archive_path = _safe_archive_path(path)
    try:
        archive = tarfile.open(archive_path, mode="r:*")
    except (OSError, EOFError, tarfile.TarError, ValueError, AttributeError, KeyError):
        _error("archive_invalid", "OCI archive is not a valid tar archive")

    with archive:
        members = _archive_members(archive)
        if "index.json" not in members or "oci-layout" not in members:
            _error("archive_invalid", "OCI archive is missing required metadata")
        layout_raw = _read_tar_member(archive, members["oci-layout"], MAX_OCI_LAYOUT_BYTES)
        layout = _strict_json(layout_raw, label="oci-layout", limit=MAX_OCI_LAYOUT_BYTES)
        if (
            not isinstance(layout, dict)
            or layout.get("imageLayoutVersion") != "1.0.0"
            or any(not isinstance(k, str) for k in layout)
        ):
            _error("archive_invalid", "OCI layout version is unsupported")

        index_raw = _read_tar_member(archive, members["index.json"], MAX_JSON_BYTES)
        index = _strict_json(index_raw, label="OCI index", limit=MAX_JSON_BYTES)
        if not isinstance(index, dict) or index.get("schemaVersion") != 2:
            _error("archive_invalid", "OCI index schema is unsupported")
        if index.get("mediaType") not in (None, _OCI_INDEX_MEDIA_TYPE):
            _error("archive_invalid", "OCI index media type is invalid")
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or len(descriptors) != 1:
            _error("archive_invalid", "OCI index must contain one image manifest")
        manifest_descriptor = _descriptor(descriptors[0], kind="manifest")
        if manifest_descriptor["mediaType"] != _OCI_MANIFEST_MEDIA_TYPE:
            _error("archive_invalid", "OCI index does not point to an image manifest")
        platform = manifest_descriptor.get("platform")
        if platform is not None:
            if not isinstance(platform, dict):
                _error("archive_invalid", "OCI platform metadata is invalid")
            if platform.get("architecture") not in (None, "amd64") or platform.get(
                "os"
            ) not in (None, "linux"):
                _error("archive_identity", "OCI image platform is not amd64 Linux")
        manifest_member = _blob_member(members, manifest_descriptor, kind="manifest")
        manifest_raw = _read_tar_member(archive, manifest_member, MAX_JSON_BYTES)
        if not hmac.compare_digest(
            hashlib.sha256(manifest_raw).hexdigest(), manifest_descriptor["digest"].split(":", 1)[1]
        ):
            _error("archive_digest_mismatch", "OCI manifest digest does not match its blob")
        manifest = _strict_json(manifest_raw, label="OCI manifest", limit=MAX_JSON_BYTES)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            _error("archive_invalid", "OCI manifest schema is unsupported")
        if manifest.get("mediaType") not in (None, _OCI_MANIFEST_MEDIA_TYPE):
            _error("archive_invalid", "OCI manifest media type is invalid")

        config_descriptor = _descriptor(manifest.get("config"), kind="config")
        if config_descriptor["mediaType"] != _OCI_CONFIG_MEDIA_TYPE:
            _error("archive_invalid", "OCI config media type is invalid")
        config_member = _blob_member(members, config_descriptor, kind="config")
        config_raw = _read_tar_member(archive, config_member, MAX_JSON_BYTES)
        if not hmac.compare_digest(
            hashlib.sha256(config_raw).hexdigest(), config_descriptor["digest"].split(":", 1)[1]
        ):
            _error("archive_digest_mismatch", "OCI config digest does not match its blob")

        layers = manifest.get("layers")
        if not isinstance(layers, list) or not layers or len(layers) > MAX_TAR_MEMBERS:
            _error("archive_invalid", "OCI manifest layers are invalid")
        for layer in layers:
            layer_descriptor = _descriptor(layer, kind="layer")
            if layer_descriptor["mediaType"] not in _OCI_LAYER_MEDIA_TYPES:
                _error("archive_invalid", "OCI layer media type is invalid")
            _blob_member(members, layer_descriptor, kind="layer")

        config = _strict_json(config_raw, label="OCI config", limit=MAX_JSON_BYTES)
        if not isinstance(config, dict):
            _error("archive_invalid", "OCI config is not an object")
        if config.get("architecture") != "amd64" or config.get("os") != "linux":
            _error("archive_identity", "OCI image architecture or OS is not supported")
        config_section = config.get("config")
        if not isinstance(config_section, dict):
            _error("archive_identity", "OCI image config labels are missing")
        labels = config_section.get("Labels")
        if not isinstance(labels, dict):
            _error("archive_identity", "OCI image labels are missing")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
            _error("archive_identity", "OCI image labels are invalid")

        if labels.get("org.opencontainers.image.title") != "Zeus OS":
            _error("archive_identity", "OCI image title is not Zeus OS")
        if labels.get("org.opencontainers.image.source") != "https://github.com/KanterLabs/zeusos":
            _error("archive_identity", "OCI image source is not the Zeus repository")
        version = _validate_version(labels.get("org.opencontainers.image.version"))
        source_commit = _validate_source_commit(labels.get("org.opencontainers.image.revision"))
        build_id = _validate_build_id(labels.get("dev.kanterlabs.zeus.build-id"), source_commit)

        sequence: int | None = None
        sequence_label = labels.get("dev.kanterlabs.zeus.update-sequence")
        if sequence_label is not None:
            if not isinstance(sequence_label, str) or _SEQUENCE_RE.fullmatch(sequence_label) is None:
                _error("archive_identity", "OCI update sequence label is invalid")
            try:
                sequence = int(sequence_label, 10)
            except ValueError:
                _error("archive_identity", "OCI update sequence label is invalid")
            if sequence > 2**63 - 1:
                _error("archive_identity", "OCI update sequence label is too large")

        annotations = manifest_descriptor.get("annotations")
        if annotations is not None and not isinstance(annotations, dict):
            _error("archive_invalid", "OCI manifest annotations are invalid")
        ref_name = (annotations or {}).get("org.opencontainers.image.ref.name")
        if ref_name is not None:
            if not isinstance(ref_name, str) or ref_name != f"localhost/zeusos:{version}-{build_id}":
                _error("archive_identity", "OCI image reference is not the Zeus image")

        return {
            "manifest_digest": manifest_descriptor["digest"],
            "architecture": "amd64",
            "version": version,
            "source_commit": source_commit,
            "build_id": build_id,
            "sequence": sequence,
        }


class _Progress:
    def __init__(self, callback: Callable[[int, int], Any] | None, total: int):
        self.callback = callback
        self.total = total
        self.last: float | None = None

    def report(self, done: int, *, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if force or self.last is None or now - self.last >= 1.0:
            self.callback(done, self.total)
            self.last = now


def _destination_path(path: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError):
        _error("unsafe_destination", "download destination is invalid")
    if not candidate.is_absolute() or candidate.name in {"", ".", ".."}:
        _error("unsafe_destination", "download destination must be an absolute path")
    # Check every existing parent component without resolving it.  This keeps
    # an ancestor symlink from redirecting the final open outside the private
    # update directory.
    current = Path(candidate.anchor)
    for component in candidate.parent.parts:
        if component == candidate.anchor:
            continue
        current /= component
        try:
            component_stat = os.lstat(current)
        except OSError:
            _error("unsafe_destination", "download destination directory is unavailable")
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            _error("unsafe_destination", "download destination directory is unsafe")
    try:
        parent_stat = os.lstat(candidate.parent)
    except OSError:
        _error("unsafe_destination", "download destination directory is unavailable")
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        _error("unsafe_destination", "download destination directory is unsafe")
    # A destination is expected to live in a private root-owned update area.
    # Permit the current user for test/development callers, but never accept a
    # directory writable or readable by group/other users.
    if parent_stat.st_mode & 0o077:
        _error("unsafe_destination", "download destination directory is not private")
    if parent_stat.st_uid not in {0, os.geteuid()}:
        _error("unsafe_destination", "download destination directory owner is invalid")
    try:
        existing = os.lstat(candidate)
    except FileNotFoundError:
        existing = None
    except OSError:
        _error("unsafe_destination", "download destination is unavailable")
    if existing is not None:
        _error("destination_exists", "download destination already exists")
    return candidate


def _unlink_owned(path: Path, device: int, inode: int) -> None:
    try:
        current = os.lstat(path)
        if current.st_dev == device and current.st_ino == inode and stat.S_ISREG(current.st_mode):
            os.unlink(path)
    except (FileNotFoundError, OSError):
        pass


def download_archive(
    manifest: Mapping[str, Any],
    path: str | os.PathLike[str],
    progress: Callable[[int, int], Any] | None = None,
) -> None:
    """Download the exact signed archive into a new private destination."""

    validated = validate_manifest(manifest)
    archive = validated["archive"]
    destination = _destination_path(path)
    expected_size = archive["size"]
    expected_hash = archive["sha256"]
    reporter = _Progress(progress, expected_size)
    fd: int | None = None
    created = False
    identity: tuple[int, int] | None = None
    success = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(destination, flags, 0o600)
        except (OSError, ValueError):
            _error("unsafe_destination", "could not create download destination")
        created = True
        opened_stat = os.fstat(fd)
        identity = (opened_stat.st_dev, opened_stat.st_ino)
        reporter.report(0)

        with closing(
            _open_network(
                archive["url"],
                hosts=_RELEASE_HOSTS,
                accept="application/octet-stream",
            )
        ) as response:
            content_length = _content_length(response)
            if content_length is not None and content_length != expected_size:
                _error("download_size_mismatch", "download length does not match the signed size")
            digest = hashlib.sha256()
            done = 0
            while done < expected_size:
                try:
                    chunk = response.read(min(MAX_NETWORK_CHUNK, expected_size - done + 1))
                except (OSError, ValueError, TypeError):
                    _error("download_error", "could not read update archive")
                if not chunk:
                    _error("download_truncated", "update archive download was truncated")
                if not isinstance(chunk, bytes):
                    _error("download_error", "update server returned invalid archive data")
                if len(chunk) > expected_size - done:
                    _error("download_size_mismatch", "download exceeds the signed size")
                try:
                    view = memoryview(chunk)
                    written = os.write(fd, view)
                except (OSError, ValueError, TypeError):
                    _error("download_error", "could not write update archive")
                if written != len(chunk):
                    _error("download_error", "could not write update archive")
                digest.update(chunk)
                done += len(chunk)
                reporter.report(done)
            # A response with no Content-Length can still contain one extra
            # byte.  Read one bounded probe to reject it without writing it.
            try:
                extra = response.read(1)
            except (OSError, ValueError, TypeError):
                _error("download_error", "could not finish update archive download")
            if extra:
                _error("download_size_mismatch", "download exceeds the signed size")
            if not hmac.compare_digest(digest.hexdigest(), expected_hash):
                _error("download_hash_mismatch", "download hash does not match the signed hash")

        os.fsync(fd)
        reporter.report(expected_size, force=True)
        success = True
    except UpdateError:
        raise
    except (OSError, ValueError, TypeError):
        _error("download_error", "update archive download failed")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not success and created and identity is not None:
            _unlink_owned(destination, identity[0], identity[1])


__all__ = [
    "DEFAULT_FEED_URL",
    "DEFAULT_SIGNERS",
    "MAX_ARCHIVE_SIZE",
    "UpdateError",
    "download_archive",
    "fetch_manifest",
    "inspect_archive",
    "validate_manifest",
    "verify_manifest",
]
