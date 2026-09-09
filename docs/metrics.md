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

## Entry template

Copy this compact block to the end of the file for every future run. Replace
`TBD` only with values present in the run evidence; keep `unknown` when a source
does not contain an exact measurement timestamp.

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
