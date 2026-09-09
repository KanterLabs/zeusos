# Native update qualification — B (`git-1a34bbfe8509`)

This is an **intermediate** candidate for the existing `0.1.0-preview.2`
installation. B booted successfully after an owner-driven update through A's
real native Updates window, but it is not the final/current handoff: post-boot
status still showed the stale message `A signed update is available` while its
structured state was correctly `up_to_date`. Both issues are corrected in the final candidate, `git-17d205103f3a`.
An intermediate `git-07a1cfeb097b` image was built but not published or deployed.

## Identity and scope

- Version: `0.1.0-preview.2`
- B build/source: `git-1a34bbfe8509` /
  `1a34bbfe8509aee7cb3271fe7ecead25d53afe6c`
- Update sequence: `1788950905`
- Signed archive: `zeusos-0.1.0-preview.2-git-1a34bbfe8509.oci`,
  `1,828,930,560` bytes, SHA-256
  `c7af19566b5f224da00bea9e467f662b0caa10a5ce9f7352827bc2c767b4c682`
- Staged image manifest digest:
  `sha256:cf9c53528f2a3ee514cabbb4e0b09ed1e05ee215450748b8bb57e2b6d8eb43f9`
- Predecessor A: `git-21a465760f53`, the current image when this update began
- Environment: existing populated review VM 115 (`zeusos-preview`), same
  native desktop and owner state used by the predecessor qualification; the
  B archive is `amd64`

The B manifest was published at `2026-09-09T10:51:14Z`. The signed checksums,
manifest, manifest signature, build identity, and asset verification are
retained with this record. This archive contains no credentials, private keys,
OCI payload, package inventory, or full owner directory state.

## Forward update result

| Phase | Observed result | Evidence |
| --- | --- | --- |
| Polkit cancel | Authentication cancellation left the install service inactive (`ActiveState=inactive`, no `ExecMainStartTimestamp`) and left the candidate `available`; no update service started. | [updater-auth-cancel-state.txt](updater-auth-cancel-state.txt) |
| Authenticated retry | One native install from authentication submit to root-ready status took **54.0 s**; this includes password entry, verified fetch, download, and staging. | [updater-install-timing.json](updater-install-timing.json) |
| Close during download | The job continued after the Updates window closed: `661,651,456 / 1,828,930,560` bytes before close and `737,148,928 / 1,828,930,560` after close. | [updater-before-close.json](updater-before-close.json), [updater-after-close.txt](updater-after-close.txt) |
| Reopen during staging | Opening Updates again from app search observed `state: staging` and the exact B candidate. | [updater-reopened-state.json](updater-reopened-state.json) |
| Ready to restart | The root job reached `state: ready` with the message to restart when convenient. | [updater-current-job.json](updater-current-job.json) |
| Exact staged identity | The staged bootc image and service journal identify B's signed manifest digest `sha256:cf9c53528f2a3ee514cabbb4e0b09ed1e05ee215450748b8bb57e2b6d8eb43f9`; published assets all verified successfully. | [updater-b-stage-verification.json](updater-b-stage-verification.json), [updater-service-journal.txt](updater-service-journal.txt), [updater-b-asset-verification.json](updater-b-asset-verification.json) |
| Explicit restart | The user accepted the Updates restart dialog, GNOME restarted, and the machine booted B. | [updater-b-first-boot.json](updater-b-first-boot.json), [updater-b-booted.json](updater-b-booted.json) |

The install timing has actual UTC boundaries: `2026-09-09T10:55:37+00:00` to
`2026-09-09T10:56:31+00:00`. The explicit restart measurement ran from
`2026-09-09T10:58:19.871391+00:00` to `2026-09-09T10:58:42.502324+00:00`, taking
`22.631 s` to changed-boot-ID SSH/GDM readiness. Its reported OS startup was
`7.171 s` (`1.375 s` kernel + `2.339 s` initrd + `3.456 s` userspace). These
are one native update boot and one install timing sample, not a cold-boot or
first-pixel benchmark.

The service journal independently shows the install service starting at
`10:55:39+00:00`, the B switch and staging work, and the root job becoming
ready at `10:56:31+00:00`. Its coarse-second log is retained alongside the
54.0-second measurement rather than treated as a replacement clock.

## Preservation and UI checks

- Six permanent-file hash checks remained `OK` through the update and reboot;
  see [updater-b-preserved.txt](updater-b-preserved.txt).
- Temp remained configured as `Never`, with the existing one-file, 48-byte
  sample present and no cleanup due; see [updater-b-temp.json](updater-b-temp.json).
- Welcome's Open Updates action opened a new Updates window, and a second click
  raised the existing window. B's icon review found `emblem-ok-symbolic`
  unavailable through the native icon theme and `object-select-symbolic`
  available; the follow-on candidate uses the installed available icon. These
  visual checks are summarized here without copying screenshots.

## Known issue and limits

[updater-b-booted-status.txt](updater-b-booted-status.txt) correctly reports
`Build git-1a34bbfe8509` and structured `state: up_to_date`, with B as both the
current and candidate identity. Its human `message` is nevertheless stale:
`A signed update is available.` This is a B defect, not evidence that B failed
to boot; it is why B remains intermediate and why the next candidate is the
release handoff target.

The record covers a successful forward update and explicit reboot. No rollback
cycle or rollback/re-forward test was run. The VM has no physical battery, so
there is no battery-runtime, battery-hours, wattage, suspend-drain, or hardware
power result here.

## Retained raw evidence

- Build identity: [build-info.json](build-info.json), [SHA256SUMS](SHA256SUMS),
  [SHA256SUMS.sig](SHA256SUMS.sig), [update-git-1a34bbfe8509.json](update-git-1a34bbfe8509.json), and [update-git-1a34bbfe8509.json.sig](update-git-1a34bbfe8509.json.sig).
- CI: [updater-b-ci.json](updater-b-ci.json).
- State transitions: [updater-auth-cancel-state.txt](updater-auth-cancel-state.txt), [updater-before-close.json](updater-before-close.json), [updater-after-close.txt](updater-after-close.txt), [updater-reopened-state.json](updater-reopened-state.json), and [updater-current-job.json](updater-current-job.json).
- Boot/staging: [updater-b-stage-verification.json](updater-b-stage-verification.json), [updater-b-first-boot.json](updater-b-first-boot.json), [updater-b-booted.json](updater-b-booted.json), and [updater-service-journal.txt](updater-service-journal.txt).
- Owner-state checks: [updater-b-preserved.txt](updater-b-preserved.txt) and [updater-b-temp.json](updater-b-temp.json).
