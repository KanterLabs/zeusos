"""Focused tests for the installed Bluetooth audio readiness validator."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import _ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-bluetooth-audio.py"
SPEC = importlib.util.spec_from_file_location("validate_bluetooth_audio", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class BluetoothAudioValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zeus-bt-audio-")
        self.root = Path(self.temporary.name)
        for spec in validator.REQUIRED_EXECUTABLES:
            path = self.root / spec["path"].lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            if spec["id"] == "pipewire_pulse":
                # Fedora packages pipewire-pulse as an in-root symlink to the
                # pipewire executable.
                path.symlink_to("pipewire")
            else:
                path.write_bytes(b"fixture executable")
                path.chmod(0o755)
        for spec in validator.REQUIRED_PLUGINS:
            path = self.root / spec["path"].lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture shared object")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _check(report, group, check_id):
        return next(item for item in report["checks"][group] if item["id"] == check_id)

    @staticmethod
    def _capability(report, capability_id):
        return next(item for item in report["capabilities"] if item["id"] == capability_id)

    def test_complete_fixture_has_prerequisites_but_hardware_is_not_tested(self):
        report = validator.validate(self.root)

        self.assertTrue(report["ok"])
        self.assertEqual(report["scope"], "fixture")
        self.assertEqual(report["software"]["status"], "prerequisites_present")
        self.assertTrue((self.root / "usr/bin/pipewire-pulse").is_symlink())
        self.assertEqual(
            (self.root / "usr/bin/pipewire-pulse").resolve(),
            self.root / "usr/bin/pipewire",
        )
        self.assertEqual(
            self._capability(report, "a2dp_music")["status"],
            "prerequisites_present",
        )
        self.assertEqual(
            self._capability(report, "hfp_microphone")["status"],
            "prerequisites_present",
        )
        self.assertEqual(report["hardware_qualification"]["status"], "NOT TESTED")
        self.assertTrue(
            all(
                value == "NOT TESTED"
                for value in report["hardware_qualification"]["tests"].values()
            )
        )

    def test_missing_aac_plugin_fails_and_names_the_missing_prerequisite(self):
        aac = self.root / "usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-aac.so"
        aac.unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(self._check(report, "plugins", "a2dp_aac")["status"], "missing")
        self.assertEqual(
            self._capability(report, "a2dp_music")["status"],
            "missing_prerequisites",
        )

    def test_missing_hfp_msbc_plugin_fails_microphone_capability(self):
        msbc = self.root / "usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-hfp-msbc.so"
        msbc.unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(self._check(report, "plugins", "hfp_msbc")["status"], "missing")
        self.assertEqual(
            self._capability(report, "hfp_microphone")["status"],
            "missing_prerequisites",
        )

    def test_missing_required_tool_fails_software_readiness(self):
        (self.root / "usr/bin/wpctl").unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(
            self._check(report, "executables", "wireplumber_wpctl")["status"],
            "missing",
        )
        self.assertEqual(
            self._capability(report, "a2dp_music")["status"],
            "missing_prerequisites",
        )
        self.assertEqual(
            self._capability(report, "hfp_microphone")["status"],
            "missing_prerequisites",
        )

    def test_missing_pipewire_fails_both_audio_capabilities(self):
        (self.root / "usr/bin/pipewire").unlink()

        report = validator.validate(self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(
            self._capability(report, "a2dp_music")["status"],
            "missing_prerequisites",
        )
        self.assertEqual(
            self._capability(report, "hfp_microphone")["status"],
            "missing_prerequisites",
        )

    def test_json_cli_returns_nonzero_when_software_is_missing(self):
        (self.root / "usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-aac.so").unlink()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = validator.main(["--root", str(self.root), "--json"])

        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["software"]["status"], "missing_prerequisites")

    def test_fixture_root_never_runs_host_probe(self):
        with mock.patch.object(
            validator.subprocess,
            "run",
            side_effect=AssertionError("fixture validation must not spawn a probe"),
        ) as run:
            report = validator.validate(self.root, runner=run)

        self.assertTrue(report["ok"])
        run.assert_not_called()

    def test_load_plugins_is_rejected_for_fixture_without_spawning(self):
        output = io.StringIO()
        with mock.patch.object(
            validator.subprocess,
            "run",
            side_effect=AssertionError("fixture dynamic loading is forbidden"),
        ) as run, contextlib.redirect_stdout(output):
            result = validator.main(["--root", str(self.root), "--load-plugins", "--json"])

        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["plugin_loadability"]["status"], "not_run")
        run.assert_not_called()

    def test_dynamic_load_failure_is_bounded_and_does_not_expose_loader_output(self):
        calls = []

        def failed_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 1, "", "loader output with private details")

        result = validator._load_plugin(
            Path("/usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-aac.so"),
            runner=failed_runner,
        )

        self.assertEqual(result, {"status": "failed", "reason": "dynamic load failed"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["timeout"], validator.PLUGIN_LOAD_TIMEOUT_SECONDS)
        self.assertIn("ctypes.CDLL", calls[0][0][2])
        self.assertIn("os.RTLD_NOW", calls[0][0][2])
        self.assertNotIn("private", json.dumps(result))

    def test_dynamic_load_smoke_uses_a_real_subprocess(self):
        extension = Path(_ctypes.__file__).resolve()

        result = validator._load_plugin(extension)

        self.assertEqual(result, {"status": "loadable"}, result)

    def test_dynamic_load_timeout_is_reported(self):
        def timed_out_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = validator._load_plugin(Path("/usr/lib64/spa-0.2/bluez5/libspa-bluez5.so"), runner=timed_out_runner)

        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("TimeoutExpired", json.dumps(result))

    def test_end_to_end_load_plugins_success_report(self):
        with tempfile.TemporaryDirectory(prefix="zeus-bt-load-") as folder:
            folder = Path(folder)
            executable_specs = tuple(
                {**spec, "path": f"{folder}{spec['path']}"}
                for spec in validator.REQUIRED_EXECUTABLES
            )
            plugin_specs = tuple(
                {**spec, "path": f"{folder}{spec['path']}"}
                for spec in validator.REQUIRED_PLUGINS
            )
            for spec in executable_specs:
                path = Path(spec["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture executable")
                path.chmod(0o755)
            for spec in plugin_specs:
                path = Path(spec["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture shared object")

            output = io.StringIO()
            with mock.patch.object(validator, "REQUIRED_EXECUTABLES", executable_specs), mock.patch.object(
                validator, "REQUIRED_PLUGINS", plugin_specs
            ), mock.patch.object(
                validator,
                "_load_plugin",
                return_value={"status": "loadable"},
            ) as load_plugin, contextlib.redirect_stdout(output):
                result = validator.main(["--load-plugins", "--json"])

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(report["plugin_loadability"]["status"], "loadable")
        self.assertEqual(load_plugin.call_count, len(validator.REQUIRED_PLUGINS))


if __name__ == "__main__":
    unittest.main()
