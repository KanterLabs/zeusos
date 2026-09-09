# Laptop Settings

Zeus Settings groups everyday laptop destinations in one native GTK4/libadwaita
window. Open it from **Welcome → Open Settings**, the **Zeus → Settings** menu,
or application search. **All Settings** retains access to GNOME's complete
control center, including in the safe desktop.

Network, Bluetooth, display, power, sound and appearance changes use the existing
GNOME panels. Temp and Updates open their existing native apps. The window shows
the locally installed version and build; opening it does not check for an update,
change preferences, pair devices, scan networks or install software.

GNOME supplies physical screen brightness in the top-right system menu on
supported hardware, as described in its
[brightness help](https://help.gnome.org/gnome-help/display-brightness.html).
The Settings window explains that route and provides access to display and power
preferences. A missing backlight, radio or battery receives an honest availability
message rather than an invented reading.

## Runtime contract

- No additional image packages, persistent Zeus service or periodic refresh job.
- Bounded local hardware discovery; unavailable and unknown are distinct states.
- Fixed desktop entries and native panel IDs; no user text is executed as a command.
- Launch with the native activation context so existing windows can receive focus.
- Unavailable panels fall back to native Settings; a missing native Settings app
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
does not qualify radio pairing, brightness adjustment, battery runtime or physical
laptop suspend/resume; those checks remain in hardware qualification.

The deployed **git-fd2125f63159** [receipt and screenshots](../iterations/git-fd2125f63159/README.md)
record completed VM qualification, all 136 source tests, unchanged dependencies,
matched metrics and preservation checks. The product version remains
**0.1.0-preview.2**.
