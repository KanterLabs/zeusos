"""Static and dry-run contracts for the disposable Developer Mode sysext fixture.

The fixture itself intentionally requires an already-installed Fedora guest and
root privileges.  These tests keep the source contract reviewable from a
checkout and exercise only its no-mutation dry-run path.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify-developer-sysext.sh"
DOC = ROOT / "docs/features/developer-mode-qualification.md"
EVIDENCE = ROOT / "docs/iterations/developer-mode-20260911"


class DeveloperSysextContract(unittest.TestCase):
    def test_fixture_is_executable_and_shell_syntax_is_valid(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepare_dry_run_is_structured_and_does_not_need_root(self) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--mode", "prepare", "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["schema"], "zeus-developer-sysext-qualification-v1")
        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["mode"], "prepare")
        self.assertEqual(evidence["result"], "dry_run")
        self.assertFalse(evidence["qualification_passed"])
        self.assertTrue(evidence["reboot"]["required"])
        self.assertFalse(evidence["reboot"]["observed"])
        self.assertEqual(evidence["reboot"]["claim"], "not_observed")
        self.assertIsNone(evidence["reboot_command"])

        self.assertIn(
            "/var/lib/extensions/zeus-qualification-fixture",
            evidence["would_mutate"],
        )
        self.assertIn(
            "/var/lib/zeus/qualification/developer-sysext",
            evidence["would_mutate"],
        )
        self.assertIn(
            "/etc/systemd/system/zeus-developer-sysext-qualification.service",
            evidence["would_mutate"],
        )
        self.assertIn("/home", evidence["protected_write_prefixes"])
        self.assertIn("/var/lib/zeus/updates", evidence["protected_write_prefixes"])
        self.assertEqual(
            evidence["safe_desktop"]["command"],
            ["zeus", "desktop", "safe", "--dry-run"],
        )
        self.assertFalse(evidence["safe_desktop"]["mutation_performed"])
        self.assertEqual(
            evidence["disabled_mode"]["boot_timing"]["claim"],
            "observed_before_apply",
        )

    def test_verify_dry_run_preserves_the_reboot_boundary(self) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--dry-run", "--mode=verify"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["mode"], "verify")
        self.assertEqual(evidence["result"], "dry_run")
        self.assertFalse(evidence["reboot"]["required"])
        self.assertFalse(evidence["reboot"]["observed"])
        self.assertEqual(evidence["reboot"]["claim"], "not_observed")
        self.assertIn("post-reboot boot activation observation", evidence["runtime_operations"])

    def test_dry_run_rejects_path_traversal_and_owner_state_paths(self) -> None:
        for path in (
            "/var/lib/zeus/qualification/../updates",
            "/var/lib/zeus/developer-mode",
        ):
            with self.subTest(path=path):
                result = subprocess.run(
                    [str(SCRIPT), "--dry-run", "--state-dir", path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r"(?:invalid_path|protected_path)")

    def test_source_contract_is_fail_closed_and_scoped(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        required_fragments = (
            "set -Eeuo pipefail",
            "acquire_lock()",
            "/usr/bin/flock -n 9",
            "systemd-sysext refresh",
            "systemd-sysext unmerge",
            "VERSION_ID rejection",
            "SYSEXT_LEVEL rejection",
            "getenforce",
            "CLOCK_MONOTONIC",
            "/proc/stat",
            "/proc/meminfo",
            "reboot_requested_by_script",
            "proxmox_operated_by_script",
            "owner_data_touched",
            "updater_touched",
            "bootloader_touched",
            "ConditionPathExists=",
            "systemd-sysext.service",
            "safe-desktop recovery dry run",
            "credentials and personal data remain untouched",
            "systemd-analyze time",
            "post_boot_disabled",
            "disabled_mode",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

        # The helper owns one fixed harmless target and cannot accept an
        # arbitrary extension name or target path from its caller.
        self.assertIn(
            "SENTINEL='/usr/share/zeus/developer-mode-qualification-sentinel'",
            text,
        )
        self.assertIn("EXTENSION_ROOT=\"${EXTENSION_PARENT}/${EXTENSION_NAME}\"", text)
        self.assertIn("protected_write_prefixes", text)
        self.assertIn("reboot_command\": None", text)
        self.assertIsNone(
            re.search(
                r"(?m)^\s*(?:(?:/usr/bin/)?systemctl\s+)?(?:reboot|poweroff|halt)\b",
                text,
            )
        )
        self.assertIsNone(re.search(r"(?m)^\s*(?:/usr/bin/)?bootc\s+", text))
        self.assertIsNone(re.search(r"(?m)^\s*(?:/usr/bin/)?qm\s+", text))

    def test_qualification_doc_defines_schema_and_records_vm119_measurements(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for fragment in (
            "zeus-developer-sysext-qualification-v1",
            "scripts/qualify-developer-sysext.sh",
            "iterations/developer-mode-20260911/sysext-runtime.json",
            "iterations/developer-mode-20260911/sysext-post-reboot.json",
            "iterations/developer-mode-20260911/sysext-squashfs.json",
            "VM119",
            "CLOCK_MONOTONIC",
            "SELinux",
            "VERSION_ID",
            "SYSEXT_LEVEL",
            "pre_reboot_ready",
            "observed_after_boot_id_change",
            "base_sha256",
            "avc_denials_during_operations",
            "idle_memory_definition",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

        for name in (
            "sysext-runtime.json",
            "sysext-post-reboot.json",
            "sysext-squashfs.json",
        ):
            with self.subTest(evidence=name):
                record = json.loads((EVIDENCE / name).read_text(encoding="utf-8"))
                self.assertEqual(record["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
