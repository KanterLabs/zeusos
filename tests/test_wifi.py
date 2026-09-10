"""Focused tests for the offline Intel Wi-Fi image readiness validator."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-wifi.py"
SPEC = importlib.util.spec_from_file_location("validate_wifi", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class WifiValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zeus-wifi-")
        self.root = Path(self.temporary.name)
        for spec in validator.REQUIRED_EXECUTABLES:
            path = self.root / spec["path"].lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture executable")
            path.chmod(0o755)
        for spec in validator.REQUIRED_FILES:
            if "patterns" in spec:
                path_text = spec["patterns"][0].replace("*", "6.19.10-300.fc44")
            else:
                path_text = spec["path"]
            path = self.root / path_text.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture file")
        firmware = self.root / "usr/lib/firmware/iwlwifi/iwlwifi-QuZ-a0-hr-b0-77.ucode.xz"
        firmware.parent.mkdir(parents=True, exist_ok=True)
        firmware.write_bytes(b"fixture firmware")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _check(report, group, check_id):
        return next(item for item in report["checks"][group] if item["id"] == check_id)

    def test_complete_fixture_passes_without_a_wireless_interface(self):
        report = validator.validate(self.root)

        self.assertTrue(report["ok"])
        self.assertEqual(report["scope"], "fixture")
        self.assertEqual(report["software"]["status"], "prerequisites_present")
        self.assertEqual(
            self._check(report, "firmware", "iwlwifi_firmware")["status"],
            "present",
        )
        self.assertEqual(report["hardware_qualification"]["status"], "NOT TESTED")
        self.assertFalse((self.root / "sys/class/net").exists())

    def test_missing_firmware_fails_even_when_network_dependencies_exist(self):
        for path in (self.root / "usr/lib/firmware").rglob("iwlwifi-*"):
            path.unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(
            self._check(report, "firmware", "iwlwifi_firmware")["status"],
            "missing",
        )

    def test_missing_networkmanager_wifi_plugin_fails(self):
        plugin = next(
            self.root.glob("usr/lib64/NetworkManager/*/libnm-device-plugin-wifi.so")
        )
        plugin.unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(
            self._check(report, "files", "networkmanager_wifi_plugin")["status"],
            "missing",
        )

    def test_top_level_firmware_layout_remains_supported(self):
        nested = self.root / "usr/lib/firmware/iwlwifi/iwlwifi-QuZ-a0-hr-b0-77.ucode.xz"
        nested.unlink()
        top_level = self.root / "usr/lib/firmware/iwlwifi-QuZ-a0-hr-b0-77.ucode.xz"
        top_level.write_bytes(b"fixture firmware")

        report = validator.validate(self.root)

        self.assertTrue(report["ok"])

    def test_json_cli_reports_failure_without_running_hardware_probe(self):
        (self.root / "usr/bin/nmcli").unlink()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = validator.main(["--root", str(self.root), "--json"])

        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["software"]["status"], "missing_prerequisites")
        self.assertEqual(report["hardware_qualification"]["status"], "NOT TESTED")

    def test_image_declares_firmware_package_and_build_gate(self):
        packages = (ROOT / "image/packages.txt").read_text().splitlines()
        self.assertIn("iwlwifi-mvm-firmware", packages)
        containerfile = (ROOT / "image/Containerfile").read_text()
        self.assertIn("COPY scripts/validate-wifi.py", containerfile)
        self.assertIn("python3 /tmp/zeus-validate-wifi.py --json", containerfile)


if __name__ == "__main__":
    unittest.main()
