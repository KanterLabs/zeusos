"""Command-line entry point for the Fedora Zeus installer launcher."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    DEFAULT_ALLOCATION_GIB,
    InstallerError,
    PRODUCT_VERSION,
    build_plan,
    json_text,
    validate_allocation,
)


def _allocation(value: str) -> int:
    try:
        return validate_allocation(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeus-installer",
        description="Review a Fedora layout and prepare a qualified Zeus OS dual-boot install.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"zeus-installer {PRODUCT_VERSION}",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    preflight = commands.add_parser(
        "preflight",
        help="collect a read-only Fedora plan",
        description="Collect and print the read-only Fedora dual-boot plan.",
    )
    preflight.add_argument(
        "--allocation-gib",
        "--allocation",
        type=_allocation,
        default=DEFAULT_ALLOCATION_GIB,
        metavar="GIB",
        help=f"total Zeus allocation including boot partitions (default: {DEFAULT_ALLOCATION_GIB})",
    )
    preflight.add_argument(
        "--json",
        action="store_true",
        help="print the reviewable machine-readable plan",
    )

    gui = commands.add_parser(
        "gui",
        aliases=("graphical",),
        help="open the GTK4 graphical launcher",
        description="Open the unprivileged GTK4 Fedora launcher.",
    )
    gui.add_argument(
        "--allocation-gib",
        "--allocation",
        type=_allocation,
        default=DEFAULT_ALLOCATION_GIB,
        metavar="GIB",
        help=f"initial total Zeus allocation (default: {DEFAULT_ALLOCATION_GIB})",
    )
    return parser


def _error_payload(error: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "supported": False,
        "blockers": [str(error)],
        "error": "preflight_unavailable",
    }


def _installed_preflight(allocation_gib: int) -> dict[str, Any] | None:
    """Use the fixed root-side scan when this is an installed launcher.

    A source checkout intentionally has no root-owned helper, so it keeps the
    useful unprivileged worker path below.  An installed package, however,
    should give CLI callers the same read-only inventory as the GTK client.
    Only the bounded numeric allocation crosses into the backend facade.
    ``helper_unavailable`` is the one transport condition that permits the
    source fallback; authorization and structured helper failures remain
    visible to the caller.
    """

    try:
        backend = importlib.import_module(f"{__package__}.backend")
    except (ImportError, ModuleNotFoundError):
        return None
    helper_path = getattr(backend, "HELPER_COMMAND", None)
    function = getattr(backend, "preflight", None)
    if not isinstance(helper_path, str) or not Path(helper_path).is_file() or not callable(function):
        return None
    try:
        result = function(allocation_gib=allocation_gib)
    except Exception as error:  # pragma: no cover - installed backend path
        if getattr(error, "code", None) in {"helper_unavailable", "preflight_unavailable"}:
            return None
        message = str(error).strip() or "The privileged Fedora preflight could not complete safely."
        raise InstallerError(message) from error
    if not isinstance(result, Mapping):
        raise InstallerError("The privileged Fedora preflight returned an invalid plan.")
    return dict(result)


def _print_human_plan(plan: dict[str, Any]) -> None:
    supported = plan.get("supported") is True
    print("Fedora dual-boot preflight")
    print(f"Supported: {'yes' if supported else 'no'}")

    inventory = plan.get("inventory")
    if isinstance(inventory, dict):
        disk = inventory.get("disk") or inventory.get("target_disk")
        if isinstance(disk, dict):
            values = []
            for key in ("model", "device", "path", "size_gib", "capacity_gib", "id"):
                value = disk.get(key)
                if value not in (None, ""):
                    values.append(f"{key}={value}")
            if values:
                print("Disk: " + ", ".join(values))

    target = plan.get("target")
    if isinstance(target, dict):
        allocation = target.get("allocation_gib")
        if allocation is None:
            allocation = target.get("zeus_allocation_gib")
        if allocation is not None:
            print(f"Zeus allocation: {allocation} GiB")

    blockers = plan.get("blockers")
    if isinstance(blockers, (list, tuple)) and blockers:
        detail_by_code: dict[str, str] = {}
        details = plan.get("blocker_details")
        if isinstance(details, (list, tuple)):
            for detail in details:
                if isinstance(detail, dict):
                    code = detail.get("code")
                    message = detail.get("message")
                    if isinstance(code, str) and isinstance(message, str) and message.strip():
                        detail_by_code[code] = message.strip()
        print("Blockers:")
        for blocker in blockers:
            if isinstance(blocker, dict):
                message = blocker.get("message") or blocker.get("reason") or blocker.get("code")
            else:
                message = detail_by_code.get(blocker, blocker) if isinstance(blocker, str) else blocker
            if message:
                print(f"  - {message}")
    elif supported:
        print("No blockers reported. The backend still must be qualified before installation.")
    else:
        print("No supported installation plan is available.")


def _run_preflight(arguments: argparse.Namespace) -> int:
    try:
        result = _installed_preflight(arguments.allocation_gib)
        if result is None:
            result = build_plan(arguments.allocation_gib)
    except (InstallerError, ValueError) as error:
        if arguments.json:
            print(json_text(_error_payload(error)))
        else:
            print(f"zeus-installer: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json_text(result))
    else:
        _print_human_plan(result)
    # A generated plan with blockers is still a successful read-only report.
    # Callers can inspect ``supported`` without losing the diagnostics to a
    # shell's ``set -e``.
    return 0


def _run_gui(arguments: argparse.Namespace) -> int:
    try:
        from .gui import launch_gui

        return int(launch_gui(allocation_gib=arguments.allocation_gib))
    except (InstallerError, RuntimeError) as error:
        print(f"zeus-installer: {error}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    if arguments.command is None:
        parser.print_help()
        return 2
    if arguments.command == "preflight":
        return _run_preflight(arguments)
    if arguments.command in {"gui", "graphical"}:
        return _run_gui(arguments)
    parser.error(f"unsupported command: {arguments.command}")
    return 2  # pragma: no cover - argparse exits above


if __name__ == "__main__":
    raise SystemExit(main())
