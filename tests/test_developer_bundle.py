"""Contract tests for the repository-backed Developer Mode bundle builder."""

from __future__ import annotations

import hashlib
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-developer-extension.py"
spec = importlib.util.spec_from_file_location("developer_bundle", SCRIPT)
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


class DeveloperBundleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Developer Bundle Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("init", "--bare", self.remote, cwd=self.root)
        self._git(
            "config",
            "url.file://" + os.fspath(self.remote) + ".insteadOf",
            "https://github.com/KanterLabs/zeusos.git",
        )
        self._git("remote", "add", "origin", builder.CANONICAL_REPOSITORY)

        source = self.repo / "desktop/rootfs/usr/share/applications/org.zeus.Settings.desktop"
        source.parent.mkdir(parents=True)
        source.write_text("[Desktop Entry]\nName=Settings base\n", encoding="utf-8")
        self.policy_path = self.repo / "developer-mode/components.json"
        self.policy_path.parent.mkdir(parents=True)
        self.policy_path.write_text(json.dumps(self.policy(), indent=2) + "\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "fixture base")
        self.base_commit = self._git_text("rev-parse", "HEAD")

        source.write_text("[Desktop Entry]\nName=Settings changed\n", encoding="utf-8")
        self._git("add", str(source.relative_to(self.repo)))
        self._git("commit", "-m", "fixture desktop change")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        self._git("push", "-u", "origin", "main")

        self.base_release = self.root / "os-release"
        self.base_release.write_text(
            "ID=fedora\nVERSION_ID=44\nSYSEXT_LEVEL=base-build-1\n",
            encoding="utf-8",
        )
        self.fake_mksquashfs = self.root / "fake-mksquashfs.py"
        self.fake_mksquashfs.write_text(
            """#!/usr/bin/env python3
import hashlib
import os
from pathlib import Path
import stat
import sys

source = Path(sys.argv[1])
output = Path(sys.argv[2])
records = []
for path in sorted((p for p in source.rglob('*') if p.is_file()), key=lambda p: p.relative_to(source).as_posix()):
    relative = path.relative_to(source).as_posix()
    records.append(f'{relative} {stat.S_IMODE(path.stat().st_mode):04o} '.encode() + hashlib.sha256(path.read_bytes()).hexdigest().encode() + b'\\n')
output.write_bytes(b'FAKE-SQUASHFS\\n' + b''.join(records))
""",
            encoding="utf-8",
        )
        self.fake_mksquashfs.chmod(0o755)
        self.fake_ssh_keygen = self.root / "fake-ssh-keygen.py"
        self.sign_log = self.root / "signing-argv.log"
        self.fake_ssh_keygen.write_text(
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys

if '-lf' in sys.argv:
    print('256 SHA256:fixture-fingerprint fixture (ED25519)')
elif '-Y' in sys.argv and 'sign' in sys.argv:
    Path({str(self.sign_log)!r}).open('a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')
    Path(sys.argv[-1] + '.sig').write_bytes(b'fixture-signature')
else:
    raise SystemExit(2)
""",
            encoding="utf-8",
        )
        self.fake_ssh_keygen.chmod(0o755)
        self.signing_key = self.root / "fixture-key"
        self.signing_key.write_text("fixture signing key, not a private key\n", encoding="utf-8")
        self.signing_key.chmod(0o600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *(os.fspath(arg) for arg in args)],
            cwd=cwd or self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def _git_text(self, *args: object) -> str:
        return self._git(*args).stdout.strip()

    @staticmethod
    def policy() -> dict[str, object]:
        return {
            "schema_version": 1,
            "product": "zeusos",
            "artifact_kind": "systemd-sysext",
            "extension_name": "zeus-developer",
            "repository": builder.CANONICAL_REPOSITORY,
            "architecture": "x86_64",
            "components": [
                {
                    "name": "settings-ui",
                    "activation": "restart-settings",
                    "tests": [],
                    "files": [
                        {
                            "source": "desktop/rootfs/usr/share/applications/org.zeus.Settings.desktop",
                            "target": "/usr/share/applications/org.zeus.Settings.desktop",
                            "type": "regular",
                            "mode": "0644",
                            "max_size": 4096,
                        }
                    ],
                }
            ],
        }

    def build(self, output: Path, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "repo": self.repo,
            "commit": self.source_commit,
            "base_source_commit": self.base_commit,
            # Exercise the default repository-relative policy lookup, which
            # must read the named Git object rather than this working-tree path.
            "components": None,
            "base_os_release": self.base_release,
            "base_build_id": "base-build-1",
            "base_image_digest": "quay.io/fedora/fedora@sha256:" + "a" * 64,
            "output_dir": output,
            "signing_key": self.signing_key,
            "mksquashfs": self.fake_mksquashfs,
            "ssh_keygen": self.fake_ssh_keygen,
            "created_at": "2026-09-11T00:00:00Z",
        }
        values.update(changes)
        return builder.build_bundle(**values)

    def test_builds_content_addressed_spool_and_repeats_bytes(self) -> None:
        first = self.build(self.root / "spool-one")
        digest = str(first["artifact_sha256"])
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        artifact = self.root / "spool-one" / digest
        self.assertEqual(Path(str(first["artifact"])), artifact / "extension.raw")
        self.assertEqual(Path(str(first["manifest"])), artifact / "manifest.json")
        self.assertEqual(Path(str(first["signature"])), artifact / "manifest.json.sig")
        manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifact_kind"], "systemd-sysext")
        self.assertEqual(manifest["architecture"], "x86_64")
        self.assertEqual(manifest["repository"], builder.CANONICAL_REPOSITORY)
        self.assertEqual(manifest["source_commit"], self.source_commit)
        self.assertEqual(manifest["base_source_commit"], self.base_commit)
        self.assertEqual(manifest["base"]["id"], "fedora")
        self.assertEqual(manifest["base"]["sysext_level"], "base-build-1")
        self.assertEqual(manifest["artifact"]["sha256"], digest)
        self.assertTrue(manifest["signature"]["signed"])
        self.assertEqual(manifest["signature"]["namespace"], "zeusos-developer")
        self.assertIn("zeusos-developer", self.sign_log.read_text(encoding="utf-8"))
        prepared = json.loads((self.root / "spool-one" / "prepared.json").read_text(encoding="utf-8"))
        self.assertEqual(set(prepared), {"schema_version", "digest"})
        self.assertEqual(prepared["digest"], digest)

        second = self.build(self.root / "spool-two")
        self.assertEqual(Path(str(second["artifact"])).read_bytes(), Path(str(first["artifact"])).read_bytes())
        self.assertEqual(Path(str(second["manifest"])).read_bytes(), Path(str(first["manifest"])).read_bytes())

    def test_dirty_detached_and_wrong_remote_sources_fail_closed(self) -> None:
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "dirty")
        self.assertEqual(context.exception.code, "dirty_source")
        (self.repo / "untracked.txt").unlink()

        self._git("config", "remote.origin.url", "https://github.com/other/project.git")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "wrong-remote")
        self.assertEqual(context.exception.code, "wrong_remote")
        self._git("config", "remote.origin.url", builder.CANONICAL_REPOSITORY)

        self._git("checkout", "--detach", self.source_commit)
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "detached")
        self.assertEqual(context.exception.code, "detached_source")

    def test_changed_paths_must_all_be_allowlisted(self) -> None:
        packages = self.repo / "image/packages.txt"
        packages.parent.mkdir(parents=True, exist_ok=True)
        packages.write_text("system-package\n", encoding="utf-8")
        self._git("add", "image/packages.txt")
        self._git("commit", "-m", "unallowlisted change")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "mixed")
        self.assertEqual(context.exception.code, "changed_path_not_allowlisted")
        self.assertEqual(context.exception.route, builder.FULL_IMAGE_LANE)

    def test_installed_base_marker_classifies_full_history(self) -> None:
        # The latest commit changes an approved source, but the intermediate
        # commit changed an unapproved file.  Comparing only HEAD^ would miss
        # that earlier change and incorrectly produce an extension artifact.
        packages = self.repo / "image/packages.txt"
        packages.parent.mkdir(parents=True, exist_ok=True)
        packages.write_text("system-package\n", encoding="utf-8")
        self._git("add", "image/packages.txt")
        self._git("commit", "-m", "intermediate system change")
        self._git("push", "origin", "main")
        source = self.repo / "desktop/rootfs/usr/share/applications/org.zeus.Settings.desktop"
        source.write_text("[Desktop Entry]\nName=Settings latest\n", encoding="utf-8")
        self._git("add", str(source.relative_to(self.repo)))
        self._git("commit", "-m", "latest desktop change")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")

        base_root = self.root / "installed-base"
        (base_root / "usr/lib").mkdir(parents=True)
        (base_root / "usr/share/zeus").mkdir(parents=True)
        (base_root / "usr/lib/os-release").write_bytes(self.base_release.read_bytes())
        (base_root / "usr/share/zeus/source-commit").write_text(self.base_commit + "\n", encoding="ascii")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "full-history", base_source_commit=None, base_root=base_root)
        self.assertEqual(context.exception.code, "changed_path_not_allowlisted")
        self.assertEqual(context.exception.route, builder.FULL_IMAGE_LANE)

    def test_changed_paths_require_explicit_base_without_installed_marker(self) -> None:
        policy = builder.load_policy(self.repo, self.source_commit, self.policy_path)
        selected = builder.select_files(self.repo, self.source_commit, policy)
        source = builder.prove_source(self.repo, self.source_commit)
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.prove_changed_paths(self.repo, source, policy, selected, None)
        self.assertEqual(context.exception.code, "base_source_required")

    def test_component_tests_cover_non_payload_changes(self) -> None:
        test_path = "tests/test_fixture_focus.py"
        fixture_test = self.repo / test_path
        fixture_test.parent.mkdir(parents=True, exist_ok=True)
        fixture_test.write_text("# focused fixture\n", encoding="utf-8")
        self._git("add", test_path)
        self._git("commit", "-m", "focused test change")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")

        policy = builder.load_policy(self.repo, self.source_commit, self.policy_path)
        component = policy.components[0]
        policy_with_test = dataclasses.replace(
            policy,
            components=(dataclasses.replace(component, tests=(test_path,)),),
        )
        selected = builder.select_files(self.repo, self.source_commit, policy_with_test)
        # The test path is classified for the selected component but never
        # enters ``selected`` and therefore cannot enter the image payload.
        self.assertEqual(
            builder.prove_changed_paths(self.repo, builder.prove_source(self.repo, self.source_commit), policy_with_test, selected, self.base_commit),
            self.base_commit,
        )

        unknown_path = self.repo / "tests/test_unknown.py"
        unknown_path.write_text("# unapproved\n", encoding="utf-8")
        self._git("add", "tests/test_unknown.py")
        self._git("commit", "-m", "unknown test change")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.prove_changed_paths(
                self.repo,
                builder.prove_source(self.repo, self.source_commit),
                policy_with_test,
                builder.select_files(self.repo, self.source_commit, policy_with_test),
                self.base_commit,
            )
        self.assertEqual(context.exception.code, "changed_path_not_allowlisted")

    def test_release_records_do_not_disable_the_desktop_fast_lane(self) -> None:
        for relative in (
            "README.md",
            "docs/metrics.md",
            "updates/preview.json",
            "updates/preview.json.sig",
        ):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"record for {relative}\n", encoding="utf-8")
        self._git("add", "README.md", "docs/metrics.md", "updates/preview.json", "updates/preview.json.sig")
        self._git("commit", "-m", "publish non-payload records")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")

        policy = builder.load_policy(self.repo, self.source_commit, self.policy_path)
        selected = builder.select_files(self.repo, self.source_commit, policy)
        self.assertEqual(
            builder.prove_changed_paths(
                self.repo,
                builder.prove_source(self.repo, self.source_commit),
                policy,
                selected,
                self.base_commit,
            ),
            self.base_commit,
        )

        workflow = self.repo / ".github/workflows/untrusted.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text("name: changed validation\n", encoding="utf-8")
        self._git("add", str(workflow.relative_to(self.repo)))
        self._git("commit", "-m", "change executable validation policy")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.prove_changed_paths(
                self.repo,
                builder.prove_source(self.repo, self.source_commit),
                policy,
                builder.select_files(self.repo, self.source_commit, policy),
                self.base_commit,
            )
        self.assertEqual(context.exception.code, "changed_path_not_allowlisted")

    def test_focused_receipt_is_required_and_must_pass(self) -> None:
        commit = "a" * 40
        focused = ["tests/test_settings_window.py"]
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.load_test_receipt(None, source_commit=commit, focused_tests=focused, created_at="2026-09-11T00:00:00Z")
        self.assertEqual(context.exception.code, "test_receipt_required")

        incomplete = self.root / "incomplete-receipt.json"
        incomplete.write_text(json.dumps({"source_commit": commit, "results": []}), encoding="utf-8")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.load_test_receipt(incomplete, source_commit=commit, focused_tests=focused, created_at="2026-09-11T00:00:00Z")
        self.assertEqual(context.exception.code, "focused_tests_incomplete")

        skipped = self.root / "skipped-receipt.json"
        skipped.write_text(
            json.dumps({"source_commit": commit, "results": [{"name": focused[0], "result": "skipped"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.load_test_receipt(skipped, source_commit=commit, focused_tests=focused, created_at="2026-09-11T00:00:00Z")
        self.assertEqual(context.exception.code, "focused_tests_incomplete")

    def test_mode_and_symlink_sources_are_rejected(self) -> None:
        source = self.repo / "desktop/rootfs/usr/share/applications/org.zeus.Settings.desktop"
        source.chmod(0o755)
        self._git("add", str(source.relative_to(self.repo)))
        self._git("commit", "-m", "unsafe source mode")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "mode")
        self.assertEqual(context.exception.code, "source_mode")

        self._git("checkout", "main")
        source.unlink()
        source.symlink_to("../../../../README.md")
        self._git("add", "-A")
        self._git("commit", "-m", "unsafe source link")
        self._git("push", "origin", "main")
        self.source_commit = self._git_text("rev-parse", "HEAD")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            self.build(self.root / "symlink")
        self.assertEqual(context.exception.code, "source_type")

    def test_policy_rejects_arbitrary_targets_and_excludes_generated_assets(self) -> None:
        checked_in = json.loads((ROOT / "developer-mode/components.json").read_text(encoding="utf-8"))
        source_names = {
            item["source"]
            for component in checked_in["components"]
            for item in component["files"]
        }
        self.assertNotIn("desktop/rootfs/usr/share/gnome-shell/extensions/zeus-shell@kanterlabs/schemas/gschemas.compiled", source_names)
        self.assertNotIn("desktop/rootfs/usr/share/gnome-shell/extensions/zeus-shell@kanterlabs/schemas/org.gnome.shell.extensions.zeus-shell.gschema.xml", source_names)
        self.assertNotIn("desktop/rootfs/usr/share/zeus/greeter.css", source_names)
        self.assertNotIn("desktop/rootfs/usr/share/zeus/zeus-wordmark.svg", source_names)

        unsafe = self.root / "unsafe-components.json"
        policy = self.policy()
        policy["components"][0]["files"][0]["target"] = "/usr/bin/arbitrary"
        unsafe.write_text(json.dumps(policy), encoding="utf-8")
        with self.assertRaises(builder.DeveloperBundleError) as context:
            builder.load_policy(self.repo, self.source_commit, unsafe)
        self.assertEqual(context.exception.code, "components_invalid")


if __name__ == "__main__":
    unittest.main()
