# Codex usage indicator

Zeus OS includes a separate AGPL-3.0 GNOME Shell extension that places current
ChatGPT Codex usage in the top bar.  It is enabled by the system default and
can be disabled from GNOME Extensions without changing the Codex CLI.

The indicator starts the fixed local command
`/usr/bin/codex app-server --listen stdio://`.  It performs the official
`initialize` / `initialized` handshake and reads structured usage with
`account/rateLimits/read`; it does not parse terminal output or call an
undocumented HTTP endpoint.  The app-server owns authentication, and the
extension never reads or writes a credential file or credential value.  API-key
authentication is rejected because this indicator is for ChatGPT subscription
usage.

The extension refreshes once when enabled and then exactly every 300 seconds.
The menu also has an explicit **Refresh Usage** action.  Provider timeouts,
bounded JSON-RPC lines/payloads, redacted error text, and one-child-process
lifecycle protection keep a failed or offline Codex command from blocking the
Shell. The child is stopped between reads and stays alive only for an active
device-code ceremony. Exact five-hour and weekly windows are selected across
the app-server's multi-limit response without exposing internal limit IDs. The
panel and menu distinguish checking, normal/low/limit, signed-out, offline,
stale, and unavailable states.

## Device-code sign-in

When signed out, choose **Sign in with ChatGPT device code**.  The extension
sends the official app-server request:

```json
{"method":"account/login/start","params":{"type":"chatgptDeviceCode"}}
```

The returned verification URL and one-time user code are shown in the menu.
Choose **Open Sign-in Page** only after reviewing the code; the action launches
only the documented `https://auth.openai.com/codex/device` page. **Cancel
Sign-in** sends the matching `account/login/cancel` request. Completion is
received through the official `account/login/completed` notification, after
which the indicator refreshes. Timer reads are suppressed during the ceremony,
and the code and URL are not persisted by the extension.

## Source and license

This is a distinct adapted AGPL-covered extension, not part of Zeus's existing
MIT-licensed `zeus-shell@kanterlabs` extension.  The source, full license,
attribution, exact upstream commit, local modification list, and official
app-server reference are recorded in
[`image/licenses/codex-usage/`](../../image/licenses/codex-usage/) and the
installed extension's `NOTICE` file.

Upstream source:
<https://github.com/radoslavdodek/codex-usage-gnome-shell-ext/tree/22baeaef5bc4642851148c99b505cb6469679199>
