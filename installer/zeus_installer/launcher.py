"""Unprivileged removal review model for the Fedora launcher.

The existing GTK installer intentionally keeps storage mutation behind the
root-owned helper.  This module supplies the owner-facing removal model
without importing GTK, reading a disk, or accepting a command/path from the
desktop process.  A caller supplies an already collected Fedora inventory and
the journal-backed :mod:`zeus_installer.removal` planner does all identity and
ownership checks.

The safe default is ``menu_only``: remove the installer-owned Fedora GRUB
snippet and regenerate Fedora's menu while retaining every Zeus partition and
all Zeus data.  Full removal is a separate, qualified choice.  It requires
the exact plan ID and explicitly reports that data on the three journal-owned
partitions will be deleted; Fedora's ESP, /boot, root, and default boot path
are retained, and free space is never reallocated automatically.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import removal


class RemovalUiError(removal.RemovalError):
    """A safe, owner-facing removal review error."""


def _ui_error(message: str) -> RemovalUiError:
    return RemovalUiError("invalid_removal_review", message)


def removal_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return sanitized copy and confirmation facts for a reviewed plan.

    The returned mapping contains no executable command or arbitrary path from
    the caller.  Paths and operations are sourced from
    :func:`removal.validate_plan`, which accepts only the fixed installer-owned
    values and journal-bound partition identities.
    """

    try:
        checked = removal.validate_plan(plan)
    except removal.RemovalError:
        raise
    mode = checked["mode"]
    destructive = mode == removal.DESTRUCTIVE
    data = checked["data"]
    fedora = checked["fedora_preservation"]
    resources = checked["zeus_resources"]
    return {
        "title": "Remove Zeus OS" if destructive else "Remove Zeus from Fedora's boot menu",
        "mode": mode,
        "plan_id": checked["plan_id"],
        "summary": (
            "Remove only the installer-owned Zeus boot-menu entry; Zeus partitions and data stay available."
            if not destructive
            else "Delete only the three journal-owned Zeus partitions after the exact confirmation."
        ),
        "data_policy": data["policy"],
        "data_action": data["action"],
        "data_warning": data["warning"],
        "zeus_resources": [
            {
                "number": item["number"],
                "path": item["path"],
                "guid": item["guid"],
                "owner": item["owner"],
            }
            for item in resources
        ],
        "fedora_preserved": {
            "default_boot": fedora["default_boot"],
            "esp": fedora["retain_esp"],
            "boot": fedora["retain_boot"],
            "root": fedora["retain_root"],
            "other_entries": fedora["other_entries"],
        },
        "automatic_space_reclaim": checked["space"]["automatic_reclaim"],
        "confirmation_required": destructive,
        "confirmation_phrase": checked["plan_id"] if destructive else None,
        "operations": [operation["kind"] for operation in checked["operations"]],
    }


# Readable aliases make the model discoverable to existing launcher callers.
format_removal = removal_summary
removal_review = removal_summary


class RemovalController:
    """Small stateful adapter for a graphical or textual launcher.

    ``planner`` and ``remover`` default to the pure module seams.  Tests and a
    privileged integration can inject a journal-backed facade without giving
    this unprivileged model a way to execute arbitrary commands.
    """

    def __init__(
        self,
        *,
        planner: Callable[..., Mapping[str, Any]] = removal.build_plan,
        remover: Callable[..., Mapping[str, Any]] = removal.remove,
        executor: Any | None = None,
    ) -> None:
        if not callable(planner) or not callable(remover):
            raise _ui_error("The removal planner and executor seams are unavailable.")
        self.planner = planner
        self.remover = remover
        self.executor = executor
        self.plan: dict[str, Any] | None = None

    def review(
        self,
        journal: Any,
        inventory: Mapping[str, Any],
        *,
        mode: str = removal.MENU_ONLY,
        data_policy: str | None = None,
        delete_data: bool | None = None,
        preserve_data: bool | None = None,
        confirm_plan_id: str | None = None,
        require_root: bool = True,
    ) -> dict[str, Any]:
        """Build and validate a read-only removal review."""

        value = self.planner(
            journal,
            inventory,
            mode=mode,
            confirm_plan_id=confirm_plan_id,
            data_policy=data_policy,
            delete_data=delete_data,
            preserve_data=preserve_data,
            require_root=require_root,
        )
        if not isinstance(value, Mapping):
            raise _ui_error("The removal planner returned an invalid review.")
        self.plan = removal.validate_plan(value)
        return self.plan

    def summary(self) -> dict[str, Any]:
        """Return the current owner-facing review or fail closed."""

        if self.plan is None:
            raise _ui_error("Review the current Fedora installation before removing Zeus.")
        return removal_summary(self.plan)

    def can_apply(self) -> bool:
        """Whether an explicitly qualified executor was supplied."""

        return self.plan is not None and self.executor is not None and getattr(self.executor, "qualified", False) is True

    def apply(
        self,
        journal: Any,
        inventory: Mapping[str, Any],
        *,
        confirm_plan_id: str | None = None,
        require_root: bool = True,
    ) -> dict[str, Any]:
        """Apply only the already reviewed fixed operations.

        A destructive review displays the exact plan ID as the confirmation
        phrase.  The module boundary still re-plans against fresh inventory
        and the root-owned journal before invoking the injected executor.
        """

        if self.plan is None:
            raise _ui_error("Review the current Fedora installation before removing Zeus.")
        if not self.can_apply():
            raise _ui_error("Removal is unavailable until a qualified fixed executor is supplied.")
        checked = removal.validate_plan(self.plan)
        if checked["mode"] == removal.DESTRUCTIVE and confirm_plan_id != checked["plan_id"]:
            raise removal.RemovalError(
                "confirmation_required",
                "Type the exact plan ID shown in the removal review to delete Zeus data.",
            )
        result = self.remover(
            journal,
            inventory,
            mode=checked["mode"],
            confirm_plan_id=confirm_plan_id,
            executor=self.executor,
            apply=True,
            data_policy=checked["data_policy"],
            require_root=require_root,
        )
        if not isinstance(result, Mapping):
            raise _ui_error("The removal executor returned an invalid result.")
        return dict(result)

    def cancel(self) -> dict[str, Any]:
        """Cancel review without mutating the journal, files, or partitions."""

        return {
            "schema_version": removal.REMOVAL_SCHEMA_VERSION,
            "ok": True,
            "state": "cancelled",
            "cancelled": True,
            "mutated": False,
            "operations": [],
        }


# ``RemovalReviewModel`` is a descriptive compatibility name for UI code.
RemovalReviewModel = RemovalController


__all__ = [
    "RemovalController",
    "RemovalReviewModel",
    "RemovalUiError",
    "format_removal",
    "removal_review",
    "removal_summary",
]
