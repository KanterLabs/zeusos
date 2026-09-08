# Zeus OS preview strategy

This is a planning document. It records the order and evidence required for preview review; it does not claim that an OS image has been built, installed, or accepted.

The supplied [Design v0.1](design-v0.1.md) is historical. The current priority is to make the first usable Zeus OS preview very lightweight, fast, and aesthetically pleasing while retaining Fedora/GNOME security and fallback behavior. The M1 baseline below is therefore a release requirement for the first preview. Remote work, personal restore, and maintained release operations can arrive as later increments.

## First usable preview

The first preview is the M1 desktop slice owned by I20 and I21. It should boot a supported GNOME/Wayland session with:

- a quiet top bar, centered dock, running indicators, intelligent hide, coherent spacing, and the specified Files, Terminal, T3 Code, Dev, and Browser entries;
- native lock and login behavior with hidden notification contents, readable contrast, keyboard navigation, scaling, reduced motion, and an on-screen keyboard; and
- a safe-desktop fallback that can disable the dock/status/lock additions and leave stock GNOME, terminal access, connectivity, credentials, and authentication usable.

The baseline uses supported GNOME facilities and a small, pinned integration layer. It does not require blur, animated wallpaper, a compositor fork, fake application menus, invasive toolkit overrides, or a full GDM redesign. Any advanced effect is a later optional M3 extension and must preserve the same security, accessibility, and fallback checks.

The preview can be reviewed before the complete I14–I19 recovery path exists. M1 remote-work and update slices are added when I05–I12 are ready; M2 then adds enrollment, profile restore, and the timed replacement-laptop journey. A later increment must not remove or weaken the already reviewed desktop baseline.

| Phase | Preview purpose | Planning cards and test scope |
| --- | --- | --- |
| M0 | Prove the pinned artifacts, isolated build/install path, GNOME boot, rollback shape, and remote-service feasibility. | I01–I05; Q01–Q06, Q08, and Q13 prototype checks. |
| M1 | Produce the first usable desktop preview, then add remote-work and update slices incrementally. | I06–I12 plus I20/I21 M1 baseline scopes; Q20 M1 visual/accessibility smoke, Q15 install, and the applicable Q17–Q19 smoke checks. |
| M2 | Make the reviewed baseline recoverable on a replacement laptop. | I13–I19; Q10/Q11/Q16 and the M2 portions of Q17–Q19. |
| M3 | Refine optional desktop presentation and complete physical input, suspend, and power qualification. | I20/I21 M3 extensions and I22; Q20 M3 completion and Q17 physical transport checks. |
| M4 | Maintain candidate channels, compatibility, signing, backups, and dated recovery evidence. | I23–I29; Q12–Q14 and Q18–Q21 full scopes. |

## Review VM and state policy

The persistent review VM is for owner-state preview review. Its current allocation is 4 vCPUs, 8 GiB RAM, and a 64-GiB disk; exact host and provisioning details are maintained in the [preview VM runbook](preview-vm.md). No candidate installation or performance result is implied by that allocation.

Keep the same review VM and its owner state across candidate snapshots so changes can be judged in a consistent environment. Apply each candidate deliberately, record the candidate ID and source commit, and perform any required reboot or session restart explicitly. A candidate update must not silently reboot the desktop or restart the remote service. Do not wipe or recreate the review VM for an ordinary candidate; preserve the previous review point so a known-good candidate can be selected deliberately.

Image assembly remains in the isolated builder defined by I03/Q04. The review VM receives versioned artifacts for inspection. Installer partitioning, two-disk safety, full-disk exhaustion, interrupted install/update/restore, and other destructive or fault-injection cases use named disposable VMs and disks from Q05, Q07, and Q14. They never use the persistent review VM or its owner state.

## Candidate cadence

Use an event-driven review cycle whenever a coherent preview change is ready:

1. Cut an immutable, dated candidate snapshot with a monotonically increasing preview ID. The snapshot records the source commit, release-manifest/profile revisions, OCI/ISO/QCOW2 digests, pinned tool versions, and the intended M1 or later increment.
2. Write build notes for that candidate. Include what changed, known limitations, required restart steps, and the exact checks intended for the review. A build note describes the artifact; it is not a test result.
3. Before applying a candidate to a populated review VM, verify a pre-upgrade backup and that the retained candidate/binary can read its populated state. Apply the candidate to the persistent review VM with an explicit restart when required. Record that the owner state remains present and identify the prior candidate available for comparison or rollback.
4. Run the M1 portion of Q20 as soon as the baseline desktop exists. Check visual hierarchy, dock and shell behavior, native lock/privacy/accessibility, stock fallback, and the declared lightweight responsiveness measurements. Add remote-work checks only when their I05–I12 capability and fixture are ready.
5. Keep the candidate snapshot, build note, review observations, measurements, and open issues together. Never overwrite a prior candidate record or describe an unrun check as passed.

The candidate sequence is separate from the M3 completion gate. M1 review establishes that the baseline is usable; M3 may add optional polish and must then complete physical display/input, suspend, and power evidence before the corresponding full Q20 scope closes.

## Lightweight responsiveness gate

The words “fast” and “lightweight” require measurements. VM sizing, package count, a screenshot, or a successful boot is not performance evidence. Before preview signoff, declare the comparison and budgets, run the same steps on the candidate and the recorded clean-GNOME baseline, and retain the actual values and variability.

At minimum, Q20 records:

- settled-idle aggregate CPU, with the existing proposed diagnostic target of less than 2%;
- settled-idle process memory and boot-to-usable time, compared with the same-profile clean-GNOME baseline;
- whether the custom shell causes any recurring idle redraw;
- launch and interaction response for the overview/search, terminal shortcut, dock show/hide, lock/unlock, and the first preview applications, using budgets declared before the run; and
- background processes, startup services, and any animation or blur work that continues after the visible transition.

Missing measurements block the M1 preview signoff. The thresholds above are planning budgets and diagnostic targets, not measured results. A later M3 power run adds physical Wh/runtime, suspend drain, thermals, Wi-Fi, display, and input evidence; VM measurements cannot stand in for those results.

## Evidence and handoff

Each candidate review records the candidate ID, date, source and profile revisions, artifact digests, review-VM configuration, restart actions, owner-state comparison, visual/accessibility findings, responsiveness measurements, fallback result, remote-work increment status, and unresolved issues. Mark a check as pending when its implementation or environment is not ready. Link implementation details to [the implementation checklist](implementation-plan.md) and test execution to [the testing checklist](testing-plan.md); this document adds no new I or Q IDs.
