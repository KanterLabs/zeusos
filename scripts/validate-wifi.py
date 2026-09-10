#!/usr/bin/env python3
"""Validate offline Wi-Fi prerequisites in an installed Zeus image.

The check is deliberately rooted in the supplied image tree.  It verifies
the Intel ``iwlwifi`` firmware, kernel module, NetworkManager daemon and Wi-Fi
plugin without looking for a network interface or attempting to connect to a
network.  That makes it safe to run while building an image and in a VM that
does not expose physical wireless hardware.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1

# Fedora's current linux-firmware package puts these files below
# /usr/lib/firmware/iwlwifi.  Keep the two historical layouts accepted as
# well, since the image base may be refreshed independently of this check.
IWLWIFI_FIRMWARE_GLOBS = (
    "/usr/lib/firmware/iwlwifi/iwlwifi-*.ucode*",
    "/usr/lib/firmware/iwlwifi-*.ucode*",
    "/usr/lib/firmware/intel/iwlwifi/iwlwifi-*.ucode*",
)
# Public aliases make the expected image contract easy to consume from a
# focused test or another offline release check.
REQUIRED_FIRMWARE_GLOBS = IWLWIFI_FIRMWARE_GLOBS

REQUIRED_EXECUTABLES = (
    {
        "id": "networkmanager",
        "path": "/usr/bin/NetworkManager",
        "package": "NetworkManager",
        "description": "NetworkManager daemon",
    },
    {
        "id": "nmcli",
        "path": "/usr/bin/nmcli",
        "package": "NetworkManager",
        "description": "NetworkManager control client",
    },
    {
        "id": "wpa_supplicant",
        "path": "/usr/bin/wpa_supplicant",
        "package": "wpa_supplicant",
        "description": "Wi-Fi authentication backend",
    },
)

REQUIRED_FILES = (
    {
        "id": "networkmanager_service",
        "path": "/usr/lib/systemd/system/NetworkManager.service",
        "package": "NetworkManager",
        "description": "NetworkManager system service",
    },
    {
        "id": "networkmanager_wifi_plugin",
        "patterns": ("/usr/lib64/NetworkManager/*/libnm-device-plugin-wifi.so",),
        "package": "NetworkManager-wifi",
        "description": "NetworkManager Wi-Fi device plugin",
    },
    {
        "id": "iwlwifi_driver",
        "patterns": (
            "/usr/lib/modules/*/kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko*",
        ),
        "package": "kernel-modules",
        "description": "Intel iwlwifi kernel module",
    },
)

REQUIRED_FIRMWARE = {
    "id": "iwlwifi_firmware",
    "patterns": IWLWIFI_FIRMWARE_GLOBS,
    "package": "iwlwifi-mvm-firmware",
    "description": "Intel iwlwifi MVM firmware images",
}


def _rooted(root: Path, path: str) -> Path:
    """Build an image-relative path without treating *path* as host absolute."""

    return root / path.lstrip("/")


def _resolved_regular_file(root: Path, path: Path) -> tuple[Path | None, str | None]:
    """Resolve one candidate and reject links escaping the image root."""

    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
        metadata = resolved.stat()
    except FileNotFoundError:
        return None, "required path is absent"
    except OSError:
        return None, "required path could not be inspected"
    except (RuntimeError, ValueError):
        return None, "required path resolves outside the image root"

    if not stat.S_ISREG(metadata.st_mode):
        return None, "required path is not a regular file"
    if metadata.st_size <= 0:
        return None, "required file is empty"
    return resolved, None


def _check_file(root: Path, spec: Mapping[str, str], *, executable: bool = False) -> dict[str, Any]:
    """Return a path-only check without invoking package or hardware tools."""

    path = _rooted(root, spec["path"])
    result: dict[str, Any] = {
        "id": spec["id"],
        "kind": "executable" if executable else "file",
        "path": spec["path"],
        "package": spec["package"],
        "description": spec["description"],
    }
    resolved, reason = _resolved_regular_file(root, path)
    if reason:
        result["status"] = "missing" if reason == "required path is absent" else "invalid"
        result["reason"] = reason
        return result
    assert resolved is not None
    if executable and not resolved.stat().st_mode & (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    ):
        result["status"] = "not_executable"
        result["reason"] = "required executable has no execute bit"
        return result
    result["status"] = "present"
    return result


def _check_glob(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Check that at least one non-empty regular file matches the spec."""

    patterns = tuple(spec["patterns"])
    result: dict[str, Any] = {
        "id": spec["id"],
        "kind": "glob",
        "path": patterns[0],
        "patterns": list(patterns),
        "package": spec["package"],
        "description": spec["description"],
        "matches": [],
    }
    invalid_reasons: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for candidate in sorted(root.glob(pattern.lstrip("/"))):
            resolved, reason = _resolved_regular_file(root, candidate)
            if reason:
                invalid_reasons.append(reason)
                continue
            assert resolved is not None
            display_path = "/" + str(candidate.relative_to(root))
            if display_path not in seen:
                seen.add(display_path)
                result["matches"].append(display_path)

    if result["matches"]:
        result["status"] = "present"
    elif invalid_reasons:
        result["status"] = "invalid"
        result["reason"] = invalid_reasons[0]
    else:
        result["status"] = "missing"
        result["reason"] = "no matching non-empty firmware or module file"
    return result


def _base_report(*, fixture: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target": {"os": "Fedora Linux", "architecture": "x86_64"},
        "scope": "fixture" if fixture else "installed-image",
        "ok": False,
        "software": {"status": "not_checked"},
        "checks": {"firmware": [], "executables": [], "files": []},
        "hardware_qualification": {
            "status": "NOT TESTED",
            "reason": "Physical wireless hardware is not required for this image check.",
        },
    }


def validate(root: Path | str = Path("/")) -> dict[str, Any]:
    """Validate one installed image tree without probing host networking."""

    root = Path(root).resolve()
    report = _base_report(fixture=root != Path("/"))
    firmware = _check_glob(root, REQUIRED_FIRMWARE)
    executables = [_check_file(root, spec, executable=True) for spec in REQUIRED_EXECUTABLES]
    files = [
        _check_glob(root, spec) if "patterns" in spec else _check_file(root, spec)
        for spec in REQUIRED_FILES
    ]
    report["checks"] = {
        "firmware": [firmware],
        "executables": executables,
        "files": files,
    }
    all_checks = [firmware, *executables, *files]
    ready = all(check["status"] == "present" for check in all_checks)
    report["ok"] = ready
    report["software"] = {
        "status": "prerequisites_present" if ready else "missing_prerequisites"
    }
    return report


def _print_human(report: Mapping[str, Any]) -> None:
    status = "PRESENT" if report.get("ok") else "MISSING"
    print(f"Wi-Fi software prerequisites: {status}")
    checks = report.get("checks", {})
    for group_name in ("firmware", "executables", "files"):
        for check in checks.get(group_name, []):
            marker = "PASS" if check["status"] == "present" else "FAIL"
            print(f"  [{marker}] {check['path']} ({check['description']})")
            if check.get("reason"):
                print(f"         {check['reason']}")
            if check.get("matches"):
                print(f"         matches: {len(check['matches'])}")
    print("Physical wireless hardware: NOT TESTED")


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate(args.root)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
