# Built-in Tailscale

Zeus OS includes Tailscale and exposes its local connection state in the
**Zeus → Tailscale** submenu. Opening the Zeus menu performs one bounded local
status read. While the menu remains open, status refreshes every 15 seconds;
closing it stops that refresh.

The menu distinguishes connected, offline, connecting, disconnected,
sign-in-required, device-approval-required, stopped-service and unavailable states. When connected, it
shows only this device's name, first Tailscale address and tailnet name. It does
not render peers, user login names, keys, command output or diagnostic logs.

## Owner actions

- **Connect…** requests administrator authentication. Zeus starts the packaged
  daemon, assigns the active local owner as Tailscale's operator, and runs the
  standard `tailscale up` flow. A fresh enrollment opens only an HTTPS URL on
  `login.tailscale.com` in the default browser.
- **Disconnect** requests administrator authentication and runs `tailscale down`.
  It preserves the device identity so a later connection does not silently
  create a new machine.
- **Refresh Status** is read-only and never prompts for authentication.

The privileged helper accepts exactly `connect` or `disconnect`; the desktop
cannot provide a command, URL, auth key, hostname, route, exit node or file
path. It resolves the active owner from Polkit's numeric caller identity. Zeus
never embeds Tailscale state or credentials in the image.

## Packaging and updates

The Tailscale RPM comes from Tailscale's official stable Fedora repository.
Both repository metadata and RPM signatures are required using the checked-in
public key, and the disabled repository is restricted to the `tailscale`
package. Tailscale upgrades arrive with signed Zeus OS images instead of a
separate self-updater.

The `tailscaled` service starts with the system, but a new installation remains
unenrolled until the owner chooses **Connect…** and completes sign-in. Existing
Tailscale machine state lives under Tailscale's normal mutable system storage
and is not replaced by an OS update.

## Qualification

Source checks cover status normalization and redaction, unknown backend states,
bounded output, the fixed Polkit command boundary, caller identity, repository
trust and the GNOME Shell menu contract. Image qualification must additionally
verify the installed RPM and enabled daemon, then exercise disconnected,
sign-in-required and connected states on a disposable device identity. Real
tailnet enrollment and revocation remain owner/admin actions and must not use a
production auth key in test fixtures.
