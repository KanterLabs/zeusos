# Scoped EFI servicing contract

This contract records the safe ongoing EFI path for the Fedora dual boot
layout. It is pinned to bootc **1.16.10** and bootupd **0.2.35**, the versions
captured in `out/installer-contract/`. It applies when the disk contains the
existing Fedora ESP before the separately allocated Zeus ESP.

## Finding

bootupd 0.2.35 has no option that selects an ESP by UUID, partition path, or
mountpoint. `bootupctl backend install` accepts `--device`, but that value is
converted to a block device and bootupd then calls
`find_partition_of_esp()`. Passing the whole disk therefore chooses its first
ESP child; passing an ESP partition itself has no child to discover and is
rejected. `--filesystem` walks the physical disks backing the path and has the
same first-ESP behavior.

The regular `bootupctl update` path is still usable for this layout. In
`efi.rs`, `run_update()` obtains one colocated ESP per backing disk and then
calls `ensure_mounted_esp()`. `ensure_mounted_esp()` first reuses an already
mounted VFAT ESP, before attempting to mount the discovered device. A
UUID-verified Zeus mount at `/boot/efi` therefore wins over the earlier Fedora
ESP while bootupd retains its tested file-tree diff, update journal, static
UUID configuration, fsync/freeze cycle, and EFI vendor handling.

The upstream `systemd/bootloader-update.service` also sets
`MountFlags=slave`. That contains bootupd's normal cleanup unmount inside the
service mount namespace. The wrapper must retain this property; invoking
`backend install` directly from a process sharing the host mount namespace is
not an ongoing-update contract because bootupd unmounts the mount it reused.

The initial-install probe recorded in this iteration creates two ESPs on one
disposable disk, mounts the second as Zeus, runs
`bootupctl backend install --component EFI --write-uuid /target`, and verifies
that a sentinel on the first Fedora ESP is unchanged. The bounded VM116
ongoing-update probe completed the same layout with a fresh 2 GiB loop image
inside `localhost/zeusos:0.1.0-preview.2`. It changed the container overlay's
EFI payload and metadata, ran the wrapper with the Zeus FAT UUID mounted, and
observed bootupd update only the Zeus payload while the Fedora sentinel and
both static UUID files remained unchanged. The payload was a synthetic
one-byte mutation of the preview image's existing shim, and the metadata used
synthetic future package versions (`shim-16.1-6` and `grub2-1:2.12-65.fc44`)
with a future timestamp to force the update comparison. These fixtures are
not released EFI artifacts. The wrapper reported bootupd 0.2.35 and the
probe's cleanup trap detached the loop device. Full command output and hashes
are recorded in `efi-update-probe.txt` and `efi-update-probe.json`.

This probe qualifies UUID-scoped file application and preservation of the
other ESP's files. It does not test firmware boot, an EFI boot manager, or
operating-system bootability; those remain unqualified.

A bounded VM117 end-to-end probe also exercised the installed updater path and
rollback. Starting from the pinned preview deployment, it used the updater's
direct `bootc switch --transport oci-archive --retain` command with a
synthetic marker-only candidate, rebooted through Fedora's existing
`zeusos-dualboot` one-shot menu entry, and then queued and booted a rollback.
Both deployments preserved the user fixture and all `/etc` scope files; the
Zeus ESP remained mounted by its configured FAT UUID; the Fedora first ESP,
Fedora boot/BLS tree, and bootloader service checks stayed intact. This
qualifies deployment and rollback integration for the scoped service in that
VM. It does not qualify a released OS or EFI payload update, firmware
behavior, or released-shim bootability. The exact backup paths, hashes,
status records, scrubbed logs, and cleanup proof are in
`vm117-update-rollback.json` and its copied evidence directory.

The preview image has `/etc` mode `0775`; the probe changed that mode to
`0755` in its disposable container overlay before creating the root-owned
scope file. The installed target must provide the same secure parent mode for
the runtime config check to pass.

## Static configuration

During installation, after the Zeus ESP UUID is known, write this file into
the installed target root at `/target/etc/zeus/efi-update.json`:

```json
{
  "esp_uuid": "1234-ABCD",
  "mountpoint": "/boot/efi",
  "schema_version": 1
}
```

`zeus_installer.efi_update.write_scope_config()` writes it atomically. The
runtime loader accepts only this schema, canonicalizes the FAT filesystem
volume ID to uppercase `XXXX-XXXX`, rejects GPT-style 36-character UUIDs,
requires a regular root-owned file, rejects group/world-writable config or
parent directories, and refuses every mountpoint other than `/boot/efi`. The
value is the partition's FAT filesystem UUID, not the GPT disk or partition
UUID.

## Runtime behavior

The bootloader service must replace its `ExecStart` with the wrapper and keep
the upstream isolation properties:

```ini
[Unit]
RequiresMountsFor=/boot/efi
After=boot-efi.mount

[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m zeus_installer.efi_update --config /etc/zeus/efi-update.json
PrivateNetwork=yes
ProtectHome=yes
KillMode=mixed
MountFlags=slave
```

The wrapper performs these checks and actions in order:

1. Load and validate the root-owned static config.
2. Require exactly one backing root device, matching bootupd's inverse
   `lsblk` topology. Multi-device or ambiguous root layouts fail before any
   ESP mount is inspected; bootupd otherwise unmounts after each root and can
   reach another ESP on a later iteration.
3. Require `/usr/bin/bootupctl --version` to report exactly `bootupctl 0.2.35`.
   Later versions fail closed until their ESP-selection semantics are audited.
4. Query the exact `/boot/efi` mount with `findmnt --mountpoint`, requiring a
   `vfat` filesystem and the configured UUID.
5. If no exact mount exists, mount only `UUID=<configured UUID>` at
   `/boot/efi`, then verify the resulting UUID. If another ESP is already
   mounted there, fail without unmounting or overwriting it.
6. Invoke exactly `/usr/bin/bootupctl update`. No `--device`, `--filesystem`,
   or user-provided command is accepted.
7. Recheck the mount identity. A mount that drifted to another UUID is an
   error and is never unmounted. A temporary mount made by the wrapper is
   removed after the update; a preexisting Zeus mount is left in place. If a
   preexisting Zeus mount unexpectedly disappeared, the wrapper remounts and
   verifies it before reporting success.

Any failed identity check, command failure, malformed probe, unsafe config,
or cleanup failure returns a nonzero result. There is no “skip EFI updates”
or permanently disabled-service fallback.

## Deliberate limits

This contract depends on bootupd's documented reuse of a premounted ESP and
the service's `MountFlags=slave`; it does not reimplement bootupd's EFI file
copy algorithm. A future bootupd release with an explicit ESP selector may
replace the wrapper guard, but until then `--device` and `--filesystem` must
not be treated as selectors for a same-disk dual-ESP layout.

The wrapper does not request firmware-variable changes. Initial install uses
`--write-uuid` against the already mounted Zeus ESP; ongoing servicing uses
the regular update path so static UUID configuration remains under bootupd's
state and file-tree ownership.
