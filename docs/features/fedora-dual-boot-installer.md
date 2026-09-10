# Fedora-launched Zeus dual-boot installer

Planning date: 2026-09-10. Status: implemented; clean VM installation and
repeated offline boots verified. Lifecycle and release evidence is recorded in
[the dated qualification record](../iterations/installer-20260910/README.md). Product remains **0.1.0-preview.2**.
Planning task: ZOS-78. This supersedes the replacement-only direction in ZOS-76.

## Feasibility and intended experience

The no-USB dual-boot route has been demonstrated on disposable Fedora 43
VMs matching the reported partition geometry. Both operating systems boot
through Fedora's menu. The Nimo laptop itself has not been modified or tested.

The owner launches a small graphical installer from Fedora, reviews the detected
disk and space allocation, downloads a verified Zeus build, and starts the
space-preparation step. It then restarts into Fedora and reopens the installer
to install Zeus into the freed space. Afterwards a visible startup menu offers **Fedora**
and **Zeus OS**. Fedora remains the default until the owner changes it. Normal
installation should not require entering firmware setup. Additional reboots may
be necessary; this is not an in-place conversion of the running Fedora desktop.

Offer an initial **128 GiB total Zeus allocation**, adjustable after preflight,
including its boot partitions. This is a proposal, not a measured minimum.
The existing image definition requests at least 56 GiB for its root filesystem.
Leave ample Fedora capacity and reserve room for Zeus downloads and rollback.

## Known constraints and architecture

- The laptop has a 931.5 GiB NVMe, 600 MiB ESP, 1 GiB Fedora `/boot`, and
  929.9 GiB Btrfs root/home. Its reported 601 GiB free is **filesystem space**,
  not an unused partition. Actual shrinkability is still unknown.
- Preserve Fedora's root/home subvolumes, account, profiles, kernel entries and
  existing EFI files. Give Zeus its own root, home and `/boot`. Do not mount the
  Fedora home as Zeus home; Temp cleanup must never traverse Fedora data.
- Prefer a separate Zeus ESP in newly freed space to avoid two Fedora-derived
  systems overwriting the same EFI vendor directory. Prove firmware selection,
  Fedora menu chainloading and bootupd update ownership in the VM spike. A shared
  ESP is an alternative only with proven distinct paths and updater behavior.
- Use `bootc install to-filesystem` only against the newly prepared empty Zeus
  target. This primitive accepts externally prepared mounts; it does not supply
  our dual-boot orchestration. The exact pinned image/tool version must be tested.
- Never run whole-disk image writes or `to-existing-root` on the laptop. Despite
  its `alongside` terminology, the latter wipes the running system's `/boot`.
- Download and verify the OCI payload before changing storage. Shrink the
  mounted Fedora Btrfs filesystem first, verify its actual device boundary,
  then reduce only the final partition's end. Preserve its start, filesystem
  UUID and partition identity. Hold a sleep/shutdown inhibitor during writes.
- Reboot into Fedora before creating Zeus partitions. Verify the changed boot
  ID, the complete expected GPT table and the kernel's new partition size.
  Continue using the original journaled allocation. Do not compute another
  allocation after reboot or reread an in-use root partition in place.
  Reject unsupported encrypted or multi-device layouts.
- Keep Fedora's existing boot path valid throughout. Add an installer-owned
  menu entry only after Zeus is complete; never overwrite generated Fedora
  configuration wholesale. Default and timeout are explicit owner settings.

The two-phase route and separate ESP remain subject to the qualification checks
below. A separate RAM maintenance environment is no longer part of this iteration.

### Implementation investigation, 2026-09-10

The implementation adopted the two-phase route: shrink Btrfs
while Fedora is running, verify the filesystem boundary, change only the final
partition's end, then require a reboot into Fedora before using the freed space.
This avoids a separate RAM maintenance image. A changed kernel boot ID, exact
expected GPT table and kernel partition size must all match before continuing.
Populated VM118 and interrupted-command refusal tests subsequently passed;
physical power-loss behavior remains untested.

Exact current payload tools are bootc **1.16.10** and bootupd **0.2.35**.
Their default bootloader installation/update paths discover ESPs on the backing
disk; this requires explicit isolation for a second Fedora-derived system.
The candidate initial installation uses `--bootloader none`, then installs only
the EFI component against the already mounted Zeus ESP without disk discovery
or firmware updates. Ongoing EFI servicing must also be restricted to Zeus before
the physical write path can be qualified. Merely creating a second ESP is not
enough to establish update safety.

## Implementation list, in dependency order

1. **Read-only preflight and plan.** Collect edition, mount/subvolume topology,
   encryption, disk IDs, partition boundaries, Btrfs usage/minimum estimates,
   bootloader/EFI state, available power and staging capacity. Emit a reviewable
   JSON plan with exact targets and rejection reasons. Revalidate immediately
   before writes; never identify a disk solely by `/dev/nvme0n1`.
2. **Boot and storage feasibility spike.** Create a populated disposable Fedora
   fixture. Prove the Fedora reboot boundary, Btrfs resizing, isolated bootc installation and
   menu selection with no USB/firmware interaction. Record exact commands,
   versions and boot ownership. This is a gate for the product installer.
3. **Transactional installation backend.** Authenticate the release metadata,
   verify archive digest before mutation, journal phases durably, enforce exact
   target ownership and implement interrupted-run detection. Verified pre-upgrade
   backup and independent recovery are physical deployment gates under the
   session's data-preservation policy. VM snapshots alone do not establish laptop
   recovery. Do not silently restore data or retry destructive stages.
4. **Fedora graphical launcher.** Package a downloadable Fedora-compatible app
   with a narrowly scoped privileged helper. Show space choice, retained Fedora,
   download progress, planned changes, restart and failure instructions. Keep
   UI responsive; cancellation before mutation changes no disk layout. During
   mutation, stop only at documented safe boundaries.
5. **Boot lifecycle and removal.** Preserve selectable Fedora across both OSes'
   kernel/bootloader updates and Zeus rollback. Provide removal from Fedora that
   targets only recorded Zeus resources, explicitly accounts for Zeus user data,
   and leaves Fedora bootable. Reclaiming partition space is a separate reviewed
   action, not automatic cleanup. No persistent polling service is needed.

## Testing implementation list

- Build a disposable UEFI Fedora 43 fixture with the reported partition sizes,
  root/home subvolumes, realistic occupancy and sentinel files/profiles. Use a
  second sentinel-filled disk to test wrong-target protection. Never use VM 115
  as an installer target. Build any OS images on VM 116 only.
- Test preflight refusal for insufficient shrinkable space, changed disk identity,
  encrypted or multi-device layouts outside support, full ESP/staging storage,
  damaged filesystem, missing AC and unexpected boot configuration.
- Test invalid signatures, corrupted/partial downloads, offline restart and
  insufficient RAM. Fail before partition writes when staging is incomplete.
- Inject process termination/reboot at every durable phase. Verify Fedora can
  still boot or document the independent recovery operation needed; do not claim
  power-loss atomicity for partition resizing. Test safe resume versus refusal.
- Verify Fedora data hashes, ownership, keyring/profile usability, partition start
  and identifiers; account for intentional partition-end and installer menu
  changes. Non-target disk table/data must be unchanged.
- Boot Fedora and Zeus twice each with networking disabled. Verify visible menu,
  default selection, isolated homes, SELinux and Zeus Chrome/Codex. Confirm Temp
  cannot delete Fedora files and credentials are absent from public artifacts.
- Update Fedora's kernel and bootloader; update and roll back Zeus. Both choices
  must remain usable and new user files must survive retained Zeus binaries.
- Test removal from Fedora with populated Zeus home, cancellation and repeated
  invocation. Never remove arbitrary partitions by label alone.
- After VM acceptance, collect laptop preflight and pass physical deployment
  gates before any writes. Qualify Wi-Fi, audio, AirPods, trackpad, brightness,
  suspend and battery separately. Append timestamped boot/idle and installation
  measurements to `docs/metrics.md`; VM results are not laptop battery evidence.

## Sources checked on 2026-09-10

- [bootc existing-root manual](https://bootc.dev/bootc/man/bootc-install-to-existing-root.8.html): existing-root boot overwrite semantics.
- [bootc filesystem manual](https://bootc.dev/bootc/man/bootc-install-to-filesystem.8.html): externally prepared filesystem installation.
- [Btrfs filesystem manual](https://btrfs.readthedocs.io/en/latest/btrfs-filesystem.html): filesystem resize and partition resize are separate; shrink constraints.

## Work records

- ZOS-79: Implement read-only Fedora dual-boot preflight.
- ZOS-80: Prove isolated Fedora and Zeus dual boot in disposable VM.
- ZOS-81: Implement journaled Zeus dual-boot installation backend.
- ZOS-82: Package Fedora graphical Zeus dual-boot launcher.
- ZOS-83: Qualify dual-boot updates and Zeus removal.


## Measured implementation status

The clean VM118 trial completed both installation phases without manual disk
repair. Its 16 hashed files (712,474,577 bytes), including Fedora EFI and BLS
entries, matched after shrinking, installing, and booting Fedora again. Fedora's
saved default remained selected. Zeus booted and accepted the `shane` login
with networking disconnected. Chrome and Codex are present in the installed
payload. Both OSes subsequently passed a second offline boot.

A real Fedora kernel/GRUB update installed kernel `7.2.4-100.fc43` and GRUB
`2.12-43.fc43`; Fedora booted the new kernel and Zeus still booted through the
same menu. All nine Zeus EFI files and the partition table were unchanged by
that Fedora update. Details and limits are recorded in the
[VM118 evidence](../iterations/installer-20260910/vm118-installation.json).

The initial VM117 development run exposed an unsupported sfdisk option and a
Fedora GRUB regeneration side effect. The clean trial uses the corrected `-N`
end-only change and `--no-grubenv-update`. Its proof does not rely on the
manual repair performed in VM117.

The tests cover populated data, exact disk boundaries, interrupted-command
refusal, signatures, and isolated boot files. They do not reproduce 328 GB of
laptop occupancy, demonstrate power-loss atomicity, or qualify laptop devices
and battery life. Physical installation still requires its own preflight and
the verified-backup receipt required by the session's preservation policy.
