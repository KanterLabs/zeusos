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

## September 9 matched VM results

Baseline: `git-bbd36cc2af2f`. Final: `git-de0d8baae5de`. Both are version
`0.1.0-preview.2`, on the same 4-vCPU/8-GiB VM115. The builder VM remained
running but idle. The shared Proxmox host was not otherwise isolated.

| Cold start | Baseline OS startup | Final OS startup | Baseline host-to-greeter probe | Final host-to-greeter probe |
| --- | ---: | ---: | ---: | ---: |
| 1 | 7.356 s | 6.992 s | 18.690 s | 18.945 s |
| 2 | 7.683 s | 7.769 s | 18.696 s | 18.125 s |
| 3 | 8.922 s | 6.993 s | 20.251 s | 46.328 s |
| Median | **7.683 s** | **6.993 s** | **18.696 s** | **18.945 s** |

The measured kernel/initrd/userspace median decreased 0.690 seconds (9.0%).
Total VM readiness did not improve: its median increased 0.249 seconds (1.3%),
with a much slower third probe. In every sample, the first successful SSH probe
already found an active greeter. This method is therefore limited by firmware,
network and SSH readiness, in addition to the desktop itself. It does not measure
exact first-pixel time, and three samples cannot establish a statistically robust
speedup. All samples are retained; the slow probe is not excluded.

The third boot's guest journal shows SSH listening at 6.895 seconds from kernel
start, an IPv4 lease by 7.050 seconds, and an accepted SSH connection at 7.343
seconds. Its Proxmox start task completed in one second. This narrows the excess
delay to the host/firmware/probe path outside the measured OS startup, but the
exact cause was not established. The final OS critical chain no longer includes
foreign-binary registration.

The intermediate `git-08c7e89dd3b9` image measured OS totals of 7.645, 7.736 and
7.675 seconds, with host probes of 18.536, 17.740 and 18.540 seconds. This showed
essentially unchanged startup and prompted inspection of its boot critical path.
Removing the optional emulator registration was the final refinement.

The first post-update boot is separate: intermediate OS startup 7.807 seconds,
final 8.023 seconds. The corresponding 24.484/24.168-second observations waited
for SSH and completed systemd analysis after an update reboot; they are not
cold-start-to-greeter samples.

Raw evidence is retained with the [final iteration](../iterations/git-de0d8baae5de/README.md).
An initial final-image idle sample is also retained as
`perf-final-idle-inhibitor-failed.json`, but excluded from the matched comparison:
its keep-awake helper ran before login completed and failed. The replacement
sample starts only after the inhibitor is confirmed active.

### Idle activity

Correction recorded September 9 while creating the metrics history: the raw
baseline files record **10 seconds** settling and the final files record **20
seconds**, followed in both cases by 120 seconds sampled every five seconds.
The settling conditions therefore were not perfectly matched. The screen was
kept awake, Temp was set to Never, and no other guest tests ran during sampling.
Keep the values as observations with this limitation; future comparisons should
use the same settling interval on both sides.

| Metric | Baseline closed | Final closed | Baseline Temp visible | Final Temp visible |
| --- | ---: | ---: | ---: | ---: |
| Median aggregate CPU | 0.300% | 0.150% | 0.325% | 0.150% |
| Median memory used | 1,054.0 MiB | 935.3 MiB | 1,152.9 MiB | 1,020.5 MiB |
| Context switches / second | 173.2 | 158.7 | 188.9 | 145.0 |
| Processes started | 6 | 5 | 6 | 4 |
| Temp cleanup service starts | 2 | 0 | 2 | 0 |

Memory is `MemTotal - MemAvailable`; CPU is normalized across the four vCPUs.
These short samples show lower idle activity and memory accounting. Cache state
and other shared-host work are not controlled, so the differences are observations
under this protocol, not guaranteed savings. The direct scheduler result is that
Never does not launch recurring cleanup work. Context switches are not a hardware
wakeup measurement, and neither CPU nor memory proves longer battery runtime.

### Native window launch

| App | Baseline samples | Final samples | Baseline median | Final median |
| --- | --- | --- | ---: | ---: |
| Temp | 414.9 / 476.7 / 404.3 ms | 435.8 / 421.8 / 420.8 ms | 414.9 ms | 421.8 ms |
| Welcome | 367.1 / 392.8 / 390.1 ms | 469.1 / 366.4 / 365.0 ms | 390.1 ms | 366.4 ms |

All three process runs are included, including the first. Temp's median increased
6.9 ms (1.7%); Welcome's decreased 23.7 ms (6.1%). No general app-launch speedup is
claimed. This times the first GTK map from Python process startup, including
imports, without controlling filesystem caches or measuring the compositor's
first presented frame. Temp's former shutdown exception is absent in the final
launch/close runs.
