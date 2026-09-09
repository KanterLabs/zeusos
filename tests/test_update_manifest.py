"""Focused tests for the signed update feed and OCI artifact checks."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "desktop" / "rootfs" / "usr" / "lib" / "zeus"
sys.path.insert(0, str(MODULE_DIR))
import update_manifest as updater  # noqa: E402


SOURCE = "0123456789abcdef0123456789abcdef01234567"
VERSION = "0.1.0-preview.2"
BUILD_ID = "git-0123456789ab"
ARCHIVE_NAME = f"zeusos-{VERSION}-{BUILD_ID}.oci"
ARCHIVE_URL = f"https://github.com/KanterLabs/zeusos/releases/download/v{VERSION}/{ARCHIVE_NAME}"


def make_manifest(**changes):
    manifest = {
        "schema_version": 1,
        "product": "zeusos",
        "channel": "preview",
        "architecture": "amd64",
        "version": VERSION,
        "build_id": BUILD_ID,
        "source_commit": SOURCE,
        "sequence": 42,
        "published_at": "2026-09-09T00:00:00Z",
        "notes": "Release notes",
        "archive": {
            "name": ARCHIVE_NAME,
            "url": ARCHIVE_URL,
            "size": 5,
            "sha256": "a" * 64,
            "manifest_digest": "sha256:" + "b" * 64,
        },
    }
    for key, value in changes.items():
        if key.startswith("archive_"):
            manifest["archive"][key.removeprefix("archive_")] = value
        else:
            manifest[key] = value
    return manifest


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def add_tar_file(archive, name, data):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def write_oci(path: Path, *, sequence: int | None = 42, extra=None, duplicate_index=False):
    config_labels = {
        "org.opencontainers.image.title": "Zeus OS",
        "org.opencontainers.image.source": "https://github.com/KanterLabs/zeusos",
        "org.opencontainers.image.version": VERSION,
        "org.opencontainers.image.revision": SOURCE,
        "dev.kanterlabs.zeus.build-id": BUILD_ID,
    }
    if sequence is not None:
        config_labels["dev.kanterlabs.zeus.update-sequence"] = str(sequence)
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": config_labels},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "d" * 64]},
    }
    config_raw = json_bytes(config)
    config_digest = hashlib.sha256(config_raw).hexdigest()
    layer_raw = b"layer"
    layer_digest = hashlib.sha256(layer_raw).hexdigest()
    image_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:" + config_digest,
            "size": len(config_raw),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": "sha256:" + layer_digest,
                "size": len(layer_raw),
            }
        ],
    }
    manifest_raw = json_bytes(image_manifest)
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + manifest_digest,
                "size": len(manifest_raw),
                "annotations": {
                    "org.opencontainers.image.ref.name": f"localhost/zeusos:{VERSION}-{BUILD_ID}"
                },
            }
        ],
    }
    if extra:
        extra_name, extra_data = extra
    else:
        extra_name = extra_data = None
    with tarfile.open(path, "w") as archive:
        add_tar_file(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_tar_file(archive, "index.json", json_bytes(index))
        add_tar_file(archive, f"blobs/sha256/{manifest_digest}", manifest_raw)
        add_tar_file(archive, f"blobs/sha256/{config_digest}", config_raw)
        add_tar_file(archive, f"blobs/sha256/{layer_digest}", layer_raw)
        if extra_name is not None:
            add_tar_file(archive, extra_name, extra_data)
        if duplicate_index:
            add_tar_file(archive, "index.json", json_bytes(index))
    return manifest_digest


class _Response:
    def __init__(self, body: bytes, *, content_length=True, final_url=None):
        self.body = body
        self.position = 0
        self.headers = {}
        if content_length:
            self.headers["Content-Length"] = str(len(body))
        self.final_url = final_url
        self.closed = False

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self.body) - self.position
        start = self.position
        self.position = min(len(self.body), self.position + amount)
        return self.body[start : self.position]

    def geturl(self):
        return self.final_url or updater.DEFAULT_FEED_URL

    def getcode(self):
        return 200

    def close(self):
        self.closed = True


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.private_key = self.directory / "signing-key"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.private_key],
            check=True,
        )
        public = self.private_key.with_name("signing-key.pub").read_text().split()
        self.allowed = self.directory / "allowed-signers"
        self.allowed.write_text(f"zeusos-preview {public[0]} {public[1]}\n")

    def tearDown(self):
        self.temp.cleanup()

    def sign(self, raw: bytes, *, namespace="zeusos-update"):
        payload = self.directory / "manifest.json"
        payload.write_bytes(raw)
        payload.with_name("manifest.json.sig").unlink(missing_ok=True)
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-Y", "sign", "-f", self.private_key, "-n", namespace, payload],
            check=True,
        )
        return payload.with_name("manifest.json.sig").read_bytes()

    def test_valid_signature_then_manifest_validation(self):
        raw = json_bytes(make_manifest())
        self.assertEqual(updater.verify_manifest(raw, self.sign(raw), self.allowed)["sequence"], 42)

    def test_unsigned_tampered_and_wrong_key_fail(self):
        raw = json_bytes(make_manifest())
        signature = self.sign(raw)
        with self.assertRaises(updater.UpdateError) as context:
            updater.verify_manifest(raw + b"x", signature, self.allowed)
        self.assertEqual(context.exception.code, "signature_invalid")

        other_key = self.directory / "other-key"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other_key],
            check=True,
        )
        other_pub = other_key.with_name("other-key.pub").read_text().split()
        wrong_allowed = self.directory / "wrong-allowed"
        wrong_allowed.write_text(f"zeusos-preview {other_pub[0]} {other_pub[1]}\n")
        with self.assertRaises(updater.UpdateError) as context:
            updater.verify_manifest(raw, signature, wrong_allowed)
        self.assertEqual(context.exception.code, "signature_invalid")

    def test_valid_signature_with_wrong_namespace_or_identity_fails(self):
        raw = json_bytes(make_manifest())
        signature = self.sign(raw, namespace="other-namespace")
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(raw, signature, self.allowed)

        bad_identity = self.directory / "bad-identity"
        public = self.private_key.with_name("signing-key.pub").read_text().split()
        bad_identity.write_text(f"other-identity {public[0]} {public[1]}\n")
        signature = self.sign(raw)
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(raw, signature, bad_identity)


class ValidationTests(unittest.TestCase):
    def test_duplicate_and_unknown_fields_are_rejected(self):
        raw = json.dumps(make_manifest()).replace(
            '"product": "zeusos"', '"product": "zeusos", "product": "zeusos"'
        ).encode()
        with tempfile.TemporaryDirectory() as temporary:
            allowed = Path(temporary) / "allowed-signers"
            allowed.write_text("placeholder\n")
            with mock.patch.object(updater, "subprocess") as process:
                process.run.return_value.returncode = 0
                with self.assertRaises(updater.UpdateError) as context:
                    updater.verify_manifest(raw, b"sig", allowed)
        self.assertEqual(context.exception.code, "manifest_duplicate_field")

        unknown = make_manifest()
        unknown["unexpected"] = True
        with self.assertRaises(updater.UpdateError) as context:
            updater.validate_manifest(unknown)
        self.assertEqual(context.exception.code, "manifest_unknown_field")

    def test_unsafe_urls_and_integer_booleans_are_rejected(self):
        for field, value in (
            ("sequence", True),
            ("archive_size", True),
            ("archive_url", "https://localhost/evil"),
            ("archive_url", ARCHIVE_URL + "?token=secret"),
            ("version", "../0.1.0"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(updater.UpdateError):
                    updater.validate_manifest({**make_manifest(), field: value})

    def test_bounds_are_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(make_manifest(notes="x" * (updater.MAX_NOTES_BYTES + 1)))
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(make_manifest(archive_size=updater.MAX_ARCHIVE_SIZE + 1))


class NetworkPolicyTests(unittest.TestCase):
    def test_release_redirect_accepts_only_fixed_https_github_hosts(self):
        request = urlrequest.Request(ARCHIVE_URL)
        for host in updater._RELEASE_HOSTS:
            with self.subTest(host=host):
                handler = updater._SafeRedirectHandler(updater._RELEASE_HOSTS)
                redirected = handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    f"https://{host}/release-asset?token=opaque",
                )
                self.assertIsInstance(redirected, urlrequest.Request)

        for target in (
            "http://github.com/release-asset",
            "https://evil.example/release-asset",
            "https://user:password@github.com/release-asset",
            "https://github.com:444/release-asset",
            "https://localhost/release-asset",
        ):
            with self.subTest(target=target):
                handler = updater._SafeRedirectHandler(updater._RELEASE_HOSTS)
                with self.assertRaises(updater._UnsafeRedirect):
                    handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_redirect_count_is_bounded(self):
        request = urlrequest.Request(ARCHIVE_URL)
        handler = updater._SafeRedirectHandler(updater._RELEASE_HOSTS)
        target = "https://objects.githubusercontent.com/release-asset"
        for _ in range(updater.MAX_REDIRECTS):
            self.assertIsInstance(
                handler.redirect_request(request, None, 302, "Found", {}, target),
                urlrequest.Request,
            )
        with self.assertRaises(updater._UnsafeRedirect):
            handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_bounded_metadata_rejects_oversize_and_truncated_content(self):
        oversized = _Response(b"x" * 8, content_length=True)
        with self.assertRaises(updater.UpdateError) as context:
            updater._read_bounded_response(oversized, 4)
        self.assertEqual(context.exception.code, "network_too_large")

        truncated = _Response(b"short", content_length=False)
        truncated.headers["Content-Length"] = "10"
        with self.assertRaises(updater.UpdateError) as context:
            updater._read_bounded_response(truncated, 32)
        self.assertEqual(context.exception.code, "network_error")

        unbounded = _Response(b"12345", content_length=False)
        with self.assertRaises(updater.UpdateError) as context:
            updater._read_bounded_response(unbounded, 4)
        self.assertEqual(context.exception.code, "network_too_large")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_archive_links_metadata_and_identity(self):
        path = self.directory / ARCHIVE_NAME
        digest = write_oci(path)
        self.assertEqual(
            updater.inspect_archive(path),
            {
                "manifest_digest": "sha256:" + digest,
                "architecture": "amd64",
                "version": VERSION,
                "source_commit": SOURCE,
                "build_id": BUILD_ID,
                "sequence": 42,
            },
        )

    def test_malicious_paths_and_duplicate_members_are_rejected(self):
        traversal = self.directory / "traversal.oci"
        write_oci(traversal, extra=("../outside", b"do not extract"))
        with self.assertRaises(updater.UpdateError) as context:
            updater.inspect_archive(traversal)
        self.assertEqual(context.exception.code, "archive_unsafe_member")

        duplicate = self.directory / "duplicate.oci"
        write_oci(duplicate, duplicate_index=True)
        with self.assertRaises(updater.UpdateError) as context:
            updater.inspect_archive(duplicate)
        self.assertEqual(context.exception.code, "archive_duplicate_member")

    def test_historical_archive_without_sequence_reports_none(self):
        path = self.directory / "historical.oci"
        write_oci(path, sequence=None)
        self.assertIsNone(updater.inspect_archive(path)["sequence"])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _download(self, body, manifest=None):
        manifest = manifest or make_manifest(
            archive_size=len(body), archive_sha256=hashlib.sha256(body).hexdigest()
        )
        responses = [_Response(body, final_url=manifest["archive"]["url"])]
        with mock.patch.object(updater, "_open_network", side_effect=responses):
            updater.download_archive(manifest, self.directory / "download.oci")

    def test_download_streams_hashes_and_fsyncs_to_private_destination(self):
        body = b"small archive payload"
        destination = self.directory / "download.oci"
        manifest = make_manifest(
            archive_size=len(body), archive_sha256=hashlib.sha256(body).hexdigest()
        )
        with mock.patch.object(updater, "_open_network", return_value=_Response(body, final_url=ARCHIVE_URL)):
            updater.download_archive(manifest, destination)
        self.assertEqual(destination.read_bytes(), body)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_truncated_and_wrong_hash_downloads_remove_only_new_destination(self):
        body = b"payload"
        for stream_body, expected_hash in ((body[:-1], hashlib.sha256(body).hexdigest()), (body, "f" * 64)):
            with self.subTest(expected_hash=expected_hash):
                destination = self.directory / ("truncated.oci" if len(stream_body) != len(body) else "wrong-hash.oci")
                manifest = make_manifest(archive_size=len(body), archive_sha256=expected_hash)
                with mock.patch.object(
                    updater, "_open_network", return_value=_Response(stream_body, final_url=ARCHIVE_URL)
                ):
                    with self.assertRaises(updater.UpdateError):
                        updater.download_archive(manifest, destination)
                self.assertFalse(destination.exists())

    def test_symlink_destination_is_never_followed(self):
        target = self.directory / "target"
        target.write_bytes(b"keep")
        destination = self.directory / "download.oci"
        destination.symlink_to(target)
        with self.assertRaises(updater.UpdateError) as context:
            updater.download_archive(make_manifest(), destination)
        self.assertEqual(context.exception.code, "destination_exists")
        self.assertEqual(target.read_bytes(), b"keep")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.archive = self.directory / ARCHIVE_NAME
        write_oci(self.archive)
        self.build_info = self.directory / "build-info.json"
        self.build_info.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "source_commit": SOURCE,
                    "build_id": BUILD_ID,
                    "update_sequence": 42,
                }
            )
        )
        self.notes = self.directory / "notes.txt"
        self.notes.write_text("A signed test release.\n")
        self.signing_key = self.directory / "publisher-key"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.signing_key],
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def command(self, output, sequence=42):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "make-update-manifest.py"),
                "--archive",
                str(self.archive),
                "--build-info",
                str(self.build_info),
                "--sequence",
                str(sequence),
                "--notes-file",
                str(self.notes),
                "--output",
                str(output),
                "--signing-key",
                str(self.signing_key),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_publisher_writes_signed_manifest_and_refuses_overwrite(self):
        output = self.directory / "preview.json"
        result = self.command(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        signature = Path(f"{output}.sig")
        self.assertTrue(output.is_file())
        self.assertTrue(signature.is_file())
        public = self.signing_key.with_name("publisher-key.pub").read_text().split()
        allowed = self.directory / "allowed-signers"
        allowed.write_text(f"zeusos-preview {public[0]} {public[1]}\n")
        published = updater.verify_manifest(output.read_bytes(), signature.read_bytes(), allowed)
        self.assertEqual(published["sequence"], 42)
        original = output.read_bytes()
        result = self.command(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), original)

    def test_publisher_requires_sequence_label_and_build_info_match(self):
        self.build_info.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "source_commit": SOURCE,
                    "build_id": BUILD_ID,
                    "update_sequence": 7,
                }
            )
        )
        result = self.command(self.directory / "preview.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sequence", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
