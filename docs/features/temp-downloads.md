# Temp downloads and compact window controls

Status: implemented and deployed on VM 115 in build git-bbd36cc2af2f, version 0.1.0-preview.2.
Verification and remaining qualification limits: [iteration notes](../iterations/git-bbd36cc2af2f/README.md).

## Product behavior

Replace Downloads in the everyday Zeus experience with a private **Temp** folder
at `~/Temp`. Browser downloads and applications using the standard download
location should save there. This is separate from the operating system's `/tmp`.
The rest of the home directory remains permanent.

| Clear Temp | Meaning |
| --- | --- |
| On boot (default) | Clear once for each new OS boot, before the owner's desktop applications start. Logging out, unlocking, waking from sleep or restarting the desktop does not count. |
| Every hour / day / week | Clear the folder when that interval elapses. These are whole-folder sweeps, not per-file age limits. |
| Custom interval | Choose a positive number of minutes, hours or days; enforce a minimum of 15 minutes. |
| Never | Keep contents until the owner clears them manually. |

For an overdue timed sweep after sleep or shutdown, run one sweep before new
applications begin downloading; do not replay every missed interval. A settings
change schedules the next sweep from that change, never an immediate deletion.
Wall-clock jumps must not cause repeated sweeps. Boot mode records the attempted
boot identifier so retrying the service or logging in twice cannot wipe new files.
On systems where the encrypted home is unavailable during early boot, perform
that boot's sweep when the home becomes available, before the user app session.

The settings page shows the current policy, next cleanup (or “Next boot”), space
used, and **Clear now**. Temp has a persistent “Temporary files — clears …” label.
**Keep…** moves selected files into a chosen permanent folder, initially Documents;
it handles name conflicts without overwriting existing files. Do not create a
second hidden permanent-downloads bucket. Explain expiration at initial setup.
Clear now confirms the file count and scope; automatic scheduled sweeps follow
the displayed policy without repeated confirmation dialogs.

“Clear” means normal permanent deletion, not secure erasure. Sweeps remove eligible
contents while leaving Temp itself in place. Do not move automatic cleanup into
Trash, which would retain the storage the feature is intended to reclaim.

## Existing installations and preservation

- Never move existing Downloads contents into an expiring folder automatically.
  Retain that folder and its files; change only the destination for future downloads.
- Detect an existing `~/Temp`. Never adopt or clear it silently. Resolve the name
  collision through an explicit setup choice before enabling cleanup.
- Existing explicit browser download destinations remain owner choices. Offer to
  switch them; verify Firefox, file chooser and sandboxed app behavior separately.
- Cleanup must operate only on the provisioned, owner-specific Temp directory.
  Reject a symlinked root; never follow child symlinks, traverse mounted filesystems,
  or accept arbitrary deletion roots from preferences. Test path replacement races.
- During a live timed sweep, skip active downloads and in-use entries. Revisit
  skipped entries on the next sweep. Define and test a conservative detection
  strategy before enabling timed cleanup; a file extension alone is insufficient.
- Capture sweep candidates before deletion. Files created after that snapshot must
  survive that sweep. A failed or interrupted sweep is retry-safe and leaves an
  actionable status without blocking desktop login indefinitely.
- Preserve policy and boot/schedule bookkeeping through updates and rollback.
  Unsupported policy versions fail closed with cleanup disabled. Policy may sync
  in a future personal profile; Temp contents are excluded from profile restore.
- An OS update reboot counts as a boot under the chosen default policy. State this
  in setup and update UX. Deletion tests run only in a disposable guest; verify a
  populated backup before changing the persistent review VM's download routing.

## Implementation list

1. **Compact window controls:** red/yellow/green painted circles approximately
   12–14 logical pixels, flat fills, no outer white rings or raised shadows, tighter
   spacing; retain at least a 24-pixel native input target. Change GTK3/GTK4 controls,
   not the top bar. Preserve native close/minimize/maximize behavior and focus cues.
2. **Temp destination and migration:** provision a private directory, integrate the
   desktop download location and browser defaults, preserve old files and custom
   destinations, and handle existing Temp collisions.
3. **Cleanup engine and scheduling:** boot-once state, interval policy, scoped
   deletion, active-file exclusions, locking, retry behavior and diagnostics. Run
   with owner permissions and serialize sweep/settings/Keep operations.
4. **Settings and Keep action:** policy controls, expiration explanation, next run,
   usage, manual clear confirmation, and a permanent-save action with conflict handling.
5. **Qualification:** implement destructive-case tests in disposable fixtures/VMs,
   verify populated-data migration and retained-version compatibility, then deploy a
   signed preview and capture console evidence without deleting existing owner files.

## Testing implementation list

- Measure actual rendered control circles and hit targets at 100% and 200% scaling;
  check light/dark, keyboard focus, hover and all three window actions in GTK3/GTK4.
- Fresh account: Temp is private and the standard download destination. Test Firefox
  downloads, save dialogs and a sandboxed app; report applications that ignore it.
- Populated account: existing Downloads, existing Temp, custom paths and unrelated
  documents survive migration byte-for-byte. Re-running setup is idempotent.
- Boot default clears old Temp data once; new files survive logout/login, unlock,
  sleep/wake and service restarts during the same boot. Test encrypted-home ordering.
- Fake-clock tests cover each preset, custom interval validation, policy changes,
  overdue execution, wall-clock jumps, disabled mode and reboot during a sweep.
- Containment tests cover symlink roots/children, nested mounts, hard links, unusual
  names, permission failures, concurrent path replacement and files created mid-sweep.
- Active browser downloads and open files survive a timed sweep; completed eligible
  entries clear later. Fail closed if the active-file strategy is unreliable.
- Keep preserves file contents, handles conflicts, and cannot race cleanup. Verify
  successful permanent saves remain after a later boot and every scheduled sweep.
- Upgrade/rollback preserves policy and bookkeeping; older binaries cannot reinterpret
  an unknown policy as authorization to clear data. Test interrupted migration.
- Record retained and deleted file inventories in the disposable VM. Verify the
  populated backup before preview deployment; never restore over owner data as an upgrade.

## Decisions to validate during implementation

The proposed timed policy deliberately clears the whole folder, including recent
completed files; an age-based policy would be a separate, explicitly named option.
Active-file detection uses a socket-activated, read-only root inspector authenticated
by the connecting UID. It returns device/inode identities only; deletion runs as
the owner. An uncertain inspection defers deletion. Partial-download markers defer
the entire sweep, preserving browser companion files. Boot cleanup gets one attempt
per boot; an interrupted or deferred attempt waits until the next boot. Timed
policies use a one-shot systemd timer for the persisted deadline. Boot and Never
leave no armed timer. Sleep or shutdown collapses missed deadlines into one
catch-up attempt; persistent inspection failures back off instead of looping.

The native Temp app is available in the dock and through Super+Shift+T. Files
receives a Temp bookmark and the XDG download destination; applications with
their own explicit destinations keep those choices. GTK3 and GTK4 bookmarks are
deduplicated while retaining unrelated entries and labels. Open in Files is
available in the window and launcher menu. Keep permanently… supports regular
files into permanent folders inside the owner home.

First use explains permanent expiration and OS update reboots. Its acknowledgement
is stored separately from cleanup policy. The visible window refreshes from
bounded filesystem monitors with debouncing, and defers scans while hidden.
Monitors cover the home directory, policy state directory and top-level Temp;
changes deep inside an existing subdirectory may require the Refresh button.

## Helm implementation cards

- ZOS-57: Controls shipped; full cross-scale console action/focus matrix remains Backlog.
- ZOS-58: Temp destination and preservation — completed.
- ZOS-59: Scoped Temp cleanup and schedules — completed.
- ZOS-60: Settings and Keep shipped; explicit first-setup update-reboot explanation remains Backlog.
- ZOS-61: Preview deletion and populated upgrade qualification — completed, with limits recorded in iteration notes.
