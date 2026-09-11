# Zeus OS implementation checklist

Source: historical [Zeus OS Design v0.1](design-v0.1.md), September 8, 2026. The current preview priority and review cadence are in [the preview strategy](preview-strategy.md) and supersede the source design's sequencing. This is a proposed backlog, not a record of implemented features. All 29 items are unclaimed and unchecked. Exact upstream versions and hardware capabilities still require verification.

Helm tracking: [implementation card index](helm-backlog.md#implementation).

Implement the product work here alongside the harnesses and acceptance runs in [the testing checklist](testing-plan.md). `I` identifies implementation work, `Q` identifies testing work, and `R`/`T` retain the design's requirement and acceptance IDs. Each item has a deliverable, dependencies, file/module ownership, and measurable acceptance criteria suitable for a future Backlog card.

The active [repository-backed Developer Mode plan](features/developer-mode.md)
defines the newer DM00–DM07 implementation sequence for fast laptop iteration.
It preserves this checklist's immutable-image, update, rollback and release
boundaries rather than repurposing the existing `zeus dev` remote-SSH command.

M0 proves the architecture while M1 produces the first usable preview: a very lightweight, fast, aesthetically pleasing macOS-like GNOME baseline. Remote work and replacement recovery arrive as later M1/M2 increments and must not delay that baseline. M3 extends the baseline with optional advanced polish and hardware/power qualification; M4 supplies maintained release operations. A functional preview is not a daily-use stable release. Signing and client trust enforcement begin in M1; M4 automates them.

## M0 — Prove the architecture

- [ ] **I01 — Establish the repository and release contract**

  **Deliver:** The proposed directory layout, contribution/build instructions, release-manifest schema, and sanitized example configuration. Record source commit, Fedora/base digest, resolved packages, third-party hashes, GNOME/extensions, builder digest, profile schema, and T3 client/server compatibility.  
  **Depends:** none. **Owns:** repository scaffolding, `docs/decisions/`, manifest schema.  
  **Accept:** An example manifest validates; missing pins are rejected; public files contain no personal profile or credentials. Document separate image, personal-profile, device-identity, and remote-data ownership. New GitHub repository ownership defaults to KanterLabs.  
  **Trace:** R01/R04/R07; design §§2, 5, 8, 13; T01/T08.

- [ ] **I02 — Select and verify the exact artifact set**

  **Deliver:** Locked Fedora bootc, GNOME, builder, T3 Linux client/server, Codex standalone binary, dock, browser, and terminal inputs. Treat Fedora 44 as the supplied proposal until verified. Check current primary documentation against the selected releases; use the maintained `osbuild/image-builder` bootc-image-builder tooling, not the archived standalone repository.  
  **Depends:** I01. **Owns:** `image/` locks, `remote/` compatibility definition, artifact decision records.  
  **Accept:** Every input has an immutable reference/checksum, source, redistribution status, architecture, and required runtime; no production input uses a floating `latest`. Verify fetched base/builder digests and recompute third-party artifact checksums, failing on mismatch. Unsupported T3 pairing or unavailable artifacts become explicit feasibility blockers.  
  **Trace:** R01/R02/R04/R05/R07; §§1, 3, 4, 8; T01/T04/T08.

- [ ] **I03 — Prepare an isolated builder and disposable test resources**

  **Deliver:** Dedicated builder-VM setup and a restricted interface for creating test VMs, disks, and artifact storage. Define the standard proposed 4-vCPU/8-GiB/64-GiB UEFI/OVMF/VirtIO disposable-test profile plus a documented lower-resource profile. The persistent preview-review VM is a separate owner-state environment; record its confirmed allocation in the [preview VM runbook](preview-vm.md) rather than treating it as a destructive-test resource.  
  **Depends:** I01. **Owns:** builder/lab definitions and operating runbook; Q01/Q02 own the test harness.  
  **Accept:** Image assembly runs outside the live dev VM and Proxmox host; build credentials cannot administer project data or backups; test resources are identifiable and safely disposable. CI uses `homelab` for short/orchestration jobs and `homelab-heavy` for Rust builds/tests, image builds, and long suites, per job.  
  **Trace:** R01/R05/R07; §§2, 8, 12; T01/T02.

- [ ] **I04 — Prove desktop installation and deployment rollback**

  **Deliver:** A thin bootc desktop candidate, recorded builder invocation, embedded-payload interactive ISO, and a second deployment for rollback experiments.  
  **Depends:** I02, I03. **Owns:** feasibility image/installer definitions and evidence notes.  
  **Accept:** A disposable UEFI VM installs without downloading its OS payload, boots GNOME/Wayland, creates a user with encrypted storage, and boots the preceding deployment after a simulated failure. Record actual `/etc`, `/var`, and home-directory persistence, explicitly discovering `/home` versus `/var/home`. Hardware encryption/Secure Boot qualification remains open.  
  **Trace:** R01/R02/R07; §§2, 7, 8, 13; T01/T06/T07.

- [ ] **I05 — Prove persistent remote T3 and Codex execution**

  **Deliver:** An exact-version remote-service prototype on an isolated development fixture, plus an execution-boundary decision record.  
  **Depends:** I02, I03. **Owns:** remote feasibility setup and compatibility evidence.  
  **Accept:** T3 attaches through its supported SSH path to one persistent user service; client disconnect/logout does not stop a submitted task or create duplicate services. Codex runs on the remote host; provider credentials stay there. Verify reconnect and supported saved-state behavior separately from VM reboot, which stops processes.  
  **Trace:** R04/R05/R06; §§2, 4, 11, 14; T04/T05.

## M1 — Build the usable preview image

- [ ] **I06 — Assemble the complete image and core application suite**

  **Deliver:** Versioned `Containerfile`, package inventory, runtime integration, application entries, and image defaults. Include T3, standalone Codex, Git/gh, SSH, Mosh, tmux, ripgrep, fd-find, fzf, jq, curl, rsync, Tailscale, one browser, one terminal, and desktop essentials. Mosh/Tailscale use follows the selected transport policy.  
  **Depends:** I01, I04, I05. **Owns:** `image/` and base application packaging.  
  **Accept:** Required tools work offline after installation; image-managed self-updaters are disabled. Retain firmware, keyring, portals, audio/Bluetooth, and accessibility. No local SDK collection, agent service, containers, databases, or project watchers start by default. Flatpak remains optional.  
  **Trace:** R01/R03/R04/R05; §§2, 4, 10; T01/T04.

- [ ] **I07 — Productize the safe installer and QCOW2 artifacts**

  **Deliver:** Interactive x86_64 UEFI installer configuration, embedded image payload, QCOW2 export, installation instructions, and artifact-to-payload mapping.  
  **Depends:** I04, I06. **Owns:** `installer/` and artifact-generation definitions.  
  **Accept:** The installer displays disk model/capacity, requires explicit selection and destructive-action confirmation, supports user creation and encryption, and exposes a fallback boot entry. ISO and QCOW2 come from the same candidate payload. Cancellation and two-disk testing prove that non-target storage remains unchanged.  
  **Trace:** R01/R04/R07; §8; T01/T02/T07.

- [ ] **I08 — Build the shared Rust command engine**

  **Deliver:** Small CLI engine with contracts for `restore`, `dev`, `doctor`, `update`, `config export`, and a safe-desktop action. Implement shared configuration parsing, structured results, errors, subprocess handling, and privilege boundaries; feature behavior arrives in the tasks below.  
  **Depends:** I01. **Owns:** `zeus/` core modules.  
  **Accept:** Subcommands expose documented inputs and exit statuses; trusted tools receive argument arrays and bounded timeouts; malformed host/profile inputs never become shell code. Logs exclude secrets, pairing links, and prompt contents. Privileged helpers do only their specified operation.  
  **Trace:** R05/R06/R07; §§9, 13; T05/T08/T09.

- [ ] **I09 — Establish device security and manual release trust**

  **Deliver:** SELinux/firewall/login defaults, OCI signing procedure, signed ISO/QCOW2 checksums, public verification material, and installed container-signature policy. Scope trust to the intended image identity and signer.  
  **Depends:** I06, I07. **Owns:** `security/`, signing/bootstrap instructions, security defaults.  
  **Accept:** A verified installer bootstraps a client that accepts a valid expected-identity candidate and rejects unsigned, wrong-signer, and wrong-identity candidates through the pinned bootc `enforce-container-sigpolicy` and signature-discovery configuration. Installations have unique identities, native authentication, no autologin or broad passwordless sudo. Validate encryption and Secure Boot independently; neither is inferred from image signing.  
  **Trace:** R01/R07; §§8–9; T08.

- [ ] **I10 — Implement private connectivity and remote launchers**

  **Deliver:** SSH configuration/tunneling, selected Tailscale enrollment integration, optional Mosh support, and Dev/T3/Codex launch behavior.  
  **Depends:** I05, I06, I08, I09. **Owns:** `zeus/` transport and launch modules, connection desktop entries.  
  **Accept:** The remote host/environment is visible before submission; default Codex launches remotely. Offline, unreachable-host, or host-key failures never fall back to local execution. SSH verifies a trusted fingerprint; agent forwarding is off; server access stays private with least-privilege network rules. Dev-VM Git credentials are not copied to clients. Reconnect backs off and provides an actionable error and SSH fallback.  
  **Trace:** R04/R05/R06; §§2, 4, 6, 9–10; T04/T05.

- [ ] **I11 — Package safe dev-VM service provisioning**

  **Deliver:** Idempotent T3 systemd user-service/lingering setup, remote tmux entry point, data-location inventory, and explicit maintenance/restart commands. Apply to the real dev VM only after fixture validation and a verified pre-change backup.  
  **Depends:** I05, I08, I10; verified existing dev-VM backup and restore evidence before any real-VM deployment. If that evidence is absent, keep work on fixtures and establish/verify pre-change backup protection with existing tooling before rollout; do not wait until M4's backup automation. **Owns:** `remote/` service and maintenance modules.  
  **Accept:** Repeated setup reuses one development-user service without disturbing active jobs or credentials. Client reconnect/update never restarts the server. Any state migration preserves populated projects/application data and retained rollback-binary compatibility; a pre-upgrade backup is verified before production changes. No Proxmox/backup-admin credentials reach agents.  
  **Trace:** R05/R06/R07; §§4, 7, 9, 11; T04/T06/T12.

- [ ] **I12 — Deliver manual update, rollback, and recovery access**

  **Deliver:** `zeus update` status/staging integration and desktop/text-console runbooks for selecting a known-good bootc deployment.  
  **Depends:** I07, I08, I09. **Owns:** update CLI module and recovery runbooks.  
  **Accept:** Display version, digest, release notes, and pending reboot; reboot requires user action. No client live-dnf upgrades or package layering. Disable conflicting inherited auto-apply behavior. A broken shell still permits terminal access and rollback with mutable state preserved; explicitly distinguish OS rollback from profile/database rollback.  
  **Trace:** R06/R07; §§5, 7, 13; T06/T07/T09.

- [ ] **I20 — Implement the baseline dock, shell defaults, and safe desktop**

  **Deliver:** Pinned dock integration, small GJS status layer, dconf defaults, cleared assets, and safe-desktop CLI action for the first usable preview. Target a quiet 28–32 logical-pixel top bar, centered 44–48-pixel dock icons, running indicators, intelligent hide, specified app entries, coherent spacing, and left window controls where supported. Keep the baseline layer small and independently disableable.  
  **Depends:** I06, I08; asset decision D06. **Owns:** `desktop/` dock/status/defaults and safe-desktop module.  
  **Accept:** The M1 preview boots into an aesthetically pleasing macOS-like GNOME layout while preserving GNOME/Wayland semantics. Super+Space searches, Super+Return opens a terminal, and terminal Ctrl+C remains intact. Dock and status modules can be disabled independently; disabling them restores stock GNOME and preserves connectivity, credentials, and a usable terminal. No fake app menus, forced blur, compositor fork, or invasive toolkit overrides. Record declared settled-idle CPU/process-memory, boot-to-usable, launch, interaction, and idle-redraw budgets with Q20 against the same-profile clean-GNOME baseline; do not call the candidate fast or lightweight until measurements exist.  
  **M3 extension:** Optional advanced transitions, shadows, translucency, or other visual refinements may be added after the M1 baseline and must remain independently disableable.  
  **Trace:** R02/R03/R06; §3; [preview strategy](preview-strategy.md); T07/T10.

- [ ] **I21 — Implement baseline native lock/login privacy and accessibility**

  **Deliver:** Simple native lock/login defaults for the M1 preview: restrained wallpaper/branding, clock/avatar presentation, hidden notification content, reduced-motion behavior, keyboard navigation, readable scaling, and on-screen keyboard support. Keep authentication native and the baseline independent of blur or a full GDM redesign.  
  **Depends:** I09, I20. **Owns:** `desktop/` lock/login/accessibility modules and asset licenses.  
  **Accept:** The M1 preview keeps native authentication and the on-screen keyboard usable, hides notification contents, and preserves keyboard navigation, readable contrast, and 100/125/150% scaling. Lock mode never captures password input. Disabling the lock/login layer leaves stock GNOME login/lock behavior and does not weaken security or accessibility. No blur is required for this baseline; any later effect must be cached, stop rendering after its transition, and pass the same fallback checks.  
  **M3 extension:** Optional advanced blur, transition, wallpaper, or GDM refinements may follow the measured baseline, with extension disable/reload cleaning up signals and resources.  
  **Trace:** R02/R03/R07; §§3, 9–10; [preview strategy](preview-strategy.md); T08/T10.

## M2 — Make a replacement laptop recoverable

- [ ] **I13 — Define the portable-profile schema and precedence**

  **Deliver:** Versioned schema, non-personal example, validation rules, managed-file/key allowlists, OS compatibility range, and last-known-good revision metadata. The real `workstation-config` repository stays private and separate.  
  **Depends:** I01, I08, I12. **Owns:** `profiles/` and profile validation module.  
  **Accept:** Resolve image defaults → approved profile → device overrides → explicit user choices. Reject secret/device-identity fields and unsupported schema/release combinations. Only allowlisted GNOME keys are managed; full dconf replay is excluded. Retained rollback releases can read or safely decline newer configuration without modifying it.  
  **Trace:** R02/R06/R07; §§5, 7; T03/T06/T09.

- [ ] **I14 — Implement independent enrollment and lost-device revocation**

  **Deliver:** Chosen owner-authentication/recovery provider integration, fresh SSH device-key authorization, network enrollment, and a lost-device runbook.  
  **Depends:** I09, I10, I11; recovery-provider/enrollment decision D02. **Owns:** identity integration and recovery-kit specification.  
  **Accept:** An owner with the old laptop unavailable can enroll using independent factors; no machine-id, Tailscale state, or private key is cloned. Verify the dev-VM host identity through a trusted path. Revoking the lost network/SSH/T3 client identity preserves current VM work and the new device's access; healthy remote provider authentication is reused.  
  **Trace:** R05/R06/R07; §§5–6, 9; T03/T05/T08.

- [ ] **I15 — Implement checkpointed, idempotent restore**

  **Deliver:** `zeus restore` validation, approved-revision retrieval, preview, managed-file backups, atomic writes, checkpoints, conflict handling, and resume behavior.  
  **Depends:** I08, I13, I14. **Owns:** restore engine and configuration migration modules.  
  **Accept:** Repeating a completed restore makes no duplicate entries or unintended changes. Interrupted steps resume safely; invalid profiles, conflicts, and full disks preserve the usable deployment and existing preferences. Preserve device overrides and unrelated keys. Populated-state migration tests cover stable identifiers, expected data, and every retained rollback binary; no reset or automatic restore replaces data.  
  **Trace:** R06/R07; §§5–6; T03/T06/T09.

- [ ] **I16 — Implement actionable readiness diagnostics**

  **Deliver:** `zeus doctor` checks for image/profile versions, connectivity, SSH trust, enrollment, remote tools/service compatibility, pending updates, and last successful export/backup metadata.  
  **Depends:** I10, I11, I13, I15. **Owns:** doctor core/readiness modules.  
  **Accept:** Each failure has a useful next step; local-only diagnostics redact sensitive content. Missing, stale, and unavailable evidence are distinct from healthy status. Backup restorability and battery performance are never inferred from connectivity. Later I29 adds the dated remote backup-health source.  
  **Trace:** R04/R05/R06/R07; §§6, 9, 11–13; T03/T05/T12.

- [ ] **I17 — Connect the first-run Setup Workstation journey**

  **Deliver:** One launcher using the shared engine for network, enrollment, profile revision, SSH trust, remote-tool checks, and final validation. A native setup window is optional.  
  **Depends:** I07, I14, I15, I16. **Owns:** setup launcher and flow integration.  
  **Accept:** A fresh install can complete setup with visible phases and retry checkpoints, without manual package installation or undocumented commands. Interrupted or unavailable personalization leaves stock GNOME and a terminal usable. Completion opens the configured T3 environment and remote terminal session.  
  **Trace:** R01/R04/R05/R06; §6; T03/T09.

- [ ] **I18 — Implement deliberate preference export and local exceptions**

  **Deliver:** `zeus config export` with allowlisted diff/review, explicit commit flow, and encrypted backup/restore hooks for approved laptop-only exceptions such as bookmarks or notes.  
  **Depends:** I13, I15. **Owns:** export module, exception allowlist, and private-profile usage documentation.  
  **Accept:** Secrets, identity state, caches, and development working trees never enter the export. Record last successful export and exception-backup times for doctor. A fresh client can recover a selected exception fixture through independent recovery access; unsaved/unexported state is clearly outside coverage.  
  **Trace:** R05/R06; §§5–6; T03/T08/T09.

- [ ] **I19 — Package the complete replacement-laptop runbook**

  **Deliver:** Prepared-media verification/storage instructions, recovery prerequisites, per-stage checklist, and lost-client handoff. Keep a verified installer accessible outside the homelab. Testing execution and timing belong to the testing checklist.  
  **Depends:** I12, I17, I18; select reference hardware/setup under D01 before the timed acceptance drill. **Owns:** recovery runbooks and reusable drill preparation.  
  **Accept:** The written journey starts at booting prepared USB and stops after desktop, T3, and existing tmux access work. Include both installation boots and authentication; record media creation separately. Budgets are 15/5/5/5 minutes. No dev-VM rebuild/reset is needed, and saved uncommitted/untracked remote work can be verified.  
  **Trace:** R05/R06; §§6, 11–13; T03/T04.

## M3 — Add advanced polish and measured power policy

The M1 baseline is the usable desktop. M3 may refine its presentation after the lightweight fallback and responsiveness evidence are recorded, then qualifies physical hardware and power/resume behavior. M3 work does not gate the first preview or require the M2 recovery journey to begin.

- [ ] **I22 — Implement conservative power and resume behavior**

  **Deliver:** One verified Fedora-supported power stack, supported 60-Hz battery mode, service audit, bounded/background backoff, and network/tunnel recovery after suspend.  
  **Depends:** I10, I12, I20, I21; reference-hardware decision D01. **Owns:** power/default-service policy and resume integration.  
  **Accept:** Lock precedes suspend; remote jobs do not hold the laptop awake; no competing power daemons or blanket experimental tuning. Run ≥3 comparable physical energy tests after warm-up against clean Fedora, including terminal-only and T3-plus-browser workloads; record median Wh/runtime and variability. Require no material regression, with the initial median-energy guardrail ≤105% of baseline. A ≥10% runtime gain remains aspirational. Record diagnostic targets of <2% settled-idle aggregate CPU, no recurring idle shell redraw, and <1% capacity/hour over eight hours of supported suspend; investigate misses.  
  **Trace:** R03/R05; §10; T05/T10/T11.

## M4 — Maintain and qualify release channels

- [ ] **I23 — Automate locked builds, signing, and release records**

  **Deliver:** Reviewed-change CI for inventory/lint/license/secret checks, isolated image assembly, OCI signing, ISO/QCOW2 generation, signed checksums, SBOM, evidence collection, artifact retention, and versioned candidate snapshot/build-note records described in [the preview strategy](preview-strategy.md).  
  **Depends:** I01, I03, I07, I09, I12. **Owns:** `.github/` build/verify workflows and release-record tooling.  
  **Accept:** Provenance ties outputs to the source commit, manifest, candidate payload digest, builder, and preview build note. Recompute fetched artifact checksums, verify base/builder digests, and compare candidate OCI identity, embedded ISO/QCOW2 payloads, and signed output checksums before promotion. Publish/sign credentials are restricted to trusted release jobs; tests use fixtures. Use runner tiers per I03. Archive rebuild inputs; do not claim byte-identical reproducibility merely because versions are pinned. A candidate record is evidence of what was built, not evidence that a VM or desktop test passed.  
  **Trace:** R01/R04/R07; §§7–8, 12; T01/T08.

- [ ] **I24 — Implement channels and client update scheduling**

  **Deliver:** Nightly/beta/stable channel promotion, retained known-good artifacts, release notes, bad-release withdrawal, and awake-only checks at a proposed six-hour interval with jitter. Download/stage defaults to AC and unmetered networking, with an explicit override.  
  **Depends:** I12, I23; support-window/soak decision D07. **Owns:** channel workflows and update scheduler.  
  **Accept:** Promotion moves the existing tested digest without rebuilding. Preview candidates follow the versioned snapshot/build-note and explicit-restart rules in [the preview strategy](preview-strategy.md); applying one never silently reboots the review VM or restarts the remote service. Configured critical checks and beta-soak criteria must pass before manual stable approval. Reject missing critical evidence and preserve a recoverable prior release; optional Flatpak/firmware records remain separate.  
  **Trace:** R03/R07; §7; T06/T07/T08/T09.

- [ ] **I25 — Implement consistent, independent dev-VM backups**

  **Deliver:** Complete data-volume inventory, hourly encrypted project/application-state backups, nightly VM backup, database-supported export/quiescence, verified QEMU guest-agent coordination, and capacity-based retention configuration.  
  **Depends:** I11; storage/capacity decision D05. **Owns:** remote backup definitions and preservation runbook.  
  **Accept:** Cover committed, uncommitted, untracked, agent-history, and database state on every relevant volume. Proposed retention is 24 hourly/14 daily/8 weekly points. Keep a copy outside the VM storage failure domain plus an encrypted offsite copy. Verify pre-change backups and writes made after backup; never use reset/recreate or automatic database restore as an upgrade.  
  **Trace:** R05/R06; §11; T12.

- [ ] **I26 — Automate compatibility and data-preserving upgrade policy**

  **Deliver:** Supported laptop/T3-server/Codex/profile/extension compatibility matrix, guarded remote maintenance, and additive, retry-safe migration procedures. This extends the preservation rules already required in I11/I15.  
  **Depends:** I11, I13, I23, I25; support-window decision D07. **Owns:** compatibility manifests and migration/restart orchestration.  
  **Accept:** Test previous stable and oldest supported installer upgrades. All retained rollback binaries can safely use retained populated state; incompatible combinations are blocked before mutation. Verify a pre-upgrade backup before production migration. Remote restarts require explicit scheduling after jobs finish. Rollback never silently restores an older database or discards later writes.  
  **Trace:** R05/R06/R07; §§5, 7, 11; T04/T06/T07/T09/T12.

- [ ] **I27 — Implement signer rotation and security servicing**

  **Deliver:** Key custody/rotation/recovery procedure, image-identity policy changes, daily upstream review, expedited security-release path, and revoked-release handling.  
  **Depends:** I09, I23, I24; signer/registry decision D04. **Owns:** `security/` lifecycle policy and security-release runbooks.  
  **Accept:** Test normal and recovery-key transition with retained installers and reject revoked/untrusted signers under the chosen policy. Preserve a verified recovery path for valid retained deployments; if compromise makes a retained artifact untrustworthy, qualify replacement recovery media before retiring that path. Expedite relevant severe fixes while retaining install, connectivity, and signature gates. Fedora/GNOME major transitions require extension/hardware qualification. Withdraw bad channel releases without rebuilding old tags in place.  
  **Trace:** R07; §§7–9, 14; T06/T08.

- [ ] **I28 — Schedule isolated backup-restore verification**

  **Deliver:** At-least-monthly restore-job scheduling, isolated destination configuration, content/integrity checks, and dated recovery records. Q12/Q21 own building/exercising the test fixture and assertions.  
  **Depends:** I25. **Owns:** remote recovery-job definitions and disaster-recovery runbook.  
  **Accept:** Restores cannot advertise the production hostname, contact production job endpoints, or run production schedules. Verify projects, Git state, history, databases, and backup decryptability from independent access. Measure dev-VM restore RTO and actual backup/offsite age; proposed project RPO is ≤1 hour. A destructive production restore requires separate explicit authorization, an exact backup target, and a pre-restore snapshot.  
  **Trace:** R05/R06; §11; T12.

- [ ] **I29 — Expose dated backup health and finish operating runbooks**

  **Deliver:** Read-only backup-health source and doctor integration for last successful backup, copy age, last restore-test date/result, and unavailable/stale states. Complete release, rollback, lost-device, provider-reauthentication, and homelab-disaster runbooks.  
  **Depends:** I16, I18, I24, I26, I27, I28. **Owns:** doctor health adapter, remote health output, operating documentation.  
  **Accept:** Laptop access cannot administer backups or Proxmox. Only dated verification records support health claims; network reachability alone proves neither restorability nor battery performance. Operating evidence distinguishes laptop-only recovery from VM/storage/whole-homelab recovery. Stable handoff references every critical T01–T12 result.  
  **Trace:** R05/R06/R07; §§6–7, 9, 11–14; T03/T06/T07/T08/T12.

## Decisions to resolve before dependent work

These are future implementation decisions, not claims that the corresponding capability is already available.

| ID | Decision/evidence needed | Due before | Closure condition |
| --- | --- | --- | --- |
| D01 | Reference laptop and supported hardware matrix | Select before I19/Q16's real recovery drill; qualify before hardware beta | First record model, GPU, Wi-Fi, display, firmware, battery capacity, and recovery setup. Then record encryption/Secure Boot, suspend, input/display, and power qualification on each supported laptop. |
| D02 | Independent secret-recovery provider and new-device authorization method | I14 | Demonstrate enrollment with the old device unavailable, trusted host identity, and lost-device revocation. |
| D03 | Exact artifacts, installer interface, and isolated build placement | I04/I05 | Pin working artifact hashes/digests; prove the selected T3 pairing, installer path, builder isolation, and CI dispatch permissions. |
| D04 | Registry namespace, expected image identity, signer custody, and rotation | I09, then I27 before stable | Record credential-free public verification policy and pass actual installed-client rejection/rotation tests. |
| D05 | Backup destinations, application-consistency method, and capacity | I25 | Confirm all volumes are covered, retention fits measured capacity, independent/offsite copies decrypt, and isolated restore succeeds. |
| D06 | Redistributable assets and dock dependency | I20 | Record sources/licenses and compatibility with the pinned GNOME release; use original/open assets. |
| D07 | Oldest supported installer/state formats, rollback retention, beta soak | I24/I26 | Version the compatibility window and measurable soak duration/journeys; configure promotion to enforce them. For the first release, label predecessor fixtures honestly rather than inventing a prior stable release. |

## Scope and handoff rules

The minimum functional preview includes the M1 baseline shell/dock and native lock/privacy/accessibility behavior. Keep advanced blur, a custom settings application, a native setup wrapper, a full live desktop, and extensive GDM redesign optional for the later M3 polish scope. Do not add Apple Silicon support, macOS application compatibility, custom package management, local development infrastructure, primary SSHFS project mounts, or silent project mirroring.

Future implementation cards should preserve these IDs, goals, dependencies, and acceptance criteria, remain unclaimed in Backlog until started, and link to their testing tasks. Storage/migration/deployment cards must carry the data-preservation criteria into their actual execution plan. No infrastructure deployment or production-data change is part of this planning delivery.
