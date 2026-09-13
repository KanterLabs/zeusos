# Zeus Codex Usage Indicator provenance

The installed extension is an adapted AGPL-3.0 work, kept separate from the
MIT-licensed Zeus Shell extension.

## Upstream source

- Project: [codex-usage-gnome-shell-ext](https://github.com/radoslavdodek/codex-usage-gnome-shell-ext)
- Exact upstream commit: `22baeaef5bc4642851148c99b505cb6469679199`
- Upstream license: [GNU Affero General Public License, version 3](https://www.gnu.org/licenses/agpl-3.0.html)
- Original author/maintainer: Radoslav Dodek

`LICENSE` is the complete AGPL-3.0 text shipped with the extension.  `NOTICE`
in the extension directory repeats the attribution for the interactive GNOME
menu and identifies the local modifications.

## Zeus modifications

Zeus adapted the upstream top-bar UI to a fixed, system-owned integration:

- only `/usr/bin/codex app-server --listen stdio://` is started;
- usage is read through the official `account/rateLimits/read` JSON-RPC method;
- the `initialize` / `initialized` handshake and
  `account/login/completed` notification are handled explicitly;
- ChatGPT device-code sign-in uses official
  `account/login/start` with `{ "type": "chatgptDeviceCode" }`;
- the verification page is opened only after an explicit menu action and only
  when it is exactly the documented HTTPS `auth.openai.com/codex/device` page;
- device-code login can be canceled through the matching
  `account/login/cancel` request;
- refresh occurs once on enable and at the fixed five-minute interval (300
  seconds), with a manual refresh menu action;
- app-server limit entries are reduced to the most constrained exact five-hour
  and weekly windows without showing internal limit IDs;
- the app-server is stopped between reads and kept only during an active login;
- provider lines/payloads and errors are bounded/redacted; no CLI terminal
  output is parsed and no credential file or credential value is read or
  written; and
- upstream mock data, pause/preferences, configurable commands, and settings
  schema were removed.

The protocol reference is the official Codex app-server documentation:
<https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md>.
The extension does not call undocumented HTTP endpoints.  The app-server and
Codex CLI remain separate upstream components with their own licensing.
