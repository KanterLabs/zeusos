#!/usr/bin/env python3
"""Run a bounded Chrome download/Temp cleanup smoke test on an installed Zeus image.

The harness is deliberately fixture-only.  It gives Chrome a disposable home,
profile and Temp directory below /tmp or /var/tmp, then exercises the deployed
HomeManager with its production /proc active-file inspector.  No owner home,
browser profile, systemd timer or production Temp path is used.
"""

from __future__ import annotations

import argparse
import base64
import datetime as datetime_module
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_CHROME = "/usr/bin/google-chrome-stable"
DEFAULT_MODULE_DIR = "/usr/lib/zeus"
MAX_RUNTIME_SECONDS = 85.0
DEFAULT_RUNTIME_SECONDS = 80.0
CDP_STARTUP_SECONDS = 12.0
DOWNLOAD_WAIT_SECONDS = 18.0
DOWNLOAD_BYTES = 512 * 1024
DOWNLOAD_CHUNK_BYTES = 4096
DOWNLOAD_CHUNK_DELAY_SECONDS = 0.02


class SmokeError(RuntimeError):
    """The installed Chrome/Temp fixture did not satisfy an assertion."""


class CDPError(SmokeError):
    """Chrome DevTools did not accept a command or websocket."""


def _utc_now() -> str:
    return datetime_module.datetime.now(datetime_module.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_parent() -> Path:
    """Return an explicitly allowed disposable parent, never TMPDIR/HOME."""

    for candidate in (Path("/tmp"), Path("/var/tmp")):
        try:
            value = candidate.resolve(strict=True)
            if value.is_dir() and os.access(value, os.W_OK | os.X_OK):
                return value
        except OSError:
            continue
    raise SmokeError("neither /tmp nor /var/tmp is writable")


def _phase_deadline(budget_deadline: float, seconds: float) -> float:
    """Bound each phase by both its local limit and the harness budget."""

    now = time.monotonic()
    if now >= budget_deadline:
        raise SmokeError("overall harness deadline exceeded")
    return min(budget_deadline, now + seconds)


class FixtureClock:
    """A fake wall clock initialized from the real clock and advanced locally."""

    def __init__(self) -> None:
        self.value = float(time.time())
        self.boot = f"chrome-temp-runtime-{secrets.token_hex(12)}"

    def now(self) -> float:
        return self.value

    def boot_id(self) -> str:
        return self.boot

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class FixtureHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class DownloadHandler(http.server.BaseHTTPRequestHandler):
    """Serve one local HTML link and a deliberately slow attachment."""

    server: FixtureHTTPServer

    def log_message(self, _format: str, *_arguments: object) -> None:
        # Do not print local fixture URLs or request details.
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/":
            body = (
                b"<!doctype html><meta charset='utf-8'><title>Zeus fixture</title>"
                b"<a id='download' href='/slow-attachment'>download</a>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/slow-attachment":
            self.send_error(404)
            return

        payload = self.server.attachment_payload  # type: ignore[attr-defined]
        started = self.server.attachment_started  # type: ignore[attr-defined]
        finished = self.server.attachment_finished  # type: ignore[attr-defined]
        started.set()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header(
            "Content-Disposition", "attachment; filename=chrome-active-fixture.bin"
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            for offset in range(0, len(payload), DOWNLOAD_CHUNK_BYTES):
                self.wfile.write(payload[offset : offset + DOWNLOAD_CHUNK_BYTES])
                self.wfile.flush()
                time.sleep(DOWNLOAD_CHUNK_DELAY_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            # The harness reports a missing completed download if Chrome aborts.
            pass
        finally:
            finished.set()


def _open_http_server(payload: bytes) -> FixtureHTTPServer:
    server = FixtureHTTPServer(("127.0.0.1", 0), DownloadHandler)
    server.attachment_payload = payload  # type: ignore[attr-defined]
    server.attachment_started = threading.Event()  # type: ignore[attr-defined]
    server.attachment_finished = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="zeus-browser-fixture-http",
    )
    thread.daemon = True
    thread.start()
    server.fixture_thread = thread  # type: ignore[attr-defined]
    return server


class WebSocket:
    """Small RFC 6455 client sufficient for the local Chrome CDP endpoint."""

    def __init__(self, websocket_url: str, *, timeout: float = 2.0) -> None:
        parsed = urlsplit(websocket_url)
        if parsed.scheme != "ws" or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise CDPError("Chrome returned a non-loopback DevTools endpoint")
        if parsed.port is None:
            raise CDPError("Chrome returned an incomplete DevTools endpoint")
        self._socket = socket.create_connection((parsed.hostname, parsed.port), timeout)
        self._socket.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        origin = f"http://127.0.0.1:{parsed.port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            self._socket.sendall(request)
            response = self._read_http_headers()
        except Exception:
            self.close()
            raise
        if not response.startswith(b"HTTP/1.1 101"):
            self.close()
            raise CDPError("Chrome rejected the DevTools websocket")

    def _read_http_headers(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise CDPError("Chrome closed the DevTools websocket")
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise CDPError("Chrome returned oversized DevTools headers")
        return bytes(data).split(b"\r\n\r\n", 1)[0]

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + length.to_bytes(8, "big")
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_json(self, value: Mapping[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        header = self._recv_exact(2)
        first, second = header
        finished = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        if length > 8 * 1024 * 1024:
            raise CDPError("Chrome returned an oversized DevTools message")
        masked = bool(second & 0x80)
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return finished, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self._socket.recv(length - len(data))
            if not chunk:
                raise CDPError("Chrome closed the DevTools websocket")
            data.extend(chunk)
        return bytes(data)

    def receive_json(self) -> dict[str, Any]:
        fragments: list[bytes] = []
        while True:
            finished, opcode, payload = self._receive_frame()
            if opcode == 0x8:
                self.close()
                raise CDPError("Chrome closed the DevTools websocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = [payload]
            elif opcode == 0x0 and fragments:
                fragments.append(payload)
            else:
                continue
            if not finished:
                continue
            try:
                value = json.loads(b"".join(fragments).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if len(fragments) == 1:
                    continue
                raise CDPError("Chrome returned malformed DevTools JSON")
            if not isinstance(value, dict):
                raise CDPError("Chrome returned a non-object DevTools message")
            return value

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def _cdp_command(
    websocket: WebSocket,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    command_id: int,
) -> dict[str, Any]:
    websocket.send_json({"id": command_id, "method": method, "params": dict(params or {})})
    while True:
        try:
            message = websocket.receive_json()
        except socket.timeout as error:
            raise CDPError(f"Chrome DevTools command timed out: {method}") from error
        if message.get("id") != command_id:
            continue
        if "error" in message:
            error = message.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            raise CDPError(f"Chrome rejected {method}: {detail or 'unknown error'}")
        return message


def _json_endpoint(port: int, path: str) -> Any:
    request = Request(f"http://127.0.0.1:{port}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=1.0) as response:
            return json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (OSError, URLError, ValueError, UnicodeDecodeError) as error:
        raise CDPError(f"Chrome DevTools endpoint unavailable: {type(error).__name__}") from error


def _wait_for_cdp(chrome: subprocess.Popen[bytes], port: int, deadline: float) -> tuple[str, str]:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if chrome.poll() is not None:
            raise CDPError("Chrome exited before exposing DevTools")
        try:
            version = _json_endpoint(port, "/json/version")
            targets = _json_endpoint(port, "/json/list")
            browser_ws = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
            if isinstance(browser_ws, str) and isinstance(targets, list):
                for target in targets:
                    if (
                        isinstance(target, dict)
                        and target.get("type") == "page"
                        and isinstance(target.get("webSocketDebuggerUrl"), str)
                    ):
                        return browser_ws, target["webSocketDebuggerUrl"]
        except CDPError as error:
            last_error = error
        time.sleep(0.1)
    suffix = f" ({type(last_error).__name__})" if last_error else ""
    raise CDPError(f"Chrome DevTools startup timed out{suffix}")


def _wait_for_download(
    chrome: subprocess.Popen[bytes],
    temp_path: Path,
    deadline: float,
) -> tuple[Path, int]:
    while time.monotonic() < deadline:
        partials = [
            path
            for path in temp_path.iterdir()
            if path.is_file() and path.name.lower().endswith(".crdownload")
        ]
        for path in partials:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if size > 0:
                return path, size
        if chrome.poll() is not None:
            raise SmokeError("Chrome exited before creating a partial download")
        time.sleep(0.05)
    raise SmokeError("Chrome did not create a partial download before the deadline")


def _wait_for_completed_download(
    chrome: subprocess.Popen[bytes],
    temp_path: Path,
    expected_size: int,
    deadline: float,
) -> Path:
    completed_name = "chrome-active-fixture.bin"
    completed = temp_path / completed_name
    while time.monotonic() < deadline:
        try:
            partials = [
                path
                for path in temp_path.iterdir()
                if path.is_file() and path.name.lower().endswith(".crdownload")
            ]
            if (
                completed.is_file()
                and completed.stat().st_size == expected_size
                and not partials
            ):
                return completed
        except FileNotFoundError:
            pass
        if chrome.poll() is not None:
            raise SmokeError("Chrome exited before completing the attachment")
        time.sleep(0.05)
    raise SmokeError("Chrome did not complete the attachment before the deadline")


def _wait_for_page_and_trigger(websocket: WebSocket, deadline: float) -> None:
    command_id = 1
    _cdp_command(websocket, "Runtime.enable", command_id=command_id)
    command_id += 1
    while time.monotonic() < deadline:
        response = _cdp_command(
            websocket,
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => { const a = document.getElementById('download'); "
                    "if (!a) return false; if (!window.__zeusClicked) { "
                    "window.__zeusClicked = true; a.click(); } return true; })()"
                ),
                "returnByValue": True,
            },
            command_id=command_id,
        )
        command_id += 1
        result = response.get("result", {}).get("result", {})
        if isinstance(result, dict) and result.get("value") is True:
            return
        time.sleep(0.1)
    raise CDPError("Chrome page did not expose the fixture download link")


def _configure_download(
    browser_ws_url: str,
    target_ws_url: str,
    download_path: Path,
    *,
    deadline: float,
) -> tuple[WebSocket, WebSocket]:
    timeout = max(0.1, min(2.0, deadline - time.monotonic()))
    browser = WebSocket(browser_ws_url, timeout=timeout)
    timeout = max(0.1, min(2.0, deadline - time.monotonic()))
    target = WebSocket(target_ws_url, timeout=timeout)
    try:
        try:
            _cdp_command(
                browser,
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(download_path)},
                command_id=1,
            )
        except CDPError:
            # Older official Chrome builds expose the equivalent Page command
            # on the target websocket.  Both paths remain fixture-only.
            _cdp_command(
                target,
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(download_path)},
                command_id=1,
            )
        return browser, target
    except Exception:
        browser.close()
        target.close()
        raise


def _chrome_group_members(group_id: int) -> list[tuple[int, str]] | None:
    """Return ``(pid, state)`` for this fixture's process group/session.

    A dedicated ``start_new_session`` group makes this narrower than a
    UID-wide Chrome lookup.  ``None`` means /proc could not be enumerated;
    callers then avoid signalling an exited group's possibly reused ID.
    """

    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return None
    members: list[tuple[int, str]] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            value = (entry / "stat").read_text(encoding="ascii")
            closing = value.rfind(")")
            fields = value[closing + 2 :].split()
            # After ``comm``, fields are state, ppid, pgrp, session.
            if len(fields) < 4:
                continue
            if int(fields[2]) == group_id and int(fields[3]) == group_id:
                members.append((int(entry.name), fields[0]))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return members


def _stop_chrome(
    chrome: subprocess.Popen[bytes] | None,
    *,
    deadline: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    report: dict[str, Any] = {
        "group_cleanup": "not-started",
        "live_members_after": None,
        "zombie_members_after": None,
    }
    if chrome is None:
        report["group_cleanup"] = "no-child"
        return True, report

    group_id = chrome.pid
    members = _chrome_group_members(group_id)
    report["group_members_before"] = len(members) if members is not None else None
    report["group_verification"] = "available" if members is not None else "unavailable"
    live_members = (
        [member for member in members if member[1] != "Z"] if members is not None else None
    )
    parent_alive = chrome.poll() is None
    # Signal a live parent directly through its private process group.  If the
    # parent already exited, signal only after /proc confirms that this exact
    # group/session still has live members; this avoids a reused-PID kill.
    if parent_alive or live_members:
        if parent_alive or members is not None:
            try:
                os.killpg(group_id, signal.SIGTERM)
                report["group_cleanup"] = "term"
            except ProcessLookupError:
                report["group_cleanup"] = "already-gone"
    try:
        timeout = 3.0
        if deadline is not None:
            timeout = max(0.1, min(timeout, deadline - time.monotonic()))
        chrome.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Recheck before escalating.  A parent can have exited while one of
        # its renderer/utility children remains in the same private group.
        members = _chrome_group_members(group_id)
        live_members = (
            [member for member in members if member[1] != "Z"]
            if members is not None
            else None
        )
        if live_members:
            try:
                os.killpg(group_id, signal.SIGKILL)
                report["group_cleanup"] = "kill"
            except ProcessLookupError:
                pass
        try:
            timeout = 1.0
            if deadline is not None:
                timeout = max(0.1, min(timeout, deadline - time.monotonic()))
            chrome.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            report["group_cleanup"] = "timed-out"

    # ``chrome.wait`` can return immediately after the browser parent exits,
    # even while a renderer child survives.  Escalate that confirmed private
    # group before the final verification pass.
    members = _chrome_group_members(group_id)
    live_members = (
        [member for member in members if member[1] != "Z"] if members is not None else None
    )
    if live_members:
        try:
            os.killpg(group_id, signal.SIGKILL)
            report["group_cleanup"] = "kill"
        except ProcessLookupError:
            pass
        try:
            timeout = 1.0
            if deadline is not None:
                timeout = max(0.1, min(timeout, deadline - time.monotonic()))
            chrome.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            report["group_cleanup"] = "timed-out"

    verification_deadline = time.monotonic() + 1.0
    if deadline is not None:
        verification_deadline = min(verification_deadline, deadline)
    while time.monotonic() < verification_deadline:
        members = _chrome_group_members(group_id)
        if members is None:
            break
        live_members = [member for member in members if member[1] != "Z"]
        if not live_members:
            break
        time.sleep(0.05)
    if members is not None:
        live_members = [member for member in members if member[1] != "Z"]
        report["live_members_after"] = len(live_members)
        report["zombie_members_after"] = len(members) - len(live_members)
        ok = not live_members and chrome.poll() is not None
    else:
        report["group_verification"] = "unavailable"
        ok = chrome.poll() is not None
    return ok, report


def _make_payload() -> bytes:
    seed = b"Zeus Chrome active download fixture payload\n"
    repeats = (DOWNLOAD_BYTES + len(seed) - 1) // len(seed)
    return (seed * repeats)[:DOWNLOAD_BYTES]


def _run_smoke(args: argparse.Namespace, result: dict[str, Any]) -> None:
    if os.geteuid() == 0:
        raise SmokeError("run as the unprivileged owner; Chrome sandbox smoke test refuses root")
    budget_deadline = time.monotonic() + float(args.timeout)

    chrome_path = Path(args.chrome).resolve()
    module_dir = Path(args.module_dir).resolve()
    if not chrome_path.is_file() or not os.access(chrome_path, os.X_OK):
        raise SmokeError("official Chrome executable is unavailable")
    module_path = module_dir / "zeus_temp.py"
    if not module_path.is_file() or not os.access(module_path, os.R_OK):
        raise SmokeError("installed zeus_temp module is unavailable")
    sys.path.insert(0, str(module_dir))
    try:
        from zeus_temp import ActiveFileError, HomeManager  # type: ignore[import-not-found]
    except (ImportError, OSError) as error:
        raise SmokeError("cannot import the installed zeus_temp module") from error

    payload = _make_payload()
    expected_hash = hashlib.sha256(payload).hexdigest()
    clock = FixtureClock()
    fixture_parent = _fixture_parent()
    server: FixtureHTTPServer | None = None
    chrome: subprocess.Popen[bytes] | None = None
    browser_websocket: WebSocket | None = None
    target_websocket: WebSocket | None = None

    fixture_directory = tempfile.TemporaryDirectory(
        prefix="zeus-browser-temp-runtime-", dir=str(fixture_parent)
    )
    try:
        temporary = fixture_directory.name
        root = Path(temporary)
        home = root / "home"
        profile = root / "chrome-user-data"
        crash_dumps = root / "chrome-crashes"
        runtime_dir = root / "runtime"
        home.mkdir(mode=0o700)
        profile.mkdir(mode=0o700)
        crash_dumps.mkdir(mode=0o700)
        runtime_dir.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        os.chmod(profile, 0o700)
        os.chmod(crash_dumps, 0o700)
        os.chmod(runtime_dir, 0o700)
        result["scope"]["fixture_parent"] = str(fixture_parent)

        # Omit proc_root intentionally: production HomeManager defaults to
        # /proc and queries the installed socket inspector when the sweep has
        # entries to inspect.
        manager = HomeManager(home, clock=clock)
        setup = manager.setup(include_usage=False)
        if setup.get("ok") is not True or setup.get("enabled") is not True:
            raise SmokeError("fixture Temp setup was not enabled")
        policy = manager.set_policy("hourly")
        if policy.get("ok") is not True or policy.get("interval_seconds") != 3600:
            raise SmokeError("fixture hourly policy was not accepted")
        initial_schedule = manager.schedule()
        if not initial_schedule.get("scheduled") or initial_schedule.get("due"):
            raise SmokeError("fixture hourly deadline was not scheduled")
        result["checks"]["fixture_temp_setup"] = True
        result["checks"]["hourly_deadline_scheduled"] = True

        temp_path = home / "Temp"
        sentinel = temp_path / "active-sweep-sentinel.txt"
        sentinel.write_bytes(b"retain while Chrome is downloading")
        documents = home / "Documents"
        documents.mkdir(mode=0o700)

        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
                # Keep Chrome's headless fixture away from the owner's
                # keyring/session bus.  This socket is intentionally absent.
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root / 'session-bus-unavailable'}",
                "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={root / 'system-bus-unavailable'}",
                "NO_AT_BRIDGE": "1",
            }
        )
        for variable in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "WAYLAND_SOCKET",
            "XAUTHORITY",
            "GDK_BACKEND",
            "QT_QPA_PLATFORM",
            "OZONE_PLATFORM",
            "PULSE_SERVER",
            "PIPEWIRE_REMOTE",
            "DBUS_STARTER_ADDRESS",
            "DBUS_STARTER_BUS_TYPE",
        ):
            env.pop(variable, None)
        version = subprocess.run(
            [
                str(chrome_path),
                "--version",
                f"--user-data-dir={profile}",
                f"--crash-dumps-dir={crash_dumps}",
            ],
            check=False,
            capture_output=True,
            timeout=max(0.1, min(8.0, budget_deadline - time.monotonic())),
            env=env,
        )
        version_text = (version.stdout or b"").decode("utf-8", "replace").strip()
        if version.returncode != 0 or not version_text.startswith("Google Chrome "):
            raise SmokeError("the configured browser is not official Google Chrome")
        result["checks"]["official_chrome"] = True

        server = _open_http_server(payload)
        debug_port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            debug_port_socket.bind(("127.0.0.1", 0))
            debug_port = int(debug_port_socket.getsockname()[1])
        finally:
            debug_port_socket.close()
        attachment_url = f"http://127.0.0.1:{server.server_port}/"
        chrome_args = [
            str(chrome_path),
            "--headless=new",
            f"--remote-debugging-port={debug_port}",
            f"--remote-allow-origins=http://127.0.0.1:{debug_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-extensions",
            "--disable-crash-reporter",
            "--disable-gpu",
            f"--crash-dumps-dir={crash_dumps}",
            attachment_url,
        ]
        if any(argument == "--no-sandbox" or argument.startswith("--no-sandbox=") for argument in chrome_args):
            raise SmokeError("Chrome sandbox bypass was requested")
        chrome = subprocess.Popen(
            chrome_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        result["checks"]["chrome_sandbox_bypass_absent"] = True
        browser_ws_url, target_ws_url = _wait_for_cdp(
            chrome, debug_port, _phase_deadline(budget_deadline, CDP_STARTUP_SECONDS)
        )
        browser_websocket, target_websocket = _configure_download(
            browser_ws_url,
            target_ws_url,
            temp_path,
            deadline=_phase_deadline(budget_deadline, 8.0),
        )
        _wait_for_page_and_trigger(
            target_websocket, _phase_deadline(budget_deadline, CDP_STARTUP_SECONDS)
        )
        partial_path, partial_size = _wait_for_download(
            chrome, temp_path, _phase_deadline(budget_deadline, DOWNLOAD_WAIT_SECONDS)
        )
        result["windows"]["partial_download_at"] = _utc_now()
        result["checks"]["real_crdownload_seen"] = True
        result["checks"]["partial_bytes_seen"] = partial_size > 0

        clock.advance(3600)
        due_schedule = manager.schedule()
        if not due_schedule.get("due"):
            raise SmokeError("fake hourly deadline did not become due")
        active_sweep = manager.sweep(include_usage=False)
        if (
            active_sweep.get("ok") is not True
            or active_sweep.get("swept")
            or active_sweep.get("reason") != "partial-download"
            or active_sweep.get("deleted", 0) != 0
        ):
            raise SmokeError("timed sweep did not defer the active Chrome download")
        if not partial_path.exists() or partial_path.stat().st_size < partial_size:
            raise SmokeError("active Chrome download bytes were not preserved")
        if not sentinel.exists() or sentinel.read_bytes() != b"retain while Chrome is downloading":
            raise SmokeError("active sweep did not preserve the fixture sentinel")
        result["windows"]["active_sweep_at"] = _utc_now()
        result["checks"]["active_sweep_deferred"] = True
        result["checks"]["active_bytes_preserved"] = True
        result["checks"]["sentinel_preserved"] = True

        completed = _wait_for_completed_download(
            chrome,
            temp_path,
            len(payload),
            _phase_deadline(budget_deadline, DOWNLOAD_WAIT_SECONDS),
        )
        if _sha256(completed) != expected_hash:
            raise SmokeError("completed Chrome attachment hash did not match")
        result["windows"]["download_completed_at"] = _utc_now()
        result["checks"]["completed_hash_verified"] = True

        eligible = temp_path / "eligible-fixture.bin"
        eligible.write_bytes(b"eligible completed fixture")
        keep_source = temp_path / "kept-fixture.bin"
        keep_payload = b"permanent fixture bytes"
        keep_source.write_bytes(keep_payload)
        kept: dict[str, Any] | None = None
        keep_deadline = _phase_deadline(budget_deadline, 5.0)
        while time.monotonic() < keep_deadline:
            try:
                kept = manager.keep("kept-fixture.bin", "Documents")
                break
            except ActiveFileError:
                # Chrome can close the completed file just after its rename;
                # give the production inspector a short bounded retry window.
                time.sleep(0.1)
        if kept is None:
            raise SmokeError("fixture Keep remained blocked by active-file inspection")
        destination = documents / "kept-fixture.bin"
        if (
            kept.get("ok") is not True
            or keep_source.exists()
            or not destination.is_file()
            or destination.read_bytes() != keep_payload
        ):
            raise SmokeError("fixture Keep did not move the completed file to Documents")
        result["checks"]["keep_to_fixture_documents"] = True

        clock.advance(3600)
        cleanup_schedule = manager.schedule()
        if not cleanup_schedule.get("due"):
            raise SmokeError("next fake hourly deadline did not become due")
        cleanup = manager.sweep(include_usage=False)
        if cleanup.get("ok") is not True or not cleanup.get("swept"):
            raise SmokeError("completed fixture cleanup did not run")
        if eligible.exists() or completed.exists() or sentinel.exists():
            raise SmokeError("completed eligible Temp files were not cleaned")
        if not destination.exists() or destination.read_bytes() != keep_payload:
            raise SmokeError("Keep destination was removed by a later cleanup")
        result["windows"]["completed_cleanup_at"] = _utc_now()
        result["checks"]["completed_fixture_cleaned"] = True
        result["checks"]["kept_file_retained_after_cleanup"] = True

        later_eligible = temp_path / "later-eligible-fixture.bin"
        later_eligible.write_bytes(b"later eligible fixture")
        clock.advance(3600)
        later_cleanup = manager.sweep(include_usage=False)
        if later_cleanup.get("ok") is not True or not later_cleanup.get("swept"):
            raise SmokeError("later fixture cleanup did not run")
        if later_eligible.exists() or not destination.exists():
            raise SmokeError("later cleanup did not retain Documents fixture")
        result["windows"]["later_cleanup_at"] = _utc_now()
        result["checks"]["kept_file_retained_after_later_cleanup"] = True

    finally:
        try:
            if target_websocket is not None:
                target_websocket.close()
            if browser_websocket is not None:
                browser_websocket.close()
            chrome_ok, chrome_cleanup = _stop_chrome(chrome, deadline=budget_deadline)
            result.setdefault("cleanup", {})["chrome"] = chrome_cleanup
            cleanup_error: str | None = None
            if not chrome_ok:
                cleanup_error = "Chrome fixture process group still has live members"
            if server is not None:
                server.shutdown()
                server.server_close()
                thread = getattr(server, "fixture_thread", None)
                if isinstance(thread, threading.Thread):
                    join_timeout = max(0.1, min(3.0, budget_deadline - time.monotonic()))
                    thread.join(timeout=join_timeout)
                    result.setdefault("cleanup", {})["http_server_thread_stopped"] = not thread.is_alive()
                    if thread.is_alive() and cleanup_error is None:
                        cleanup_error = "fixture HTTP server thread did not stop"
            if cleanup_error is not None:
                raise SmokeError(cleanup_error)
        finally:
            fixture_directory.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="installed official Chrome executable")
    parser.add_argument("--module-dir", default=DEFAULT_MODULE_DIR, help="installed Zeus Python module directory")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_RUNTIME_SECONDS,
        help=f"overall harness budget in seconds (maximum {MAX_RUNTIME_SECONDS:g})",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    result: dict[str, Any] = {
        "schema": "zeus-browser-temp-runtime-v1",
        "ok": False,
        "windows": {"started_at": _utc_now()},
        "scope": {
            "unprivileged": os.geteuid() != 0,
            "fixture_home_and_temp": True,
            "fixture_chrome_user_data_dir": True,
            "fixture_parent": "pending",
            "production_owner_home_touched": False,
            "production_temp_touched": False,
            "production_browser_profiles_touched": False,
            "installed_user_systemd_touched": False,
            "proc_root": "/proc (HomeManager default)",
            "active_file_inspector": "installed /run/zeus-temp-inspector.sock",
            "loopback_http_only": True,
            "session_bus_isolated": True,
            "display_isolated": True,
        },
        "checks": {},
        "test_scope": [
            "real Chrome .crdownload creation",
            "deferred hourly sweep with active bytes and sentinel",
            "completed hash and eligible-file cleanup",
            "Keep into fixture Documents and later retention",
        ],
        "errors": [],
    }
    try:
        if not 0 < args.timeout <= MAX_RUNTIME_SECONDS:
            raise SmokeError(f"--timeout must be in (0, {MAX_RUNTIME_SECONDS:g}] seconds")
        _run_smoke(args, result)
        result["ok"] = True
    except Exception as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        result["windows"]["finished_at"] = _utc_now()
        result["duration_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
