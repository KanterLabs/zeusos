# Fedora Zeus dual-boot launcher

This directory contains the downloadable Fedora launcher for Zeus OS
`0.1.0-preview.2`. It is a small, unprivileged GTK4/PyGObject application
that collects a read-only Fedora inventory, asks for a total Zeus allocation
(128 GiB by default, including Zeus boot partitions), and displays the exact
plan before any preparation can begin.

The graphical process runs without root privileges. Its fixed administrative
helper downloads and verifies Zeus, prepares the space, and installs the OS
across two phases with a Fedora reboot between them. Fedora remains the default
startup choice. The supported route has passed disposable Fedora 43 VM testing;
the Nimo laptop's hardware and physical installation remain untested. See the
[qualification record](../docs/iterations/installer-20260910/README.md).

On an installed system the GTK launcher uses one privileged `review` request
to read the existing operation and collect the Fedora layout only when idle.
The CLI also exposes a standalone read-only preflight through the fixed
`/usr/libexec/zeus-installer-helper preflight <allocation-gib>` action, so a
normal Fedora desktop user can inspect the same root-owned inventory used for
revalidation. The allocation is passed as a number; the launcher cannot
provide a plan path, command, or helper executable. A source checkout without
that facade falls back to the unprivileged worker and reports the resulting
permission blocker.

## Supported layout and installation requirements

This preview targets Fedora 43 on x86_64 with UEFI, Secure Boot disabled, a
separate FAT EFI partition and ext4 `/boot`, followed by one unencrypted Btrfs
partition containing Fedora root and home. It checks actual identities and
shrinkable space before proceeding. Other layouts produce a refusal report.
Fedora's ext4 `/boot` may use either the generic Linux data or Linux extended
boot (XBOOTLDR) GPT type. The original type, names, attributes and identifiers
are retained; accepting XBOOTLDR does not change the supported partition order.

The disk-writing path requires a verified pre-change backup under this
session's data-preservation policy. The root executor reads the protected
`/etc/zeus-dualboot-backup.json` receipt; staging and reviewing the layout do not
require it. A missing receipt stops installation before resizing. The current
package does not create or verify laptop backups for the owner. Do not create
a receipt claiming verification until an independent backup has actually been
checked. This administrative requirement is separate from VM qualification.

The receipt is a root-owned regular file with mode `0600`, containing
`verified`, `backup_target`, and the reviewed plan `fingerprint`; verification
evidence should also record the backup digest, size, time, and exact GPT table
fingerprint. The disposable VM qualification receipts under
`docs/iterations/installer-20260910/` document actual checked test backups;
they cannot be reused as laptop receipts.

The installer downloads the signed build qualified with its pinned toolchain.
That metadata URL is fixed to a Git commit, so a later preview-feed change
cannot silently select a different installation image. Once installed, Zeus's
normal updater checks the current preview feed.

## Running it

Download the `zeus-installer` RPM from the existing **0.1.0-preview.2** GitHub
release, then run these commands in the directory containing that RPM:

```sh
sudo dnf install ./zeus-installer-*.noarch.rpm
zeus-installer
```

Choose the allocation, use **Download and prepare**, then start installation.
If you change the allocation, choose **Check again** before downloading.
The window shows checking, connecting, downloading and verification stages.
Downloads show received/total bytes and percentage; other stages show activity
and elapsed time. A completed download still needs verification before
installation becomes available. Reopening an active operation displays its
last reported status, rather than claiming to monitor it live.

When asked, restart Fedora and reopen Zeus Installer to continue. After the
second phase, restart again and choose **Zeus OS** from Fedora's startup menu.
This personal preview provisions user **shane** with the requested password
**root**. Fedora's existing account and password are preserved. No restart is
automatic.

From a source checkout:

```text
installer/zeus-installer
installer/zeus-installer preflight --json
installer/zeus-installer preflight --allocation-gib 160 --json
installer/zeus-installer gui
```

`preflight --json` writes one JSON object to stdout and performs no writes to
storage. A generated plan is a successful report even when `supported` is
false; inspect `supported` and `blockers` in scripts. Collection failures
produce an `ok: false` JSON diagnostic with a non-zero status when `--json`
is supplied. `--version` reports `0.1.0-preview.2`.

The public preflight boundary is:

```python
inventory = preflight.collect()
plan = preflight.plan(inventory, allocation_gib=128)
```

The plan must be a JSON-compatible mapping containing `supported`,
`blockers`, `inventory`, `target`, and `fingerprint`. The launcher renders
the target disk identity and Fedora preservation information from the
inventory, while leaving policy decisions to the preflight worker.

## Backend qualification boundary

The UI looks for a sibling `zeus_installer.backend` module. Verified
preparation requires a callable `prepare()` facade after a supported plan;
installation and restart remain fail-closed until all of these executor
conditions are true:

1. The module returns `{"qualified": true}` from `qualification()` (or
   `get_qualification()` / `qualification_status()`), or defines the literal
   code-level `QUALIFIED = True` marker.
2. It exposes a separately qualified `install(plan)` executor.
3. It exposes `restart()`.

The facade exposes `review(allocation_gib)` for the GUI's combined status and
read-only scan. Older facades can provide separate `status()` and
`preflight(allocation_gib)` calls. Review is allowed to authenticate because the fixed
helper only collects and validates an allocation; it never accepts an
arbitrary command or filesystem path.

Starting preparation uses the reviewed allocation and fingerprint without
another desktop review. The root helper still recollects the current layout
and checks that fingerprint, then revalidates inside the operation lock.
Preparation callbacks carry either `{"stage": "checking_target"}` (or another
fixed stage identifier) or `{"bytes": 123, "total": 456}`. These bounded advisory
events do not alter the journal or authorize any operation.

`status()` should report the root-owned journal phase and an explicit
`current_boot_changed` boolean. Before reboot, `phase: "reboot_required"`
enables **Restart Fedora to continue**. After reopening Fedora, the same
phase with `current_boot_changed: true` enables **Continue installation**;
that action calls `install()` without forwarding a newly collected plan, so
the executor resumes the original journal plan and verifies its recorded
post-reboot change.

`prepare(plan, progress=None)` owns signed release retrieval, digest verification, staging, durable
phase journaling, and target ownership checks. The packaged helper is installed
at `/usr/libexec/zeus-installer-helper`; a trusted backend facade may invoke
that fixed path through `/usr/bin/pkexec` with only the numeric allocation.
The root helper recollects and replans the host from its installed modules; no
desktop-supplied plan path crosses the privilege boundary. The GUI never
accepts a helper path or command from the desktop user. `prepare`
must return a mapping with
`ok: true` and `state: "ready"` (or `prepared: true` / `staged: true`) before
the **Install into new space** action is enabled. The executor must then
return `phase: "reboot_required"` before the restart button is enabled. A
result with `phase: "installed"` is terminal; once the backend is qualified,
the launcher offers an explicit **Restart to choose an OS** action. Fedora
remains the default boot choice, and the launcher never restarts automatically.
The launcher never accepts a URL, archive path, shell command, or credential
from the desktop user. The GTK process does not invoke `pkexec` directly.

`restart()` is an explicit owner action after the executor reaches
`reboot_required` or completes installation. It is never called automatically
when a download or install phase completes. `cancel()` is optional and, when
provided, is called only through the backend's documented safe-boundary
operation.

The current backend does not offer cancellation during a running operation,
so the launcher hides that control. Review before starting and wait for each
step to finish; do not treat closing the window as confirmation that it stopped.

## Building a downloadable package

Run the packaging script from the repository root:

```text
scripts/package-installer.sh
```

It creates a source tarball and SHA-256 sidecar under `out/installer/`:

```text
out/installer/zeus-installer-0.1.0-preview.2.tar.gz
out/installer/zeus-installer-0.1.0-preview.2.tar.gz.sha256
```

Each package includes a `BUILD-INFO` receipt. The launcher version stays
`0.1.0-preview.2`; RPM release metadata additionally carries the source git
commit count and short commit ID (with a `dirty` marker for local edits), so a
later committed build has a distinct upgrade identity. Release automation can
set `ZEUS_INSTALLER_BUILD_ID` to a stable RPM-safe identifier.

When Fedora `rpmbuild` is installed, the same command also builds a noarch
RPM using `installer/packaging/zeus-installer.spec` under
`out/installer/rpm/`. If `rpmbuild` is unavailable, RPM creation is reported
as skipped and the tarball still succeeds. The script compiles the launcher
modules and packages only the installer source, desktop entry, spec and
documentation and license; it never builds the Zeus OS image and never includes private keys,
account tokens, or local build output.

Install the RPM on a Fedora test system with the normal owner-approved package
workflow, then launch **Zeus Installer** from the applications view. Keep a
verified copy of the package outside the homelab before any physical laptop
qualification.

The removal module has a tested menu-only operation that preserves partitions
and files. A graphical uninstaller and automatic space reclamation are not
included in this preview.

Published builds include a `SHA256SUMS-zeus-installer-<build-id>` manifest and
an SSH detached signature using the existing Zeus release key. Verify it with
`security/allowed_signers` from an independently obtained checkout, signer
`zeusos-preview` and namespace `zeusos-release`, then check the downloaded
files against that manifest. This is separate from RPM's native GPG signing.
