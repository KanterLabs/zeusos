"""Fedora launcher for the Zeus OS dual-boot installer.

The launcher is deliberately a thin, unprivileged client.  Storage discovery
and planning are provided by :mod:`zeus_installer.preflight`; a separately
qualified backend owns download verification, staging, and any privileged
filesystem work.  Keeping those boundaries here makes it possible to ship
the Fedora UI before the destructive installation path is qualified.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PRODUCT_VERSION = "0.1.0-preview.2"
__version__ = PRODUCT_VERSION
DEFAULT_ALLOCATION_GIB = 128
PLAN_KEYS = ("supported", "blockers", "inventory", "target", "fingerprint")


class InstallerError(RuntimeError):
    """A safe, owner-facing launcher error.

    The exception intentionally carries only a short diagnostic.  A preflight
    or backend implementation must not put credentials, command output, or a
    traceback into an owner-facing JSON response.
    """


def validate_allocation(value: Any) -> int:
    """Return a positive integer GiB allocation or raise ``ValueError``."""

    if isinstance(value, bool):
        raise ValueError("allocation must be a positive whole number of GiB")
    try:
        allocation = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("allocation must be a positive whole number of GiB") from error
    if allocation <= 0 or str(value).strip() in {"", "-0", "+0"}:
        raise ValueError("allocation must be greater than zero GiB")
    # Reject lossy conversion such as 12.5.  Strings containing an integer
    # remain accepted because argparse and small integrations commonly pass
    # values as text.
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("allocation must be a positive whole number of GiB")
    return allocation


def _load_preflight_module() -> Any:
    """Load the sibling preflight module without importing GTK.

    Importing lazily is important: ``zeus-installer preflight --json`` is
    useful over SSH and on minimal Fedora systems where PyGObject is absent.
    """

    module_name = f"{__package__}.preflight" if __package__ else "preflight"
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as error:
        raise InstallerError(
            "the Fedora preflight module is unavailable; installation is not supported"
        ) from error


def collect_inventory(*, preflight_module: Any | None = None) -> dict[str, Any]:
    """Collect the read-only Fedora inventory from the qualified preflight API."""

    module = preflight_module or _load_preflight_module()
    collect = getattr(module, "collect", None)
    if not callable(collect):
        raise InstallerError("the Fedora preflight module does not expose collect()")
    try:
        inventory = collect()
    except InstallerError:
        raise
    except Exception as error:  # pragma: no cover - implementation-specific failures
        raise InstallerError("Fedora preflight could not collect a safe inventory") from error
    if not isinstance(inventory, Mapping):
        raise InstallerError("Fedora preflight returned an invalid inventory")
    return dict(inventory)


def build_plan(
    allocation_gib: int = DEFAULT_ALLOCATION_GIB, *, preflight_module: Any | None = None
) -> dict[str, Any]:
    """Collect and plan a Fedora layout without changing storage.

    The preflight worker owns policy and hardware support.  This wrapper keeps
    the public call shape stable for both the CLI and GTK launcher.
    """

    allocation = validate_allocation(allocation_gib)
    module = preflight_module or _load_preflight_module()
    plan_function = getattr(module, "plan", None)
    if not callable(plan_function):
        raise InstallerError("the Fedora preflight module does not expose plan()")
    inventory = collect_inventory(preflight_module=module)
    try:
        plan = plan_function(inventory, allocation_gib=allocation)
    except InstallerError:
        raise
    except Exception as error:  # pragma: no cover - implementation-specific failures
        raise InstallerError("Fedora preflight could not create a safe installation plan") from error
    if not isinstance(plan, Mapping):
        raise InstallerError("Fedora preflight returned an invalid installation plan")
    # Keep one stable boundary for the CLI, GTK client, and privileged
    # backend.  The preflight worker calls this field
    # ``proposed_target_layout`` to make its review intent explicit; the
    # launcher/backend contract uses the shorter ``target`` name.  Preserve
    # the original field too so diagnostics remain source-grounded.
    detached = dict(plan)
    if "target" not in detached and isinstance(detached.get("proposed_target_layout"), Mapping):
        detached["target"] = dict(detached["proposed_target_layout"])
    detached.setdefault("allocation_gib", allocation)
    return detached


# Keep ``zeus_installer.preflight`` available as the actual worker module.
# Defining a callable with that name shadows the submodule for
# ``from zeus_installer import preflight`` and makes the documented
# ``preflight.collect()`` boundary fail in subtle ways.  Resolve it lazily so
# importing the package remains cheap for the CLI and test doubles.
def __getattr__(name: str) -> Any:
    if name == "preflight":
        return _load_preflight_module()
    raise AttributeError(name)


# A short callable alias remains useful for integrations that want this
# package's collect-and-plan wrapper rather than the worker module itself.
plan = build_plan


def _json_default(value: Any) -> Any:
    """Serialize common read-only inventory values without leaking objects."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    if isinstance(value, bytes):
        return "<binary value omitted>"
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def json_text(value: Any, *, pretty: bool = True) -> str:
    """Render a plan as stable JSON for the CLI and downloadable diagnostics."""

    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "default": _json_default,
    }
    if pretty:
        options.update(indent=2)
    return json.dumps(value, **options)


__all__ = [
    "DEFAULT_ALLOCATION_GIB",
    "InstallerError",
    "PLAN_KEYS",
    "PRODUCT_VERSION",
    "build_plan",
    "collect_inventory",
    "json_text",
    "plan",
    "preflight",
    "validate_allocation",
]
