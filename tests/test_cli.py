import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "zeus" / "Cargo.toml"
BINARY = ROOT / "zeus" / "target" / "debug" / "zeus"
VERSION = "0.1.0-preview.2"


def make_executable(directory, name, contents):
    path = Path(directory) / name
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ZeusCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(
            ["cargo", "build", "--manifest-path", str(MANIFEST), "--offline", "--locked"],
            cwd=ROOT,
            check=True,
        )

    def run_cli(self, *arguments, env=None):
        command_env = os.environ.copy()
        if env:
            command_env.update({key: str(value) for key, value in env.items()})
        return subprocess.run(
            [str(BINARY), *arguments],
            cwd=ROOT,
            env=command_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_version_contract(self):
        result = self.run_cli("version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"Zeus OS {VERSION}")

    def test_doctor_json_reports_session_and_redacts_bootc_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            bootc = make_executable(
                temporary,
                "bootc",
                '#!/bin/sh\nprintf \'{"version":"0.1.0","token":"do-not-print"}\\n\'\n',
            )
            terminal = make_executable(temporary, "ptyxis", "#!/bin/sh\nexit 0\n")
            result = self.run_cli(
                "doctor",
                "--json",
                env={
                    "ZEUS_BOOTC_BIN": bootc,
                    "ZEUS_TERMINAL_BIN": terminal,
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["version"], VERSION)
            self.assertEqual(report["session"]["desktop"], "GNOME")
            self.assertEqual(report["session"]["type"], "wayland")
            self.assertTrue(report["bootc"]["available"])
            self.assertEqual(report["bootc"]["status"], "ready")
            self.assertNotIn("do-not-print", result.stdout)
            self.assertNotIn("token", report["bootc"]["summary"].lower())

    def test_update_status_is_read_only_and_does_not_reboot(self):
        with tempfile.TemporaryDirectory() as temporary:
            arguments_log = Path(temporary) / "bootc-args"
            bootc = make_executable(
                temporary,
                "bootc",
                f'#!/bin/sh\nprintf "%s\\n" "$@" > "{arguments_log}"\nprintf "Version: 0.1.0-preview.2\\nPending reboot: yes\\n"\n',
            )
            result = self.run_cli("update", "status", env={"ZEUS_BOOTC_BIN": bootc})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(arguments_log.read_text(encoding="utf-8").splitlines(), ["status"])
            self.assertIn("Pending reboot: yes", result.stdout)
            self.assertIn("No reboot was requested", result.stdout)
            self.assertNotIn("upgrade", arguments_log.read_text(encoding="utf-8"))

            json_result = self.run_cli(
                "update",
                "status",
                "--json",
                env={"ZEUS_BOOTC_BIN": bootc},
            )
            self.assertEqual(json_result.returncode, 0, json_result.stderr)
            status = json.loads(json_result.stdout)
            self.assertFalse(status["reboot_requested"])
            self.assertEqual(status["status"], "ready")

    def test_dev_dry_run_uses_one_validated_ssh_destination(self):
        result = self.run_cli("dev", "--target", "dev.example", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ssh -- dev.example")

    def test_dev_rejects_shell_syntax_before_spawning(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "ssh-called"
            ssh = make_executable(
                temporary,
                "ssh",
                f'#!/bin/sh\ntouch "{marker}"\n',
            )
            result = self.run_cli(
                "dev",
                "--target",
                "dev.example;touch",
                env={"ZEUS_SSH_BIN": ssh},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid SSH target", result.stderr)
            self.assertFalse(marker.exists())

    def test_desktop_safe_dry_run_preserves_terminal_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            terminal = make_executable(temporary, "ptyxis", "#!/bin/sh\nexit 0\n")
            extensions = make_executable(temporary, "gnome-extensions", "#!/bin/sh\nexit 0\n")
            result = self.run_cli(
                "desktop",
                "safe",
                "--dry-run",
                env={
                    "ZEUS_TERMINAL_BIN": terminal,
                    "ZEUS_GNOME_EXTENSIONS_BIN": extensions,
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("disable dash-to-dock@micxgx.gmail.com", result.stdout)
            self.assertIn("Terminal remains available through ptyxis", result.stdout)
            self.assertIn("Credentials and personal data would remain untouched", result.stdout)

    def test_desktop_safe_requires_terminal_before_mutating_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "extension-called"
            extensions = make_executable(
                temporary,
                "gnome-extensions",
                f'#!/bin/sh\ntouch "{marker}"\n',
            )
            result = self.run_cli(
                "desktop",
                "safe",
                env={
                    "PATH": temporary,
                    "ZEUS_GNOME_EXTENSIONS_BIN": extensions,
                    "ZEUS_TERMINAL_BIN": Path(temporary) / "missing-ptyxis",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("usable terminal", result.stderr)
            self.assertFalse(marker.exists())

    def test_desktop_actions_call_only_the_expected_extension_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = Path(temporary) / "extension-calls"
            extensions = make_executable(
                temporary,
                "gnome-extensions",
                f'#!/bin/sh\nprintf "%s %s\\n" "$1" "$2" >> "{calls}"\n',
            )
            terminal = make_executable(temporary, "ptyxis", "#!/bin/sh\nexit 0\n")
            environment = {
                "ZEUS_TERMINAL_BIN": terminal,
                "ZEUS_GNOME_EXTENSIONS_BIN": extensions,
                "PATH": temporary,
            }
            safe = self.run_cli("desktop", "safe", env=environment)
            restore = self.run_cli("desktop", "restore", env=environment)
            self.assertEqual(safe.returncode, 0, safe.stderr)
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    "disable dash-to-dock@micxgx.gmail.com",
                    "enable dash-to-dock@micxgx.gmail.com",
                ],
            )

    def test_unsupported_commands_fail_informatively(self):
        for arguments in (("restore",), ("config", "export")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unavailable in this preview", result.stderr)

    def test_welcome_assets_have_install_contract(self):
        welcome = ROOT / "zeus" / "assets" / "welcome.py"
        desktop = ROOT / "zeus" / "assets" / "org.zeus.Welcome.desktop"
        self.assertTrue(welcome.exists())
        desktop_entry = desktop.read_text(encoding="utf-8")
        self.assertIn("Exec=/usr/libexec/zeus-welcome", desktop_entry)
        self.assertIn("Icon=org.zeus.Welcome", desktop_entry)
        subprocess.run(["python3", "-m", "py_compile", str(welcome)], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
