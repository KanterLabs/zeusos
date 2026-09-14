# Laptop Settings

Zeus Settings groups everyday laptop destinations in one native GTK4/libadwaita
window. Open it from **Welcome → Open Settings**, the **Zeus → Settings** menu,
or application search. Every destination first opens a Zeus-owned page. The
stock Fedora control center is retained under **Advanced → Fedora compatibility**
as a recovery path while individual controls move into Zeus. Its desktop entry
is hidden from normal application search, but the executable remains installed
for recovery from a terminal with `gnome-control-center`.

At desktop widths the destination list stays visible beside the active page.
On compact or highly scaled displays it collapses into a one-pane route with a
clear Back button; fixed deep links such as `zeus-settings-window bluetooth`
open the same page without accepting arbitrary commands.

The top-panel **Zeus → Tailscale** submenu is a separate compact network status
surface. It shows this device's connection state and provides fixed Connect,
Disconnect and Refresh actions; see [Built-in Tailscale](tailscale.md).

Wi-Fi and Bluetooth now use event-driven Zeus pages backed directly by
NetworkManager and BlueZ. They cover radio state, scanning, saved and available
Wi-Fi networks, connection and removal, supported hotspots, and normal Bluetooth
pair/connect/trust/remove flows. Zeus registers a bounded BlueZ `Agent1` while
the window is open, so passkeys and confirmation stay in Zeus dialogs. Bluetooth
discovery runs only while its page is open and only devices belonging to the
selected adapter are shown. Display, power, sound and appearance are labeled
honestly as compatibility-backed during the next migration slices. Temp and
Updates open their existing Zeus apps.

The existing AirPods connection card deep-links to this Bluetooth page. Optional
AirPods battery/noise controls remain in ZOS-71, and the authenticated QR action
from ZOS-124 is reserved for the active Wi-Fi row; neither secret/device protocol
is duplicated in the base connectivity controller.

Wi-Fi passwords use a bounded native password field, travel directly to
NetworkManager in memory, and are not placed in logs, shell commands, process
arguments, screenshots, the clipboard, or Zeus-owned storage. NetworkManager
continues to own its normal saved-connection and secret policy. Enterprise and
legacy authentication use the explicit compatibility action until their native
flows are qualified. WPA3-Personal uses NetworkManager's SAE key management,
and hotspot connections are volatile so repeated starts do not accumulate saved
profiles. Loading, backend failure, missing-radio, radio-off and in-progress
states have distinct copy; repeated operations are guarded until completion.

GNOME supplies physical screen brightness in the top-right system menu on
supported hardware, as described in its
[brightness help](https://help.gnome.org/gnome-help/display-brightness.html).
The Settings window explains that route and provides access to display and power
preferences. A missing backlight, radio or battery receives an honest availability
message rather than an invented reading.

## Runtime contract

- No additional image packages, persistent Zeus service or periodic refresh job.
- Bounded local hardware discovery; unavailable and unknown are distinct states.
- NetworkManager and BlueZ remain authoritative; no `nmcli`, `bluetoothctl`, shell,
  or Zeus network database is introduced.
- Fixed desktop entries and native panel IDs; no user text is executed as a command.
- Launch with the native activation context so existing windows can receive focus.
- Unavailable compatibility panels fall back to Fedora Settings; a missing app
  produces an actionable message while the rest of the window remains usable.
- Closing the window ends its work. Owner preferences and Temp policy are retained.
- OS updater state remains owned by the existing signed updater. Opening the
  Updates destination explicitly opens that app and follows its normal behavior.

## Qualification

Source checks cover hardware presence, unavailable/unknown states and launch
fallbacks. Native review covers every destination, keyboard access, repeated
activation, 100%/200% scaling, light/dark style and safe desktop fallback.
Verification must include a populated-data comparison, a fresh verified backup,
matched boot/idle measurements in [metrics.md](../metrics.md), and real screenshots
from the deployed build before the feature is marked complete in Helm.

VM 115 provides Ethernet, display configuration and native power profiles. It has
no physical Wi-Fi/Bluetooth adapter, backlight or battery. Successful VM navigation
and backend/UI probes do not qualify Wi-Fi authentication, radio toggling, hotspot,
Bluetooth pairing, AirPods reconnect, brightness adjustment, battery runtime or
physical laptop suspend/resume; those checks remain in hardware qualification.

The deployed **git-fd2125f63159** [receipt and screenshots](../iterations/git-fd2125f63159/README.md)
record completed VM qualification, all 136 source tests, unchanged dependencies,
matched metrics and preservation checks. The product version remains
**0.1.0-preview.2**.
