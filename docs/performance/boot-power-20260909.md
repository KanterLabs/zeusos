# Boot and background-work optimization protocol

This pass keeps version 0.1.0-preview.2. Before image changes, record the exact
booted source and preserve permanent-file hashes and the Temp policy. Retain a
verified populated backup. Hold Temp at Never during repeated benchmark boots;
restore the original policy for handoff without backdating its schedule.

Compare the same VM115 (4CPU/8GiB, same disk, graphics, display and Proxmox host):

1. Three cold starts from a clean shutdown, recording host start-to-active-GDM
   and the kernel/initrd/userspace breakdown. Polling adds overhead; this is not
   exact time-to-first-pixel. Record console evidence separately. Include first
   post-update boot separately from subsequent boots.
2. Two-minute idle samples after a short settle, both with Temp visible and
   with applications closed. Keep the display/session awake during sampling,
   with no concurrent guest tests. Record CPU, available-memory accounting,
   process starts, context switches and Temp service activations. These are VM
   activity counters, not hardware wakeups or battery energy.
3. Native Temp and Welcome launch-to-first-GTK-map samples from separate
   processes. Distinguish first run from warm repetitions. Do not call these
   compositor-presentation or cold-disk launch measurements.
4. Verify native power profiles, compressed swap, suspend/display preferences,
   Temp safety/scheduler/monitoring, shortcuts, reduced-motion behavior and
   account/file preservation. Test application interactions through the console.

The shared host introduces variability; publish samples and medians, and report
regressions instead of selecting the fastest run. Do not strip hardware drivers,
security checks or device support to manufacture fast boot numbers. Physical
laptop discharge, resume reliability and peripheral-specific energy remain
hardware qualification tasks. A separate read-only battery harness supports that
measurement when an actual battery is available.

## Implementation

- Temp has no minute timer for Boot or Never. Timed policies arm one deadline;
  settings changes and completed sweeps synchronize the timer. Failed inspections
  back off. Background setup/scheduling omit UI usage scans and file inventories.
- The Temp window uses bounded file monitors and one-shot debounce work, suspending
  refresh while hidden. Shell panel/dock discovery uses signals. Application
  search caches the installed list until it changes and respects reduced motion.
- Native `power-profiles-daemon` supplies the GNOME power profile interface;
  Balanced retains responsive normal use and Power Saver is available. The low
  battery setting requests Power Saver through GNOME. No second tuning daemon
  competes for the same hardware controls.
- Overridable laptop defaults dim when idle, blank the display after three minutes,
  and suspend after ten minutes idle on battery. Existing personal settings win.
- Fedora's zram defaults add compressed RAM swap for memory pressure. This is
  intended to improve responsiveness under load, not proof of lower battery draw.
- Hardware database compilation moves into image construction. The unused NFS
  client target is disabled at boot, removing its network dependency from the
  graphical login path. NFS packages remain available for configured use.
- The laptop image omits 18 `qemu-user-static` emulator packages (171 MiB of
  installed package contents). The intermediate image registered 31 foreign
  binary handlers at boot, and `systemd-binfmt` took 883 ms in its critical path.
  Cross-architecture emulation becomes an optional development-image feature;
  native applications and the separate QEMU guest agent remain supported.

The GRUB timeout was already one second and stays there, with recovery entries
retained. The generic initramfs keeps laptop/storage drivers; no VM-only driver
pruning, security-service disabling, or blanket device autosuspend was applied.
Deprecated LocalSearch battery preferences were inspected and deliberately not
added as ineffective tuning.

Native power behavior is defined by the
[upstream power profiles service](https://upower.pages.freedesktop.org/power-profiles-daemon/)
and [GNOME power settings](https://teams.pages.gitlab.gnome.org/Websites/help.gnome.org/gnome-help/power-batterylife.html).
The selected physical driver and effective savings must be checked on the target
laptop using the [battery protocol](battery-test-protocol.md).
