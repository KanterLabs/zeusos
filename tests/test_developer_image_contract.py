"""Static contracts for the Developer Mode image foundation.

These checks deliberately inspect the image inputs instead of attempting to
boot or mutate an image.  The latter belongs to the disposable sysext smoke
tests; these checks keep the immutable build contract reviewable in a source
checkout.
"""

import base64
import json
import re
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "image/Containerfile"
ROOTFS_INPUTS = (ROOT / "desktop/rootfs", ROOT / "image/rootfs")


def _developer_inputs(relative_directory: str, pattern: str) -> list[Path]:
    """Return regular files matching *pattern* in either image rootfs input."""

    paths: list[Path] = []
    for root in ROOTFS_INPUTS:
        directory = root / relative_directory
        if directory.is_dir():
            paths.extend(path for path in directory.glob(pattern) if path.is_file())
    return sorted(paths)


class DeveloperImageContract(unittest.TestCase):
    def test_base_identity_tracks_the_build_id(self) -> None:
        text = CONTAINERFILE.read_text()

        self.assertIn("ARG VERSION=0.1.0-preview.2", text)
        self.assertIn("ARG BUILD_ID=development", text)
        self.assertIn("/usr/share/zeus/build-id", text)
        self.assertRegex(text, r"SYSEXT_LEVEL=\$\{BUILD_ID\}")
        self.assertIn("grep -q '^SYSEXT_LEVEL=' /usr/lib/os-release", text)
        self.assertIn("printf 'SYSEXT_LEVEL=%s\\n' \"${BUILD_ID}\"", text)

        # A base-provided value must be replaced, while a base without the
        # field receives one.  Appending a second field would make the
        # compatibility identity ambiguous to systemd-sysext.
        self.assertRegex(
            text,
            r"sed -i \"s\|\^SYSEXT_LEVEL=\.\*\$\|SYSEXT_LEVEL=\$\{BUILD_ID\}\|\"",
        )

    def test_developer_trust_is_separate_from_release_updates(self) -> None:
        text = CONTAINERFILE.read_text()
        developer_policy = ROOT / "security/developer_allowed_signers"
        self.assertTrue(developer_policy.is_file())

        self.assertIn(
            "COPY security/allowed_signers /usr/share/zeus/update-allowed-signers",
            text,
        )
        self.assertIn(
            "COPY developer-mode/components.json /usr/share/zeus/developer-components.json",
            text,
        )
        self.assertIn(
            "COPY security/developer_allowed_signers /usr/share/zeus/developer-allowed-signers",
            text,
        )
        self.assertNotIn(
            "COPY security/developer_allowed_signers /usr/share/zeus/update-allowed-signers",
            text,
        )
        self.assertNotIn(
            "COPY security/allowed_signers /usr/share/zeus/developer-allowed-signers",
            text,
        )

        lines = [
            line.strip()
            for line in developer_policy.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        fields = lines[0].split()
        self.assertGreaterEqual(len(fields), 3)
        self.assertEqual(fields[0], "zeusos-developer")
        self.assertEqual(fields[1], "ssh-ed25519")
        key_blob = base64.b64decode(fields[2], validate=True)
        # An SSH public-key blob contains the algorithm name followed by the
        # 32-byte Ed25519 key.  Validate both lengths without accepting a
        # random base64 placeholder.
        self.assertGreaterEqual(len(key_blob), 8 + 32)
        algorithm_length = int.from_bytes(key_blob[:4], "big")
        self.assertEqual(key_blob[4 : 4 + algorithm_length], b"ssh-ed25519")
        key_length_offset = 4 + algorithm_length
        key_length = int.from_bytes(key_blob[key_length_offset : key_length_offset + 4], "big")
        self.assertEqual(key_length, 32)
        self.assertEqual(
            len(key_blob), key_length_offset + 4 + key_length
        )
        self.assertNotIn("zeusos-preview", developer_policy.read_text())
        self.assertNotIn("zeusos-update", developer_policy.read_text())

        component_map = ROOT / "developer-mode/components.json"
        self.assertTrue(component_map.is_file())
        components = json.loads(component_map.read_text())
        self.assertEqual(components["product"], "zeusos")
        self.assertEqual(components["extension_name"], "zeus-developer")
        for component in components["components"]:
            for entry in component["files"]:
                self.assertTrue(entry["source"])
                self.assertTrue(entry["target"].startswith("/usr/"))

    def test_developer_inputs_contain_no_private_key_material(self) -> None:
        private_key = re.compile(
            rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"
        )
        paths = [
            ROOT / "security/developer_allowed_signers",
            ROOT / "developer-mode/components.json",
            CONTAINERFILE,
        ]
        for root in ROOTFS_INPUTS:
            if root.is_dir():
                paths.extend(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            self.assertIsNone(private_key.search(path.read_bytes()), str(path))

    def test_developer_files_are_installed_with_restricted_modes(self) -> None:
        text = CONTAINERFILE.read_text()
        self.assertRegex(
            text,
            r"find /usr/libexec .* -name 'zeus-developer\*' -exec chmod 0755",
        )
        self.assertRegex(
            text,
            r"find /usr/lib/zeus .* -name 'developer\*' -exec chmod 0644",
        )
        self.assertRegex(
            text,
            r"find /usr/share/polkit-1/actions .* -iname '\*developer\*\.policy' -exec chmod 0644",
        )
        self.assertRegex(
            text,
            r"find /usr/lib/tmpfiles\.d .* -iname '\*developer\*\.conf' -exec chmod 0644",
        )
        self.assertRegex(
            text,
            r"find /usr/lib/systemd/system .* -iname '\*developer\*\.service' -exec chmod 0644",
        )
        self.assertRegex(
            text,
            r"chmod 0644 [^\n]*?/usr/share/zeus/update-allowed-signers "
            r"/usr/share/zeus/developer-allowed-signers",
        )

        launchers = _developer_inputs("usr/libexec", "zeus-developer*")
        self.assertTrue(launchers, "Developer Mode launcher input is missing")
        for path in launchers:
            self.assertFalse(path.is_symlink(), str(path))

        policies = _developer_inputs("usr/share/polkit-1/actions", "*")
        policies = [path for path in policies if "developer" in path.name.lower()]
        self.assertTrue(policies, "Developer Mode authorization policy is missing")
        for path in policies:
            root = ET.parse(path).getroot()
            actions = root.findall("action")
            self.assertTrue(actions, str(path))
            self.assertTrue(
                any(
                    (action.findtext("defaults/allow_active") or "").strip() == "auth_admin"
                    for action in actions
                ),
                str(path),
            )
            self.assertIn("/usr/libexec/zeus-developer-admin", path.read_text())
            self.assertFalse(path.is_symlink(), str(path))

        tmpfiles = _developer_inputs("usr/lib/tmpfiles.d", "*")
        tmpfiles = [path for path in tmpfiles if "developer" in path.name.lower()]
        self.assertTrue(tmpfiles, "Developer Mode tmpfiles policy is missing")
        for path in tmpfiles:
            tmpfile_text = path.read_text()
            self.assertIn("/var/lib/zeus/developer-mode", tmpfile_text)
            self.assertRegex(tmpfile_text, r"(?m)^d\s+\S*developer-mode\S*\s+0700(?:\s|$)")
            self.assertNotRegex(tmpfile_text, r"(?m)^\S+\s+/(?:etc|home|usr)/")
            self.assertFalse(path.is_symlink(), str(path))

        services = _developer_inputs("usr/lib/systemd/system", "*")
        services = [path for path in services if "developer" in path.name.lower()]
        self.assertTrue(services, "Developer Mode boot service is missing")
        for path in services:
            service = path.read_text()
            self.assertRegex(service, r"(?m)^Type=oneshot$")
            self.assertRegex(service, r"(?m)^ExecStart=")
            self.assertFalse(path.is_symlink(), str(path))

    def test_developer_boot_is_off_by_default_or_guarded(self) -> None:
        text = CONTAINERFILE.read_text()
        enable_lines = re.findall(r"(?m)^\s*systemctl enable .*", text)
        developer_enable_lines = [
            line for line in enable_lines if "zeus-developer" in line
        ]
        self.assertEqual(len(developer_enable_lines), 1)
        self.assertIn(
            "zeus-developer-activate.service", developer_enable_lines[0]
        )

        services = _developer_inputs("usr/lib/systemd/system", "*")
        services = [path for path in services if "developer" in path.name.lower()]
        self.assertTrue(services, "Developer Mode boot service is missing")
        for path in services:
            service = path.read_text()
            self.assertIn("Type=oneshot", service)
            self.assertNotIn("Restart=always", service)
            self.assertIn("WantedBy=multi-user.target", service)
            triggers = re.findall(
                r"(?m)^ConditionPathExists=\|([^\n]+)$", service
            )
            self.assertEqual(
                set(triggers),
                {
                    "/var/lib/zeus/developer-mode/active",
                    "/var/lib/zeus/developer-mode/pending.json",
                },
                str(path),
            )
        for root in ROOTFS_INPUTS:
            wants = root / "usr/lib/systemd/system"
            if not wants.is_dir():
                continue
            for path in wants.rglob("zeus-developer*.service"):
                if "wants" in path.parts:
                    self.assertFalse(path.is_symlink(), str(path))

    def test_required_developer_tools_are_in_the_image_contract(self) -> None:
        packages = (ROOT / "image/packages.txt").read_text().splitlines()
        self.assertIn("git", packages)
        self.assertIn("openssh-clients", packages)

        text = CONTAINERFILE.read_text()
        self.assertRegex(text, r"test -x /usr/bin/systemd-sysext")
        self.assertRegex(text, r"test -x /usr/bin/ssh-keygen")
        self.assertIn("systemd-sysext", text)


if __name__ == "__main__":
    unittest.main()
