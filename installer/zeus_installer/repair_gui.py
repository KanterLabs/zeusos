"""GTK4 screen for updating an existing Zeus installation.

The existing-install update is deliberately a small, standalone client.  It
does not use the dual-boot installer controller and it does not expose any
user-selected command, path, or release URL.  The one privileged operation is
the fixed helper invocation in :data:`UPDATE_COMMAND`; its stdout is a
newline-delimited stream of advisory stage/byte events followed by one final
result object.

GTK and PyGObject are imported only when :func:`launch_update_existing` is
called.  This keeps ``zeus-installer preflight`` and other CLI commands usable
on systems without the graphical dependencies.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import InstallerError


APPLICATION_ID = "org.zeus.Installer.UpdateExisting"
HELPER_COMMAND = "/usr/libexec/zeus-installer-helper"
PKEXEC_COMMAND = "/usr/bin/pkexec"

# Keep every item fixed in source.  In particular, this module never accepts
# an archive path, URL, shell command, or build identifier from the desktop
# user.  ``list()`` is used at the process boundary so a caller cannot mutate
# this module constant through a subprocess implementation.
UPDATE_COMMAND = [
    PKEXEC_COMMAND,
    "--disable-internal-agent",
    HELPER_COMMAND,
    "update_existing",
    "--progress-json",
]

_MAX_PROTOCOL_LINE = 64 * 1024
_MAX_STAGE_LENGTH = 128
_MAX_MESSAGE_LENGTH = 1024


class GtkUnavailableError(InstallerError):
    """Raised when GTK4/PyGObject is unavailable for the update screen."""


class UpdateError(InstallerError):
    """Raised when the fixed helper cannot produce a safe update result."""


class UpdateProtocolError(UpdateError):
    """Raised for malformed or unsupported helper stream records."""


_STAGE_LABELS = {
    "checking": "Checking for an existing Zeus installation…",
    "checking_target": "Checking the existing Zeus installation…",
    "checking_existing": "Checking the existing Zeus installation…",
    "checking_installation": "Checking the existing Zeus installation…",
    "checking_release": "Checking for a Zeus update…",
    "connecting": "Connecting to the Zeus release service…",
    "downloading": "Downloading the Zeus update…",
    "verifying": "Verifying the Zeus update…",
    "staging": "Staging the Zeus update…",
    "copying_update": "Staging the Zeus update…",
    "staged": "Update staged for offline application…",
    "applying": "Preparing the offline update…",
    "finalizing": "Finishing update preparation…",
    "ready": "Update ready to apply in Zeus…",
}


def _bounded_text(value: Any, fallback: str, *, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text:
        return fallback
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _decode_line(line: str | bytes) -> str:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UpdateProtocolError("The update helper returned invalid text.") from error
    if not isinstance(line, str):
        raise UpdateProtocolError("The update helper returned an invalid response line.")
    if len(line.encode("utf-8", errors="replace")) > _MAX_PROTOCOL_LINE:
        raise UpdateProtocolError("The update helper response was too large.")
    return line.strip()


def parse_event(line: str | bytes) -> dict[str, Any] | None:
    """Validate and return one helper stream record.

    Blank lines are ignored.  Stage and progress records are normalized to the
    small protocol the window consumes.  A final record is recognized by its
    literal boolean ``ok`` field and otherwise retained so its owner-facing
    message and optional build ID can be shown.  Booleans are rejected as
    byte counters because Python considers them integers.
    """

    text = _decode_line(line)
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise UpdateProtocolError("The update helper returned invalid JSON.") from error
    if not isinstance(value, Mapping):
        raise UpdateProtocolError("The update helper returned an invalid response.")
    record = dict(value)

    if "ok" in record:
        if type(record.get("ok")) is not bool:
            raise UpdateProtocolError("The update helper returned an invalid final result.")
        # Keep final diagnostics bounded before they reach a label.  The
        # original mapping remains useful to callers, while non-string values
        # simply receive the safe fallback at display time.
        return record

    event = record.get("event")
    if event == "stage":
        stage = record.get("stage")
        if not isinstance(stage, str) or not stage.strip() or len(stage) > _MAX_STAGE_LENGTH:
            raise UpdateProtocolError("The update helper returned an invalid stage.")
        return {"event": "stage", "stage": stage.strip()}

    if event == "progress":
        progress = record.get("progress")
        if not isinstance(progress, Mapping):
            raise UpdateProtocolError("The update helper returned invalid download progress.")
        done = progress.get("bytes")
        total = progress.get("total")
        if (
            type(done) is not int
            or type(total) is not int
            or total <= 0
            or done < 0
            or done > total
        ):
            raise UpdateProtocolError("The update helper returned invalid download progress.")
        return {"event": "progress", "progress": {"bytes": done, "total": total}}

    raise UpdateProtocolError("The update helper returned an unknown event.")


def _is_final(value: Mapping[str, Any]) -> bool:
    return type(value.get("ok")) is bool


def _helper_failure_message(returncode: Any) -> str:
    if returncode in {126, 127}:
        return "Administrator approval was not completed. Try again when ready."
    return "The existing Zeus update did not complete safely."


def run_update_existing(
    on_event: Callable[[Mapping[str, Any]], Any] | None = None,
    *,
    popen_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the fixed helper and return its final structured result.

    The caller should invoke this function on a worker thread.  It waits for
    the helper without imposing a short client timeout because downloading and
    staging can take up to an hour.  Invalid stream lines are remembered while
    the process is drained, preventing a malformed advisory event from
    deadlocking or truncating the privileged operation.
    """

    factory = popen_factory or subprocess.Popen
    try:
        process = factory(
            list(UPDATE_COMMAND),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            shell=False,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        raise UpdateError("The privileged Zeus update helper is unavailable.") from error

    stream = getattr(process, "stdout", None)
    if stream is None:
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except (OSError, ValueError, TypeError):
                pass
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait()
            except (OSError, subprocess.SubprocessError, TypeError, ValueError):
                pass
        raise UpdateError("The privileged Zeus update helper returned no output stream.")

    final: dict[str, Any] | None = None
    protocol_error: UpdateProtocolError | None = None
    try:
        for line in _iter_lines(stream):
            try:
                event = parse_event(line)
            except UpdateProtocolError as error:
                if protocol_error is None:
                    protocol_error = error
                continue
            if event is None:
                continue
            if _is_final(event):
                if final is not None and protocol_error is None:
                    protocol_error = UpdateProtocolError(
                        "The update helper returned more than one final result."
                    )
                else:
                    final = event
                continue
            if final is not None:
                if protocol_error is None:
                    protocol_error = UpdateProtocolError(
                        "The update helper returned events after its final result."
                    )
                continue
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    # UI delivery is advisory; a callback failure must not
                    # change the helper's operation or prevent stream drain.
                    pass
    finally:
        try:
            returncode = process.wait()
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
            raise UpdateError("The existing Zeus update did not finish safely.") from error
        try:
            stream.close()
        except (OSError, ValueError, AttributeError):
            pass

    if protocol_error is not None:
        raise protocol_error
    if final is None:
        raise UpdateError(_helper_failure_message(returncode))
    if final.get("ok") is not True and not _bounded_text(
        final.get("message"), "", limit=_MAX_MESSAGE_LENGTH
    ):
        final["message"] = _helper_failure_message(returncode)
    if final.get("ok") is True and returncode != 0:
        raise UpdateError("The existing Zeus update returned an invalid completion status.")
    return final


def _iter_lines(stream: Any) -> Iterable[str | bytes]:
    """Iterate a helper stdout stream without assuming text mode in fixtures."""

    try:
        iterator = iter(stream)
    except TypeError as error:
        raise UpdateError("The update helper returned an invalid output stream.") from error
    for line in iterator:
        yield line


def format_stage(stage: Any) -> str:
    """Return a readable stage label without exposing arbitrary long text."""

    value = _bounded_text(stage, "Working on the Zeus update…", limit=_MAX_STAGE_LENGTH)
    key = value.lower().replace("-", "_").replace(" ", "_")
    if key in _STAGE_LABELS:
        return _STAGE_LABELS[key]
    words = value.replace("_", " ").replace("-", " ").split()
    if not words:
        return "Working on the Zeus update…"
    return " ".join(words).capitalize() + "…"


def format_bytes(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError, OverflowError):
        return "unknown size"
    if amount < 0:
        return "unknown size"
    if amount < 1024:
        return f"{amount} B"
    display = float(amount)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        display /= 1024
        if display < 1024 or unit == "TiB":
            return f"{display:.1f} {unit}"
    return f"{amount} B"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    if minutes:
        return f"Elapsed: {minutes}m {remainder:02d}s"
    return f"Elapsed: {remainder}s"


def _load_gtk() -> tuple[Any, Any]:
    try:
        import gi

        os.environ.setdefault("GDK_DEBUG", "no-portals")
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
    except (ImportError, RuntimeError, ValueError) as error:
        raise GtkUnavailableError("GTK4 and PyGObject are required for the existing-install update screen.") from error
    return GLib, Gtk


def _make_application(gtk_parts: tuple[Any, Any]) -> Any:
    GLib, Gtk = gtk_parts

    class UpdateWindow(Gtk.ApplicationWindow):
        def __init__(self, application: Any) -> None:
            super().__init__(application=application, title="Update existing Zeus")
            self.set_default_size(720, 560)
            self.set_size_request(520, 440)
            self._closed = False
            self._busy = False
            self._completed = False
            self._operation_token = 0
            self._started_at: float | None = None
            self._timer_id = 0
            self._download_active = False
            self._thread: threading.Thread | None = None
            self.connect("close-request", self._close_request)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            root.set_margin_top(28)
            root.set_margin_bottom(30)
            root.set_margin_start(30)
            root.set_margin_end(30)

            heading = Gtk.Label(label="Update existing Zeus")
            heading.set_xalign(0)
            heading.set_wrap(True)
            heading.add_css_class("title-1")
            root.append(heading)

            explanation = Gtk.Label(
                label=(
                    "Download the update while you are in Fedora, then boot Zeus to apply it offline. "
                    "Fedora and your existing Zeus files are preserved."
                )
            )
            explanation.set_xalign(0)
            explanation.set_wrap(True)
            root.append(explanation)

            instructions = Gtk.Label(
                label=(
                    "When the update is ready, restart Zeus once more to finish applying it. "
                    "No restart happens automatically."
                )
            )
            instructions.set_xalign(0)
            instructions.set_wrap(True)
            instructions.add_css_class("dim-label")
            root.append(instructions)

            self._status = Gtk.Label(label="Ready to update existing Zeus.")
            self._status.set_xalign(0)
            self._status.set_wrap(True)
            self._status.add_css_class("heading")
            root.append(self._status)

            self._progress = Gtk.ProgressBar()
            self._progress.set_hexpand(True)
            self._progress.set_show_text(True)
            self._progress.set_fraction(0.0)
            self._progress.set_text("Ready to update")
            root.append(self._progress)

            self._elapsed = Gtk.Label(label="Elapsed: 0s")
            self._elapsed.set_xalign(0)
            self._elapsed.add_css_class("dim-label")
            root.append(self._elapsed)

            self._error = Gtk.Label(label="")
            self._error.set_xalign(0)
            self._error.set_wrap(True)
            self._error.add_css_class("error")
            self._error.set_visible(False)
            root.append(self._error)

            self._note = Gtk.Label(label="")
            self._note.set_xalign(0)
            self._note.set_wrap(True)
            self._note.add_css_class("dim-label")
            self._note.set_visible(False)
            root.append(self._note)

            self._build = Gtk.Label(label="")
            self._build.set_xalign(0)
            self._build.set_wrap(True)
            self._build.add_css_class("dim-label")
            self._build.set_visible(False)
            root.append(self._build)

            self._update_button = Gtk.Button(label="Download and update existing Zeus")
            self._update_button.add_css_class("suggested-action")
            self._update_button.connect("clicked", self._update_clicked)
            root.append(self._update_button)

            self.set_child(root)

        def _close_request(self, *_args: Any) -> bool:
            if self._busy:
                self._status.set_text("Update is still running.")
                self._note.set_text("Keep this window open until the update finishes safely.")
                self._note.set_visible(True)
                return True
            self._closed = True
            self._stop_timer()
            # Closing after the operation completes never requests a reboot
            # and never starts a second operation.
            return False

        def _stop_timer(self) -> None:
            if self._timer_id:
                try:
                    GLib.source_remove(self._timer_id)
                except (AttributeError, RuntimeError, TypeError):
                    pass
                self._timer_id = 0

        def _activity_tick(self) -> bool:
            if self._closed or not self._busy:
                self._timer_id = 0
                return False
            if not self._download_active:
                self._progress.pulse()
            if self._started_at is not None:
                self._elapsed.set_text(format_elapsed(time.monotonic() - self._started_at))
            return True

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._update_button.set_sensitive(not busy and not self._completed)
            if busy:
                self._started_at = time.monotonic()
                self._timer_id = GLib.timeout_add(1000, self._activity_tick)
            else:
                self._stop_timer()
                if self._started_at is not None:
                    self._elapsed.set_text(format_elapsed(time.monotonic() - self._started_at))

        def _queue_event(self, event: Mapping[str, Any], token: int) -> None:
            if self._closed:
                return
            try:
                GLib.idle_add(self._apply_event, dict(event), token)
            except (AttributeError, RuntimeError, TypeError):
                pass

        def _apply_event(self, event: Mapping[str, Any], token: int) -> bool:
            if self._closed or not self._busy or token != self._operation_token:
                return False
            if event.get("event") == "stage":
                self._download_active = False
                stage = format_stage(event.get("stage"))
                self._status.set_text(stage)
                self._progress.set_fraction(0.0)
                self._progress.set_text(stage)
                self._progress.pulse()
                return False
            if event.get("event") == "progress":
                progress = event.get("progress")
                if not isinstance(progress, Mapping):
                    return False
                done = progress.get("bytes")
                total = progress.get("total")
                if (
                    type(done) is not int
                    or type(total) is not int
                    or total <= 0
                    or done < 0
                    or done > total
                ):
                    return False
                self._download_active = True
                fraction = done / total
                percentage = int(fraction * 100)
                progress_text = (
                    f"Downloading the Zeus update… {percentage}% · "
                    f"{format_bytes(done)}/{format_bytes(total)}"
                )
                self._progress.set_fraction(fraction)
                self._progress.set_text(progress_text)
                self._status.set_text("Downloading the Zeus update…")
                return False
            return False

        def _queue_result(self, result: Mapping[str, Any] | None, error: Exception | None, token: int) -> None:
            if self._closed:
                return
            try:
                GLib.idle_add(self._apply_result, result, error, token)
            except (AttributeError, RuntimeError, TypeError):
                pass

        def _run_worker(self, token: int) -> None:
            try:
                result = run_update_existing(
                    on_event=lambda event: self._queue_event(event, token)
                )
                self._queue_result(result, None, token)
            except Exception as error:  # pragma: no cover - GTK runtime path
                self._queue_result(None, error, token)

        def _update_clicked(self, _button: Any) -> None:
            if self._busy or self._completed:
                return
            self._operation_token += 1
            token = self._operation_token
            self._error.set_visible(False)
            self._error.set_text("")
            self._note.set_visible(False)
            self._note.set_text("")
            self._build.set_visible(False)
            self._build.set_text("")
            self._download_active = False
            self._progress.set_fraction(0.0)
            self._progress.set_text("Checking for an existing Zeus installation…")
            self._progress.pulse()
            self._status.set_text("Checking for an existing Zeus installation…")
            self._set_busy(True)
            self._thread = threading.Thread(
                target=self._run_worker,
                args=(token,),
                name="zeus-existing-update",
                daemon=True,
            )
            self._thread.start()

        def _apply_result(
            self,
            result: Mapping[str, Any] | None,
            error: Exception | None,
            token: int,
        ) -> bool:
            if self._closed or not self._busy or token != self._operation_token:
                return False
            self._set_busy(False)
            if error is not None:
                message = _bounded_text(
                    str(error),
                    "The existing Zeus update did not complete safely.",
                    limit=_MAX_MESSAGE_LENGTH,
                )
                self._status.set_text("Update failed")
                self._progress.set_fraction(0.0)
                self._progress.set_text("Update failed")
                self._error.set_text(message)
                self._error.set_visible(True)
                self._note.set_visible(False)
                return False
            if not isinstance(result, Mapping):
                self._status.set_text("Update failed")
                self._progress.set_fraction(0.0)
                self._progress.set_text("Update failed")
                self._error.set_text("The update helper returned no final result.")
                self._error.set_visible(True)
                self._note.set_visible(False)
                return False
            if result.get("ok") is not True:
                message = _bounded_text(
                    result.get("message"),
                    "The existing Zeus update did not complete safely.",
                    limit=_MAX_MESSAGE_LENGTH,
                )
                self._status.set_text("Update failed")
                self._progress.set_fraction(0.0)
                self._progress.set_text("Update failed")
                self._error.set_text(message)
                self._error.set_visible(True)
                self._note.set_visible(False)
                return False

            message = _bounded_text(
                result.get("message"),
                "The Zeus update is ready to apply offline.",
                limit=_MAX_MESSAGE_LENGTH,
            )
            build_id = _bounded_text(result.get("build_id"), "", limit=_MAX_MESSAGE_LENGTH)
            self._completed = True
            self._update_button.set_sensitive(False)
            self._status.set_text(message)
            self._progress.set_fraction(1.0)
            self._progress.set_text("Update downloaded and staged")
            if build_id:
                self._build.set_text(f"Build: {build_id}")
                self._build.set_visible(True)
            self._note.set_text(
                "Boot Zeus to apply the update offline, then restart Zeus once more when it is ready."
            )
            self._note.set_visible(True)
            return False

    class UpdateApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id=APPLICATION_ID)

        def do_activate(self) -> None:
            window = self.props.active_window
            if window is None:
                window = UpdateWindow(self)
            window.present()

    return UpdateApplication()


def launch_update_existing() -> int:
    """Run the standalone GTK4 existing-install update screen."""

    gtk_parts = _load_gtk()
    application = _make_application(gtk_parts)
    return int(application.run([]))


__all__ = [
    "APPLICATION_ID",
    "GtkUnavailableError",
    "HELPER_COMMAND",
    "PKEXEC_COMMAND",
    "UPDATE_COMMAND",
    "UpdateError",
    "UpdateProtocolError",
    "format_bytes",
    "format_elapsed",
    "format_stage",
    "launch_update_existing",
    "parse_event",
    "run_update_existing",
]
