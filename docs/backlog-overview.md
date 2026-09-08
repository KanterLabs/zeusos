# Zeus OS planning backlog

The supplied [Zeus OS Design v0.1](design-v0.1.md) is a historical design baseline. The current priority is the [preview strategy](preview-strategy.md): the first usable preview must be a very lightweight, fast, aesthetically pleasing macOS-like GNOME desktop. Remote work and replacement recovery arrive in later increments without changing that baseline.

The historical design is broken into two checklists:

- [Implementation work](implementation-plan.md): **29 tasks**, I01–I29, covering product code, image/installer configuration, remote services, recovery, desktop behavior, and release operations.
- [Testing implementation and acceptance work](testing-plan.md): **21 tasks**, Q01–Q21, covering test infrastructure, sanitized fixtures, early M1 visual checks, fault injection, automated checks, and physical recovery/hardware drills.

Each list includes dependencies, deliverables, acceptance criteria, and references to the source design. The testing plan maps all **7 requirements (R01–R07)** and **12 acceptance tests (T01–T12)**. All work remains unchecked; this delivery is a reviewed plan, with no OS build or acceptance-test result claimed.

| Order | Implementation list | Required outcome before moving on |
| --- | --- | --- |
| M0 — Feasibility | I01–I05: release contract, version pins, isolated lab, installer/rollback and remote-service prototypes | Demonstrate a bootable GNOME session, interactive encrypted VM install, persistent remote T3 connection, and manual rollback. |
| M1 — Usable preview/image | I06–I12 plus the M1 baseline scopes of I20–I21: application suite, ISO/QCOW2, command engine, security, remote launchers/services, update recovery, macOS-like shell, native lock/privacy/accessibility | Boot the lightweight desktop baseline early, with stock GNOME fallback and measured responsiveness evidence; layer offline install, remote-work, and update/recovery slices as they become ready. |
| M2 — Personal recovery | I13–I19: profile, independent identity recovery, checkpointed restore, doctor, setup, export, runbook | Complete a wiped-client recovery in ≤30 minutes on the reference setup, with the old laptop unavailable and saved remote work unchanged. |
| M3 — Advanced polish and power | I20–I22: optional advanced shell/lock polish extensions and measured power/resume policy | Baseline desktop work is already usable; optional effects remain disableable, and physical hardware passes input/display/suspend checks and the measured energy guardrail. |
| M4 — Maintained beta | I23–I29: signed CI, channels, independent backups, compatibility, key rotation, restore verification, health | Qualify release operations and dated evidence; stable additionally requires the defined beta soak, all critical acceptance tests, and explicit human promotion approval. |

Build testing support alongside each feature. Run the M1 visual baseline checks as soon as the first preview candidate exists, then extend them into the full regression suites as implementation lands. Hardware checks and recovery drills require recorded execution; CI configuration alone cannot satisfy them.

Start with I01, then I02/I03, using Q01 to establish the evidence format. Resolve the exact artifacts and isolated builder before I04/I05. M0 is a go/no-go checkpoint for the technical baseline; M1 should produce the baseline desktop preview before full recovery infrastructure is complete. Advanced polish and physical qualification remain M3 work.

The first usable preview includes the M1 macOS-like shell/dock baseline and simple native lock/privacy/accessibility behavior, alongside whatever installer and application slices are available for that candidate. M0–M2 then add the remote-work and replacement-recovery journeys; M3 adds optional advanced effects/polish and physical power qualification; M4 adds maintained release operations. This does not establish the full R01–R07 daily-use release claim. Advanced blur, a custom settings app, a native setup wrapper, and a full GDM redesign remain optional.

Four acceptance boundaries must stay visible:

- **Laptop recovery:** ≤30 minutes from booting prepared USB to configured desktop, connected remote T3, and existing terminal-session access; authentication and both installation boots count. The 15/5/5/5-minute stages are planning budgets. Zero lost work covers data already saved on a healthy dev VM, including uncommitted and untracked files.
- **Power:** ≥10% longer runtime is aspirational. The initial stable guardrail is no material regression, with median energy no more than 5% above clean Fedora under equivalent physical tests. VM tests cannot establish battery life.
- **Persistent data:** OS rollback does not undo profile/application databases. Verify populated-data migrations, pre-upgrade backups, and compatibility with retained rollback binaries. Dev-VM backup RPO is proposed at ≤1 hour; actual copy age and measured restore time govern homelab recovery.
- **Preview responsiveness:** A candidate is called lightweight only after settled-idle CPU/process memory, boot-to-usable time, idle-redraw, launch, and interaction budgets are measured against the same-profile clean-GNOME baseline. The current persistent review VM allocation is 4 vCPUs, 8 GiB RAM, and 64 GiB disk; see the [preview VM runbook](preview-vm.md). Its configuration is not a performance result. Use the persistent review VM for owner-state review, with explicit restarts and no wipe between candidates; use disposable VMs/disks for destructive install and failure-injection tests.

The [decision table](implementation-plan.md#decisions-to-resolve-before-dependent-work) identifies seven unresolved items: reference hardware, independent recovery/enrollment, exact artifacts/build placement, registry/signing trust, backup storage/capacity, redistributable assets, and the supported-release/soak policy. Candidate review and restart rules are in [the preview strategy](preview-strategy.md).

The plans are now tracked in the **ZeusOS (`ZOS`) Helm project**. See the [Helm card index](helm-backlog.md) for all 29 implementation and 21 testing cards. The updated plan verified all 50 cards in Backlog and unclaimed, with 186 unchecked acceptance criteria and 76 native implementation dependency edges. Testing prerequisites remain linked by phase so early cases can run before later suites are complete. ZOS-1 tracks the original import; ZOS-52 tracks desktop-first reprioritization and review-VM provisioning.
