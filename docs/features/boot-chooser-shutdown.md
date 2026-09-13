# Shut down to the boot chooser

Status: source implemented and fixture-qualified under **ZOS-119** on 2026-09-12.
The first native dual-boot shutdown/start cycle remains a separate qualification
boundary; no deployment, reboot, or physical boot claim is made here.

## Goal

Give owners of an installer-managed Fedora + Zeus system a deliberate **Shut
Down to Boot Chooser…** action. The machine powers off normally, then firmware
enters the already-qualified Fedora-owned GRUB chooser on the next startup.
The action is one-shot: it never changes the persistent default OS, GRUB saved
entry, firmware `BootOrder`, or any operating-system data.

The normal GNOME power actions remain unchanged. A standalone Zeus image, an
older installation without the required provenance, or a system whose boot
identity has changed does not offer the new action.

## Why this uses UEFI BootNext

The Zeus dual-boot installer deliberately leaves Fedora in charge of the first
boot menu. Fedora GRUB contains an installer-owned `zeusos-dualboot` entry that
chainloads the independent Zeus ESP. Zeus's mounted `/boot` therefore belongs
to the second-stage Zeus loader, not the parent chooser.

`systemctl --boot-loader-menu` and direct edits to the mounted Zeus `grubenv`
would target that second-stage loader. They cannot safely request Fedora's
parent menu. The runtime instead selects the already-verified Fedora firmware
entry with UEFI `BootNext`. Firmware consumes `BootNext` once, while the
installer's visible Fedora menu supplies the chooser. This uses the UEFI
one-shot mechanism without rewriting Fedora's GRUB files from Zeus.

## Installer record

After the installer has written the Zeus chainload entry, regenerated Fedora's
menu with `--no-grubenv-update`, and proved the final boot state, it writes a
root-owned `/etc/zeus/boot-chooser.json` into Zeus. The bounded schema records:

- the exact Fedora `Boot####` identifier and normalized shim path;
- the Fedora ESP and `/boot` partition GUIDs;
- the fixed `zeusos-dualboot` menu-entry ID;
- the visible `menu` style and non-zero timeout; and
- an explicit assertion that Fedora's existing default was preserved.

The record contains no owner data or arbitrary executable/path input. Missing,
ambiguous, or inconsistent preflight evidence prevents the record from being
created and keeps the desktop action unavailable.

## Runtime and privilege boundary

The unprivileged `zeus-boot-chooser status --json` command performs bounded,
read-only checks and reports only a small availability result. It does not
mount filesystems, set EFI variables, or shut down the system.

After an explicit confirmation, GNOME Shell invokes exactly:

```text
pkexec --disable-internal-agent /usr/libexec/zeus-boot-chooser poweroff --json
```

The root helper accepts no disk, entry, path, command, or timeout argument. It
serializes requests, revalidates the root-owned record against live verbose
`efibootmgr` output, refuses an unrelated pending `BootNext`, and temporarily
mounts only the recorded Fedora `/boot` partition read-only with
`nosuid,nodev,noexec`. Before an EFI write it proves the generated Fedora GRUB
configuration still has a visible, non-zero menu and the fixed Zeus chainload
entry, then unmounts it.

The helper sets only the validated Fedora entry as `BootNext`, reads EFI state
back, and proves that `BootOrder` and every boot entry are unchanged before
requesting poweroff. Any identity, mount, menu, write, or readback uncertainty
fails before shutdown. If the poweroff request itself fails, the helper clears
only the one-shot value it just created and verifies that rollback.

Distinct operating-system loaders may share the Fedora ESP, as is common with
Windows and Fedora. Availability requires the recorded Fedora shim identity to
be unique; it does not incorrectly require Fedora to own the ESP exclusively.
An inactive Fedora firmware entry or a Fedora `/boot` partition already mounted
elsewhere is refused before any EFI write.

## User experience

The Zeus menu checks availability only while the menu is open; there is no
background poller. On a qualified multi-OS installation it offers **Shut Down
to Boot Chooser…**. Confirmation explains that open work must be saved, the
computer will power off, and the normal OS chooser will appear on the next
start. Cancel, authentication denial, and helper failure leave the system
running and display an actionable error.

## Qualification

Source and fixture coverage proves:

- exact marker generation from unambiguous installer evidence, after Fedora
  GRUB regeneration and on safe finalization retry;
- root ownership, mode, schema, identifier, GUID, shim-path, and symlink checks;
- exact EFI entry matching, stable `BootOrder`/entry records, one-shot readback,
  idempotent same-entry behavior, and conflicting-`BootNext` refusal;
- read-only Fedora `/boot` mounting, visible-menu and managed-entry checks, and
  guaranteed unmount before EFI mutation;
- no poweroff after missing provenance, identity drift, malformed GRUB,
  unavailable efivars/tools, readback mismatch, or authentication denial;
- exact fixed desktop argv, explicit confirmation, repeat-click suppression,
  bounded output, and no idle polling; and
- ordinary GNOME shutdown, updater state, owner files, Fedora/Zeus defaults,
  GRUB environment, and persistent firmware order remain untouched.

The repository's full 499-test suite and the focused Python, JavaScript syntax,
Polkit XML, and diff checks pass. These checks exercise the installer/runtime
failure boundaries without writing host EFI variables or powering off a guest.

Native qualification must use a disposable installer-managed dual-boot VM
before the laptop. Capture pre/post `efibootmgr -v`, Fedora GRUB hashes,
partition identities, owner sentinels, the poweroff boundary, and a console
screenshot of the next-start chooser. Boot both Fedora and Zeus afterward and
confirm the following ordinary start uses the unchanged persistent default.

VM 115 is a populated Zeus-only review guest and must not be used to claim the
multi-OS boot result. Physical qualification requires a fresh verified backup
and the existing laptop deployment gates before any boot-variable write.
