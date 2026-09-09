# AirPods support

Status: **ZOS-70 is awaiting Bluetooth-equipped test hardware**. The current
owner-provided pair is described as a newer/better AirPods Pro pair, but its
exact model, case model and firmware are **UNKNOWN**. An earlier
screenshot-based identification is stale and is not qualification evidence.
The software readiness gate is implemented and verified; the broader AirPods
support outcome remains incomplete.
The software inventory was verified on `git-fd2125f63159`; this document is an
implementation and qualification contract, not a claim of a tested AirPods pair.
Continue iterating **0.1.0-preview.2** with separate build IDs.

## Current hardware target — exact model pending — 2026-09-09 UTC

The current pair is the first Zeus hardware-qualification target, subject to
recording its exact model and firmware before testing. The prior screenshot and
its inferred model/firmware are not stored as current target evidence in this
repository. Record the model, case model, firmware, Bluetooth adapter/driver
and date again at the start of actual hardware testing; do not inherit results
from an unidentified or untested version.

Use music and calls as the initial qualification workload. First verify native
pairing, AAC/SBC negotiation, playback, headset microphone routing, stereo
restoration and reconnect/suspend behavior. Then qualify the optional left/right/
case battery readings, ANC/Transparency and ear-detection integration on this
model. Other listening modes need confirmed device support before being exposed.

## Experience to deliver

- Pair from Zeus Settings using the native Bluetooth authorization flow.
- Reconnect a previously paired device after opening the case, restarting Zeus,
  waking the laptop and returning within range, without repeated pairing.
- Prefer the best mutually supported stereo playback codec. Report the active
  connection honestly; a codec library being installed does not prove negotiation.
- Make microphone use understandable. Calls can require a lower-quality Bluetooth
  headset profile; offer the laptop microphone route when high-quality listening
  matters, and restore stereo playback after capture ends.
- Show accurate connection and battery information when the device exposes it.
  Distinguish stale/unavailable values; do not invent separate earbud/case levels.
- Add supported noise controls and ear detection through a reviewed implementation
  of the AirPods protocol, with explicit model/firmware qualification.
- Keep idle work proportional to actual use. Avoid continuous discovery when no
  user-requested pairing or relevant device connection requires it.
- Keep owner pairing keys, device names, audio choices and other Bluetooth devices
  intact across updates and rollback. No Bluetooth database reset as a repair step.

## Implementation checklist

- [x] Record actual deployed BlueZ, PipeWire, WirePlumber and codec capabilities.
- [ ] Record the current target model, case model and reported firmware before
  hardware testing. Start with both music and calls.
- [x] Review native-stack gaps and current upstream control implementations;
  retain native audio and scope the optional control adapter separately.
- [x] Gate image builds on the existing audio executables and loadable AAC,
  SBC, CVSD and mSBC plugins, with regression coverage for failures.
- [ ] Implement supported controls with clear missing-adapter, disconnected,
  busy, unavailable-profile and unsupported-model states.
- [ ] Keep core Bluetooth audio functional if optional AirPods controls fail.
- [x] Document which features are provided by standard Bluetooth, which need the
  AirPods protocol, and which remain unavailable or unqualified.

## Testing checklist

- [ ] Exercise native UI and backend success/failure transitions with bounded
  fixtures, including disappearing devices and repeated actions.
- [x] Verify actual installed dependencies and native audio policy values.
- [x] Validate the new readiness gate against the installed VM and an image build.
- [ ] Compare owner settings, audio/pairing state and populated-file hashes before
  and after the change; preserve a verified pre-update backup and retained rollback.
- [ ] Measure dependency/image cost and closed/active idle activity. Record dated
  results in [metrics.md](../metrics.md), with no battery inference from VM counters.
- [ ] On the target hardware, pair once and run at least ten reconnect cycles across
  case close/open, out-of-range return, reboot and suspend/resume.
- [ ] Play audio for 30 minutes and record dropouts, codec/profile and volume behavior.
- [ ] Run a browser call with the AirPods microphone, then with the laptop microphone;
  check input routing and stereo restoration after the call finishes.
- [ ] Verify available left/right/case readings against a current reference, without
  treating a stale or absent reading as zero. Test ANC/Transparency/other modes only
  where that model advertises them; verify controls by listening, not UI state alone.
- [ ] Check ear removal/reinsertion and media behavior on supported models, competing
  Apple-device connections, AirPods firmware, Bluetooth adapter/driver and battery cost.

VM 115 currently has no physical Bluetooth adapter or AirPods. Successful UI and
codec inspection cannot qualify acoustic quality, radio reliability, microphone,
noise cancellation, ear detection or reconnect/suspend behavior.

## Verified native foundation — 2026-09-09 UTC

The [deployed inventory](airpods/capabilities.json) confirms BlueZ 5.87,
PipeWire 1.6.8 and WirePlumber 0.5.14, with AAC, SBC and headset speech codec
plugins. [Observed policy values](airpods/audio-policy.json) enable headset
switching, Bluetooth state persistence and profile/route restoration. This is
software evidence only; it does not prove an AirPods connection or audible sound.
There is no missing codec package to install for the basic playback/call path.

The review VM has only a virtual audio sink and no microphone. Both VM 115 and
the Proxmox host expose zero Bluetooth controllers. No radio was scanned, device
paired, pairing key reset or audio preference changed during this inventory.
The current owner pair is described as a newer/better AirPods Pro pair, but its
exact model and firmware remain unknown; this supplies no physical test result.

## Integration decision

Keep native BlueZ/PipeWire/WirePlumber for audio. Add a build-time software
readiness check so future images cannot silently lose the existing AAC and
microphone codec prerequisites. This check must never report physical AirPods
qualification from installed software alone.

Advanced controls are tracked in **ZOS-71: Add optional AirPods battery and noise
controls to Zeus Settings**, an unclaimed Backlog card. They require a separate
Zeus integration and an actual test pair:

| Candidate inspected | Finding | Decision |
| --- | --- | --- |
| [LibrePods `53679cc`](https://github.com/librepods-org/librepods/tree/53679cc90222e94ade84e66542d97ace2540e626) | The current Linux README directs users to a rewrite/nightlies. The older Linux app needs Qt6; its control client depends on the running tray app. | Keep as protocol/reference evidence; do not add the tray stack to the default image. |
| [Linux Rust branch `672e65a`](https://github.com/librepods-org/librepods/tree/672e65ad36eebf21ff1c1a508066f9197ee56d17/linux-rust) | A GUI/tray rewrite with its own discovery/connection behavior, without a ready external Settings control interface. | Requires lifecycle and integration work before adoption. |
| [librepods-rs `8ec9f45`](https://github.com/brianpht/librepods-rs/tree/8ec9f459aaf833a40c207ff309b8127ef3b65021) | Reusable protocol/Linux crates are promising, but the CLI has no ready control IPC or release history. | Candidate for a small optional adapter after protocol tests, license review and target-hardware validation. |

The prospective adapter should watch connection events for already-paired,
explicitly selected devices, connect its control channel only while needed and
release it on disconnect. Scanning must be user-triggered. Its API should expose
battery freshness and supported noise modes, with confirmed responses before UI
success. Core audio must keep working when the optional adapter is unavailable.

Upstream privilege instructions need independent validation: the Rust transport
opens a `SOCK_SEQPACKET` L2CAP socket using libc; its README/error hint recommends
capabilities or sudo, which does not prove both capabilities are necessary on the
target kernel. Do not copy broad privilege grants into the base image. Determine
the minimum working access on an isolated hardware test target. Global Bluetooth
VendorID changes are outside the default integration; pairing identity and other
Bluetooth devices must remain intact.

No upstream binary was installed or benchmarked in this investigation. Source
inspection does not establish resource use, audio quality or control reliability.
Siri, Find My and Apple-account handoff are not part of the current supported
contract. Hardware qualification must name model, firmware and adapter/driver.

## Build gate and current validation

`image/Containerfile` runs `scripts/validate-bluetooth-audio.py --load-plugins
--json` after package removal. Missing programs, missing codec plugins or failed
dynamic loads stop the image build. The script is removed from the image's final
filesystem; it adds no startup service, timer, discovery loop or runtime package.

Run the same read-only check inside a Zeus image:

```sh
python3 scripts/validate-bluetooth-audio.py --load-plugins --json
```

For path-only tests, `--root ROOT --json` checks a fixture tree. Dynamic loading
is rejected for fixture roots. Each real plugin load runs in a separate process
with a five-second timeout. Reports contain fixed software paths, no device names
or addresses, and always leave physical AirPods qualification as **NOT TESTED**.

The [installed-image result](airpods/installed-image-check.json) passed all six
executable and five plugin checks on VM 115. The
[validation summary](airpods/validation-summary.json) records **148 passing Python
tests**, including **12 focused validator tests**, and unchanged installed package
inventory and WirePlumber settings. The focused suite covers missing AAC, missing
headset codecs, missing audio programs, Fedora's PipeWire symlink, a real loader
subprocess, loader failure/timeout, and forbidden fixture execution.

The [dedicated-builder result](airpods/builder-check.json) confirms the actual
Containerfile gate passed for `git-9b2977112916`, still version
`0.1.0-preview.2`. Its package inventory matches the deployed image exactly at
1,011 packages, and the checker is absent from the final filesystem. Bootc lint
reported 10 checks passed, one skipped and three warnings concerning runtime
directories, package-manager logs and `/var` content; see the retained
[lint excerpt](airpods/builder-lint.txt).
[Source CI](https://github.com/KanterLabs/zeusos/actions/runs/34390942069)
passed both `image-policy` and `cli` jobs. The dated inventory and limits are also
recorded in [metrics.md](../metrics.md).

No new AirPods runtime feature was deployed during that inventory on
`git-fd2125f63159`; it made no reboot or audio-preference changes. Physical pairing,
audio, microphone, battery and noise-control results remain pending.

To resume hardware qualification, use the current owner pair with a
Bluetooth-equipped laptop or attach an available Bluetooth controller to VM 115.
Record its exact model, case model and firmware, plus the adapter/driver, before
testing. Do not reuse the superseded screenshot identification as a test target.
Neither VM 115 nor the current Proxmox host exposed a controller during this
inspection. ZOS-71 remains unclaimed until the advanced-control work is started.

## Upstream references

- [Apple pairing instructions](https://support.apple.com/guide/airpods/pair-airpods-with-a-non-apple-device-dev499c9718b/web)
- [WirePlumber Bluetooth policy](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/settings.html)
- [Fedora Bluetooth audio test case](https://fedoraproject.org/wiki/QA:Testcase_PipeWire_Bluetooth_Devices)
- [LibrePods upstream](https://github.com/librepods-org/librepods)
