"""Contracts for the built-in, credential-free Tailscale menu."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "desktop/rootfs/usr/lib/zeus"
EXTENSION = ROOT / (
    "desktop/rootfs/usr/share/gnome-shell/extensions/"
    "zeus-shell@kanterlabs/extension.js"
)
POLICY = ROOT / "desktop/rootfs/usr/share/polkit-1/actions/org.zeus.Tailscale.policy"
CONTAINERFILE = ROOT / "image/Containerfile"
REPOSITORY = ROOT / "image/repos/tailscale.repo"
KEY = ROOT / "image/keys/tailscale.asc"


sys.path.insert(0, str(MODULES))
import tailscale_control as control  # noqa: E402


def completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def status_payload(state="Running", *, online=True):
    return {
        "BackendState": state,
        "TailscaleIPs": ["100.64.0.12", "fd7a:115c:a1e0::12", "not-an-ip"],
        "Self": {
            "Online": online,
            "HostName": "zeus-laptop",
            "DNSName": "zeus-laptop.example.ts.net.",
            "UserID": 7,
        },
        "CurrentTailnet": {"Name": "example.com"},
        "Peer": {"secret-peer-key": {"HostName": "private-server"}},
        "User": {"7": {"LoginName": "owner@example.com"}},
    }


class StatusTests(unittest.TestCase):
    def test_status_reduces_peer_rich_payload_to_safe_local_fields(self):
        result = control.parse_status(status_payload())
        self.assertEqual(result["state"], "connected")
        self.assertTrue(result["online"])
        self.assertEqual(result["device_name"], "zeus-laptop")
        self.assertEqual(result["tailnet"], "example.com")
        self.assertEqual(result["addresses"], ["100.64.0.12", "fd7a:115c:a1e0::12"])
        encoded = json.dumps(result)
        self.assertNotIn("private-server", encoded)
        self.assertNotIn("owner@example.com", encoded)
        self.assertNotIn("Peer", encoded)

    def test_backend_states_are_distinct_and_unknown_fails_closed(self):
        self.assertEqual(control.parse_status(status_payload("NeedsLogin"))["state"], "needs_login")
        self.assertEqual(control.parse_status(status_payload("NeedsMachineAuth"))["state"], "needs_approval")
        self.assertEqual(control.parse_status(status_payload("Stopped"))["state"], "disconnected")
        self.assertEqual(control.parse_status(status_payload("Starting"))["state"], "connecting")
        unknown = control.parse_status(status_payload("FutureState"))
        self.assertEqual(unknown["state"], "error")
        self.assertFalse(unknown["ok"])

    def test_query_is_bounded_and_never_returns_raw_command_errors(self):
        broken = lambda args, timeout: completed(args, 1, "", "socket path and secret detail")
        result = control.query_status(runner=broken)
        self.assertEqual(result["state"], "service_stopped")
        self.assertNotIn("secret", json.dumps(result))

        oversized = "{" + "x" * control.MAX_OUTPUT_BYTES + "}"
        huge = lambda args, timeout: completed(args, 0, oversized, "")
        self.assertEqual(control.query_status(runner=huge)["state"], "error")


class ActionTests(unittest.TestCase):
    def test_connect_uses_only_fixed_commands_and_returns_only_login_url(self):
        calls = []

        def runner(arguments, timeout):
            arguments = tuple(arguments)
            calls.append((arguments, timeout))
            if arguments == (control.TAILSCALE, "up", "--timeout=10s"):
                return completed(arguments, 1, "https://login.tailscale.com/a/example-token\n", "")
            if arguments == (control.TAILSCALE, "status", "--json", "--peers=false"):
                return completed(arguments, 0, json.dumps(status_payload("NeedsLogin")), "")
            return completed(arguments)

        owner = SimpleNamespace(pw_uid=1000, pw_name="shane")
        with mock.patch.object(control.pwd, "getpwuid", return_value=owner):
            result = control.admin_action(
                "connect",
                runner=runner,
                environment={"PKEXEC_UID": "1000"},
                effective_uid=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "needs_login")
        self.assertEqual(result["login_url"], "https://login.tailscale.com/a/example-token")
        self.assertEqual(
            [call[0] for call in calls],
            [
                (control.SYSTEMCTL, "start", "tailscaled.service"),
                (control.TAILSCALE, "set", "--operator=shane"),
                (control.TAILSCALE, "up", "--timeout=10s"),
                (control.TAILSCALE, "status", "--json", "--peers=false"),
            ],
        )

    def test_actions_reject_untrusted_identity_and_parameters(self):
        with self.assertRaises(control.TailscaleError):
            control.admin_action("logout", environment={"PKEXEC_UID": "1000"}, effective_uid=0)
        with self.assertRaises(control.TailscaleError):
            control.admin_action("connect", environment={"PKEXEC_UID": "0"}, effective_uid=0)
        with self.assertRaises(control.TailscaleError):
            control.admin_action("connect", environment={"PKEXEC_UID": "1000"}, effective_uid=1000)

    def test_client_crosses_one_exact_polkit_boundary(self):
        calls = []

        def runner(arguments, timeout):
            calls.append((tuple(arguments), timeout))
            return completed(arguments, 0, json.dumps(control.parse_status(status_payload())))

        result = control.client_action("connect", runner=runner)
        self.assertEqual(result["state"], "connected")
        self.assertEqual(
            calls,
            [((control.PKEXEC, control.ADMIN_HELPER, "connect"), control.CLIENT_TIMEOUT)],
        )


class PackagingAndUiTests(unittest.TestCase):
    def test_image_uses_restricted_signed_vendor_repository(self):
        packages = (ROOT / "image/packages.txt").read_text().splitlines()
        repository = REPOSITORY.read_text()
        containerfile = CONTAINERFILE.read_text()
        self.assertEqual(packages.count("tailscale"), 1)
        self.assertIn("enabled=0", repository)
        self.assertIn("gpgcheck=1", repository)
        self.assertIn("repo_gpgcheck=1", repository)
        self.assertIn("sslverify=1", repository)
        self.assertIn("includepkgs=tailscale", repository)
        self.assertIn("--enable-repo=tailscale-stable", containerfile)
        self.assertIn("systemctl enable", containerfile)
        self.assertIn("tailscaled.service", containerfile)
        self.assertEqual(
            hashlib.sha256(KEY.read_bytes()).hexdigest(),
            "53c6f7dfbd774839d9f37e6c5022ba952108aba9a0e556a56f292a9eb605d7cf",
        )

    def test_polkit_action_is_one_fixed_helper(self):
        tree = ET.parse(POLICY)
        action = tree.find("./action")
        self.assertIsNotNone(action)
        self.assertEqual(action.attrib["id"], "org.zeus.Tailscale.manage")
        annotations = {
            item.attrib["key"]: item.text for item in action.findall("./annotate")
        }
        self.assertEqual(
            annotations,
            {"org.freedesktop.policykit.exec.path": "/usr/libexec/zeus-tailscale-admin"},
        )

    def test_zeus_menu_exposes_status_and_fixed_actions(self):
        source = EXTENSION.read_text()
        self.assertIn("PopupSubMenuMenuItem('Tailscale · Checking…')", source)
        self.assertIn("Tailscale · ${headings[state]}", source)
        self.assertIn("[TAILSCALE_HELPER, action]", source)
        self.assertIn("? 'Disconnect'", source)
        self.assertIn("login\\.tailscale\\.com", source)
        self.assertNotIn("['/usr/bin/tailscale'", source)
        self.assertNotIn("payload.Peer", source)


if __name__ == "__main__":
    unittest.main()
