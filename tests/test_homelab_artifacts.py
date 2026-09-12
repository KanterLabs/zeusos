"""Static contract for the append-only public artifact publisher."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "scripts" / "publish-homelab-artifact.sh"
DEPLOY = ROOT / "deploy" / "homelab-updates"


class HomelabArtifactPublisherTests(unittest.TestCase):
    def test_publisher_is_valid_shell_with_fixed_append_only_destination(self):
        subprocess.run(["bash", "-n", str(PUBLISHER)], check=True)
        script = PUBLISHER.read_text()
        self.assertIn("debian@10.0.0.101", script)
        self.assertIn("sudo /usr/local/sbin/zeus-publish-artifact", script)
        self.assertIn("https://updates.shanekanterman.dev/zeusos/preview/", script)
        self.assertIn("cf-cache-status", script)
        self.assertIn("Content-Length", script)
        self.assertIn("sha256sum", script)
        self.assertNotIn("rm -rf", script)

    def test_origin_is_loopback_only_cache_bypassed_and_path_scoped(self):
        caddyfile = (DEPLOY / "Caddyfile").read_text()
        self.assertIn("bind 127.0.0.1", caddyfile)
        self.assertIn("host updates.shanekanterman.dev", caddyfile)
        self.assertIn("method GET HEAD", caddyfile)
        self.assertIn("/zeusos/preview/", caddyfile)
        self.assertIn('Cache-Control "no-store, no-transform"', caddyfile)
        self.assertIn("respond 404", caddyfile)
        self.assertNotIn("0.0.0.0", caddyfile)

    def test_tunnel_keeps_canary_and_has_a_terminal_404(self):
        config = (DEPLOY / "cloudflared-config.yml").read_text()
        self.assertIn("hostname: canary-homelab.shanekanterman.dev", config)
        self.assertIn("hostname: updates.shanekanterman.dev", config)
        self.assertIn("service: https://127.0.0.1:19101", config)
        self.assertTrue(config.rstrip().endswith("- service: http_status:404"))
        self.assertNotIn("noTLSVerify", config)

    def test_storage_and_receiver_preserve_append_only_contract(self):
        mount = (DEPLOY / "srv-zeusos.mount").read_text()
        self.assertIn("What=/dev/disk/by-label/zeus-updates", mount)
        self.assertIn("Options=rw,nosuid,nodev,noexec,noatime", mount)
        receiver = (DEPLOY / "zeus-publish-artifact").read_text()
        subprocess.run(["bash", "-n", str(DEPLOY / "zeus-publish-artifact")], check=True)
        self.assertIn("mountpoint -q", receiver)
        self.assertIn("flock", receiver)
        self.assertIn("set -o noclobber", receiver)
        self.assertIn("mv --no-clobber", receiver)
        self.assertIn("sha256sum", receiver)
        self.assertNotIn("rm -rf", receiver)

    def test_dns_tool_is_pinned_to_one_hostname_and_tunnel(self):
        script = (DEPLOY / "configure-cloudflare-dns.py").read_text()
        self.assertIn('NAME = "updates.shanekanterman.dev"', script)
        self.assertIn("b0ba744b-0ca9-4098-8172-136f72f76799.cfargotunnel.com", script)
        self.assertIn('parser.add_argument("--apply"', script)
        self.assertNotIn("--delete", script)


if __name__ == "__main__":
    unittest.main()
