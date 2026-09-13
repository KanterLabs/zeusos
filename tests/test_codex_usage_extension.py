"""Contract checks for the isolated AGPL Codex usage GNOME extension."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / (
    "desktop/rootfs/usr/share/gnome-shell/extensions/"
    "codex-usage@kanterlabs"
)
EXTENSION = EXTENSION_DIR / "extension.js"
METADATA = EXTENSION_DIR / "metadata.json"
PROVENANCE_DIR = ROOT / "image/licenses/codex-usage"
DCONF = ROOT / "desktop/rootfs/etc/dconf/db/local.d/00-zeus"
SAFE_HELPER = ROOT / "desktop/rootfs/usr/libexec/zeus-desktop-safe"
CONTAINERFILE = ROOT / "image/Containerfile"
UPSTREAM_COMMIT = "22baeaef5bc4642851148c99b505cb6469679199"


class CodexUsageExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = EXTENSION.read_text(encoding="utf-8")

    def test_extension_identity_targets_gnome_46_through_50(self):
        metadata = json.loads(METADATA.read_text(encoding="utf-8"))
        self.assertEqual(metadata["uuid"], "codex-usage@kanterlabs")
        self.assertEqual(set(metadata["shell-version"]), {"46", "47", "48", "49", "50"})
        self.assertNotIn("settings-schema", metadata)

    def test_fixed_app_server_and_five_minute_refresh(self):
        self.assertIn("const CODEX_COMMAND = '/usr/bin/codex';", self.source)
        self.assertIn("'app-server'", self.source)
        self.assertIn("'--listen'", self.source)
        self.assertIn("'stdio://'", self.source)
        self.assertIn("export const REFRESH_INTERVAL_SECONDS = 300;", self.source)
        self.assertRegex(
            self.source,
            r"GLib\.timeout_add_seconds\(\s*\n?\s*GLib\.PRIORITY_DEFAULT,\s*\n?\s*REFRESH_INTERVAL_SECONDS",
        )
        self.assertIn("this._refresh();", self.source)
        self.assertIn("Refresh Usage", self.source)
        self.assertNotIn("getSettings", self.source)
        self.assertNotIn("Gio.Settings", self.source)
        self.assertFalse((EXTENSION_DIR / "prefs.js").exists())
        self.assertFalse((EXTENSION_DIR / "schemas").exists())

    def test_only_official_json_rpc_methods_are_used(self):
        for method in (
            "initialize",
            "initialized",
            "account/rateLimits/read",
            "account/login/start",
            "account/login/completed",
        ):
            self.assertIn(method, self.source)
        self.assertIn("{type: 'chatgptDeviceCode'}", self.source)
        self.assertNotIn("login', 'status", self.source)
        self.assertNotIn("login status", self.source)
        self.assertNotIn("fetch(", self.source)
        self.assertNotIn("/v1/", self.source)
        self.assertNotIn("auth.json", self.source)
        self.assertNotRegex(self.source, r"(?:read|write|open).*credential")

    def test_device_code_url_is_https_guarded_and_explicit(self):
        self.assertIn("export function isSafeVerificationUrl", self.source)
        self.assertIn("GLib.Uri.parse(value, GLib.UriFlags.NONE)", self.source)
        self.assertIn("uri.get_scheme() === 'https'", self.source)
        self.assertIn("uri.get_host()?.toLowerCase() === 'auth.openai.com'", self.source)
        self.assertIn("uri.get_path() === '/codex/device'", self.source)
        self.assertIn("uri.get_port() === -1", self.source)
        self.assertIn("Gio.AppInfo.launch_default_for_uri", self.source)
        self.assertIn("Open Sign-in Page", self.source)
        self.assertIn("this._provider.openLoginPage()", self.source)
        self.assertIn("account/login/completed", self.source)
        self.assertIn("verificationUrl", self.source)
        self.assertIn("userCode", self.source)

    def test_device_code_cancel_uses_the_official_login_id(self):
        self.assertIn("Cancel Sign-in", self.source)
        self.assertIn("account/login/cancel", self.source)
        self.assertIn("{loginId: login.loginId}", self.source)
        self.assertIn("this._clearLogin()", self.source)
        self.assertIn("async cancelDeviceCodeLogin()", self.source)

    def test_device_code_start_is_single_flight(self):
        self.assertIn("this._loginStartPromise = null", self.source)
        self.assertIn("if (this._loginStartPromise)", self.source)
        self.assertIn("this._loginStartPromise === loginStartPromise", self.source)
        self.assertIn("Boolean(this._provider?.loginStarting)", self.source)
        self.assertIn("!operationBusy && !hasLogin", self.source)
        self.assertIn(
            "if (this._provider.loginStarting || this._provider.login)",
            self.source,
        )

    def test_transport_is_bounded_and_lifecycle_is_cleaned_up(self):
        self.assertIn("MAX_JSON_LINE_BYTES = 64 * 1024", self.source)
        self.assertIn("MAX_JSON_PAYLOAD_BYTES = 256 * 1024", self.source)
        self.assertIn("response line exceeded the safety limit", self.source)
        self.assertIn("response exceeded the safety limit", self.source)
        self.assertIn("export function redactError", self.source)
        self.assertIn("force_exit", self.source)
        self.assertIn("this._client.destroy()", self.source)
        self.assertIn("removeSource(this._refreshTimerId)", self.source)
        self.assertIn("this._indicator?.destroy()", self.source)
        self.assertIn("this._pending.clear()", self.source)
        self.assertIn("this._readerGeneration++", self.source)

    def test_app_server_is_stopped_between_reads_and_failed_handshakes_recover(self):
        self.assertIn("if (!this._login)\n                this._client.stop();", self.source)
        self.assertIn("const initializationPromise = (async () =>", self.source)
        self.assertIn("this._initializationPromise = initializationPromise;", self.source)
        self.assertIn("this._initializationPromise === initializationPromise", self.source)
        self.assertIn("this._stopProcess(error instanceof ProviderError", self.source)

    def test_fixture_searches_all_limit_entries_and_chooses_constrained_windows(self):
        # v0.154 can return a codex entry with only the weekly window and put
        # the 5-hour window under a different opaque limit id.  The UI must
        # still select exact durations without displaying that internal id.
        fixture = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 30, "windowDurationMins": 10080}
                },
                "opaque-limit": {
                    "primary": {"usedPercent": 70, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 50, "windowDurationMins": 10080},
                },
            }
        }
        self.assertEqual(
            fixture["rateLimitsByLimitId"]["opaque-limit"]["primary"]["windowDurationMins"],
            300,
        )
        self.assertIn("const byId = root.rateLimitsByLimitId;", self.source)
        self.assertIn("for (const entry of Object.values(byId))", self.source)
        self.assertIn("mostConstrainedWindow", self.source)
        self.assertIn("Number(window.windowDurationMins) !== durationMinutes", self.source)
        self.assertIn("candidate.percentRemaining < selected.percentRemaining", self.source)
        self.assertNotIn("${entry.limitId}", self.source)

        # Execute the actual pure normalization and URL-guard functions under
        # Node, excluding only their GNOME imports/UI tail.
        model_source = self.source[
            self.source.index("const STATES = Object.freeze(") :
            self.source.index("function launchVerificationUrl")
        ].replace("export ", "")
        script = f"""
globalThis.GLib = {{
    UriFlags: {{NONE: 0}},
    Uri: {{
        parse(value) {{
            const parsed = new URL(value);
            return {{
                get_scheme: () => parsed.protocol.slice(0, -1),
                get_host: () => parsed.hostname,
                get_path: () => parsed.pathname,
                get_port: () => parsed.port ? Number(parsed.port) : -1,
                get_query: () => parsed.search ? parsed.search.slice(1) : null,
                get_userinfo: () => parsed.username || parsed.password ? `${{parsed.username}}:${{parsed.password}}` : null,
                get_fragment: () => parsed.hash ? parsed.hash.slice(1) : null,
            }};
        }},
    }},
}};
const api = new Function({json.dumps(model_source + '; return {normalizeRateLimits, isSafeVerificationUrl};')})();
const snapshot = api.normalizeRateLimits({json.dumps(fixture)}, 1700000000);
if (!snapshot.fiveHour.available || snapshot.fiveHour.percentRemaining !== 30)
    throw new Error('wrong five-hour window');
if (!snapshot.weekly.available || snapshot.weekly.percentRemaining !== 50)
    throw new Error('wrong weekly window');
if (!api.isSafeVerificationUrl('https://auth.openai.com/codex/device'))
    throw new Error('official URL rejected');
for (const value of [
    'http://auth.openai.com/codex/device',
    'https://auth.openai.com.evil.example/codex/device',
    'https://auth.openai.com/codex/device?redirect=evil',
    'https://user:pass@auth.openai.com/codex/device',
]) {{
    if (api.isSafeVerificationUrl(value))
        throw new Error(`unsafe URL accepted: ${{value}}`);
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_key_auth_is_rejected_without_an_api_key_login_path(self):
        self.assertIn("export function isApiKeyAuth", self.source)
        self.assertIn("API-key authentication is not supported", self.source)
        self.assertIn("value.type === 'apiKey'", self.source)
        login_request = self.source.split("async startDeviceCodeLogin", 1)[1]
        self.assertIn("{type: 'chatgptDeviceCode'}", login_request)
        self.assertNotIn("{type: 'apiKey'", login_request)
        self.assertNotIn("apiKey:", login_request)

    def test_provenance_and_agpl_notice_are_present(self):
        self.assertIn(UPSTREAM_COMMIT, self.source)
        self.assertIn("radoslavdodek/codex-usage-gnome-shell-ext", self.source)
        self.assertIn("GNU Affero General Public License", (EXTENSION_DIR / "NOTICE").read_text(encoding="utf-8"))
        self.assertIn("GNU Affero General Public License", (EXTENSION_DIR / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn(UPSTREAM_COMMIT, (PROVENANCE_DIR / "README.md").read_text(encoding="utf-8"))
        self.assertIn(UPSTREAM_COMMIT, (PROVENANCE_DIR / "sources.json").read_text(encoding="utf-8"))
        self.assertIn("AGPL-3.0-only", json.loads((PROVENANCE_DIR / "sources.json").read_text(encoding="utf-8"))["license"])

    def test_system_default_enables_the_separate_extension(self):
        dconf = DCONF.read_text(encoding="utf-8")
        self.assertIn("codex-usage@kanterlabs", dconf)
        self.assertIn("zeus-shell@kanterlabs", dconf)
        self.assertIn(
            "'codex-usage@kanterlabs'",
            SAFE_HELPER.read_text(encoding="utf-8"),
        )

    def test_image_installs_separate_license_provenance(self):
        containerfile = CONTAINERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "COPY image/licenses/codex-usage/ /usr/share/licenses/zeus-codex-usage/",
            containerfile,
        )
        self.assertIn(
            "chmod 0644 /usr/share/gnome-shell/extensions/codex-usage@kanterlabs/*",
            containerfile,
        )

    def test_every_javascript_file_passes_node_syntax_check(self):
        for path in sorted(EXTENSION_DIR.rglob("*.js")):
            result = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
