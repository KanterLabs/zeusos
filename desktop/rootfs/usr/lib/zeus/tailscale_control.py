"""Bounded status and fixed privileged actions for the Zeus Tailscale menu."""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


TAILSCALE = "/usr/bin/tailscale"
SYSTEMCTL = "/usr/bin/systemctl"
PKEXEC = "/usr/bin/pkexec"
ADMIN_HELPER = "/usr/libexec/zeus-tailscale-admin"
MAX_OUTPUT_BYTES = 128 * 1024
STATUS_TIMEOUT = 8
ACTION_TIMEOUT = 20
CLIENT_TIMEOUT = 35
LOGIN_HOST = "login.tailscale.com"
VALID_ACTIONS = frozenset({"status", "connect", "disconnect"})


class TailscaleError(RuntimeError):
    """A stable, user-safe error raised by a Tailscale operation."""


Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


def _run(arguments: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _text(value: Any, *, limit: int = 253) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().replace("\n", " ")[:limit]


def _address(value: Any) -> str:
    text = _text(value, limit=64)
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _base_status(state: str, message: str, *, ok: bool = True) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": ok,
        "state": state,
        "online": False,
        "device_name": "",
        "tailnet": "",
        "addresses": [],
        "message": message,
    }


def parse_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce Tailscale's detailed status to the fields the menu may display."""

    backend = _text(payload.get("BackendState"), limit=32).lower()
    self_node = payload.get("Self")
    if not isinstance(self_node, Mapping):
        self_node = {}

    dns_name = _text(self_node.get("DNSName")).rstrip(".")
    host_name = _text(self_node.get("HostName"))
    device_name = host_name or dns_name.split(".", 1)[0]
    tailnet_data = payload.get("CurrentTailnet")
    tailnet = ""
    if isinstance(tailnet_data, Mapping):
        tailnet = _text(tailnet_data.get("Name"))

    raw_addresses = payload.get("TailscaleIPs")
    if not isinstance(raw_addresses, list):
        raw_addresses = self_node.get("TailscaleIPs")
    addresses: list[str] = []
    if isinstance(raw_addresses, list):
        for value in raw_addresses[:4]:
            address = _address(value)
            if address and address not in addresses:
                addresses.append(address)
            if len(addresses) == 2:
                break

    if backend == "running":
        online = self_node.get("Online") is not False
        result = _base_status(
            "connected",
            "Connected to Tailscale." if online else "Connected; network is currently offline.",
        )
        result.update(
            online=online,
            device_name=device_name,
            tailnet=tailnet,
            addresses=addresses,
        )
        return result
    if backend in {"needslogin", "nostate"}:
        return _base_status("needs_login", "Sign in to connect this device.")
    if backend == "stopped":
        return _base_status("disconnected", "Tailscale is disconnected.")
    if backend == "needsmachineauth":
        return _base_status("needs_approval", "This device needs tailnet approval.")
    if backend == "starting":
        return _base_status("connecting", "Tailscale is connecting.")
    return _base_status("error", "Tailscale status is unavailable.", ok=False)


def query_status(*, runner: Runner = _run) -> dict[str, Any]:
    try:
        result = runner((TAILSCALE, "status", "--json", "--peers=false"), STATUS_TIMEOUT)
    except FileNotFoundError:
        return _base_status("unavailable", "Tailscale is not installed.", ok=False)
    except (OSError, subprocess.TimeoutExpired):
        return _base_status("error", "Tailscale status is unavailable.", ok=False)

    stdout = result.stdout.encode("utf-8", errors="replace")
    if len(stdout) > MAX_OUTPUT_BYTES:
        return _base_status("error", "Tailscale status is unavailable.", ok=False)
    if result.returncode != 0:
        return _base_status("service_stopped", "The Tailscale service is not running.", ok=False)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return _base_status("error", "Tailscale returned an invalid status.", ok=False)
    if not isinstance(payload, Mapping):
        return _base_status("error", "Tailscale returned an invalid status.", ok=False)
    return parse_status(payload)


def _login_url(output: str) -> str:
    for candidate in re.findall(
        r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+",
        output[:MAX_OUTPUT_BYTES],
    ):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.hostname == LOGIN_HOST and parsed.username is None:
            return candidate
    return ""


def _owner_name(environment: Mapping[str, str]) -> str:
    raw_uid = environment.get("PKEXEC_UID", "")
    if not raw_uid.isascii() or not raw_uid.isdecimal():
        raise TailscaleError("The active owner could not be identified.")
    uid = int(raw_uid)
    if uid < 1000:
        raise TailscaleError("Tailscale actions require an active owner account.")
    try:
        record = pwd.getpwuid(uid)
    except KeyError as error:
        raise TailscaleError("The active owner could not be identified.") from error
    if record.pw_uid != uid or not record.pw_name:
        raise TailscaleError("The active owner could not be identified.")
    return record.pw_name


def _action_result(status: Mapping[str, Any], *, login_url: str = "") -> dict[str, Any]:
    result = dict(status)
    result["ok"] = status.get("state") not in {"error", "unavailable", "service_stopped"}
    if login_url:
        result["login_url"] = login_url
    return result


def admin_action(
    action: str,
    *,
    runner: Runner = _run,
    environment: Mapping[str, str] | None = None,
    effective_uid: int | None = None,
) -> dict[str, Any]:
    """Run one root-only, argument-free Tailscale action."""

    if action not in {"connect", "disconnect"}:
        raise TailscaleError("Unsupported Tailscale action.")
    if (os.geteuid() if effective_uid is None else effective_uid) != 0:
        raise TailscaleError("Administrator authentication is required.")

    owner = _owner_name(os.environ if environment is None else environment)
    try:
        started = runner((SYSTEMCTL, "start", "tailscaled.service"), ACTION_TIMEOUT)
        if started.returncode != 0:
            raise TailscaleError("The Tailscale service could not be started.")

        if action == "connect":
            operator = runner((TAILSCALE, "set", f"--operator={owner}"), ACTION_TIMEOUT)
            if operator.returncode != 0:
                raise TailscaleError("Tailscale access could not be assigned to the owner.")
            changed = runner((TAILSCALE, "up", "--timeout=10s"), ACTION_TIMEOUT)
            combined = f"{changed.stdout}\n{changed.stderr}"
            login_url = _login_url(combined)
            status = query_status(runner=runner)
            if status["state"] == "connected":
                return _action_result(status)
            if login_url:
                status = _base_status("needs_login", "Finish signing in in the browser.")
                return _action_result(status, login_url=login_url)
            raise TailscaleError("Tailscale could not connect.")

        changed = runner((TAILSCALE, "down"), ACTION_TIMEOUT)
        if changed.returncode != 0:
            raise TailscaleError("Tailscale could not disconnect.")
        status = query_status(runner=runner)
        if status["state"] == "error":
            return _action_result(_base_status("disconnected", "Tailscale is disconnected."))
        return _action_result(status)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise TailscaleError("The Tailscale action did not complete.") from error


def _public_error(message: str) -> dict[str, Any]:
    result = _base_status("error", message, ok=False)
    return result


def admin_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 1:
        print(json.dumps(_public_error("Choose exactly one Tailscale action.")))
        return 2
    try:
        result = admin_action(arguments[0])
    except TailscaleError as error:
        print(json.dumps(_public_error(str(error)), sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


def _decode_admin_result(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if len(result.stdout.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        raise TailscaleError("Tailscale returned too much data.")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise TailscaleError("The authenticated Tailscale action failed.") from error
    if not isinstance(payload, dict):
        raise TailscaleError("The authenticated Tailscale action failed.")
    return payload


def client_action(action: str, *, runner: Runner = _run) -> dict[str, Any]:
    if action == "status":
        return query_status(runner=runner)
    if action not in {"connect", "disconnect"}:
        raise TailscaleError("Unsupported Tailscale action.")
    try:
        result = runner((PKEXEC, ADMIN_HELPER, action), CLIENT_TIMEOUT)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise TailscaleError("Administrator authentication did not complete.") from error
    payload = _decode_admin_result(result)
    if result.returncode != 0 or payload.get("ok") is not True:
        raise TailscaleError(_text(payload.get("message"), limit=240) or "The Tailscale action failed.")
    return payload


def client_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 1 or arguments[0] not in VALID_ACTIONS:
        print(json.dumps(_public_error("Use status, connect, or disconnect."), sort_keys=True))
        return 2
    try:
        result = client_action(arguments[0])
    except TailscaleError as error:
        print(json.dumps(_public_error(str(error)), sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


__all__ = [
    "ADMIN_HELPER",
    "LOGIN_HOST",
    "MAX_OUTPUT_BYTES",
    "PKEXEC",
    "SYSTEMCTL",
    "TAILSCALE",
    "TailscaleError",
    "admin_action",
    "admin_main",
    "client_action",
    "client_main",
    "parse_status",
    "query_status",
]
