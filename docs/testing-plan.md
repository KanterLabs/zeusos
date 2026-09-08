# Zeus OS v0.1 testing implementation plan

Helm tracking: [testing card index](helm-backlog.md#testing-implementation-and-acceptance).

This is a build-and-run checklist derived from the historical [Zeus OS v0.1 design](design-v0.1.md). The current first-preview priority and candidate cadence are in [the preview strategy](preview-strategy.md). Every item is intentionally unchecked: this document records work to do and evidence to collect, and makes no claim that a build, version, hardware result, or acceptance test has passed. Version validation remains part of the work; pin the actual Fedora/bootc, GNOME, T3 Code, Codex, extension, builder, and RPM inputs in the release manifest before relying on them.

The first fourteen items build the test system and sanitized fixtures. Q15–Q21 execute release acceptance against those fixtures. `Product deps` below point to capabilities in [the implementation checklist](implementation-plan.md); product deps are required only when the corresponding feature case executes, while a harness checkbox closes only after its full scope passes. Early gates name specific smoke cases with the minimal available I inputs and never require a whole later Q task. Q prerequisites name only Q IDs or a plain capability and point backward, so the test sequence is reviewable and acyclic. A skipped critical acceptance test is a stable-promotion failure. Test evidence must include the image digest, source/profile commits, pinned tool versions, hardware or VM configuration, timestamps, result, and redacted logs/screenshots.

Use `homelab` for short policy, lint, metadata, short VM dispatch, and orchestration jobs. Use `homelab-heavy` per job for Rust workspace builds/tests, image builds, and long VM, integration, or browser suites. Image assembly executes in a dedicated builder VM; CI must record and verify that execution boundary. Short jobs that only dispatch/collect use `homelab`; jobs performing sustained builds or tests use `homelab-heavy`. The persistent review VM is for owner-state candidate review, while disposable Proxmox resources are required for install/update/recovery and destructive or fault-injection work; never build on the live dev VM or the Proxmox host, and never expose production secrets to a test job.

## Harness and fixture construction

- [ ] **Q01 — Establish the test contract and evidence ledger (M0, harness).**
  - Deliverable: machine-readable cases for R01–R07 and T01–T12, criticality (`stable-blocking` or diagnostic), result vocabulary, artifact naming, redaction rules, and a ledger that records skipped/blocked cases explicitly.
  - Depends on: supplied v0.1 design; no Q prerequisite.
  - Product deps: I01 (IDs/manifest shape only; no implementation gate).
  - Pass evidence: every requirement and acceptance test has at least one downstream Q ID; the 30-minute clock boundaries, stage budgets, power targets, and laptop/homelab RPO scopes are represented verbatim; the ledger contains no “passed” default.
  - Environment/automation: schema and documentation lint on `homelab`; retain immutable JSON/Markdown evidence with each candidate digest.
  - Trace: R01–R07; T01–T12.

- [ ] **Q02 — Build CI routing and isolation policy (M0–M4, harness).**
  - Deliverable: checks and sanitized workflow fixtures under `tests/ci/` for per-job runner selection, retention/concurrency rules, builder isolation, and disposable resources. Extend them to verify the daily refresh and expedited security lanes when I23/I27 supply those workflows; product workflow implementation stays in I23/I24/I27.
  - Depends on: Q01.
  - Product deps: I01, I03 initially; I23, I24, I27 for maintained CI/release/security lanes.
  - Pass evidence: a policy test rejects `ubuntu-latest`, generic `self-hosted`, live-dev-VM paths, Proxmox-host builds, or production credentials; heavy jobs are independently labeled and artifacts identify the ephemeral job/resource.
  - Environment/automation: GitHub Actions on the homelab fleet; policy/lint on `homelab`, sustained builds/tests on `homelab-heavy`; no test result is inferred from runner configuration alone.
  - Trace: R01, R07; supports T01, T06–T09.

- [ ] **Q03 — Implement local unit, static, and content checks (M0–M3, harness).**
  - Deliverable: Rust CLI/profile/restore unit suites; schema and migration checks; shell-module fallback checks; argument-array and bounded-timeout static checks; image `/usr`, persistent `/etc`, runtime `/var`, and discovered home-layout assertions; dconf allowlist checks; `zeus config export` diff and `zeus doctor` record checks; secret, license, and redistribution-cleared asset scans.
  - Depends on: Q01, Q02.
  - Product deps: I08, I13, I15, I16, I18, I20, I21 (CLI, profile, restore, doctor, export, and desktop surfaces under test).
  - Pass evidence: M0 static checks cover available artifact inputs; shared-engine tests begin when I08 arrives, followed by profile, restore, export, M1 baseline shell/lock checks, and later M3 polish checks. Fixtures cover preview, atomic writes, checkpoints, idempotent aliases/keys/services/desktop entries, visible conflicts, preceding-OS compatibility, deliberate allowlisted export, measured responsiveness inputs, and stock-GNOME fallback. Scans fail on embedded private keys, credential caches, populated machine identities, or passwords; supported empty identity placeholders and public verification material remain valid. Asset provenance/license checks reject uncleared redistribution, including proprietary Apple assets. Static checks do not claim a candidate is fast; Q20 records runtime measurements.
  - Environment/automation: lint/static/policy on `homelab`; Rust builds/tests on `homelab-heavy`; output is a report, not a claim of passing implementation.
  - Trace: R02, R04, R06, R07; supports T08–T10.

- [ ] **Q04 — Wire artifact provenance and payload assertions (M0–M4, harness).**
  - Deliverable: test adapters under `tests/release/` that consume the product OCI/ISO/QCOW2 from I04/I07 (and later I23), validate the locked manifest, compare payload digests, verify checksums/signatures and SBOM coverage, and check the independent installer copy. These adapters do not build product images or sign production artifacts.
  - Depends on: Q01, Q02.
  - Product deps: I01, I02, I03, I04, I07, I09 initially; I23, I24 for automated release artifacts and channel promotion.
  - Pass evidence: validate source commit, Fedora major, base digest, resolved RPMs, third-party hashes, extensions, builder digest/interface, profile schema, T3 compatibility, licenses, and test references. Recompute artifact checksums and verify expected digests/signatures; corrupt-content and mismatched-payload fixtures fail. Promotion changes the reference to the tested digest without rebuilding. Byte-for-byte reproducibility is separately reported if attempted, never assumed.
  - Environment/automation: short metadata/dispatch checks on `homelab`; sustained image inspection/rebuild-verification jobs on `homelab-heavy`. Verify that product image assembly ran in the dedicated builder VM; use a registry/publishing fixture with no production credentials.
  - Trace: R01, R04, R07; supports T01, T06, T08.

- [ ] **Q05 — Provision disposable VM and resource profiles (M0, harness).**
  - Deliverable: Proxmox automation for the proposed default x86_64, 4 vCPU, 8 GiB RAM, 64 GiB disposable-disk, UEFI/OVMF, VirtIO profile; a low-resource regression profile; a two-disk installer profile; snapshots, network isolation, teardown, and evidence capture. Keep the persistent 4-vCPU/8-GiB/64-GiB preview-review VM outside this disposable resource pool and retain its owner state between candidates.
  - Depends on: Q02.
  - Product deps: I03 (isolated builder/test-resource interface).
  - Pass evidence: each disposable run records disk model/capacity and target selection, UEFI/firmware, CPU/RAM/disk profile, network controls, snapshot lineage, and cleanup; no production VM, persistent preview owner state, hostname, secret, or backup is in scope. The 64-GiB disposable size is a proposed test profile, not a claim about test results.
  - Environment/automation: disposable Proxmox resources orchestrated by `homelab`; sustained VM/image/integration runs on `homelab-heavy`; destructive partition tests use only disposable disks.
  - Trace: R01, R07; supports T01, T02, T06, T07, T09.

- [ ] **Q06 — Wire the offline-installer and suite test adapter (M0–M1, harness).**
  - Deliverable: test adapters under `tests/installer/` that consume the product ISO/QCOW2 from I07, block the network, inspect embedded payload/architecture/UEFI metadata, drive target confirmation and encrypted install, and inventory the required local suite: T3 Code, Codex CLI, Git/gh, openssh-clients, mosh, tmux, ripgrep, fd-find, fzf, jq, curl, rsync, Tailscale, browser, terminal, Files, Settings, keyring, and firmware tooling. Capture the installed desktop handoff for Q20's M1 baseline smoke. Check retained desktop runtimes and optional Flatpak availability. The adapter does not package the installer or applications.
  - Depends on: Q05; Q04 payload-provenance smoke adapter.
  - Product deps: I04, I06, I07 (candidate installer and image suite).
  - Pass evidence: the M0 smoke slice proves embedded payload, x86_64 UEFI boot, interactive target selection, and encrypted install; the complete required-suite assertion is exercised by Q15 in M1. A network-blocked path needs no post-install package download; no unattended “first disk” policy, local SDK/container/database/LLM bundle, or self-updater is accepted; version values come from the candidate manifest and are not presumed current.
  - Environment/automation: disposable Proxmox offline VM and installer media; long install/browser checks on `homelab-heavy`; the same ISO is retained for later recovery timing.
  - Trace: R01, R04, R07; supports T01, T02, T03, T06.

- [ ] **Q07 — Implement the two-disk safety oracle (M1, acceptance harness).**
  - Deliverable: sentinel-filled target and non-target disks, pre/post partition-table capture and full non-target-disk hashes, explicit model/capacity selection recorder, and cancellation/confirmation assertions.
  - Depends on: Q05, Q06.
  - Product deps: I07 (interactive destructive-action path).
  - Pass evidence: the installer requires a human-confirmed target; after install, the non-target disk's complete hash and partition table match before-install evidence. Canceling before destructive confirmation leaves both disks unchanged. Any automatic first-disk selection, ambiguous prompt, or changed non-target disk fails T02.
  - Environment/automation: two-disk disposable UEFI/OVMF VM only; destructive cleanup is limited to named disposable disks; evidence is attached to the candidate.
  - Trace: R01; T02.

- [ ] **Q08 — Build the remote-workspace continuity fixture (M0 prototype, M1 complete, harness).**
  - Deliverable: isolated dev VM fixture with the exact T3 client/server compatibility pair under test, systemd user-service persistence/lingering, remote tmux, a seeded project, separate committed/uncommitted/untracked sentinel files, an explicitly scoped task-output/counter path, redacted logs, duplicate-execution detector, and client-visible remote-environment indicator.
  - Depends on: Q02, Q05; Q06 installed-client smoke for the prototype, complete suite adapter for M1.
  - Product deps: I05, I10, I11 (remote T3/Codex, transport, and dev-VM service capabilities).
  - Pass evidence: the default Zeus launcher enters the dev VM and has no silent local fallback; reconnect attaches to one service/session without restarting active work; fixture distinguishes submitted remote work from unsent prompts and local editor buffers, and separates expected task writes from sentinel/Git integrity checks; a VM reboot run records that processes stop and continuity requires saved state/supported resume, not tmux alone.
  - Environment/automation: dedicated disposable dev VM or isolated clone reachable through SSH/Tailscale; long integration runs on `homelab-heavy`; never use production projects or provider credentials.
  - Trace: R04, R05, R06; supports T04, T05, T12.

- [ ] **Q09 — Add transport and session fault controls (M1, harness).**
  - Deliverable: controllable client kill, Wi-Fi/network interruption, suspend/resume trigger, SSH fallback, host-key-change, timeout, duplicate-submit, and reconnect scenarios with timestamped event markers.
  - Depends on: Q08.
  - Product deps: I05, I10, I11, I12 (remote launch, transport, service, and recovery paths).
  - Pass evidence: each fault has an expected user-visible error and recovery action; transport loss is distinct from dev-VM reboot; no scenario claims access while both network paths are unavailable; changed host fingerprints block automatic trust; logs redact tokens, pairing links, prompts, and secrets.
  - Environment/automation: virtual network fault injection for repeatable VM runs; suspend, Wi-Fi, and accessibility portions require the reference laptop; orchestration on `homelab`, long sessions on `homelab-heavy`.
  - Trace: R05, R06; supports T04, T05, T09.

- [ ] **Q10 — Seed populated profile/application and retained-release fixtures (M1–M4, harness).**
  - Deliverable: sanitized versioned profile revisions, managed-file backups, namespaced dconf settings, device overrides, browser bookmarks/unsent-draft allowlist, and populated application/project data with stable IDs, relationships, counts, content hashes, and a write-after-backup marker. Include first-run progress/retry checkpoints, stock-desktop fallback, a verified pre-upgrade backup, and a retained predecessor/oldest-supported OS state labeled honestly as a feasibility candidate when no prior stable exists.
  - Depends on: Q05; relevant Q03 profile/schema checks and Q04 candidate-digest smoke adapter.
  - Product deps: I13, I15, I17, I18, I26 (profile, restore/setup/export, and data-preserving upgrade capabilities).
  - Pass evidence: populated fixture IDs, relationships/counts, content hashes, and post-backup writes survive profile application and OS upgrade/rollback across every retained binary. Profile restore preserves unrelated dconf choices and device identity. Record `/var` persistence and last-known-good profile/OS ranges; block migrations that cannot preserve retained-binary compatibility before mutation. A backup does not substitute for that compatibility, and no reset or automatic database restore replaces data. Failed personalization leaves stock GNOME and a terminal usable. Recovering an older backup is separately scoped in Q21.
  - Environment/automation: disposable VM snapshots and seeded user data; Rust/profile integration on `homelab-heavy`; no personal secret or production database.
  - Trace: R06, R07; supports T03, T06, T07, T09.

- [ ] **Q11 — Make independent enrollment and credential-recovery fixtures (M2, harness).**
  - Deliverable: second-device/offline-code recovery kit, fresh per-install SSH key, new Tailscale identity, verified host-fingerprint path, least-privilege grant/revoke stubs, and unavailable-old-client simulation.
  - Depends on: Q06, Q08; Q10's M2 profile/device fixtures, without its M4 migration matrix.
  - Product deps: I09, I10, I14, I17 (security, transport, enrollment, and setup capabilities).
  - Pass evidence: recovery authorizes a replacement with the old laptop absent; it never copies Tailscale state, machine-id, another device’s private key, Git credentials, or provider credentials; revoked device loses network/SSH/T3 access while the dev VM and its provider session remain; host-key changes require a visible decision.
  - Environment/automation: wiped/disposable client plus isolated dev VM and independent recovery material; recovery orchestration on `homelab`; no real secret values in fixtures or logs.
  - Trace: R05, R06, R07; supports T03, T08.

- [ ] **Q12 — Seed backup and isolated-restore test adapters (M2–M4, harness).**
  - Deliverable: sanitized source/backup fixtures and adapters under `tests/backup/` that consume I25/I28/I29, select a known backup, verify data-volume inventory, exercise guest-agent freeze/thaw and supported database export metadata, and guard isolated network/hostname/jobs. Assert proposed hourly encrypted file, nightly full, 24-hourly/14-daily/8-weekly retention metadata, separate failure-domain copy, encrypted offsite copy, and read-only doctor source; do not implement production backup jobs here.
  - Depends on: Q08, Q10.
  - Product deps: I11, I16, I25, I28, I29 (remote data, diagnostics, backup, restore verification, and dated health capabilities).
  - Pass evidence: adapters can prove all data-bearing volumes and supported database state are represented; restored VM cannot advertise production or run production jobs; health output exposes dated backup/restore-test records only. Record proposed dev-VM RPO ≤1 hour separately from actual offsite-copy age for whole-homelab loss; no 30-minute whole-homelab guarantee. Product backup scheduling and restore execution remain I25/I28 work.
  - Environment/automation: disposable Proxmox restore VM and independent storage; adapter/restore assertions on `homelab-heavy`; schedule monthly execution through product I28 without claiming it has run.
  - Trace: R05, R06; supports T12.

- [ ] **Q13 — Construct update, rollback, signature, and boot-trust fixtures (M0–M4, harness).**
  - Deliverable: retained predecessor/oldest-supported installer states (labeled as a feasibility candidate rather than a fictional previous stable for the initial release); staged bootc update/reboot/rollback inputs; expected image identity and signer policy; an accepted candidate signed by the valid signer, a validly signed wrong-image-identity candidate, unsigned and wrong-signer candidates, a revoked-signer candidate, and old/new signing-key rotation fixture; Secure Boot/encrypted-install test inputs; client/server/profile compatibility matrix.
  - Depends on: Q05; Q04 artifact-digest smoke adapter.
  - Product deps: I04, I09, I12, I23, I24, I26, I27 (deployment, trust, update, release, compatibility, and rotation capabilities).
  - Pass evidence: M0 smoke uses a predecessor/candidate rollback path; M1 adds accepted expected-identity, unsigned, wrong-signer, and valid-signer/wrong-identity cases. Later cases cover revocation, rotation, and the full compatibility matrix. Exercise actual bootc `enforce-container-sigpolicy` and signature discovery, with separate signing and installed-client rejection evidence. Rotation preserves an independently verified recovery path; compromised recovery artifacts need a tested replacement. Secure Boot and encryption have independent evidence. Verify the proposed six-hour awake/jitter checks, AC/unmetered staging, explicit reboot, offline/suspend backoff, no silent remote restart, and blocked unqualified major transitions.
  - Environment/automation: disposable VM for bootc/update/signature tests; real hardware run for Secure Boot/encryption; heavy image/integration work on `homelab-heavy`; no production signer keys.
  - Trace: R07; supports T06–T08.

- [ ] **Q14 — Add interrupted-operation and resource-exhaustion injection (M1–M3, harness).**
  - Deliverable: deterministic kill/power/network/full-disk faults at every restore/download checkpoint plus shell-module fault and staged-update interruption, with resume/rollback commands and evidence collection.
  - Depends on: Q05, Q13's update/rollback fixtures; relevant Q03 engine checks. Add Q10 populated fixtures for M2 data preservation and Q03 desktop checks for the M1 baseline fallback and M3 cosmetic recovery.
  - Product deps: I08, I12, I15, I17, I20 (shared engine, update/restore/setup, and cosmetic fallback paths).
  - Pass evidence: M1 injects update/download interruption, a full disposable volume, a broken-session fault for manual rollback, and the baseline desktop fallback path. M2 adds interrupted profile restore at every implemented checkpoint with populated state; M3 adds any optional polish-module failure path. Preserve the working deployment, managed-file backups, and resumable checkpoints without duplicate aliases/keys/services/entries. Cosmetics cannot remove terminal/connectivity/credentials; partial updates never become silently active.
  - Environment/automation: disposable VM fault-injection suite on `homelab-heavy`; full-disk limits apply only to disposable volumes; record every injected failure and recovery point.
  - Trace: R06, R07; supports T07, T09.

## Acceptance execution

- [ ] **Q15 — Run T01 offline install and required-suite acceptance (M1, execution).**
  - Deliverable: fresh-media run record keyed to ISO/OCI digest, installer manifest, boot logs, and required-tool inventory.
  - Depends on: Q05, Q06; Q04 payload-provenance smoke adapter.
  - Product deps: I04, I06, I07 (desktop image, application suite, installer, and QCOW2).
  - Pass evidence: from a fresh prepared USB/ISO, the interactive installer installs offline from the embedded payload, counts both installation boots where applicable, boots the installed UEFI system, and exposes the required suite without manual package installation; a failure or skipped critical step blocks stable.
  - Environment/automation: default and low-resource disposable Proxmox VMs with network blocked for payload; install suite on `homelab-heavy`.
  - Trace: R01, R04; T01.

- [ ] **Q16 — Run T03 wiped-client recovery drill and 30-minute clock (M2, execution).**
  - Deliverable: timed report from a wiped replacement client with old laptop unavailable, healthy unchanged dev VM, independent recovery factors, stage timestamps, doctor output, T3 remote session evidence, terminal/tmux attachment, and file comparison.
  - Depends on: Q07, Q11, Q15; Q10's M2 profile/restore fixtures and Q08's saved-work/task markers. M4 backup automation is not a prerequisite for this laptop-only recovery test; real-VM changes still require I11's verified existing backup protection.
  - Product deps: I07, I10, I13, I14, I15, I16, I17, I19; D01 reference-hardware selection (installer, transport, profile, enrollment, restore, doctor, setup, and runbook capabilities).
  - Pass evidence: start when the replacement boots prepared USB installer; stop only when configured desktop is available, dev VM is reachable, T3 opens the remote environment, terminal reattaches to the existing session, the exactly-one submitted task is still running or has its expected completion, and saved Git state is verified. Authentication and both installation boots count. Total ≤30 minutes is the gate. Record Install 15, Connect/enroll 5, Restore preferences 5, Validate/resume 5 as diagnostic planning budgets; an individual stage overrun is not an independent failure when total remains ≤30. USB download/creation is recorded separately. Saved committed/uncommitted/untracked sentinels are unchanged; task output/counter is checked against its expected write. Unsent prompts and unsaved client buffers are outside the promise.
  - Environment/automation: reference setup first in a disposable client and then the supported replacement laptop/recovery drill; homelab-heavy for long remote validation; record blockers such as unknown hardware, Wi-Fi, homelab, or identity-provider outage instead of reporting success.
  - Trace: R05, R06; T03.

- [ ] **Q17 — Run T04 continuity and T05 transport acceptance (M1 smoke, M2 recovery, M3 hardware, execution).**
  - Deliverable: event timeline and before/after hashes for client kill, submitted remote task, network/Wi-Fi interruption, suspend/resume, SSH fallback, reconnect, host-key error, and a separate dev-VM reboot.
  - Depends on: Q08, Q09.
  - Product deps: I05, I10, I11, I12, I14, I22 (remote execution, transport, service, enrollment, recovery, and resume capabilities).
  - Pass evidence: M1 proves exactly one submitted job, preserved committed/uncommitted/untracked sentinels, expected task output, reconnect without duplicate service/task, and virtual transport drop/SSH fallback. M2 adds independent enrollment, host-key errors, and a separate dev-VM reboot; Q16 is not a prerequisite. Add physical Wi-Fi/suspend checks when reference hardware is ready and complete qualification with I22 in M3. VM reboot stops processes; continuation must use supported saved state, not assumed tmux survival. Errors explain unreachable paths/changed keys; both paths unavailable means remote access is unavailable.
  - Environment/automation: disposable dev VM and virtual network faults for M1/M2; reference hardware for Wi-Fi/suspend, fully qualified in M3; long suites on `homelab-heavy`; hardware interactions include manual review.
  - Trace: R05, R06; T04, T05.

- [ ] **Q18 — Run T06 update and T07 rollback smoke/full acceptance (M1 smoke, M2–M4 full, execution).**
  - Deliverable: upgrade matrix report from the retained predecessor/oldest-supported installer (label the initial feasibility predecessor honestly when no previous stable exists), staged/rebooted deployment evidence, release/profile manifest comparison, stable-ID/relationship/count/content-hash comparison, verified pre-upgrade backup, writes-after-backup sentinel, injected baseline or optional cosmetic failure as applicable, desktop/text-console rollback transcript, and remote-access check.
  - Depends on: Q13, Q15; Q10 populated-fixture extension and Q14 interruption smoke for full data-preservation cases.
  - Product deps: I04, I11, I12, I13, I23, I24, I26 (deployments, remote service, rollback, profile, release, channel, and compatibility capabilities).
  - Pass evidence: M1 smoke uses the available feasibility predecessor and one candidate to verify explicit reboot, deployment selection, terminal/remote access, baseline shell-fault fallback, and rollback. M2–M4 full runs cover the oldest-supported origin and retained predecessor with expected image/profile versions. Verify the pre-upgrade backup before mutation, then make a controlled write after that backup and prove it, stable IDs, relationship counts, and application data remain present through upgrade/rollback. `/var` application data and profile rollback scope are recorded; no reset or automatic restore discards later writes; client update never silently restarts or upgrades the remote agent; promotion uses the exact tested digest.
  - Environment/automation: disposable Proxmox VMs with retained deployments and isolated state backups; update/rollback suites on `homelab-heavy`; text-console path is captured separately from desktop path.
  - Trace: R07; T06, T07.

- [ ] **Q19 — Run T08 trust and T09 failure-safety acceptance (M1 smoke, M2–M4 full, execution).**
  - Deliverable: accepted-valid-signer/expected-identity positive control, valid-signer/wrong-image-identity, unsigned, wrong-signer, revoked-signer, and key-rotation results; image/profile/OCI secret-scan report; per-install identity comparison; Secure Boot/encryption evidence; installed security-posture report; populated-state fingerprints and writes-after-backup sentinel; and interrupted restore/download/full-disk recovery report.
  - Depends on: Q03, Q13, Q15; Q14 fault-injection smoke for M1 and Q11 identity fixture for M2+ enrollment cases.
  - Product deps: I09, I12, I14, I15, I23, I26, I27 (security defaults, update/restore, enrollment, release, compatibility, and signer lifecycle capabilities).
  - Pass evidence: M1 runs the accepted-candidate control, unsigned/wrong-signer/wrong-image-identity rejection, and secret scan. Later runs add revoked signers, rotation, enrollment, and physical Secure Boot/encryption. Actual bootc policy must enforce the intended signer and image identity. Verify unique identities/keys, SELinux enforcing, firewall, native login/no autologin, lock-on-suspend, least privilege, no broad passwordless sudo, no public dev/Proxmox endpoint, and no default agent forwarding. Test signing, policy enforcement, Secure Boot, and encryption independently. Interrupted profile/update operations preserve populated IDs/counts/content and post-backup writes with resumable checkpoints; no reset or automatic database restore is allowed.
  - Environment/automation: disposable Proxmox VMs for trust/fault tests; real supported hardware for Secure Boot/encryption; heavy suites on `homelab-heavy`; a signed artifact alone is insufficient without installed-client rejection.
  - Trace: R06, R07; T08, T09.

- [ ] **Q20 — Run T10 desktop/accessibility and T11 physical power acceptance (M1 baseline smoke, M3 complete execution).**
  - Deliverable: visual/accessibility review, screenshots and input matrix; M1 responsiveness dataset with settled-idle CPU/process memory, boot-to-usable time, launch/interaction timings, idle-redraw result, same-profile clean-GNOME comparison, and variability; per-supported-laptop power/suspend dataset with model, full-charge capacity, firmware, kernel, brightness, refresh rate, display/network path, room conditions, workload, warm-up, median Wh/runtime, and variability.
  - Depends on: Q03, Q05, Q15; Q03 supplies the desktop/accessibility checks. This prerequisite set supports the M1 smoke and points backward; M3 physical completion adds its own hardware setup and does not wait for M4 signing-key rotation.
  - Product deps: I06, I09, I20, I21 for the M1 baseline; I22 for M3 power/resume qualification (desktop, security/login/accessibility, and power capabilities).
  - Pass evidence: **M1 baseline smoke** runs as soon as I20/I21 produce a candidate, before the full I14–I19 recovery path is complete. Review the approximately 28–32 logical-pixel top bar, centered 44–48-pixel dock, left controls where supported, native lock/login privacy, reduced motion, contrast, keyboard navigation, on-screen keyboard, 100/125/150% scaling, required shortcuts, and stock-GNOME fallback. Record declared settled-idle CPU/process-memory, boot-to-usable, launch, interaction, and idle-redraw budgets against a same-profile clean-GNOME baseline, including actual values and variability; missing measurements block preview signoff, and VM sizing is not performance evidence. **M3 completion** adds optional-polish fallback review plus physical display/input/accessibility, suspend/Wi-Fi, and power checks. On the same hardware/settings, run at least three comparable warm runs including terminal-only and T3-plus-browser workloads. Record the aspirational ≥10% runtime improvement versus clean Fedora; stable gate is no material regression and initial guardrail median energy ≤5% above baseline. Also record diagnostic targets of <2% settled-idle aggregate CPU, no recurring idle shell redraw, and <1% capacity/hour across an 8-hour supported suspend test. VM results cannot stand in for battery, suspend, thermals, trackpad, or Wi-Fi evidence.
  - Environment/automation: use the persistent 4-vCPU/8-GiB review VM for owner-state candidate review, with its 64-GiB allocation recorded in the [preview VM runbook](preview-vm.md); use named disposable VMs/disks for install, partition, full-disk, and fault-injection cases. M3 requires supported reference laptop(s), real GPU/Wi-Fi/display/battery, and a mix of automated posture/measurement checks with dated manual visual/input review; no hardware result is implied by this checklist.
  - Trace: R02, R03, R07; T05, T10, T11.

- [ ] **Q21 — Run T12 backup restore acceptance (M2–M4, execution).**
  - Deliverable: dated isolated restore record, measured VM restore RTO, source backup/copy age, volume inventory, independent decryption evidence, project/Git/file and stable-ID/relationship/count comparison against the selected backup point, agent-history/database-integrity results, isolation proof, and doctor-output cross-check.
  - Depends on: Q08, Q12; Q10's seeded data and backup-point inventory. The laptop recovery drill is independent of this backup-restoration test.
  - Product deps: I25, I26, I28, I29 (backup, compatibility/migration, isolated restore, and dated health capabilities).
  - Pass evidence: independently decrypt and restore the selected backup in isolation; compare committed/uncommitted/untracked files, application data, IDs/counts/relationships, and database integrity to their recorded state at that recovery point. Later writes are outside that snapshot: report the actual recovery-point gap, while leaving the source VM and its newer writes unchanged. Record measured VM restore RTO and actual backup/offsite age; compare project coverage to the proposed ≤1-hour RPO without making a 30-minute homelab guarantee. The restored VM cannot advertise production or run production jobs. Missing/stale/unavailable doctor evidence cannot show success, and doctor alone never proves restorability. This critical test blocks stable when failed or skipped.
  - Environment/automation: disposable Proxmox restore VM with independent encrypted copy/offsite fixture; backup, database, decryption, and long restore checks on `homelab-heavy`; schedule recurring monthly execution after v0.1.
  - Trace: R05, R06; T12.

## Traceability

| Source | Q coverage | Evidence that must exist before stable |
| --- | --- | --- |
| R01 bootable/installable ISO and disposable VM images | Q04–Q07, Q15 | Digest-keyed ISO/QCOW2 records, offline install, disposable profiles, two-disk evidence |
| R02 macOS-like GNOME layout and interaction | Q03, Q20 | M1 baseline visual/accessibility and responsiveness record; M3 scaling/display and stock-fallback completion |
| R03 battery, low background activity, minimal local compute | Q03, Q20 | M1 measured lightweight-baseline inputs; M3 same-hardware power dataset and guardrail |
| R04 required local suite and connectivity tools | Q03, Q04, Q06, Q08, Q15 | Manifest inventory and offline availability; remote launcher proof |
| R05 development state remains on homelab dev VM | Q08–Q12, Q16, Q17, Q21 | Continuity hashes, remote-environment indicator, isolated backup restore |
| R06 replacement recovery target | Q01, Q03, Q08–Q12, Q14, Q16, Q17, Q19, Q21 | Independent-factor drill, ≤30-minute timed record, scoped RPO and failure evidence |
| R07 signed image updates and rollback | Q03, Q04, Q10, Q13, Q14, Q18–Q20 | Exact-digest promotion, update/rollback records, actual rejection, key rotation, Secure Boot/encryption evidence |

| Acceptance | Q coverage | Required result |
| --- | --- | --- |
| T01 install | Q06, Q15 | Offline embedded payload; required suite present; boots |
| T02 disk safety | Q05, Q07 | Explicit target; second disk unchanged byte/partition evidence |
| T03 personalization/recovery | Q10, Q11, Q16 | Old client absent; independent recovery; ≤30-minute clock and saved-work comparison |
| T04 continuity | Q08, Q09, Q17 | Client/transport loss reconnects without duplicate task or changed saved files |
| T05 transport | Q09, Q17, Q20 | Suspend/resume, Wi-Fi interruption, SSH fallback, clear errors; reboot distinction |
| T06 update | Q10, Q13, Q18 | Retained predecessor (honestly labeled if pre-release) and oldest-supported origins upgrade and report image/profile versions |
| T07 rollback | Q10, Q13, Q14, Q18 | Shell fault and previous deployment yield usable terminal/remote access |
| T08 trust | Q03, Q11, Q13, Q19 | Unsigned/wrong signer rejected; no secrets; unique identity; independent trust checks |
| T09 failure safety | Q10, Q13, Q14, Q19 | Interrupted restore/download/full disk preserve deployment and checkpoints |
| T10 desktop | Q03, Q20 | M1 baseline shell/lock/privacy/accessibility review; M3 optional-polish, scaling, and external-display completion |
| T11 power | Q20 | ≥10% is aspirational; no material regression is gate; ≤5% median-energy guardrail and physical measurements in M3 |
| T12 backup | Q12, Q21 | Isolated restore validates uncommitted/untracked work and application data |

## Product capability map

This compact map links testing work to the product items in [the implementation checklist](implementation-plan.md); it adds no Q prerequisite edges. A feature case waits for its listed I capability, while an early smoke may use the earliest available slice. The Q checkbox still closes only after that Q item’s full scope is evidenced.

| Q item(s) | Product implementation input(s) |
| --- | --- |
| Q01 | I01 |
| Q02 | I01, I03; later I23/I24/I27 for maintained CI/release lanes |
| Q03 | I08, I13, I15, I16, I18, I20, I21 |
| Q04 | I01, I02, I03, I04, I07, I09, I23, I24 |
| Q05 | I03 |
| Q06, Q15 | I04, I06, I07 |
| Q07 | I07 |
| Q08, Q17 | I05, I10, I11, I12, I14, I22 |
| Q09 | I05, I10, I11, I12 |
| Q10 | I13, I15, I17, I18, I26 |
| Q11 | I09, I10, I14, I17 |
| Q12, Q21 | I11, I16, I25, I26, I28, I29 |
| Q13, Q18, Q19 | I04, I09, I11, I12, I13, I14, I15, I23, I24, I26, I27 |
| Q14 | I08, I12, I15, I17, I20 |
| Q16 | I07, I10, I13, I14, I15, I16, I17, I19 |
| Q20 | I06, I09, I20, I21, I22 |

## Recommended release gates

- **M0 / feasibility:** Complete Q01, the Q02 isolation/runner checks, and Q05; run Q04/Q06/Q08/Q13 prototype cases using I04/I05. Record pinned provenance, embedded-payload UEFI/encrypted install, GNOME login, persistent remote T3, and manual predecessor rollback. Later profile, complete-suite, enrollment, and release-automation cases remain unchecked.
- **M1 / usable preview:** Run the M1 baseline portion of Q20 as soon as I20/I21 produce a candidate, before full recovery infrastructure. Require the shell/dock layout, native lock/privacy/accessibility, stock fallback, required shortcuts, and declared launch/interaction/idle responsiveness measurements; missing measurements block preview signoff. Then run the complete offline-suite/two-disk checks in Q06/Q07/Q15, Q04 candidate-payload assertions, Q08/Q09 virtual fault controls, and Q17/Q18/Q19 M1 cases. Add remote execution, exactly-one-job continuity, basic update/rollback, accepted-signer and signer/identity rejection, and update-interruption recovery as those increments become ready. Any declared critical case that fails or is skipped blocks promotion.
- **M2 / personal recovery:** Complete Q11/Q16 using Q10's M2 profile/restore fixtures and run the M2 portions of Q17/Q18/Q19 with Q13/Q14's applicable fault controls. Require the old-device-absent ≤30-minute drill and preserved saved remote work. Profile application and OS upgrade/rollback must retain post-backup writes. The M1 desktop baseline remains the preview contract; full release-compatibility, signer-rotation, automated-backup, and M3 hardware cases stay open until their capabilities arrive; I11's existing-backup protection is mandatory before real-VM changes.
- **M3 / advanced polish + power:** Require the M3 portion of Q20 on each supported laptop, including optional-polish fallback, accessibility, native password/input, suspend/Wi-Fi, and power review. Hardware evidence is dated and manually reviewed; it is not an all-automated gate. Treat ≥10% battery improvement as an aspirational product target; gate stable on no material regression and the stated ≤5% median-energy guardrail, with diagnostic targets reported separately.
- **M4 / maintained beta:** Require the automated portions of Q03, Q04, Q12–Q14, Q18, Q19, and Q21, exact-digest channel promotion, signature rejection/rotation, compatibility matrix, backup visibility, and repeatable long suites. Keep Q20’s hardware evidence separate as dated manual review; require explicit stable approval and the defined beta soak before stable. Beta/stable promotion must have install, connect, restore, update, rollback, remote-work preservation, and backup evidence; any skipped critical test blocks stable.

For the first usable preview, the definition of done includes the M1 baseline shell/dock and native lock/privacy/accessibility behavior, with measured lightweight responsiveness evidence and stock-GNOME fallback. Remote-first launchers, tested personal restore, and manual bootc rollback arrive as later M1/M2 increments. Advanced blur, a custom settings app, and a full login-screen redesign can remain optional M3 polish. A maintained beta additionally needs recurring hardware coverage, signed-build/channel automation, compatibility and key-rotation evidence, failure-injection regression, and monthly isolated backup-restore verification.
