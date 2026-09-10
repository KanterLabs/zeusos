"""GTK4 graphical launcher for the Fedora Zeus dual-boot installer.

This module keeps preflight and backend calls behind a small controller so the
GTK import remains optional for the JSON CLI.  The window is always an
unprivileged review surface.  It enables signed preparation when the plan is
supported and a preparation facade is available.  Installation is a separate
qualified executor phase; restart is enabled only after that executor reports
``reboot_required``.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import DEFAULT_ALLOCATION_GIB, InstallerError, PRODUCT_VERSION, build_plan, validate_allocation


APPLICATION_ID = "org.zeus.Installer"
BACKEND_MODULE_NAME = f"{__package__}.backend" if __package__ else "backend"
QUALIFIED_STATES = frozenset({"qualified", "ready", "supported"})
PREPARED_STATES = frozenset({"ready", "prepared", "staged"})
JOURNAL_PHASES = frozenset(
    {"preparing", "downloading", "verifying", "prepared", "installing", "error", "interrupted"}
)


class BackendUnavailableError(InstallerError):
    """Raised when a requested backend action is absent or unavailable."""


class GtkUnavailableError(InstallerError):
    """Raised when the graphical dependencies are unavailable."""


def _load_backend_module() -> Any | None:
    try:
        return importlib.import_module(BACKEND_MODULE_NAME)
    except (ImportError, ModuleNotFoundError):
        return None


def _safe_text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return fallback
    return str(value).strip()


def _qualification_value(module: Any) -> Mapping[str, Any] | None:
    """Read an explicit backend qualification record without trusting prose."""

    for name in ("qualification", "get_qualification", "qualification_status"):
        candidate = getattr(module, name, None)
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:  # pragma: no cover - backend-specific diagnostics
                return None
        if isinstance(candidate, Mapping):
            return candidate

    # A code-level bool is the smallest backend contract and is intentionally
    # separate from an environment variable or a user-controlled config file.
    if getattr(module, "QUALIFIED", None) is True:
        return {"qualified": True, "status": "qualified"}
    if getattr(module, "BACKEND_QUALIFIED", None) is True:
        return {"qualified": True, "status": "qualified"}
    return None


def backend_qualification(module: Any | None = None) -> dict[str, Any]:
    """Return a sanitized, fail-closed qualification report.

    Qualification requires an explicit ``qualified: true`` value (or one of
    the two code-level bool markers), plus callable preparation, install, and
    restart methods.  Merely installing a Python module never enables
    destructive actions.
    """

    backend = module if module is not None else _load_backend_module()
    if backend is None:
        return {
            "available": False,
            "qualified": False,
            "reason": "The installation backend is not available in this preview.",
        }

    report = _qualification_value(backend)
    explicit = isinstance(report, Mapping) and report.get("qualified") is True
    status = _safe_text(report.get("status")) if isinstance(report, Mapping) else ""
    prepare = any(callable(getattr(backend, name, None)) for name in ("prepare", "download_and_prepare"))
    install = any(callable(getattr(backend, name, None)) for name in ("install", "execute_install"))
    restart = any(callable(getattr(backend, name, None)) for name in ("restart", "request_restart"))
    qualified = bool(
        explicit
        and (not status or status.lower() in QUALIFIED_STATES)
        and prepare
        and install
        and restart
    )
    if qualified:
        return {
            "available": True,
            "qualified": True,
            "status": status or "qualified",
            "version": _safe_text(report.get("version")) if isinstance(report, Mapping) else "",
        }

    reason = "The installation backend has not passed the dual-boot qualification gate."
    if explicit and not (prepare and install and restart):
        reason = "The qualified backend contract is incomplete; installation remains disabled."
    return {
        "available": True,
        "qualified": False,
        "status": status or "unqualified",
        "reason": reason,
    }


def plan_is_supported(plan: Mapping[str, Any] | None) -> bool:
    """Require the preflight worker's explicit support result and no blockers."""

    if not isinstance(plan, Mapping) or plan.get("supported") is not True:
        return False
    blockers = plan.get("blockers")
    if blockers is None:
        return True
    if isinstance(blockers, (str, bytes)):
        return not bool(blockers)
    try:
        return len(blockers) == 0
    except TypeError:
        return False


def _blocker_text(blocker: Any) -> str:
    if isinstance(blocker, Mapping):
        for key in ("message", "reason", "detail", "code"):
            value = _safe_text(blocker.get(key))
            if value:
                return value
        return "The preflight worker reported an unspecified blocker."
    return _safe_text(blocker, "The preflight worker reported an unspecified blocker.")


def plan_blockers(plan: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(plan, Mapping):
        return ["No preflight plan is available."]
    blockers = plan.get("blockers")
    if blockers is None:
        return [] if plan.get("supported") is True else ["The Fedora layout is not supported."]
    if isinstance(blockers, (str, bytes)):
        return [_blocker_text(blockers)] if blockers else []
    details = plan.get("blocker_details")
    detail_by_code: dict[str, str] = {}
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes)):
        for detail in details:
            if not isinstance(detail, Mapping):
                continue
            code = _safe_text(detail.get("code"))
            message = _safe_text(detail.get("message"))
            if code and message:
                detail_by_code[code] = message
    try:
        result: list[str] = []
        for item in blockers:
            text = _blocker_text(item)
            if isinstance(item, str):
                text = detail_by_code.get(item, text)
            if text:
                result.append(text)
        return result
    except TypeError:
        return [_blocker_text(blockers)]


def _backend_function(module: Any, names: Sequence[str]) -> Callable[..., Any] | None:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    return None


def _invoke_prepare(function: Callable[..., Any], plan: Mapping[str, Any], progress: Callable[[Any], None] | None) -> Any:
    """Call a backend using the narrow plan/progress interface."""

    try:
        signature = inspect.signature(function)
        parameters = signature.parameters
    except (TypeError, ValueError):  # pragma: no cover - extension backend
        parameters = {}
    accepts_progress = "progress" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_progress:
        return function(plan, progress=progress)
    return function(plan)


def _invoke_preflight(function: Callable[..., Any], allocation_gib: int) -> Any:
    """Call the fixed-helper facade with a numeric allocation only.

    The installed backend owns the ``pkexec`` boundary.  Keeping this call
    shape numeric prevents the GTK process from forwarding a user-selected
    path, command, or helper executable into the privileged process while
    allowing small backend facades to use either ``allocation_gib`` or
    ``allocation`` as their parameter name.
    """

    try:
        signature = inspect.signature(function)
        parameters = signature.parameters
    except (TypeError, ValueError):  # pragma: no cover - extension backend
        parameters = {}
    if "allocation_gib" in parameters:
        return function(allocation_gib=allocation_gib)
    if "allocation" in parameters:
        return function(allocation=allocation_gib)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return function(allocation_gib=allocation_gib)
    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if positional:
        return function(allocation_gib)
    return function()


def _invoke_status(function: Callable[..., Any]) -> Any:
    """Read the backend's durable journal state without supplying a plan."""

    return function()


def _facade_unavailable(error: Exception) -> bool:
    """Identify an absent installed helper so source fallback can run."""

    return getattr(error, "code", None) in {"helper_unavailable", "preflight_unavailable"}


def _normalise_plan(value: Any, allocation_gib: int) -> dict[str, Any]:
    """Copy a worker/facade plan and provide the stable target alias."""

    if not isinstance(value, Mapping):
        raise InstallerError("The privileged preflight returned an invalid installation plan.")
    plan = dict(value)
    if "target" not in plan and isinstance(plan.get("proposed_target_layout"), Mapping):
        plan["target"] = dict(plan["proposed_target_layout"])
    if plan.get("ok") is False and not plan.get("blockers"):
        diagnostic = _safe_text(plan.get("message") or plan.get("error"))
        if diagnostic:
            plan["blockers"] = [diagnostic]
    plan.setdefault("allocation_gib", allocation_gib)
    return plan


def _result_ok(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, Mapping):
        # A backend may omit ``ok`` for a legacy success response, but any
        # present status must be the literal boolean ``True``.  Strings such
        # as ``"false"`` and integer ``0`` are not safe success signals.
        return "ok" not in result or result.get("ok") is True
    return bool(result)


def _result_ready(result: Any) -> bool:
    if isinstance(result, Mapping):
        if not _result_ok(result):
            return False
        if result.get("prepared") is True or result.get("staged") is True:
            return True
        state = _safe_text(result.get("state")) or _safe_text(result.get("phase"))
        state = state.lower()
        return state in PREPARED_STATES
    return False


def _result_reboot_ready(result: Any) -> bool:
    """Accept only the executor's explicit maintenance reboot phase."""

    if not isinstance(result, Mapping) or not _result_ok(result):
        return False
    phase = _safe_text(result.get("phase")) or _safe_text(result.get("state"))
    phase = phase.lower()
    return phase == "reboot_required"


def _status_phase(result: Any) -> str:
    if not isinstance(result, Mapping) or not _result_ok(result):
        return ""
    phase = _safe_text(result.get("phase")) or _safe_text(result.get("state"))
    return phase.lower()


def _status_boot_changed(result: Mapping[str, Any]) -> bool:
    """Read the helper's explicit post-reboot proof marker."""

    for key in ("current_boot_changed", "boot_changed", "rebooted", "resumed_after_reboot"):
        if result.get(key) is True:
            return True
    return False


def _status_resume_invalid(result: Mapping[str, Any]) -> bool:
    """Keep both actions disabled when post-reboot proof failed."""

    verification = _safe_text(result.get("resume_verification")).lower()
    return bool(result.get("resume_error")) or verification in {"failed", "unavailable"}


def _status_error_message(status: Mapping[str, Any]) -> str:
    """Turn durable backend error codes into owner-facing next steps."""

    error = _safe_text(status.get("error")).lower()
    messages = {
        "backup_receipt_missing": "A verified backup receipt is missing. Create and verify the backup before continuing.",
        "backup_unverified": "The backup receipt is missing or invalid. Verify the backup before continuing.",
        "backup_target_mismatch": "The verified backup belongs to another target disk or layout. Review the backup before continuing.",
    }
    if error in messages:
        return messages[error]
    return _safe_text(status.get("message") or status.get("error"))


def _status_display_plan(status: Mapping[str, Any], allocation_gib: int, phase: str) -> dict[str, Any]:
    """Build a review-only display plan from the durable helper response."""

    status_plan = status.get("plan")
    if isinstance(status_plan, Mapping):
        return _normalise_plan(status_plan, allocation_gib)
    status_target = status.get("target") or status.get("proposed_target_layout")
    return {
        "schema_version": 1,
        "supported": True,
        "blockers": [],
        "inventory": dict(status.get("inventory", {}))
        if isinstance(status.get("inventory"), Mapping)
        else {},
        "target": dict(status_target) if isinstance(status_target, Mapping) else {},
        "fingerprint": status.get("fingerprint"),
        "allocation_gib": status.get("allocation_gib", allocation_gib),
        "phase": phase,
    }


@dataclass
class InstallerController:
    """Pure orchestration state shared by the CLI tests and GTK window."""

    allocation_gib: int = DEFAULT_ALLOCATION_GIB
    preflight_module: Any | None = None
    backend_module: Any | None = None

    def __post_init__(self) -> None:
        self.allocation_gib = validate_allocation(self.allocation_gib)
        self.plan: dict[str, Any] | None = None
        self.preparation: Any | None = None
        self.installation: Any | None = None
        self.resume_status: dict[str, Any] | None = None
        self.resume_pending = False
        self.resume_blocked = False
        self.operation_pending = False
        self.operation_blocked = False
        self.terminal = False
        self.prepared = False
        self.reboot_ready = False
        self.last_error: str | None = None

    @property
    def qualification(self) -> dict[str, Any]:
        return backend_qualification(self.backend_module)

    def refresh(self, allocation_gib: int | None = None) -> dict[str, Any]:
        if allocation_gib is not None:
            self.allocation_gib = validate_allocation(allocation_gib)
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        self.plan = None
        self.preparation = None
        self.installation = None
        self.prepared = False
        self.reboot_ready = False
        self.last_error = None
        self.resume_status = None
        self.resume_pending = False
        self.resume_blocked = False
        self.operation_pending = False
        self.operation_blocked = False
        self.terminal = False
        status_function = _backend_function(
            backend, ("status", "get_status", "operation_status")
        ) if backend is not None else None
        if status_function is not None:
            status: Any | None = None
            try:
                status = _invoke_status(status_function)
            except Exception as error:  # pragma: no cover - backend-specific failures
                if not _facade_unavailable(error):
                    raise InstallerError("The existing installer operation could not be read safely.") from error
            phase = _status_phase(status)
            if not phase and isinstance(status, Mapping):
                # Keep a failed helper response visible instead of silently
                # replacing its durable error with a newly collected plan.
                phase = (_safe_text(status.get("phase")) or _safe_text(status.get("state"))).lower()
            if (
                phase in {"reboot_required", "installed"}
                and isinstance(status, Mapping)
                and _result_ok(status)
            ):
                self.resume_status = dict(status)
                self.resume_blocked = phase == "reboot_required" and _status_resume_invalid(status)
                self.resume_pending = (
                    phase == "reboot_required"
                    and _status_boot_changed(status)
                    and not self.resume_blocked
                )
                self.terminal = phase == "installed"
                self.plan = _status_display_plan(status, self.allocation_gib, phase)
                self.preparation = status
                self.prepared = False
                self.installation = None if self.resume_pending else status
                self.reboot_ready = (
                    phase == "reboot_required"
                    and not self.resume_pending
                    and not self.resume_blocked
                )
                return self.plan
            if isinstance(status, Mapping) and phase in JOURNAL_PHASES:
                # A prepared or interrupted journal is already authoritative.
                # Recollecting here could offer a second allocation while a
                # root-owned operation is still bound to the first one.
                self.resume_status = dict(status)
                self.operation_pending = True
                self.operation_blocked = phase != "prepared" or not _result_ok(status)
                self.plan = _status_display_plan(status, self.allocation_gib, phase)
                self.preparation = status
                self.prepared = phase == "prepared" and _result_ok(status)
                self.last_error = _status_error_message(status)
                return self.plan
        privileged_preflight = _backend_function(
            backend,
            ("preflight", "privileged_preflight", "read_only_preflight", "collect_preflight"),
        ) if backend is not None else None
        if privileged_preflight is not None:
            try:
                self.plan = _normalise_plan(
                    _invoke_preflight(privileged_preflight, self.allocation_gib),
                    self.allocation_gib,
                )
            except BackendUnavailableError:
                raise
            except Exception as error:  # pragma: no cover - backend-specific failures
                if not _facade_unavailable(error):
                    raise InstallerError("Privileged Fedora preflight could not complete safely.") from error
                self.plan = build_plan(self.allocation_gib, preflight_module=self.preflight_module)
        else:
            self.plan = build_plan(self.allocation_gib, preflight_module=self.preflight_module)
        self.preparation = None
        self.installation = None
        self.prepared = False
        self.reboot_ready = False
        self.last_error = None
        return self.plan

    def can_prepare(self) -> bool:
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        return (
            not self.resume_pending
            and not self.resume_blocked
            and not self.terminal
            and not self.operation_blocked
            and not self.prepared
            and self.installation is None
            and plan_is_supported(self.plan)
            and _backend_function(backend, ("prepare", "download_and_prepare")) is not None
        )

    def can_install(self) -> bool:
        return (
            not self.terminal
            and not self.resume_blocked
            and not self.operation_blocked
            and (self.prepared or self.resume_pending)
            and self.installation is None
            and plan_is_supported(self.plan)
            and self.qualification.get("qualified") is True
        )

    def can_restart(self) -> bool:
        return (
            (self.reboot_ready or self.terminal)
            and self.qualification.get("qualified") is True
        )

    def prepare(self, progress: Callable[[Any], None] | None = None) -> Any:
        if not self.can_prepare():
            raise BackendUnavailableError(
                "Preparation is disabled until Fedora preflight passes and a preparation backend is available."
            )
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        if backend is None:
            raise BackendUnavailableError("The installation backend is unavailable.")
        function = _backend_function(backend, ("prepare", "download_and_prepare"))
        if function is None:
            raise BackendUnavailableError("The preparation backend does not expose prepare().")
        try:
            result = _invoke_prepare(function, self.plan or {}, progress)
        except BackendUnavailableError:
            raise
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise InstallerError("The verified Zeus payload could not be prepared safely.") from error
        if not _result_ok(result):
            message = result.get("message") if isinstance(result, Mapping) else None
            raise InstallerError(_safe_text(message, "The backend refused to prepare this plan."))
        self.preparation = result
        self.prepared = _result_ready(result)
        self.operation_pending = True
        self.operation_blocked = not self.prepared
        return result

    def install(self) -> Any:
        """Run the separately qualified executor after verified staging."""

        if not self.can_install():
            raise BackendUnavailableError(
                "Installation is disabled until a qualified executor is available and a verified payload is staged."
            )
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        if backend is None:
            raise BackendUnavailableError("The installation backend is unavailable.")
        function = _backend_function(backend, ("install", "execute_install"))
        if function is None:
            raise BackendUnavailableError("The qualified backend does not expose install().")
        try:
            # A rebooted operation must continue the root-owned journal plan.
            # Do not forward the newly collected display plan, whose target
            # fingerprint may legitimately differ after the shrink boundary.
            result = function() if self.resume_pending else function(self.plan or {})
        except BackendUnavailableError:
            raise
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise InstallerError("The qualified installer executor could not run safely.") from error
        if not _result_ok(result):
            message = result.get("message") if isinstance(result, Mapping) else None
            raise InstallerError(_safe_text(message, "The backend refused to install this plan."))
        self.installation = result
        self.reboot_ready = _result_reboot_ready(result)
        if _status_phase(result) == "installed":
            self.resume_pending = False
            self.terminal = True
        return result

    def request_restart(self) -> Any:
        if not self.can_restart():
            raise BackendUnavailableError(
                "Restart is available only after the qualified executor reaches a safe boundary or completes installation."
            )
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        if backend is None:
            raise BackendUnavailableError("The installation backend is unavailable.")
        function = _backend_function(backend, ("restart", "request_restart"))
        if function is None:
            raise BackendUnavailableError("The qualified backend does not expose restart().")
        try:
            result = function()
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise InstallerError("The staged installer could not request a safe restart.") from error
        if not _result_ok(result):
            message = result.get("message") if isinstance(result, Mapping) else None
            raise InstallerError(_safe_text(message, "The backend refused to restart."))
        return result

    def supports_cancel(self) -> bool:
        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        return backend is not None and _backend_function(backend, ("cancel",)) is not None

    def cancel(self) -> Any:
        """Request cancellation only through an explicitly provided backend API."""

        backend = self.backend_module if self.backend_module is not None else _load_backend_module()
        function = _backend_function(backend, ("cancel",)) if backend is not None else None
        if function is None:
            raise InstallerError("This step cannot be cancelled. Wait for it to finish.")
        try:
            result = function()
        except Exception as error:  # pragma: no cover - backend-specific failures
            raise InstallerError("The backend could not cancel before its next safe boundary.") from error
        if (
            not isinstance(result, Mapping)
            or result.get("ok") is not True
            or result.get("supported") is False
            or result.get("state") not in {"cancel_requested", "cancelled"}
        ):
            raise InstallerError("Cancellation was not accepted. Wait for the current step to finish.")
        return result


def _format_disk(plan: Mapping[str, Any] | None) -> str:
    inventory = plan.get("inventory") if isinstance(plan, Mapping) else None
    if not isinstance(inventory, Mapping):
        return "Disk details will appear after read-only preflight."
    disk = inventory.get("disk") or inventory.get("target_disk")
    if not isinstance(disk, Mapping):
        candidates = inventory.get("block_devices") or inventory.get("devices")
        if isinstance(candidates, Mapping):
            candidates = candidates.get("blockdevices") or candidates.get("devices")
        if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
            disks = [
                candidate
                for candidate in candidates
                if isinstance(candidate, Mapping)
                and _safe_text(candidate.get("type")).lower() in {"disk", "nvme", "drive"}
            ]
            if len(disks) == 1:
                disk = disks[0]
    if not isinstance(disk, Mapping):
        return "The preflight worker did not provide a target disk identity."
    fields: list[str] = []
    for key in ("model", "device", "path", "size_gib", "capacity_gib", "serial", "id"):
        value = disk.get(key)
        if value not in (None, ""):
            fields.append(f"{key.replace('_', ' ').title()}: {_safe_text(value)}")
    return " · ".join(fields) if fields else "Disk identity was not reported by preflight."


def _format_target(plan: Mapping[str, Any] | None) -> str:
    target = None
    if isinstance(plan, Mapping):
        target = plan.get("target") or plan.get("proposed_target_layout")
    if not isinstance(target, Mapping):
        return "The target layout will appear after preflight."
    def format_gib(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return _safe_text(value)
        return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"

    values: list[str] = []
    allocation = target.get("allocation_gib", target.get("zeus_allocation_gib"))
    if allocation not in (None, ""):
        values.append(f"Allocation: {format_gib(allocation)} GiB")

    # The storage plan's partition roles are authoritative.  In particular,
    # Zeus home is normally a directory/subvolume inside the root partition;
    # an ESP's one-GiB size must never be presented as a separate home.
    parts = target.get("new_partitions") or target.get("partitions")
    role_sizes: dict[str, Any] = {}
    if isinstance(parts, Sequence) and not isinstance(parts, (str, bytes)):
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            role = _safe_text(part.get("role") or part.get("purpose")).lower().replace("_", "-")
            if role in {"efi", "esp", "boot-efi"}:
                role = "esp"
            elif role in {"boot", "root", "home", "var-home"}:
                role = "home" if role == "var-home" else role
            else:
                continue
            size = part.get("size_gib")
            if size in (None, ""):
                size = part.get("gib")
            if size in (None, ""):
                raw_bytes = part.get("size_bytes")
                try:
                    size = float(raw_bytes) / (1024**3) if raw_bytes is not None else None
                except (TypeError, ValueError, OverflowError):
                    size = None
            if size not in (None, "") and role not in role_sizes:
                role_sizes[role] = size
    for role, label_text in (("esp", "ESP"), ("boot", "Boot"), ("root", "Root"), ("home", "Home")):
        if role in role_sizes:
            values.append(f"{label_text}: {format_gib(role_sizes[role])} GiB")

    # Older plans may expose scalar partition sizes without the role list.
    # Preserve those explicit fields as a compatibility fallback.
    if not role_sizes:
        for key, label_text in (("root_gib", "Root"), ("home_gib", "Home"), ("boot_gib", "Boot"), ("esp_gib", "ESP")):
            value = target.get(key)
            if value not in (None, ""):
                values.append(f"{label_text}: {format_gib(value)} GiB")
    return " · ".join(values) if values else "Preflight will identify newly allocated Zeus space."


def _format_fedora(plan: Mapping[str, Any] | None) -> str:
    """Describe the Fedora resources carried through the review plan."""

    inventory = plan.get("inventory") if isinstance(plan, Mapping) else None
    if not isinstance(inventory, Mapping):
        return "Fedora preservation details will appear after read-only preflight."
    mounts = inventory.get("mounts") or inventory.get("findmnt")
    retained: list[str] = []
    if isinstance(mounts, Mapping):
        mounts = mounts.get("filesystems") or mounts.get("mounts")
    if isinstance(mounts, Sequence) and not isinstance(mounts, (str, bytes)):
        for mount in mounts:
            if not isinstance(mount, Mapping):
                continue
            target = _safe_text(mount.get("target", mount.get("mountpoint")))
            fstype = _safe_text(mount.get("fstype", mount.get("filesystem")))
            if target in {"/", "/home", "/var/home", "/boot", "/boot/efi", "/efi"}:
                retained.append(f"{target} ({fstype or 'filesystem'})")
    if retained:
        return "Fedora preserved: " + ", ".join(retained) + "; existing users, files and EFI entries stay in place."
    return "Fedora preservation: existing root/home subvolumes, users, files and EFI entries remain untouched by this review."


def _load_gtk() -> tuple[Any | None, Any, Any, Any]:
    """Load GTK4 and the optional libadwaita layer.

    Fedora builds normally include libadwaita, but the launcher is also
    useful on a minimal Fedora rescue/live environment where only GTK4 is
    present.  Keep the application usable there with the equivalent GTK
    primitives; importing the CLI still never loads either GI namespace.
    """

    try:
        import gi

        os.environ.setdefault("GDK_DEBUG", "no-portals")
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
    except (ImportError, RuntimeError, ValueError) as error:
        raise GtkUnavailableError("GTK4 and PyGObject are required for the graphical launcher.") from error

    try:
        gi.require_version("Adw", "1")
        from gi.repository import Adw
    except (ImportError, RuntimeError, ValueError):
        # libadwaita is a visual enhancement, not a requirement for the
        # review-only launcher.  _make_application selects GTK equivalents.
        Adw = None
    return Adw, GLib, Gtk, gi


def _make_application(controller: InstallerController, gtk_parts: tuple[Any, Any, Any, Any]) -> Any:
    Adw, GLib, Gtk, _gi = gtk_parts

    # Keep the fallback GTK-only build legible when the distribution has no
    # libadwaita theme classes.  The same small set of names is harmless when
    # Adwaita is present and gives the review cards a clear hierarchy.
    try:
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_data(
                b"""
                .display-4 { font-size: 24pt; font-weight: 700; }
                .title-2 { font-weight: 700; }
                .body { font-size: 11pt; }
                .caption { font-size: 9pt; opacity: 0.78; }
                .card { padding: 12px; border-radius: 10px; background-color: rgba(127,127,127,0.12); }
                .error { color: #c01c28; }
                """
            )
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except (ImportError, AttributeError, RuntimeError, TypeError):
        pass

    def label(text: str, *, css: str = "", wrap: bool = True) -> Any:
        widget = Gtk.Label(label=text)
        widget.set_xalign(0)
        if wrap:
            widget.set_wrap(True)
        if css:
            widget.add_css_class(css)
        return widget

    application_window_type = Adw.ApplicationWindow if Adw is not None else Gtk.ApplicationWindow
    application_type = Adw.Application if Adw is not None else Gtk.Application

    class InstallerWindow(application_window_type):
        def __init__(self, application: Any) -> None:
            super().__init__(application=application, title="Install Zeus OS alongside Fedora")
            # Keep the review surface large enough for the complete action
            # row at the native GTK scale.  The scroller still allows the
            # detailed blocker list to grow on smaller displays.
            self.set_default_size(900, 760)
            self.set_size_request(680, 600)
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            self._busy = False

            toolbar = Adw.ToolbarView() if Adw is not None else Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            header = Adw.HeaderBar() if Adw is not None else Gtk.HeaderBar()
            header.set_title_widget(label("Zeus OS installer", css="title-2", wrap=False))
            if Adw is not None:
                toolbar.add_top_bar(header)
            else:
                toolbar.append(header)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            scroller.set_child(self._content())
            if Adw is not None:
                self._toast = Adw.ToastOverlay()
                self._toast.set_child(scroller)
                toolbar.set_content(self._toast)
                self.set_content(toolbar)
            else:
                self._toast = None
                toolbar.append(scroller)
                self.set_child(toolbar)

            self._set_busy(True, "Running read-only Fedora preflight…")
            self._submit(self._refresh_worker, self._apply_refresh)

        def _content(self) -> Any:
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            content.set_margin_top(24)
            content.set_margin_bottom(28)
            content.set_margin_start(28)
            content.set_margin_end(28)

            content.append(label("Install Zeus OS alongside Fedora", css="display-4", wrap=False))
            content.append(
                label(
                    f"Review the detected layout, choose the total Zeus allocation, and prepare a separate boot entry. Fedora stays preserved. {PRODUCT_VERSION}",
                    css="body",
                )
            )

            allocation_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            allocation_row.append(label("Total Zeus allocation (GiB)", wrap=False))
            adjustment = Gtk.Adjustment.new(DEFAULT_ALLOCATION_GIB, 1, 4096, 1, 16, 0)
            self._allocation_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
            self._allocation_spin.set_value(controller.allocation_gib)
            self._allocation_spin.set_numeric(True)
            self._allocation_spin.set_width_chars(8)
            self._allocation_spin.connect("value-changed", self._allocation_changed)
            allocation_row.append(self._allocation_spin)
            allocation_row.append(label("Includes Zeus root, home, /boot and its boot data.", wrap=True))
            content.append(allocation_row)

            self._disk = label("Disk details will appear after read-only preflight.")
            self._disk.add_css_class("card")
            content.append(self._disk)
            self._fedora = label(_format_fedora(None))
            content.append(self._fedora)
            self._target = label("The target layout will appear after preflight.")
            content.append(self._target)

            self._blockers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            content.append(self._blockers)

            self._status = label("Preflight has not run yet.")
            self._status.add_css_class("dim-label")
            content.append(self._status)
            self._progress = Gtk.ProgressBar()
            self._progress.set_show_text(True)
            self._progress.set_text("Waiting for preflight")
            content.append(self._progress)

            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            self._refresh_button = Gtk.Button(label="Refresh preflight")
            self._refresh_button.connect("clicked", self._refresh_clicked)
            buttons.append(self._refresh_button)
            self._prepare_button = Gtk.Button(label="Download and prepare")
            self._prepare_button.add_css_class("suggested-action")
            self._prepare_button.connect("clicked", self._prepare_clicked)
            self._prepare_button.set_sensitive(False)
            buttons.append(self._prepare_button)
            self._install_button = Gtk.Button(label="Install into new space")
            self._install_button.connect("clicked", self._install_clicked)
            self._install_button.set_sensitive(False)
            buttons.append(self._install_button)
            content.append(buttons)

            action_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            self._restart_button = Gtk.Button(label="Restart Fedora to continue")
            self._restart_button.connect("clicked", self._restart_clicked)
            self._restart_button.set_sensitive(False)
            action_buttons.append(self._restart_button)
            self._cancel_button = Gtk.Button(label="Cancel")
            self._cancel_button.connect("clicked", self._cancel_clicked)
            self._cancel_button.set_sensitive(False)
            self._cancel_button.set_visible(controller.supports_cancel())
            action_buttons.append(self._cancel_button)
            content.append(action_buttons)

            self._footer = label(
                "Review the layout, then download Zeus. Installation availability follows testing.",
                css="caption",
            )
            content.append(self._footer)
            return content

        def _allocation_changed(self, _spin: Any) -> None:
            if self._busy:
                return
            self._prepare_button.set_sensitive(False)
            self._install_button.set_sensitive(False)
            self._restart_button.set_sensitive(False)
            self._status.set_text("Allocation changed. Refresh preflight to review this plan.")

        def _set_busy(self, busy: bool, status: str | None = None) -> None:
            self._busy = busy
            self._refresh_button.set_sensitive(not busy)
            self._allocation_spin.set_sensitive(
                not busy
                and not controller.resume_pending
                and not controller.operation_pending
                and not controller.prepared
                and not controller.terminal
            )
            if controller.terminal:
                self._restart_button.set_label("Restart to choose an OS")
            else:
                self._restart_button.set_label(
                    "Continue installation"
                    if controller.resume_pending
                    else "Restart Fedora to continue"
                )
            self._install_button.set_label(
                "Continue installation" if controller.resume_pending else "Install into new space"
            )
            self._prepare_button.set_sensitive(not busy and controller.can_prepare())
            self._install_button.set_sensitive(not busy and controller.can_install())
            self._restart_button.set_sensitive(
                not busy and controller.can_restart()
            )
            self._cancel_button.set_sensitive(
                busy and controller.preparation is not None and controller.supports_cancel()
            )
            if status:
                self._status.set_text(status)

        def _refresh_worker(self, allocation_gib: int | None = None) -> dict[str, Any]:
            allocation = controller.allocation_gib if allocation_gib is None else allocation_gib
            return controller.refresh(allocation)

        def _submit(self, function: Callable[[], Any], callback: Callable[[Any, Exception | None], None]) -> None:
            future = self._executor.submit(function)

            def done(completed: concurrent.futures.Future[Any]) -> None:
                try:
                    result = completed.result()
                    error = None
                except Exception as caught:  # pragma: no cover - GTK runtime path
                    result = None
                    error = caught
                GLib.idle_add(callback, result, error)

            future.add_done_callback(done)

        def _refresh_clicked(self, _button: Any) -> None:
            allocation = int(self._allocation_spin.get_value())
            self._set_busy(True, "Running read-only Fedora preflight…")
            self._submit(lambda: self._refresh_worker(allocation), self._apply_refresh)

        def _apply_refresh(self, plan: Any, error: Exception | None) -> bool:
            if error is not None:
                self._set_busy(False, f"Preflight unavailable: {error}")
                self._render_blockers([str(error)])
                return False
            self._render_plan(plan)
            self._set_busy(False)
            return False

        def _render_blockers(self, blockers: Sequence[str]) -> None:
            child = self._blockers.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                self._blockers.remove(child)
                child = next_child
            for blocker in blockers:
                row = Gtk.Label(label=f"• {blocker}")
                row.set_xalign(0)
                row.set_wrap(True)
                row.add_css_class("error")
                self._blockers.append(row)

        def _render_plan(self, plan: Mapping[str, Any]) -> None:
            self._disk.set_text(_format_disk(plan))
            self._fedora.set_text(_format_fedora(plan))
            self._target.set_text(f"Planned Zeus target: {_format_target(plan)}")
            blockers = plan_blockers(plan)
            self._render_blockers(blockers)
            qualification = controller.qualification
            if controller.terminal:
                if qualification.get("qualified") is True:
                    self._status.set_text("Installation complete. Restart to choose Fedora or Zeus.")
                else:
                    self._status.set_text(
                        "Installation complete. Restart is unavailable because this installer build has not completed testing."
                    )
                self._progress.set_fraction(1.0)
                self._progress.set_text("Installation complete")
                if qualification.get("qualified") is True:
                    self._footer.set_text("Fedora remains the default choice.")
                else:
                    self._footer.set_text(
                        "Restart is unavailable because this installer build has not completed testing."
                    )
            elif controller.resume_pending:
                self._status.set_text(
                    "A maintenance operation is waiting after reboot. Continue installation to resume the recorded plan."
                )
                self._footer.set_text(
                    "Continue uses the original root-owned journal plan after the Fedora reboot boundary."
                )
            elif controller.resume_blocked:
                self._status.set_text(
                    "The recorded reboot boundary could not be verified. Restart and continuation are disabled pending review."
                )
                self._footer.set_text("The recorded operation needs review before another restart or continuation.")
            elif controller.operation_blocked:
                phase = _safe_text(controller.resume_status.get("phase")) if controller.resume_status else ""
                self._status.set_text(
                    f"An existing installer operation is at {phase or 'an unknown phase'}. Review it before starting another action."
                )
                if controller.last_error:
                    self._status.set_text(controller.last_error)
                self._footer.set_text("The existing journal operation must reach a safe boundary before another action.")
            elif controller.prepared:
                self._status.set_text("Download verified. Ready to install.")
                if qualification.get("qualified") is True:
                    self._footer.set_text("Install Zeus when you are ready.")
                else:
                    self._footer.set_text(
                        "Install is unavailable because this installer build has not completed testing."
                    )
            elif controller.reboot_ready:
                self._status.set_text(
                    "Restart Fedora to continue installing Zeus."
                )
                self._footer.set_text("Restart Fedora to continue. Fedora remains the default boot choice.")
            elif blockers:
                self._status.set_text("Preflight blocked this layout. Resolve the listed issue before continuing.")
                self._footer.set_text("Resolve the listed read-only preflight issue before preparation.")
            elif qualification.get("qualified") is True and plan_is_supported(plan):
                self._status.set_text("Ready to download Zeus.")
                self._footer.set_text("Download the verified installer when you are ready.")
            elif plan_is_supported(plan):
                self._status.set_text("Ready to download Zeus.")
                self._footer.set_text(
                    "Installation is unavailable because this installer build has not completed testing."
                )
            else:
                self._status.set_text("This Fedora layout is not supported for dual-boot preparation.")
                self._footer.set_text("No storage changes were made. Review the preflight blockers above.")
            if not controller.terminal:
                if controller.prepared:
                    self._progress.set_fraction(1.0)
                    self._progress.set_text("Verified installer staged")
                else:
                    self._progress.set_fraction(0.0)
                    self._progress.set_text("Ready for review")

        def _prepare_clicked(self, _button: Any) -> None:
            allocation = int(self._allocation_spin.get_value())
            self._set_busy(True, "Rechecking the target before preparation…")
            self._progress.set_fraction(0.0)
            self._progress.set_text("Preparing verified payload")

            def worker() -> Any:
                plan = controller.refresh(allocation)
                if not controller.can_prepare():
                    raise BackendUnavailableError(
                        "Preparation is disabled until Fedora preflight passes and a preparation backend is available."
                    )

                def progress(value: Any) -> None:
                    GLib.idle_add(self._progress_update, value)

                result = controller.prepare(progress=progress)
                return plan, result

            self._submit(worker, self._apply_preparation)

        def _progress_update(self, value: Any) -> bool:
            if isinstance(value, Mapping):
                done = value.get("bytes", value.get("completed"))
                total = value.get("total")
                try:
                    if total and float(total) > 0:
                        fraction = max(0.0, min(1.0, float(done or 0) / float(total)))
                        self._progress.set_fraction(fraction)
                        self._progress.set_text(f"Preparing verified payload ({int(fraction * 100)}%)")
                        return False
                except (TypeError, ValueError):
                    pass
            self._progress.pulse()
            return False

        def _apply_preparation(self, value: Any, error: Exception | None) -> bool:
            if error is not None:
                self._set_busy(False, f"Preparation stopped safely: {error}")
                self._restart_button.set_sensitive(False)
                return False
            plan, result = value
            self._render_plan(plan)
            if controller.prepared:
                self._progress.set_fraction(1.0)
                self._progress.set_text("Verified installer staged")
                self._status.set_text("Download verified. Ready to install.")
            else:
                state = result.get("state") if isinstance(result, Mapping) else "preparing"
                self._status.set_text(
                    f"Backend state: {_safe_text(state, 'preparing')}. Installation remains disabled until staging is ready."
                )
            self._set_busy(False)
            return False

        def _install_clicked(self, _button: Any) -> None:
            self._set_busy(True, "Installing Zeus…")
            self._progress.set_fraction(0.0)
            self._progress.set_text("Installing Zeus…")

            def worker() -> Any:
                if not controller.can_install():
                    raise BackendUnavailableError(
                        "Installation is disabled until a qualified executor is available and a verified payload is staged."
                    )
                result = controller.install()
                return controller.plan or {}, result

            self._submit(worker, self._apply_installation)

        def _apply_installation(self, value: Any, error: Exception | None) -> bool:
            if error is not None:
                self._set_busy(False, f"Installation stopped safely: {error}")
                self._restart_button.set_sensitive(False)
                return False
            plan, result = value
            self._render_plan(plan)
            if controller.reboot_ready:
                self._progress.set_fraction(1.0)
                self._progress.set_text("Maintenance reboot ready")
                self._status.set_text("Restart Fedora to continue installing Zeus.")
            else:
                phase = "unknown"
                if isinstance(result, Mapping):
                    phase = _safe_text(result.get("phase")) or _safe_text(result.get("state"))
                if phase.lower() == "installed":
                    self._progress.set_fraction(1.0)
                    self._progress.set_text("Installation complete")
                    self._status.set_text("Installation complete. Restart to choose Fedora or Zeus.")
                else:
                    self._progress.set_text("Installation state requires review")
                    self._status.set_text(
                        f"Executor state: {_safe_text(phase, 'unknown')}. Restart remains disabled until it reports reboot_required."
                    )
            self._set_busy(False)
            return False

        def _restart_clicked(self, _button: Any) -> None:
            self._set_busy(True, "Requesting the explicit restart…")
            self._submit(controller.request_restart, self._apply_restart)

        def _apply_restart(self, _result: Any, error: Exception | None) -> bool:
            if error is not None:
                self._set_busy(False, f"Restart was not requested: {error}")
            else:
                if controller.terminal:
                    self._set_busy(False, "Restart requested. Choose Fedora or Zeus; Fedora remains the default.")
                else:
                    self._set_busy(False, "Restart requested. Reopen Zeus Installer in Fedora to continue the recorded operation.")
            return False

        def _cancel_clicked(self, _button: Any) -> None:
            try:
                controller.cancel()
            except InstallerError as error:
                self._status.set_text(str(error))
                return
            self._status.set_text("Cancellation requested. Wait for the current step to stop safely.")

    class InstallerApplication(application_type):
        def __init__(self) -> None:
            super().__init__(application_id=APPLICATION_ID)

        def do_activate(self) -> None:
            window = self.props.active_window
            if window is None:
                window = InstallerWindow(self)
            window.present()

    return InstallerApplication()


def launch_gui(*, allocation_gib: int = DEFAULT_ALLOCATION_GIB) -> int:
    """Run the graphical launcher and return its GTK application status."""

    allocation = validate_allocation(allocation_gib)
    gtk_parts = _load_gtk()
    application = _make_application(InstallerController(allocation_gib=allocation), gtk_parts)
    return int(application.run([]))


__all__ = [
    "APPLICATION_ID",
    "BackendUnavailableError",
    "GtkUnavailableError",
    "InstallerController",
    "backend_qualification",
    "launch_gui",
    "plan_blockers",
    "plan_is_supported",
]
