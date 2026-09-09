# Zeus OS desktop baseline

This directory is the image overlay for the first Zeus OS GNOME preview. It
keeps the desktop layer small and reversible: GNOME owns the session, lock
screen, authentication, accessibility, and window-management behavior; the
only shell extension enabled by default is the pinned
`dash-to-dock@micxgx.gmail.com` package.

The overlay contains:

- `/etc/dconf/profile/user` selecting the writable user database followed by
  the read-only `system-db:local` defaults database;
- `/etc/dconf/db/local.d/00-zeus` with the GNOME 50 defaults;
- original light and dark SVG wallpapers, plus `zeus.xml` in GNOME's
  background-list format;
- the original `org.zeus.Welcome.svg` hicolor icon used by the Welcome
  launcher; and
- `zeus-desktop-safe`, a narrow fallback action that disables or restores only
  the dock UUID in the user's `disabled-extensions` list.

The image builder should copy the overlay into `/` and install the package
`gnome-shell-extension-dash-to-dock`. The parent image layer supplies the
`org.zeus.Welcome.desktop` launcher. No T3 Code or Dev launcher is invented by
this layer.

## Defaults and schema choices

`org.gnome.shell.favorite-apps` is the complete five-item dock list:

`org.gnome.Nautilus.desktop`, `org.gnome.Ptyxis.desktop`, `google-chrome.desktop`,
`org.gnome.Settings.desktop`, and `org.zeus.Welcome.desktop`.

The dash-to-dock settings use its published GSettings schema: bottom floating
placement, 48px maximum and fixed icons, intelligent hide, pointer reveal,
running indicators, opaque-but-translucent indigo background, and no extra
trash, mount, or applications buttons. Custom shell CSS is disabled. The
extension's own `apply-custom-theme` remains `false`, so this does not replace
GNOME's theme or compositor.

Search uses the native
`org.gnome.settings-daemon.plugins.media-keys search` binding. GNOME Settings
Daemon dispatches that action to GNOME Shell's supported `FocusSearch` method,
so `Super+Space` focuses the normal GNOME search entry. `Super+Return` is a
native custom media-key binding that launches `/usr/bin/ptyxis`; terminal
editing and `Ctrl+C` remain application behavior. The default Super+Space
input-source chord is moved to `Super+Alt+Space`, with the hardware
`XF86Keyboard` binding retained, to avoid two native handlers competing for the
same accelerator.

The interface defaults use GNOME's supported `Adwaita Sans 11`, `Adwaita Sans
12`, and `Adwaita Mono 11` fonts, a blue accent, 24-hour clock, and a dark
color-scheme with a matching dark wallpaper. GNOME's animation, text scaling,
display scaling, keyboard navigation, and accessibility defaults are left
unlocked; a user can choose reduced motion, 100/125/150% scaling, high
contrast, or the on-screen keyboard through native settings. A convenient
native on-screen keyboard toggle is provided at `Super+Alt+K`.

The lock baseline stays native: locking is enabled with no post-activation
delay, the full name is hidden on the lock shield, and
`org.gnome.desktop.notifications show-in-lock-screen=false` keeps notification
contents off the locked display. There is no password-input interception,
custom GDM, blur effect, global application menu, or compositor change.

## Install and apply

For a disposable test host with the required package already installed, the
exact overlay installation is:

```sh
sudo cp -a --no-preserve=ownership desktop/rootfs/. /
sudo dconf update
```

The image build equivalent is `COPY desktop/rootfs/ /` followed by `dconf
update` in the image layer. The dconf profile is selected when a session
starts, so log out and back in after applying it. A shell extension change may
also require a new GNOME session. The safe fallback can be tried from a
terminal with `/usr/libexec/zeus-desktop-safe disable`; restore only the dock
with `/usr/libexec/zeus-desktop-safe enable`.

## Validation

These are syntax and static checks only; they do not claim a booted GNOME
session, a rendered screenshot, or performance evidence.

```sh
python3 - <<'PY'
from pathlib import Path
from xml.etree import ElementTree

for path in sorted(Path("desktop/rootfs/usr/share").rglob("*.svg")):
    ElementTree.parse(path)
for path in sorted(Path("desktop/rootfs/usr/share/backgrounds/zeus").glob("*.xml")):
    ElementTree.parse(path)
print("SVG/XML syntax: OK")
PY
sh -n desktop/rootfs/usr/libexec/zeus-desktop-safe
```

On a Fedora image with the target schemas installed, also run the repository's
desktop validator (when present) and compile the dconf database:

```sh
python3 scripts/validate-desktop.py
sudo dconf update
```

`dconf update` must run after any edit to `local.d/00-zeus`. Inspect the
effective user values after a fresh login with `gsettings get`; user settings
take precedence over these defaults by design.

## Provenance and licenses

The wallpaper SVGs, background XML, and `org.zeus.Welcome.svg` are original
Zeus OS artwork authored for this preview. They contain no Apple, GNOME, or
third-party artwork and are released with the preview under **CC0-1.0**
(public-domain dedication where permitted). The dconf defaults and
`zeus-desktop-safe` helper are original project code/configuration under
**MIT**. The eventual repository release manifest is authoritative if
it supplies a compatible project-wide license or a later asset decision.

The dock itself is the upstream GNOME Shell extension
`dash-to-dock@micxgx.gmail.com`; its source and license remain with the
Fedora package and must be pinned and recorded by the image release manifest.
GNOME Shell, GNOME Settings Daemon, GNOME desktop schemas, Adwaita fonts, and
the application icons remain upstream system components under their own
licenses. This directory redistributes none of those upstream files.
