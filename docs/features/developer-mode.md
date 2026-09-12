# Repository-backed Developer Mode

Status: desktop-extension foundation implemented for the next signed preview
iteration. The active product version remains
**0.1.0-preview.2**; developer iterations are identified by exact Git commits
and never create a product version by themselves.

## Goal

Developer Mode shortens the edit-to-review loop on Shane's Zeus laptop while
keeping the repository as the source of truth:

```text
edit in ZeusOS repo → focused checks → commit and push → verified developer artifact
→ apply on Zeus → review → include the same files in the next immutable image
```

A change is not complete when it exists only on the laptop. Every applied
artifact must be built from a clean commit that is present on the configured
`KanterLabs/zeusos` remote. The artifact and the laptop status both record the
full source commit. The normal image builder continues to copy these same repo
files into later bootc images.

Developer Mode is optional and off by default. Enabling or applying it requires
an active local administrator to authenticate. It never enables automatic
updates, automatic restarts, passwordless root access, live RPM layering, or
background source watchers.

The foundation itself ships once through the normal signed image updater. That
image adds the compatibility identity, helper, policy and Settings surface.
Afterward, approved desktop changes no longer need a complete image rebuild.

## Three iteration lanes

| Lane | Intended changes | Activation | Required testing |
| --- | --- | --- | --- |
| Desktop extension | Zeus windows, non-privileged desktop integration, icons, wallpapers, GTK styles, and approved GNOME extension files | Restart the affected app; log out for shell changes; no OS reboot | Changed-file checks, focused feature tests, bundle trust tests |
| Developer image | Packages, compiled binaries, system integration, firmware, networking, boot behavior, or other image-level work | Stage a complete bootc image, then explicitly reboot | Changed-file checks, image validators, and a relevant VM smoke test |
| Release candidate | A build proposed for a new product version or promoted preview | Normal signed Updates and explicit reboot | Complete source, image, install, update, rollback, preservation, trust, visual, performance, and applicable hardware gates |

The desktop extension is the fast path. The developer-image path exists for
changes that cannot safely be represented by a desktop extension. The release
gate remains separate so ordinary visual iteration does not repeatedly run the
largest suites.

## User experience

Developer Mode appears under **Settings → Advanced → Developer Mode**. It shows:

- whether the mode is off, enabled, active, incompatible, applying, or needs
  attention;
- the immutable base build, active developer commit and artifact digest;
- the changed components and whether an app restart, logout, or OS reboot is
  required;
- the most recent focused-test receipt and application time; and
- **Apply prepared change**, **Undo last apply**, and **Turn off Developer
  Mode** actions.

The Zeus status area displays a restrained **DEV** indicator whenever a
developer artifact is active. This makes screenshots and diagnostics visibly
different from a qualified release without adding a large permanent control.
`zeus version` and `zeus doctor --json` report both identities as well.

The local command surface is separate from the existing `zeus dev` remote-SSH
launcher:

```text
zeus developer status
zeus developer enable
zeus developer apply
zeus developer undo
zeus developer disable
```

The unprivileged builder runs from a ZeusOS checkout before `apply`. It refuses
a dirty tree, detached unpublished commit, wrong remote, failed focused check,
or commit that is not the exact tip pushed to its remote branch. It builds from
the Git object with `git archive`, not by copying the mutable working tree, and
writes a bounded `prepared.json` selection into the caller's private runtime
spool. `apply` consumes only that digest selection and the three fixed artifact
filenames below it.

## Desktop extension design

Zeus uses `systemd-sysext`, already present in the Fedora 44 image, to merge a
read-only developer extension into `/usr`. This keeps the bootc base unchanged
and provides a standard, reversible overlay on an immutable system.

Before using it on the owner laptop, a disposable Zeus VM must prove the actual
Fedora 44/systemd 259 lifecycle: directory and SquashFS formats, activation at
boot, refresh, file replacement, unmerge, incompatible-base rejection, SELinux
labels, failure restoration, safe-desktop access, and disabled-mode boot cost.
The feature fails closed to the full-image lane until that proof passes.

The base image will add `SYSEXT_LEVEL=<base-build-id>` to
`/usr/lib/os-release`. Every developer extension carries a matching
`/usr/lib/extension-release.d/extension-release.zeus-developer` file. An image
update changes the base build ID, so systemd rejects an extension built against
the previous base before it can appear in `/usr`.

The first allowlist is deliberately narrow:

| Component | Repository inputs | Result | Activation |
| --- | --- | --- | --- |
| Settings UI | `desktop/rootfs/usr/libexec/zeus-settings-window`, approved non-privileged model files and app assets | Replaces the Zeus Settings presentation | Close and reopen Settings |
| Temp UI | `desktop/rootfs/usr/libexec/zeus-temp-window` and app assets | Replaces presentation only; cleanup/deletion engine stays image-managed | Close and reopen Temp |
| Updates UI | `desktop/rootfs/usr/libexec/zeus-update-window` and app assets | Replaces presentation only; privileged updater stays image-managed | Close and reopen Updates |
| Welcome UI | `zeus/assets/welcome.py` plus approved launcher/icon assets mapped to their image targets | Replaces Welcome presentation | Close and reopen Welcome |
| Theme and artwork | Zeus theme CSS/assets, icons, wallpapers, background metadata | Makes repo artwork available through the normal paths | Reopen affected apps; reselect background when needed |
| Shell presentation | Approved files below the two Zeus GNOME extension directories | Replaces the optional Zeus presentation layer | Log out and back in; never replace GNOME Shell on Wayland |

The component map is an explicit repository file, proposed as
`developer-mode/components.json`. Each source maps to one fixed `/usr` target,
expected file type and mode, maximum size, activation action, and focused test
set. Unknown sources or targets fail closed and are routed to the developer
image lane.

The first desktop lane excludes:

- `/etc`, `/var`, `/home`, credentials, device identity and mutable application
  data;
- the updater engine/admin helper, Polkit policy and signing policy;
- installer, partitioning, bootloader, initramfs, kernel, firmware and package
  changes;
- system services, tmpfiles definitions, SELinux policy and networking;
- Temp cleanup/deletion code and any persistent-data migration;
- the base `zeus-desktop-safe` fallback used to escape a broken extension; and
- arbitrary executables, symlinks, device nodes and paths not present in the
  checked-in component map.

Those exclusions do not prohibit the repo changes. They require a complete
developer image and the matching risk-specific checks before laptop review.

## Artifact and provenance contract

The deterministic developer bundle contains a read-only extension image, a
detached manifest and its signature. The extension itself contains approved
`/usr` files plus an inner provenance record; the detached signed manifest
binds that extension image's hash without creating a circular self-hash. The
manifest records:

- schema, product, artifact kind and architecture;
- canonical repository, pushed branch and full 40-character source commit;
- active product version, immutable base build ID and base image digest;
- selected components, source paths, fixed targets, file modes and SHA-256
  hashes;
- required activation actions;
- focused test names, results and timestamps; and
- artifact hash, creation time and developer signer identity.

Developer artifacts use a dedicated `zeusos-developer` signer and SSH-signature
namespace. They do not use or expand the normal `zeusos-update` release trust.
Enabling Developer Mode enrolls the developer public key into a fixed,
root-owned policy after administrator authentication. The private key remains
outside the image and outside the repository.

The root helper accepts only exact manifest identity and digest selections from
a bounded local spool. It independently verifies the signature, archive hash,
base identity, component allowlist, file hashes, types, modes and sizes. Before
activation it mounts each SquashFS in a private read-only, no-exec location and
matches the complete tree to the signed file list; unknown formats and extra
files are rejected. It does not accept a URL, shell command, target path, or
executable from the desktop process.

Root-owned state lives below `/var/lib/zeus/developer-mode/`:

```text
status.json                 public, bounded status without secrets
active                      atomic reference to the active artifact
previous                    atomic reference to the preceding artifact or base
artifacts/<digest>/         verified, immutable, private artifact storage
```

The public status records the base identity, active and previous artifact
digests, source commit, components, state, required activation action, focused
test receipt, application time, and authenticated actor UID. It never exposes
private key material, credentials, URLs, or unbounded command output.

Application is transactional: copy and verify the new artifact, atomically
switch the active reference, run `systemd-sysext refresh`, confirm the merged
identity, then publish status. If refresh or validation fails, the helper
restores the previous reference and refreshes again. It retains the last known
working extension until the next application has been confirmed. Undo returns
to that extension or to the untouched base.

There is no long-running Developer Mode daemon. A conditional boot oneshot
activates an already-verified compatible extension and exits. Disabled and
active boot timing, idle CPU and memory are measured so the fast path does not
quietly weaken Zeus's boot and battery goals.

## Interaction with normal Updates

Normal Updates keeps its current release key, signed feed, root-owned lock,
private download storage, explicit restart, retained deployment and rollback
behavior.

If an extension is active, Updates offers **Pause developer changes and
continue** before staging a normal image. A normal update never copies the
extension into the new deployment and never silently reapplies it afterward.
The `SYSEXT_LEVEL` mismatch keeps a stale extension inactive on the new base.
Developer Mode then reports **Needs rebuild for this base** and can rebuild the
same repo component against the installed image.

A staged normal update, queued bootc rollback, active OS update, or another
developer transaction blocks a developer apply. Both paths acquire the existing
`/var/lib/zeus/updater/operation.lock` and preserve the already-staged
deployment. Leaving Developer Mode never restores `/var`, home data,
preferences, or application databases.

## Developer image lane

Changes outside the desktop allowlist build a complete bootc OCI image from the
exact pushed commit on the isolated homelab builder. The product version stays
fixed while the image build ID remains `git-<12-character-commit>`.

The developer image has a separately signed developer manifest and is staged
through a Developer Mode action that reuses the updater's archive validation,
identity checks, disk-space checks, operation lock and `bootc switch --retain`
behavior. It does not change the public preview feed. The running qualified
image remains the rollback deployment, and activation always requires an
explicit reboot.

The full image receipt also records the builder/base digests and resolved
package-lock hash. The commit identifies its source, but byte-identical full
image rebuilds are not claimed while Fedora package repositories remain
unsnapshotted.

The public preview/release feed can point at that exact tested image digest only
after the release gate passes. Promotion never rebuilds the candidate.

## Test cadence

### Fast checks on each commit

A checked-in change selector maps repository paths and components to existing
tests. Every pushed commit runs formatting/syntax checks and the directly
affected feature tests. Examples:

- Settings UI: `test_settings_window.py` plus Python compilation;
- Temp UI: `test_temp_window.py` and presentation-only Temp tests;
- shell/lock presentation: `test_desktop_interactions.py`,
  `test_style_preservation.py`, and `lock_background.test.mjs`;
- updater presentation: `test_updater_window.py` without treating it as
  permission to change the privileged updater; and
- Rust CLI: formatting, Cargo tests/build, and `test_cli.py` on
  `homelab-heavy`.

Changes to the selector, component map, trust code, test harness or unknown
paths widen the check set rather than selecting zero tests.

The current complete Python source suite is only about sixteen seconds on the
development environment (369 tests in the planning baseline), so it may remain
a cheap push safety net. The expensive work being deferred is image assembly,
disk export, repeated VM install/update/rollback cycles and physical hardware
qualification—not useful fast tests merely because they are numerous. Record
new timings before changing this decision.

### Before a desktop apply

The exact commit must pass its selected checks plus the Developer Mode contract
suite: clean/pushed commit proof, deterministic archive, signature, hashes,
allowlist, base compatibility, path/type/mode limits, transaction locking,
status, apply, failed-apply restoration and undo. The receipt embedded in the
artifact must match the source commit. No full image build is required.

### Before a developer-image apply

Run the selected source tests, all build-time validators, `bootc container
lint`, image identity checks and a relevant disposable-VM smoke test. Changes
to boot, storage, installer, updater trust, cleanup, or persistent state also
run their existing preservation/failure cases before reaching the laptop.

### Before a new version or promoted release

Run the complete source suite, Rust build/tests, clean image build and lint,
secret/provenance checks, fresh install, upgrade, rollback, interrupted-update,
signature rejection, populated-data preservation, desktop/accessibility and
performance gates. Hardware-sensitive releases also require the applicable
physical Wi-Fi, Bluetooth, suspend, input and battery checks. The exact
qualified digest is promoted; any source or artifact change invalidates the
gate.

GitHub Actions will be split into:

- a path-aware **Fast checks** workflow on every push and pull request, using
  `homelab` for short checks and `homelab-heavy` only for Rust or heavy work;
- an owner-triggered **Developer artifact** workflow for a selected exact
  commit, producing either the extension or developer image with its receipt;
  and
- an owner-triggered **Release gate** workflow that runs the full suite and is
  the only path allowed to publish a new version or promote a public feed.

## Implementation list

- [x] **DM00 — Qualify the extension mechanism.** Prove systemd-sysext on a
  disposable Zeus VM, including runtime and boot activation, replacement,
  unmerge, SELinux, incompatible-base rejection, recovery and disabled-mode
  boot/idle cost. Do not expose the laptop toggle until this passes.
- [x] **DM01 — Define components, manifest and base compatibility.** Add the
  explicit component map and schemas, embed `SYSEXT_LEVEL` in image identity,
  define the separate developer signer policy, and reject dirty/unpushed source
  or a mismatched base.
- [x] **DM02 — Build deterministic developer artifacts.** Build from `git
  archive`, select focused tests from changed paths, emit a signed receipt and
  reproducible read-only extension, and refuse unknown/disallowed files.
- [x] **DM03 — Implement transactional apply and undo.** Add the narrow
  root helper, private content-addressed storage, operation locking, atomic
  active/previous references, sysext refresh/confirmation and failure rollback.
- [x] **DM04 — Add CLI, Settings and visible provenance.** Implement the
  `zeus developer` commands, Settings page, DEV indicator, restart/logout
  guidance, status integration and terminal recovery instructions.
- [x] **DM05 — Coordinate Developer Mode with Updates.** Preserve normal update
  trust and staging, block conflicting transactions, pause overlays deliberately
  and leave incompatible artifacts inactive after a base update.
- [ ] **DM06 — Add the full developer-image lane.** Build and sign an exact
  pushed commit on the isolated builder, reuse the safe bootc staging engine,
  retain the normal deployment, and avoid modifying the public release feed.
- [ ] **DM07 — Split fast CI from the release gate.** Add conservative
  changed-path test selection, developer-artifact CI, and an explicit complete
  release workflow using the correct homelab runner tier per job.

## Testing implementation list

- [x] **DT00 — Build the sysext lifecycle fixture.** Exercise the actual Fedora
  44/systemd 259 commands in a disposable bootc guest and retain console,
  mount/status, timing and recovery evidence.
- [x] **DT01 — Source and manifest fixtures.** Cover dirty, uncommitted,
  unpushed, wrong-remote and detached-source cases; prove artifacts contain
  files from the named Git object and reproduce byte-for-byte.
- [x] **DT02 — Trust and path-boundary tests.** Reject wrong signer/namespace,
  altered manifests, hash/size/mode mismatches, traversal, links, device files,
  unknown components, arbitrary targets, credentials and mutable-data paths.
- [x] **DT03 — Transaction and recovery tests.** Exercise concurrent apply,
  interrupted copy/verify/refresh, failed merge confirmation, exact retry,
  undo, disable, full storage and status atomicity while retaining the previous
  working artifact.
- [x] **DT04 — Base/update compatibility tests.** Prove an extension activates
  only on its exact base build, becomes inactive after a normal image update,
  cannot replace a staged update/rollback, and can be rebuilt without changing
  user data.
- [ ] **DT05 — Desktop review smoke.** On the persistent review VM, apply and
  undo each approved component, reopen the relevant app or session, capture
  screenshots, run safe-desktop fallback, and verify the displayed commit.
- [ ] **DT06 — Developer-image smoke.** On a populated disposable dual-boot
  fixture, stage, reboot, verify identity, preserve partitions and owner data,
  rollback, and confirm no public feed or release version changed.
- [ ] **DT07 — Release-gate enforcement.** Prove failed/missing full checks
  prevent promotion, successful qualification promotes the exact tested digest,
  and ordinary fast-lane commits cannot publish a new product version.

## Initial success targets

For an approved UI-only commit, the target is commit-to-active review in under
two minutes on the laptop, without an OS reboot. Undo should take under thirty
seconds plus any required app restart. These are targets to measure, not current
results. The metrics history will record actual bundle size, test time, apply
time, activation time, idle impact and failures without replacing earlier
measurements.

The first implementation milestone is DM00–DM05 plus DT00–DT05. That produces
the useful desktop fast path. DM06/DT06 adds system-level iteration afterward;
DM07/DT07 formalizes release promotion once both lanes are proven.
