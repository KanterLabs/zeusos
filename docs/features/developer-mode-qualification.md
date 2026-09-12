# Developer Mode systemd-sysext qualification

Status: the disposable-guest fixture and evidence contract are checked in;
the measurements below are an observed Fedora 44 guest qualification. The
Developer Mode product path remains gated on this qualification and its
feature-specific tests.

This record covers the reversible systemd-sysext mechanism used by the
Developer Mode desktop lane. It does not qualify the owner laptop, a normal
release update, a bootc image update, or a physical device.

## Fixture and safety contract

Run [`scripts/qualify-developer-sysext.sh`](../../scripts/qualify-developer-sysext.sh)
as root inside an already-installed, disposable Fedora 44 Zeus guest. The
guest must provide:

- `ID=fedora`, `VERSION_ID=44`, a non-empty `SYSEXT_LEVEL`, and systemd 259;
- enforcing SELinux with a readable `/sys/fs/selinux/enforce` value of `1`;
- a regular, root-owned Zeus qualification sentinel at
  `/usr/share/zeus/developer-mode-qualification-sentinel`; and
- the normal `systemd-sysext`, `systemctl`, `journalctl`, `findmnt`,
  `restorecon`, `systemd-analyze`, `zeus`, and measurement commands used by
  the fixture. `/usr/libexec/zeus-desktop-safe` must be a regular executable
  so the safe-desktop recovery path can be checked without changing the
  owner's session.

The sentinel is a dedicated immutable-base test file. Its bytes, mode, owner,
group, and SELinux context are captured before any merge. The fixture creates
only the fixed directory extension
`/var/lib/extensions/zeus-qualification-fixture`, the private phase state and
JSON evidence below `/var/lib/zeus/qualification/developer-sysext`, and, when
the installed sysext service is not already enabled, the fixed conditional
oneshot unit
`/etc/systemd/system/zeus-developer-sysext-qualification.service`.
Existing paths at those locations are treated as collisions or unsafe state;
the script never overwrites them. `/home`, `/var/home`, owner update state,
`/boot`, EFI paths, bootc state, `/usr`, `/etc`, `/proc`, `/sys`, and `/dev` are
protected from arbitrary path arguments and are not fixture write targets.
The state directory also holds a private `flock` lock so a second qualification
run cannot race the first one.

The extension has the standard
`/usr/lib/extension-release.d/extension-release.zeus-qualification-fixture`
metadata. Its `ID`, `VERSION_ID`, `ARCHITECTURE`, and `SYSEXT_LEVEL` match the
guest, and it replaces only the dedicated sentinel with a fixed harmless
qualification string. The fixture calls `restorecon` after creating it.

## Two-phase run

The operator owns the reboot boundary. The script never invokes `reboot`,
`systemctl reboot`, Proxmox, `bootc`, or the updater.

```text
guest# scripts/qualify-developer-sysext.sh --mode prepare
operator: reboot the disposable guest through the approved VM workflow
guest# scripts/qualify-developer-sysext.sh --mode verify
```

`prepare` refuses stale phase state, records the current kernel boot ID, and
leaves the compatible fixture in place for the operator's reboot. It also
records the disabled pre-apply `systemd-analyze time` sample and runs
`zeus desktop safe --dry-run`; the dry-run must confirm terminal access and
that credentials and personal data remain untouched. It reports
`result: "pre_reboot_ready"` and `boot.claim: "not_observed"`; that result is
not a reboot or boot-activation claim. `verify` requires the root-owned private
`prepare.json` from the same state directory and refuses to proceed until the
kernel boot ID differs. It then observes activation, checks the active service,
records the active boot timing, samples the post-cleanup disabled idle state,
and removes the extension and optional unit before reporting
`result: "qualified"`.

For a read-only contract check, use either mode with `--dry-run`. Dry-run emits
the schema and planned paths without requiring root, touching the guest, or
creating state. Evidence may be sent to stdout with `--output -` for an
operator-controlled collection path; file output is confined below the private
qualification state directory.

## Cases proven by the fixture

The runtime phase records each case with monotonic start/end timestamps and a
pass or failure status:

1. A compatible directory extension merges with `systemd-sysext refresh`, the
   sentinel contains the override, `/usr` is observed as an overlay, and the
   extension appears in `systemd-sysext list` and `status`.
2. `systemd-sysext unmerge` restores the sentinel's exact bytes, mode, owner,
   group, and SELinux context. The same extension is merged once more to prove
   the lifecycle is repeatable.
3. An extension with an incompatible `VERSION_ID` is ignored/rejected and
   cannot replace the base sentinel.
4. An extension with an incompatible `SYSEXT_LEVEL` is ignored/rejected and
   cannot replace the base sentinel.
5. After the runtime cases, the compatible extension is present for the
   operator-controlled reboot. At verify time, activation must be attributable
   to the pre-existing `systemd-sysext.service` or the fixture's conditional
   oneshot. The post-boot AVC scan is filtered to sysext-related units.
6. `zeus desktop safe --dry-run` confirms that a terminal remains available and
   credentials and personal data are untouched. This is a non-mutating
   recovery contract check; the fixture does not change the owner's session.
7. The final unmerge, refresh, extension-list hash, sentinel metadata, and
   AVC scan must all match the pre-qualification state. Any failed cleanup is
   reported and the extension is retained for manual recovery rather than
   being removed through an uncertain path.

The fixture records `CLOCK_MONOTONIC` phase and command timing. Baseline,
merged, post-boot-active, and post-cleanup-disabled samples include aggregate
guest CPU from `/proc/stat` and used memory defined as `MemTotal -
MemAvailable` from `/proc/meminfo`. `systemd-analyze time` is captured before
the extension is applied (`disabled`) and after boot activation (`active`);
these are observations, not a matched cold-boot performance claim. Journal AVC
records are represented by counts and digests; command stdout/stderr is
represented by SHA-256 digests in the JSON so unbounded journal text is not
copied into evidence.

## Evidence schema

Every non-dry-run output is JSON with
`schema: "zeus-developer-sysext-qualification-v1"` and
`schema_version: 1`. The durable `prepare.json` is the verify hand-off; output
files are root-owned and written atomically. The top-level contract is:

| Field | Meaning |
| --- | --- |
| `mode` | `prepare` or `verify` |
| `result` | `pre_reboot_ready`, `qualified`, or `failed` |
| `qualification_passed` | True only for an observed successful verify |
| `errors` | Bounded error code/message entries; no raw command output |
| `guest` | Fedora ID/version, `SYSEXT_LEVEL`, architecture, systemd version, SELinux mode |
| `boot` | Pre/post boot IDs, observed boot change, reboot requirement, activation source and claim |
| `sentinel` | Fixed path, base SHA-256, mode/UID/GID/context, override hash, restoration rule |
| `extension` | Fixed name/path, release metadata path, format, pre-existing list hash and cleanup state |
| `selinux` | Enforcing gate and AVC counts for operations and boot activation |
| `safe_desktop` | Non-mutating fallback command, helper hash/context, and data-preservation checks |
| `metrics` | Monotonic phase durations, disabled/active boot timing, and idle sample records |
| `cases` | Runtime, rejection, boot, restoration, scope, and metrics case results |
| `commands` | Allowlisted command argv, expected result, return code, monotonic duration, output digests |
| `scope` | Allowed mutation paths, protected prefixes, and explicit false owner/updater/bootloader/reboot/Proxmox flags |

`qualification_passed` is deliberately false for dry-run and prepare output.
The boot claim is `observed_after_boot_id_change` only after verify has seen a
different boot ID and the service/overlay checks pass. In particular,
`sentinel.base_sha256` is the exact restoration reference,
`selinux.avc_denials_during_operations` and
`selinux.avc_denials_during_boot_activation` must be zero, and each idle
sample set carries the `metrics.idle_memory_definition` used to interpret its
memory values. A qualified record also has
`metrics.disabled_mode.idle_claim: "observed_after_cleanup"`, a disabled
pre-apply boot timing observation, and
`safe_desktop.mutation_performed: false`.

## Observed disposable guest measurement: VM119

The following values come from the parent qualification run on disposable
**VM119**. The sanitized machine-readable records are
[`sysext-runtime.json`](../iterations/developer-mode-20260911/sysext-runtime.json),
[`sysext-post-reboot.json`](../iterations/developer-mode-20260911/sysext-post-reboot.json), and
[`sysext-squashfs.json`](../iterations/developer-mode-20260911/sysext-squashfs.json). They are retained
here as historical measurements; they are not constants used by the reusable
fixture and do not substitute for a future run's evidence.

### Directory extension runtime

| Measurement | VM119 result |
| --- | --- |
| Base sentinel SHA-256 | `efdd0f34e6cf518a11a700ba7bef68869236c8caaadf1e1e4f647b6f8ec47eca` |
| Active value after merge | `0.1.0-preview.2+sysext-qualified` |
| Base value after unmerge | `0.1.0-preview.2` |
| Merged `/usr` status | `zeus-qualification` present in the hierarchy |
| Apply / unmerge | 11.833103 ms / 7.583882 ms |
| Incompatible identity result | Base value remained active; no extension merged |
| Runtime SELinux | Enforcing; 0 relevant AVC records |
| Runtime boot timing sample | 1.495 s kernel + 2.379 s initrd + 3.247 s userspace = 7.122 s total; graphical target after 3.244 s userspace |

The restored sentinel SHA-256 exactly matched the captured base hash after the
runtime unmerge and final cleanup.

### Boot activation and cleanup

VM119 was rebooted by the operator between the runtime and post-reboot phases.
The post-reboot record observed the following:

| Measurement | VM119 result |
| --- | --- |
| Boot activation | `systemd-sysext.service` active; merged value `0.1.0-preview.2+sysext-qualified` |
| Sysext boot cost | `45 ms systemd-sysext.service` |
| Boot timing | 1.424 s kernel + 2.372 s initrd + 3.801 s userspace = 7.598 s total; graphical target after 3.800 s userspace |
| Resident sysext processes after boot | 0 |
| SELinux | Enforcing; 0 sysext AVC records |
| Safe desktop dry-run | Passed; context `system_u:object_r:bin_t:s0`, SHA-256 `43e1247089f54ed25d8fbf3b5f51b532348ab9b0ab89dfac5840903f9db29577`; shell remained available and credentials/personal data remained untouched |
| Final state | Extension list empty; restored sentinel SHA-256 matched the base hash |

This is the only record that claims boot activation because it contains both
the observed post-reboot service state and the changed boot phase. A prepare
record alone never makes that claim.

### SquashFS and failure restoration

The separate VM119 SquashFS exercise merged and removed a raw image, then
attempted a deliberately incompatible/broken image:

| Measurement | VM119 result |
| --- | --- |
| SquashFS apply | 102.740496 ms |
| SquashFS active value | `0.1.0-preview.2+squashfs-qualified` |
| Image SHA-256 | `73adaa74ec64909d9f38f844f698a791760af7a270d5179ac099af49c020bb89` |
| Broken refresh | Exit code 1: `Failed to read metadata for image zeus-broken: Package not installed` |
| Broken/restored base SHA-256 | `efdd0f34e6cf518a11a700ba7bef68869236c8caaadf1e1e4f647b6f8ec47eca` |
| SquashFS SELinux | 0 sysext AVC records |

The reusable qualification script uses a directory extension for its fixed
sentinel and records its own future format-specific output. The VM119
SquashFS result is supporting evidence for the mechanism's separate raw-image
path, not a claim that the fixture silently creates or leaves a SquashFS image.

## Review commands

From a source checkout, the no-guest checks are:

```text
python3 -m unittest tests.test_developer_sysext_contract
scripts/qualify-developer-sysext.sh --dry-run --mode prepare
scripts/qualify-developer-sysext.sh --dry-run --mode verify
```

For the underlying merge semantics, see the
[systemd-sysext documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysext.html).
