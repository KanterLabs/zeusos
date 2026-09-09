#!/usr/bin/env python3
"""Check the installed-image prerequisites for Bluetooth audio.

This is an offline readiness check.  It examines only the supplied root tree
and never asks BlueZ, PipeWire, or WirePlumber to discover or connect to a
device.  Dynamic loading is deliberately opt-in and is allowed only when the
root is the running image (``/``).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
PLUGIN_LOAD_TIMEOUT_SECONDS = 5.0

# These are the Fedora x86_64 paths observed in the Zeus image.  Package names
# are informational evidence in the report; the validator intentionally checks
# the installed paths so it can run against an unpacked image or fixture tree.
REQUIRED_EXECUTABLES = (
    {
        "id": "bluez_bluetoothctl",
        "path": "/usr/bin/bluetoothctl",
        "package": "bluez",
        "description": "BlueZ control client",
    },
    {
        "id": "bluez_daemon",
        "path": "/usr/libexec/bluetooth/bluetoothd",
        "package": "bluez",
        "description": "BlueZ daemon backend",
    },
    {
        "id": "pipewire",
        "path": "/usr/bin/pipewire",
        "package": "pipewire",
        "description": "PipeWire audio server",
    },
    {
        "id": "pipewire_pulse",
        "path": "/usr/bin/pipewire-pulse",
        "package": "pipewire-pulseaudio",
        "description": "PipeWire PulseAudio compatibility server",
    },
    {
        "id": "wireplumber",
        "path": "/usr/bin/wireplumber",
        "package": "wireplumber",
        "description": "WirePlumber session manager",
    },
    {
        "id": "wireplumber_wpctl",
        "path": "/usr/bin/wpctl",
        "package": "wireplumber",
        "description": "WirePlumber control client",
    },
)

REQUIRED_PLUGINS = (
    {
        "id": "bluez_spa_backend",
        "path": "/usr/lib64/spa-0.2/bluez5/libspa-bluez5.so",
        "package": "pipewire-libs",
        "description": "PipeWire BlueZ SPA backend",
    },
    {
        "id": "a2dp_aac",
        "path": "/usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-aac.so",
        "package": "pipewire-libs",
        "description": "A2DP AAC codec",
    },
    {
        "id": "a2dp_sbc",
        "path": "/usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-sbc.so",
        "package": "pipewire-libs",
        "description": "A2DP SBC codec",
    },
    {
        "id": "hfp_cvsd",
        "path": "/usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-hfp-cvsd.so",
        "package": "pipewire-libs",
        "description": "HFP CVSD speech codec",
    },
    {
        "id": "hfp_msbc",
        "path": "/usr/lib64/spa-0.2/bluez5/libspa-codec-bluez5-hfp-msbc.so",
        "package": "pipewire-libs",
        "description": "HFP mSBC speech codec",
    },
)

CAPABILITIES = (
    {
        "id": "a2dp_music",
        "description": "Bluetooth stereo playback",
        "requires": ("bluez_spa_backend", "a2dp_aac", "a2dp_sbc"),
        "requires_executables": tuple(spec["id"] for spec in REQUIRED_EXECUTABLES),
    },
    {
        "id": "hfp_microphone",
        "description": "Bluetooth headset microphone",
        "requires": ("bluez_spa_backend", "hfp_cvsd", "hfp_msbc"),
        "requires_executables": tuple(spec["id"] for spec in REQUIRED_EXECUTABLES),
    },
)

HARDWARE_TESTS = (
    "pairing",
    "audio_playback",
    "calls_microphone",
    "reconnect",
    "battery",
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _rooted(root: Path, path: str) -> Path:
    """Build an image-relative path without interpreting *path* as absolute."""

    return root / path.lstrip("/")


def _check_path(root: Path, spec: Mapping[str, str], *, executable: bool) -> dict[str, Any]:
    """Return a path-only check, without reading file contents."""

    root = root.resolve()
    path = _rooted(root, spec["path"])
    result: dict[str, Any] = {
        "id": spec["id"],
        "kind": "executable" if executable else "plugin",
        "path": spec["path"],
        "package": spec["package"],
        "description": spec["description"],
    }

    try:
        # Fedora's pipewire-pulse is an in-root symlink to pipewire.  Resolve
        # links for validation while rejecting links that escape a fixture or
        # installed image root.
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
        file_info = resolved.lstat()
    except FileNotFoundError:
        result["status"] = "missing"
        result["reason"] = "required path is absent"
        return result
    except OSError:
        result["status"] = "unreadable"
        result["reason"] = "required path could not be inspected"
        return result
    except (RuntimeError, ValueError):
        result["status"] = "invalid"
        result["reason"] = "required path resolves outside the image root"
        return result

    if not stat.S_ISREG(file_info.st_mode):
        result["status"] = "invalid"
        result["reason"] = "required path is not a regular file"
        return result
    if executable and not file_info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        result["status"] = "not_executable"
        result["reason"] = "required executable has no execute bit"
        return result

    result["status"] = "present"
    return result


def _load_plugin(path: Path, *, runner: Runner | None = None) -> dict[str, str]:
    """Load one plugin in a bounded child process and discard child output."""

    if runner is None:
        runner = subprocess.run
    # ctypes.CDLL performs a real RTLD_NOW load.  The child is intentionally
    # limited to loading the fixed image path; it does not invoke any BlueZ or
    # PipeWire control client and therefore cannot pair, scan, or set policy.
    command = [
        sys.executable,
        "-c",
        "import ctypes, os, sys; ctypes.CDLL(sys.argv[1], mode=os.RTLD_NOW)",
        str(path),
    ]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=PLUGIN_LOAD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": "dynamic load exceeded the bounded timeout"}
    except OSError:
        return {"status": "unavailable", "reason": "dynamic load checker could not start"}

    if completed.returncode == 0:
        return {"status": "loadable"}
    # Do not return stderr: loader diagnostics can contain arbitrary paths,
    # host details, or device-derived strings.
    return {"status": "failed", "reason": "dynamic load failed"}


def _hardware_report() -> dict[str, Any]:
    return {
        "status": "NOT TESTED",
        "tests": {name: "NOT TESTED" for name in HARDWARE_TESTS},
        "reason": "Requires a physical Bluetooth adapter and AirPods hardware.",
    }


def _base_report(*, fixture: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target": {"os": "Fedora Linux", "architecture": "x86_64"},
        "scope": "fixture" if fixture else "installed-image",
        "ok": False,
        "software": {"status": "not_checked"},
        "checks": {"executables": [], "plugins": []},
        "capabilities": [],
        "plugin_loadability": {
            "requested": False,
            "status": "not_run",
            "checks": [],
        },
        "hardware_qualification": _hardware_report(),
    }


def _capability_report(
    checks: Mapping[str, Mapping[str, Any]],
    load_checks: Mapping[str, Mapping[str, Any]],
    *,
    load_requested: bool,
) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    for capability in CAPABILITIES:
        required = list(capability["requires"])
        required_executables = list(capability["requires_executables"])
        missing = [
            name
            for name in required + required_executables
            if checks[name]["status"] != "present"
        ]
        failed_load = [
            name
            for name in required
            if load_requested and load_checks.get(name, {}).get("status") != "loadable"
        ]
        if missing:
            status = "missing_prerequisites"
        elif failed_load:
            status = "load_failed"
        else:
            status = "prerequisites_present"
        capabilities.append(
            {
                "id": capability["id"],
                "description": capability["description"],
                "status": status,
                "requires": required,
                "requires_executables": required_executables,
            }
        )
    return capabilities


def validate(
    root: Path | str = Path("/"),
    *,
    load_plugins: bool = False,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Validate one installed image tree.

    ``load_plugins`` is intentionally rejected for fixture roots.  This keeps
    tests and offline image inspection from executing host code or touching a
    host audio/Bluetooth session.
    """

    root = Path(root).resolve()
    if load_plugins and root != Path("/"):
        raise ValueError("--load-plugins is supported only with --root /")

    report = _base_report(fixture=root != Path("/"))
    executable_checks = [
        _check_path(root, spec, executable=True) for spec in REQUIRED_EXECUTABLES
    ]
    plugin_checks = [_check_path(root, spec, executable=False) for spec in REQUIRED_PLUGINS]
    report["checks"] = {
        "executables": executable_checks,
        "plugins": plugin_checks,
    }

    all_checks = executable_checks + plugin_checks
    check_by_id = {check["id"]: check for check in all_checks}
    load_results: list[dict[str, Any]] = []
    if load_plugins:
        for check in plugin_checks:
            if check["status"] != "present":
                load_results.append(
                    {
                        "id": check["id"],
                        "path": check["path"],
                        "status": "not_run",
                        "reason": "required plugin is not present",
                    }
                )
                continue
            result = _load_plugin(_rooted(root, check["path"]), runner=runner)
            load_results.append({"id": check["id"], "path": check["path"], **result})

    load_by_id = {result["id"]: result for result in load_results}
    if load_plugins:
        load_status = "loadable" if all(
            result["status"] == "loadable" for result in load_results
        ) else "failed"
    else:
        load_status = "not_run"
    report["plugin_loadability"] = {
        "requested": load_plugins,
        "status": load_status,
        "checks": load_results,
    }
    report["capabilities"] = _capability_report(
        check_by_id,
        load_by_id,
        load_requested=load_plugins,
    )

    software_ready = all(check["status"] == "present" for check in all_checks)
    if load_plugins:
        software_ready = software_ready and load_status == "loadable"
    report["ok"] = software_ready
    report["software"] = {
        "status": "prerequisites_present"
        if software_ready
        else "missing_prerequisites"
    }
    return report


def _error_report(message: str) -> dict[str, Any]:
    report = _base_report(fixture=True)
    report["error"] = message
    report["software"] = {"status": "not_checked"}
    report["plugin_loadability"] = {
        "requested": True,
        "status": "not_run",
        "checks": [],
        "reason": "dynamic loading is restricted to the actual installed image",
    }
    return report


def _print_human(report: Mapping[str, Any]) -> None:
    status = "PRESENT" if report.get("ok") else "MISSING"
    print(f"Bluetooth audio software prerequisites: {status}")
    if report.get("error"):
        print(f"Error: {report['error']}")
    checks = report.get("checks", {})
    for group_name in ("executables", "plugins"):
        for check in checks.get(group_name, []):
            marker = "PASS" if check["status"] == "present" else "FAIL"
            print(f"  [{marker}] {check['path']} ({check['description']})")
    for capability in report.get("capabilities", []):
        print(f"  {capability['id']}: {capability['status']}")
    loadability = report.get("plugin_loadability", {})
    if loadability.get("requested"):
        print(f"Plugin dynamic loading: {loadability.get('status', 'failed')}")
        for check in loadability.get("checks", []):
            print(f"  {check['id']}: {check['status']}")
    print("Physical AirPods qualification: NOT TESTED")
    for name in HARDWARE_TESTS:
        print(f"  {name}: NOT TESTED")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        metavar="ROOT",
        help="installed image root or fixture tree (default: /)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--load-plugins",
        action="store_true",
        help="dynamically load required plugins; allowed only with --root /",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        report = validate(root, load_plugins=args.load_plugins)
    except ValueError as error:
        report = _error_report(str(error))
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            _print_human(report)
        return 2

    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
