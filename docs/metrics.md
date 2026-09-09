# Zeus OS metrics history

This file is an append-only record of measured observations. Add a new dated
entry at the end of the history; keep earlier entries, raw samples, failures,
and regressions intact.

`Recorded at (UTC)` is an RFC 3339 timestamp for when the entry was transcribed
into this log. It is not a claim about when the measurement ran. `Measurement
date` is the date stated by the source. Where the source contains no event
timestamp, the exact measurement time is recorded as unavailable instead of
being inferred from a filename, commit, or log-edit time.

Units are part of every result: seconds (`s`), milliseconds (`ms`), MiB,
aggregate guest CPU percent (`%`), context switches per second (`/s`), and
counts. Idle CPU is normalized across the guest's vCPUs. Idle memory is
`MemTotal - MemAvailable`. A median is calculated from the listed samples; all
samples remain linked and visible.

No VM entry is a physical battery measurement. Runtime, wattage, and
battery-hours are left unreported until the same physical laptop can be tested
with the [battery protocol](performance/battery-test-protocol.md). A failed or
unavailable battery run is recorded as such and never converted to zero or an
estimate.

## History

### 2026-09-08 — Preview 1 desktop baseline

- Recorded at (UTC): `2026-09-09T10:08:37Z` (entry transcription)
- Measurement date: `2026-09-08` (the release note gives the deployment date;
  exact measurement time is unavailable)
- Version: `0.1.0-preview.1`
- Source/build: `38bb7fc53002a76dbfa4a5c891c3d1b1bbdb4f20` (the release note
  does not provide a separate build ID)
- Environment: VM 115, 4 vCPUs, 8 GiB RAM, 64 GiB disk, 1280x800, VirtIO
  software rendering (`kms_swrast`), shared host
- Protocol: 60 seconds settling followed by five samples at four-second
  intervals for idle; cold boot was timed from the host `qm start` command,
  with SSH GDM readiness and one-second console polling for the visible greeter

| Metric | Samples or result | Median/result | Unit |
| --- | --- | ---: | --- |
| Zeus settled idle aggregate CPU | 0.877, 0.063, 0.250, 0.125, 0.188 | 0.188 | % |
| Zeus settled idle memory | 909.9, 909.5, 908.1, 908.1, 908.1 | 908.1 | MiB |
| Stock-GNOME presentation idle CPU | 0.250, 0.063, 0.187, 0.125, 0.187 | 0.187 | % |
| Stock-GNOME presentation idle memory | 882.1, 882.1, 882.1, 882.1, 882.1 | 882.1 | MiB |
| Cold boot to visible greeter | — | 21.83 | s |
| Kernel + initrd + systemd userspace boot | — | 7.774 | s |

The release note describes the 26.0 MiB memory difference and the 0.001
percentage-point CPU difference as a single VM sample, below useful CPU
significance. Virtual graphics and a shared host limit interpretation; this
entry does not qualify laptop GPU, suspend, input hardware, or battery life.

Raw evidence: [Preview 1 release note](releases/preview-1.md), [idle Zeus
JSON](releases/preview-1/idle-zeus.json), [idle baseline
JSON](releases/preview-1/idle-baseline.json), [cold-boot
JSON](releases/preview-1/cold-boot.json), and the [declared Preview 1
protocol](releases/preview-1-budgets.md).

### Measurement date unavailable — Preview 2 settled idle point (historical)

- Recorded at (UTC): `2026-09-09T10:08:37Z` (entry transcription)
- Measurement date: not stated in the source; exact measurement timestamp is
  unavailable
- Version: `0.1.0-preview.2`
- Source/build: `c11e57b8569b2332bbd570b6aef51d79b5c38cec` (the receipt has no
  separate build ID)
- Environment: VM 115, 4 vCPUs, 8 GiB RAM, 64 GiB disk, software-rendered
  Proxmox console; host contention was not isolated in the source
- Protocol: 60 seconds settling, then five samples at four-second intervals

| Metric | Samples | Median | Unit |
| --- | --- | ---: | --- |
| Zeus settled idle aggregate CPU | 0.313, 0.250, 0.375, 0.125, 0.313 | 0.313 | % |
| Zeus settled idle memory | 1,053.8 for each sample | 1,053.8 | MiB |

This is a historical single-image idle point and is not a controlled comparison
with the later 120-second matched run. It is VM activity evidence, not a
physical energy or battery result.

Raw evidence: [Preview 2 release note](releases/preview-2.md), [idle JSON](releases/preview-2/idle-zeus.json), and [build receipt](releases/preview-2/build-receipt.json).

### 2026-09-09 — matched baseline and current deployed payload

- Recorded at (UTC): `2026-09-09T10:08:37Z` (entry transcription)
- Measurement date: `2026-09-09` (the report title and filename give the date;
  exact measurement times are unavailable in the raw JSON)
- Version: `0.1.0-preview.2` for both sides
- Baseline: `git-bbd36cc2af2f`, source
  `bbd36cc2af2f0cbf095688f970eaf97141eab1c7`
- Current deployed payload: `git-de0d8baae5de`, source
  `de0d8baae5de8533c97e7287f997235c7a10d43d`
- Environment: the same VM 115 with 4 vCPUs and 8 GiB RAM, disk, graphics,
  display, and Proxmox host; the builder VM was idle, but the shared host was
  not otherwise isolated

The declared comparison protocol used three cold starts from clean shutdown.
OS startup is the `systemd-analyze` kernel/initrd/userspace total. The separate
host probe is the host `qm start` command to an active GDM greeter, with SSH
polled every 0.5 seconds; it includes firmware and probe overhead and is not
first-pixel time. All three samples are retained.

| Cold start | Baseline OS startup (s) | Current OS startup (s) | Baseline host probe (s) | Current host probe (s) |
| --- | ---: | ---: | ---: | ---: |
| 1 | 7.356 | 6.992 | 18.690 | 18.945 |
| 2 | 7.683 | 7.769 | 18.696 | 18.125 |
| 3 | 8.922 | 6.993 | 20.251 | **46.328** |
| Median | **7.683** | **6.993** | **18.696** | **18.945** |
| Change, current minus baseline | — | **-0.690 s (-9.0%)** | — | **+0.249 s (+1.3%)** |

The current OS startup median is lower, while the host-greeter median is higher
and includes a 46.328-second outlier. The report narrows that sample's excess
to the host/firmware/probe path but does not establish an exact cause. Three
samples cannot establish a statistically robust speedup.

The first post-update readiness observations are retained separately because
they are not cold-start-to-greeter samples: intermediate OS startup was 7.807 s
with 24.484 s SSH/systemd readiness, and current OS startup was 8.023 s with
24.168 s readiness. The intermediate refinement (`git-08c7e89dd3b9`) also stays
visible in the report: OS totals 7.645, 7.736, and 7.675 s; host probes 18.536,
17.740, and 18.540 s. These observations have different update/readiness
conditions and are context, not additional matched medians.

For the two-minute idle comparison, the report declares 20 seconds settling,
120 seconds of sampling, five-second intervals, screen awake, Temp set to
Never, and no concurrent guest tests. The raw baseline JSON files say
`settle_seconds: 10`, while the final JSON files say `settle_seconds: 20`.
That source discrepancy is retained here as a protocol limitation; the values
remain useful observations but should not be described as perfectly matched
settling conditions.

| State and metric | Baseline median | Current median | Unit |
| --- | ---: | ---: | --- |
| Closed, aggregate CPU | 0.300 | **0.150** | % |
| Temp visible, aggregate CPU | 0.325 | **0.150** | % |
| Closed, memory used | 1,054.0 | 935.3 | MiB |
| Temp visible, memory used | 1,152.9 | 1,020.5 | MiB |
| Closed, context switches | 173.2 | 158.7 | /s |
| Temp visible, context switches | 188.9 | 145.0 | /s |
| Closed, processes started | 6 | 5 | count / 120 s |
| Temp visible, processes started | 6 | 4 | count / 120 s |
| Closed, Temp cleanup service starts | 2 | 0 | count / 120 s |
| Temp visible, Temp cleanup service starts | 2 | 0 | count / 120 s |

These are guest counters. Context switches are not hardware wakeups, and lower
CPU or memory does not prove longer battery runtime. The failed initial current
idle attempt is retained as a failure: its keep-awake helper ran before login
completed, so `perf-final-idle-inhibitor-failed.json` is excluded from the
comparison rather than silently folded into the median.

Native launch timing used three separate Python processes per app, from process
startup to first GTK map. It includes imports, includes the first run, does not
measure a compositor-presented frame, and does not control filesystem caches.

| App | Baseline samples (ms) | Current samples (ms) | Baseline median (ms) | Current median (ms) | Change |
| --- | --- | --- | ---: | ---: | ---: |
| Temp | 414.9 / 476.7 / 404.3 | 435.8 / 421.8 / 420.8 | 414.9 | 421.8 | +6.9 ms (+1.7%) |
| Welcome | 367.1 / 392.8 / 390.1 | 469.1 / 366.4 / 365.0 | 390.1 | 366.4 | -23.7 ms (-6.1%) |

No general application-launch speedup is claimed: Temp's median increased and
Welcome's decreased under this small, cache-uncontrolled sample.

Raw evidence: [matched performance report](performance/boot-power-20260909.md),
[current README](iterations/git-de0d8baae5de/README.md), [current build
info](iterations/git-de0d8baae5de/build-info.json), [current build
receipt](iterations/git-de0d8baae5de/build-receipt.json), [payload CI
record](iterations/git-de0d8baae5de/payload-ci.json), [baseline build
receipt](iterations/git-bbd36cc2af2f/build-receipt.json), [baseline boot 1](iterations/git-de0d8baae5de/boot-before-1.json), [baseline boot 2](iterations/git-de0d8baae5de/boot-before-2.json), [baseline boot 3](iterations/git-de0d8baae5de/boot-before-3.json), [current boot 1](iterations/git-de0d8baae5de/boot-final-1.json), [current boot 2](iterations/git-de0d8baae5de/boot-final-2.json), [current boot 3](iterations/git-de0d8baae5de/boot-final-3.json), [intermediate boot 1](iterations/git-de0d8baae5de/boot-after-1.json), [intermediate boot 2](iterations/git-de0d8baae5de/boot-after-2.json), [intermediate boot 3](iterations/git-de0d8baae5de/boot-after-3.json), [first intermediate update boot](iterations/git-de0d8baae5de/boot-first-after-update.json), [first current update boot](iterations/git-de0d8baae5de/boot-first-final-update.json), [baseline closed idle](iterations/git-de0d8baae5de/perf-before-idle.json), [baseline Temp-visible idle](iterations/git-de0d8baae5de/perf-before-open.json), [current closed idle](iterations/git-de0d8baae5de/perf-final-idle.json), [current Temp-visible idle](iterations/git-de0d8baae5de/perf-final-open.json), [excluded failed idle](iterations/git-de0d8baae5de/perf-final-idle-inhibitor-failed.json), and [launch samples](iterations/git-de0d8baae5de/launch-samples.json).

The battery probe also has an explicit retained result: [`battery-vm-unavailable.json`](iterations/git-de0d8baae5de/battery-vm-unavailable.json) reports `status: error` and `error: no_battery`. The VM therefore has no physical battery result, estimated battery-hours result, wattage result, or runtime claim.

### 2026-09-09 — pre-updater idle baseline and A bootstrap (intermediate)

- Recorded at (UTC): `2026-09-09T11:18:29Z` (entry transcription)
- Pre-updater idle build: `0.1.0-preview.2`, `git-de0d8baae5de`, source
  `de0d8baae5de8533c97e7287f997235c7a10d43d`
- Subsequent A bootstrap warm-reboot build: `0.1.0-preview.2`,
  `git-21a465760f53`, source `21a465760f53cb7882b3ae65d527532cfd336ead`
- Environment: existing populated VM 115, 4 vCPUs, 8 GiB RAM, with a long-lived
  logged-in session and the same owner state used for the updater qualification
- Idle measurement window (UTC):
  `2026-09-09T10:34:03.238700597Z` → `2026-09-09T10:36:03.250473641Z`
- Idle protocol: 20 seconds settling, 120.01 seconds measured, five-second
  samples (24 rows), closed applications, awake display, and Temp set to
  `Never`

| Metric | Result | Unit |
| --- | ---: | --- |
| Settled aggregate guest CPU median | 0.15 | % |
| Settled memory-used median | 1,127.2 | MiB |
| Context switches | 136.3 | /s |
| Processes started | 6 | count / 120 s |
| Temp cleanup activations | 0 | count / 120 s |

The idle result is the pre-updater reference on de0; the warm reboot result is
A's bootstrap. They are distinct observations and do not establish an
unmatched de0-versus-updater speedup. The idle result is a VM activity
observation; lower guest activity does not imply lower physical power use.

A's explicit warm update boot was measured from
`2026-09-09T10:43:14.101303+00:00` to `2026-09-09T10:43:39.194244+00:00`:
`25.093 s` to changed-boot-ID SSH/GDM readiness and `8.774 s` reported OS
startup. This is one warm reboot sample, not a cold-start or first-pixel
measurement.

Raw evidence: [A qualification README](iterations/git-21a465760f53/README.md),
[A build info](iterations/git-21a465760f53/build-info.json), [Pre-updater idle
JSON (de0)](iterations/git-21a465760f53/idle-before-updater.json), [A warm-boot
JSON](iterations/git-21a465760f53/first-update-boot.json), [A payload CI](iterations/git-21a465760f53/payload-ci.json), [A asset verification](iterations/git-21a465760f53/asset-verification.json), and [A update manifest](iterations/git-21a465760f53/update-git-21a465760f53.json).

### 2026-09-09 — A → B native update observations (`git-1a34bbfe8509`, intermediate)

- Recorded at (UTC): `2026-09-09T11:18:29Z` (entry transcription)
- Version: `0.1.0-preview.2`
- Previous/current at update start: A, `git-21a465760f53`
- Candidate and booted payload: B, `git-1a34bbfe8509`, source
  `1a34bbfe8509aee7cb3271fe7ecead25d53afe6c`, sequence `1788950905`
- Staged and booted image manifest:
  `sha256:cf9c53528f2a3ee514cabbb4e0b09ed1e05ee215450748b8bb57e2b6d8eb43f9`
- Protocol: owner-driven native Updates flow on the existing populated review
  VM; cancel Polkit first, retry authentication, allow background download and
  staging, close/reopen the UI, then accept the explicit restart dialog

| Observation | Result | Unit/meaning |
| --- | ---: | --- |
| Native authentication submit → root-ready | 54.0 | s |
| Download progress before → after window close | 661,651,456 → 737,148,928 of 1,828,930,560 | bytes |
| Explicit Updates restart → changed-boot-ID SSH/GDM | 22.631 | s |
| OS startup in that update boot | 7.171 | s |
| Permanent-file hashes retained | 6 | checks, all `OK` |
| Existing Temp sample retained | 48 | bytes; policy `Never` |

The install measurement window is
`2026-09-09T10:55:37+00:00` → `2026-09-09T10:56:31+00:00`. The update-boot
measurement window is `2026-09-09T10:58:19.871391+00:00` →
`2026-09-09T10:58:42.502324+00:00`. The 54.0-second interval includes password
entry, verified fetch, download, and staging. The 22.631-second interval is a
single native warm reboot to SSH/GDM readiness; its 7.171-second OS total is
`1.375 s` kernel + `2.339 s` initrd + `3.456 s` userspace. Neither is a cold
boot, first-pixel, or application-launch benchmark.

The Polkit-cancel path left the install service inactive and the candidate
available. After retry, the download advanced while the Updates window was
closed, reopening showed `state: staging`, and the root job reached `state:
ready` before the user-authorized restart. B then booted successfully. The
post-boot status file still has the stale human message `A signed update is
available` despite structured `state: up_to_date` and B as the current build.
That regression keeps B intermediate. Final build `git-17d205103f3a` is
recorded separately below; no final numbers are included in this entry.

No rollback cycle or rollback/re-forward result is claimed. The VM has no
physical battery, so this update observation supplies no battery-runtime,
battery-hours, wattage, or hardware-power result and cannot be used as an
unmatched speedup claim.

Raw evidence: [B qualification README](iterations/git-1a34bbfe8509/README.md),
[B build info](iterations/git-1a34bbfe8509/build-info.json), [B update
manifest](iterations/git-1a34bbfe8509/update-git-1a34bbfe8509.json), [B CI](iterations/git-1a34bbfe8509/updater-b-ci.json), [auth-cancel state](iterations/git-1a34bbfe8509/updater-auth-cancel-state.txt), [install timing](iterations/git-1a34bbfe8509/updater-install-timing.json), [before-close state](iterations/git-1a34bbfe8509/updater-before-close.json), [after-close state](iterations/git-1a34bbfe8509/updater-after-close.txt), [reopened staging state](iterations/git-1a34bbfe8509/updater-reopened-state.json), [ready job state](iterations/git-1a34bbfe8509/updater-current-job.json), [staged identity](iterations/git-1a34bbfe8509/updater-b-stage-verification.json), [asset verification](iterations/git-1a34bbfe8509/updater-b-asset-verification.json), [B first-boot timing](iterations/git-1a34bbfe8509/updater-b-first-boot.json), [booted image](iterations/git-1a34bbfe8509/updater-b-booted.json), [booted status](iterations/git-1a34bbfe8509/updater-b-booted-status.txt), [service journal](iterations/git-1a34bbfe8509/updater-service-journal.txt), [preservation checks](iterations/git-1a34bbfe8509/updater-b-preserved.txt), and [Temp state](iterations/git-1a34bbfe8509/updater-b-temp.json).

### 2026-09-09 — final D cold boot, idle, and B → D update (`git-17d205103f3a`)

- Recorded at (UTC): `2026-09-09T11:28:58Z` (entry transcription)
- Version/build: `0.1.0-preview.2`, `git-17d205103f3a`, source
  `17d205103f3aa885fe682d06a724f37e805a15a2`, sequence `1788951816`
- Environment: VM 115, `amd64`, 4 vCPUs, 8 GiB RAM, 64 GiB disk, VirtIO
  software graphics, 1280x800; builder VM 116 was stopped during the cold and
  idle runs, while the shared Proxmox host was otherwise unisolated
- Update path: native B → D update, with B retained as the rollback deployment

Cold-start protocol used three clean shutdown/start trials. The host probe is
`qm start` to active GDM via SSH polling every 0.5 seconds; OS startup is the
separate `systemd-analyze` total. The host probe includes firmware and probe
overhead and does not measure first-pixel time.

| Trial | Measurement window (UTC) | OS startup (s) | Host → GDM probe (s) |
| --- | --- | ---: | ---: |
| 1 | `2026-09-09T11:18:54.990192889Z` → `2026-09-09T11:19:13.740678662Z` | 6.849 | 18.541 |
| 2 | `2026-09-09T11:20:04.495669497Z` → `2026-09-09T11:20:22.330277406Z` | 6.835 | 17.635 |
| 3 | `2026-09-09T11:20:28.061828024Z` → `2026-09-09T11:20:46.868044459Z` | 6.861 | 18.574 |
| Median | — | **6.849** | **18.541** |

Idle protocol used a freshly logged-in session after the cold trials, 20 seconds
settling, 120.01 seconds measured, 24 five-second samples per state, an awake
display, Temp set to `Never`, and no concurrent guest tests.

| State | Measurement window (UTC) | CPU median | Memory median | Context switches | Processes started | Temp starts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Closed | `2026-09-09T11:22:12.030873406Z` → `2026-09-09T11:24:12.041708493Z` | **0.15%** | 964.6 MiB | 128.3 /s | 3 | 0 / 120 s |
| Updates open | `2026-09-09T11:25:07.584588199Z` → `2026-09-09T11:27:07.594766876Z` | **0.15%** | 1,029.5 MiB | 129.2 /s | 8 | 0 / 120 s |

The final D cold and idle observations are under different conditions from the
earlier de0 observations: D stopped builder VM 116, while the earlier run had
the builder idle and used a different session history. For orientation only,
de0's existing matched record above reports 6.993 s OS startup and 18.945 s
host-probe median, including its retained 46.328 s sample. These are separate
observations, so this entry claims no controlled performance win and no battery
inference.

The native install from B to D ran from
`2026-09-09T11:16:13.705339+00:00` to `2026-09-09T11:17:08.854175+00:00` and
took **55.149 s**. It includes authentication entry, signed metadata fetch,
download, staging, and up to three seconds of status-probe overhead. The
subsequent explicit restart ran from `2026-09-09T11:17:52.605446+00:00` to
`2026-09-09T11:18:18.234951+00:00`, taking **25.629 s** to SSH/GDM readiness;
the reported OS startup was **8.518 s** (`1.426 s` kernel + `3.433 s` initrd +
`3.658 s` userspace). Both are one-sample update observations, not cold-start
or first-pixel benchmarks.

The local status captured before a new network check reports D as `up_to_date`
with message `The selected update is installed.`; the later checked status
retains the same corrected message. This closes B's stale-status regression.
No new app-launch or physical battery measurement was made here. The VM result
does not establish wattage, runtime, battery-hours, suspend drain, or hardware
power behavior; no rollback cycle was run in this iteration.

Raw evidence: [final README](iterations/git-17d205103f3a/README.md), [build
info](iterations/git-17d205103f3a/build-info.json), [update manifest](iterations/git-17d205103f3a/update-git-17d205103f3a.json), [payload CI](iterations/git-17d205103f3a/payload-ci.json), [cold boot 1](iterations/git-17d205103f3a/cold-boot-1.json), [cold boot 2](iterations/git-17d205103f3a/cold-boot-2.json), [cold boot 3](iterations/git-17d205103f3a/cold-boot-3.json), [closed idle](iterations/git-17d205103f3a/idle-closed.json), [Updates-open idle](iterations/git-17d205103f3a/idle-updates-open.json), [native install timing](iterations/git-17d205103f3a/native-install-timing.json), [native update boot](iterations/git-17d205103f3a/native-update-boot.json), [status before check](iterations/git-17d205103f3a/installed-status-before-check.json), [checked status](iterations/git-17d205103f3a/checked-status.json), [staged bootc identity](iterations/git-17d205103f3a/staged-bootc.json), [booted bootc identity](iterations/git-17d205103f3a/booted-bootc.json), and [the earlier de0 outlier](iterations/git-de0d8baae5de/boot-final-3.json).

### 2026-09-09 — laptop Settings, matched preview comparison

- Recorded at (UTC): `2026-09-09T14:11:01.144645+00:00` (entry transcription)
- Measurement date: `2026-09-09`; actual UTC bounds are retained in every raw JSON
- Version: **0.1.0-preview.2** on both sides
- Baseline: `git-17d205103f3a`, source `17d205103f3aa885fe682d06a724f37e805a15a2`
- Candidate: `git-fd2125f63159`, source `fd2125f6315947d97292c4f3a250c3b91d742280`
- Environment: VM 115, 4 vCPUs, 8 GiB RAM, 64 GiB disk, same Proxmox host and
  software-rendered 1280x800 console. Builder VM 116 was stopped for both sets;
  backup validation had finished before baseline collection. The shared host
  was otherwise unisolated.
- Protocol: three cold starts after graceful shutdown, followed by a fresh login
  for closed-desktop idle. Idle used 20 seconds settling, 120 seconds sampling,
  five-second intervals, awake display, Temp held at Never and no concurrent
  guest tests or idle inhibitor. Candidate Settings-open idle followed closed
  idle in the same session. Temp was restored to On boot after all test boots.

| Cold start | Baseline OS (s) | Candidate OS (s) | Baseline host probe (s) | Candidate host probe (s) |
| --- | ---: | ---: | ---: | ---: |
| 1 | 7.537 | 7.686 | 19.004 | 18.755 |
| 2 | 7.343 | 7.901 | 18.708 | 20.276 |
| 3 | 6.964 | **10.818** | 18.680 | **27.868** |
| Median | **7.343** | **7.901** | **18.708** | **20.276** |
| Candidate minus baseline | — | **+0.558 s (+7.6%)** | — | **+1.568 s (+8.4%)** |

OS startup is the systemd kernel/initrd/userspace total. The host probe measures
`qm start` to an active GDM greeter using 0.5-second SSH polling; it includes
firmware and probe overhead, not exact first-pixel presentation. Baseline cold
measurements span `2026-09-09T13:01:26.285074190Z` through
`2026-09-09T13:02:36.075113571Z`; candidate cold measurements span
`2026-09-09T13:57:15.088313482Z` through `2026-09-09T13:58:33.798359405Z`.
Consult the raw files for each trial's complete timestamps.

The slower third candidate run is retained. Its
[critical chain](iterations/git-fd2125f63159/cold-3-critical-chain.txt) and
[unit timings](iterations/git-fd2125f63159/cold-3-blame.txt) show time in existing
filesystem, D-Bus and NetworkManager startup. Settings adds no startup unit, but
these traces do not isolate the cause of the slower run. No speedup is claimed.

| Idle state | Measurement UTC bounds | CPU median | Memory median | Context switches | Processes started | Temp starts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Baseline closed | `2026-09-09T13:04:16.336849753Z` → `2026-09-09T13:06:16.347699765Z` | 0.125% | 957.1 MiB | 123.9 /s | 4 | 0 |
| Candidate closed | `2026-09-09T13:59:57.166298170Z` → `2026-09-09T14:01:57.178210446Z` | 0.150% | 954.2 MiB | 125.3 /s | 3 | 0 |
| Candidate Settings open | `2026-09-09T14:03:10.289495250Z` → `2026-09-09T14:05:10.299693047Z` | 0.125% | 1021.3 MiB | 126.9 /s | 8 | 0 |

Closed idle changed by **+0.025 CPU percentage points / -2.9 MiB**. Settings open
used **67.1 MiB** more whole-guest memory than closed in this pair of windows;
CPU variation is too small to assign a causal improvement. The planned review
thresholds (+0.25 CPU percentage points, +64 MiB closed memory, +1 s median OS
startup) were not exceeded. Three boot trials and one idle window per state do
not establish a statistically robust performance result. All samples are kept.

The RPM inventory remained byte-identical at **1,011 packages**. The full OCI
archive grew from **1,828,931,072** to **1,829,006,336 bytes**, a **75,264-byte
(73.5 KiB)** increase. No new package, background service, timer or recurring
status refresh was introduced. Closing Settings ends its process.

Native installation ran from `2026-09-09T13:53:38Z` to `2026-09-09T13:54:58Z`
and took **79.701 s**, including authentication entry,
metadata fetch, download, staging and up to three seconds of status-probe
overhead. The explicit warm update reboot ran from
`2026-09-09T13:55:57.124166+00:00` to `2026-09-09T13:56:24.815048+00:00`,
taking **27.691 s** to changed boot ID and SSH/GDM readiness;
OS startup was **10.410 s**. These are separate one-sample update observations.

No physical battery, energy, suspend-drain or radio measurement was possible on
this VM. No new installer or VM rollback cycle was tested. The populated backup,
owner-file hashes, original preferences and restored Temp policy are recorded in
the [iteration receipt](iterations/git-fd2125f63159/README.md).

Raw evidence: [baseline boot 1](iterations/git-fd2125f63159/baseline-cold-1.json),
[baseline boot 2](iterations/git-fd2125f63159/baseline-cold-2.json),
[baseline boot 3](iterations/git-fd2125f63159/baseline-cold-3.json),
[candidate boot 1](iterations/git-fd2125f63159/cold-1.json),
[candidate boot 2](iterations/git-fd2125f63159/cold-2.json),
[candidate boot 3](iterations/git-fd2125f63159/cold-3.json),
[baseline idle](iterations/git-fd2125f63159/baseline-idle-closed.json),
[candidate idle](iterations/git-fd2125f63159/idle-closed.json),
[Settings-open idle](iterations/git-fd2125f63159/idle-settings-open.json),
[comparison](iterations/git-fd2125f63159/performance-comparison.json),
[image cost](iterations/git-fd2125f63159/image-cost.json),
[install timing](iterations/git-fd2125f63159/native-install-timing.json), and
[update reboot](iterations/git-fd2125f63159/native-update-boot.json).

### 2026-09-09 — AirPods software prerequisites and build gate

- Recorded at (UTC): `2026-09-09T18:48:32Z`
- Inventory timestamp: `2026-09-09T18:25:40.955453+00:00`; builder verification:
  `2026-09-09T18:46:47.288265+00:00`.
- Version: `0.1.0-preview.2`. Deployed image: `git-fd2125f63159`;
  build-gate candidate: `git-9b2977112916`, built on dedicated VM 116.
- Environment: Proxmox review VM 115, 4 vCPU / 8 GiB RAM; no exposed Bluetooth
  controller, microphone or physical battery. Host also exposed no Bluetooth controller.
- Protocol: read-only installed program/plugin inventory, isolated plugin loads,
  actual image build, before/after audio-policy comparison and package-lock hash comparison.

| Check | Result |
| --- | --- |
| Required Bluetooth/audio programs | 6 of 6 present |
| BlueZ backend and AAC/SBC/CVSD/mSBC plugins | 5 of 5 dynamically loadable on VM and builder image |
| Python source tests | 148 passed, including 12 Bluetooth validator tests |
| Installed RPM inventory | 1,011 packages; candidate and deployed inventory byte-identical |
| WirePlumber settings after read-only VM check | Unchanged, ignoring one trailing output newline |
| Physical AirPods qualification | Not tested; model and controller/device pair unavailable |

The gate adds no runtime package, service, timer or discovery loop. It was removed
from the candidate's final filesystem. The owner VM was not updated or rebooted.
No new boot, idle, archive-size, audio-dropout, radio or physical battery measurements
were collected. These checks establish installed software prerequisites only;
ANC, battery reporting, ear detection, audible quality and reconnect remain unqualified.

Evidence: [installed-image check](features/airpods/installed-image-check.json),
[builder result](features/airpods/builder-check.json),
[package and policy preservation](features/airpods/validation-summary.json),
[source CI](https://github.com/KanterLabs/zeusos/actions/runs/34390942069),
and [AirPods implementation/testing contract](features/airpods.md).

### 2026-09-09 — New York desktop, lock and login

- Recorded at (UTC): `2026-09-09T21:09:32.401394+00:00`.
- Version/build: `0.1.0-preview.2` / `git-31f0851a9d07`; baseline `git-fd2125f63159`.
- Environment: persistent Proxmox VM115, 4 vCPU, 8 GiB RAM, 64 GiB disk,
  VirtIO GPU, 1280×800 at 100%, UEFI Secure Boot and enforcing SELinux.
- Protocol: three cold starts per image; 20 s settling then 120 s closed-desktop
  idle with 5 s samples. Temp Never; builder stopped; backup work complete.
  Shared host otherwise unisolated. Thresholds were [declared before sampling](iterations/git-31f0851a9d07/measurement-plan.json).

| Measurement | Before samples | Candidate samples | Before / candidate median | Unit |
| --- | --- | --- | --- | --- |
| OS startup | 7.041, 7.168, 6.925 | 6.834, 7.265, 6.797 | 7.041 / 6.834 | s |
| Host start to active GDM | 17.699, 18.660, 18.513 | 17.609, 18.646, 17.599 | 18.513 / 17.609 | s |
| Closed idle CPU | qualifying repeat | fresh candidate login | 0.150 / 0.150 | % |
| Closed idle memory | qualifying repeat | fresh candidate login | 1,057.5 / 904.7 | MiB |
| Temp cleanup activations | 0 | 0 | 0 / 0 | count |
| Runtime packages | 1,011 | 1,011 | unchanged | count |
| Full OCI size | 1,829,006,336 | 1,834,208,768 | +5,202,432 (4.96 MiB) | bytes |

Cold-start UTC windows (host measurement bounds):

| Trial | Before | Candidate |
| --- | --- | --- |
| 1 | `2026-09-09T20:14:47.993143034Z` → `2026-09-09T20:15:05.877454016Z` | `2026-09-09T20:58:08.973386375Z` → `2026-09-09T20:58:26.749552759Z` |
| 2 | `2026-09-09T20:15:11.479524005Z` → `2026-09-09T20:15:30.306703191Z` | `2026-09-09T20:58:32.266006924Z` → `2026-09-09T20:58:51.100078005Z` |
| 3 | `2026-09-09T20:15:36.609650413Z` → `2026-09-09T20:15:55.315955574Z` | `2026-09-09T20:59:41.310649492Z` → `2026-09-09T20:59:59.075997711Z` |

Qualifying baseline idle ran from `2026-09-09T20:31:40.825480479Z`
to `2026-09-09T20:33:40.836128575Z`; candidate idle ran from
`2026-09-09T21:02:32.783349962Z` to
`2026-09-09T21:04:32.793998188Z`. Baseline repeat had
121.5 context switches/s and 7 processes started; candidate had 124.0/s and 3.

The first baseline idle (20:20:09.548967338Z–20:22:09.559557090Z) measured
0.150% CPU and 939.8 MiB. Three failed read-only guest probes overlapped it;
retain it as observational evidence and use the repeat for the declared comparison.
The repeat followed extra lock/unlock activity; candidate idle followed a fresh
login. The before/after values do not establish a lasting speedup or memory
reduction. Every cold-start sample is retained. No declared review threshold
was exceeded; no new runtime package, service or recurring timer was added.

Native installation ran from `2026-09-09T20:46:42.094667+00:00` to
`2026-09-09T20:47:57.686851+00:00` (**75.592 s**), including Polkit
entry, metadata/archive fetch, staging and up to 5 s observation overhead.
The explicit update reboot is separate from cold-start comparison. During public
feed propagation the client briefly reported a verification failure; the later
correct signed pair verified before installation. The exact failed pair was not
retained. No physical battery, energy, radio or
suspend-drain claim follows from these VM results.

The [receipt and screenshots](iterations/git-31f0851a9d07/README.md) record native
100%/200%, high contrast, fallback and password tests, the verified populated
backup, seven preserved file hashes, restored Temp On boot and owner preferences.
Raw evidence: [boot/idle comparison](iterations/git-31f0851a9d07/performance-comparison.json),
[initial baseline](iterations/git-31f0851a9d07/baseline-idle-closed.json),
[baseline repeat](iterations/git-31f0851a9d07/baseline-idle-closed-repeat.json),
[candidate idle](iterations/git-31f0851a9d07/candidate-idle-closed.json),
[image cost](iterations/git-31f0851a9d07/image-cost.json),
[install](iterations/git-31f0851a9d07/native-install-timing.json) and
[update reboot](iterations/git-31f0851a9d07/update-reboot-observation.json).

### 2026-09-09 — Chrome default and native Temp qualification

- Recorded at (UTC): `2026-09-09T22:52:41.677903+00:00`.
- Version/build: `0.1.0-preview.2` / `git-9c2cfbdcb703`; historical reference
  `git-31f0851a9d07`.
- Environment: VM115, 4 vCPU, 8 GiB, 64 GiB VirtIO, 1280×800/100%, UEFI
  Secure Boot and enforcing SELinux. Builder stopped; backup verification
  finished; Temp held Never during sampling and restored On boot afterward.
- Protocol: three cold starts; separate closed-desktop and one-tab Chrome idle
  windows, each with 20 s settle, 120 s duration and 5 s samples. No guest
  probes or GUI actions overlapped the idle windows; shared host unisolated.

| Measurement | City reference | Chrome build | Unit |
| --- | --- | --- | --- |
| OS startup samples | 6.834, 7.265, 6.797 | 7.033, 7.030, 7.108 | s |
| OS startup median | 6.834 | 7.033 | s |
| Host-to-GDM samples | 17.609, 18.646, 17.599 | 46.938, 17.681, 17.606 | s |
| Host-to-GDM median | 17.609 | 17.681 | s |
| Closed idle CPU | 0.150 | 0.150 | % |
| Closed idle memory | 904.7 | 911.3 | MiB |
| Chrome open idle CPU / memory | not measured | 0.200 / 1,253.1 | % / MiB |
| Runtime packages | 1,011 | 1,023 | count |
| OCI size | 1,834,208,768 | 1,925,267,456 | bytes |

Cold-start measurement windows:

| Trial | UTC start | UTC end |
| --- | --- | --- |
| 1 | `2026-09-09T22:32:57.609645040Z` | `2026-09-09T22:33:44.709277318Z` |
| 2 | `2026-09-09T22:34:57.028778909Z` | `2026-09-09T22:35:14.871813299Z` |
| 3 | `2026-09-09T22:36:08.061553721Z` | `2026-09-09T22:36:25.843579879Z` |

Closed idle ran `2026-09-09T22:38:17.950163646Z` →
`2026-09-09T22:40:17.961012087Z`: 128.9 context switches/s,
6 process starts and 0 Temp cleanup activations. Chrome-open idle ran
`2026-09-09T22:44:09.432502833Z` →
`2026-09-09T22:46:09.442765694Z`: 159.5 context switches/s,
16 process starts and 0 Temp cleanup activations. Its single visible page was
HTTPS Example Domain; browser initialization and this fresh profile remain
part of the workload. Closing the final window left no Chrome or crashpad
processes within the 0.481 s observation.

The first 46.938 s host sample is retained; its extra host/firmware/network/probe
delay is unexplained while OS startup remained 7.033 s. Median OS startup is
0.199 s above the historical reference and closed memory is 6.6 MiB higher;
these are small shared-host observations, not a controlled Firefox comparison.
The full OCI grew 86.840 MiB. Three SELinux RPMs also advanced with the current
repositories; the package diff records all changes.

Native Updates was clicked at 22:13:52.496652 UTC, authentication submitted at
22:14:40.126594 and ready status observed at 22:15:47.142750. These are observed
UI timings, not pure installer CPU time. The explicit restart-to-new-build SSH
observation was 22.209 s. The publication record retains the briefly premature
feed pointer, its restoration before installation and final verified publication.

159 Python tests, 9 lock lifecycle tests and Rust/CLI checks passed. Native
Chrome defaults, HTTPS, recommended policies, sandbox and a real Temp download
passed; the isolated active-download/Keep fixture passed in 3.624 s with zero
surviving fixture processes. Eight owner hashes and the original VM keyring
are preserved. The earlier login-keyring password mismatch and its local repair
are recorded. Physical battery, radios, AirPods and laptop migration remain
unmeasured/unexecuted; no laptop backup was started.

[Receipt and screenshots](iterations/git-9c2cfbdcb703/README.md),
[comparison and all cold boots](iterations/git-9c2cfbdcb703/performance-comparison.json),
[closed idle](iterations/git-9c2cfbdcb703/idle-closed.json),
[Chrome-open idle](iterations/git-9c2cfbdcb703/idle-chrome-open.json),
[package diff](iterations/git-9c2cfbdcb703/package-diff.json) and
[qualification notes](iterations/git-9c2cfbdcb703/qualification-notes.json).

## Entry template

Insert this compact block immediately above the Entry template section for every
future run, keeping history chronological and the template at the bottom.
Replace `TBD` only with values present in the run evidence; keep `unknown` when
a source does not contain an exact measurement timestamp.

```markdown
### YYYY-MM-DD — short run name

- Recorded at (UTC): `YYYY-MM-DDTHH:MM:SSZ` (when this entry was added)
- Measurement date: `YYYY-MM-DD` or `unknown` (source-reported; exact time if present)
- Version: `...`
- Source/build/payload: `...`
- Environment: hardware or VM ID, CPU/vCPU, RAM, disk, display/GPU, host, and relevant contention
- Protocol: workload, warm-up/settle, sample count/interval, clocks, and comparison controls

| Metric | Samples or result | Median/result | Unit |
| --- | --- | ---: | --- |
| ... | ... | ... | ... |

Status/caveats: retain every sample, outlier, regression, exclusion, and failure;
link raw JSON/log evidence here. Do not infer battery energy, wattage, runtime,
or battery-hours from VM counters.
```
