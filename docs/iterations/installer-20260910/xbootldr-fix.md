# Fedora extended-boot preflight correction

Recorded at 2026-09-10 15:07 UTC. Product version remains `0.1.0-preview.2`;
the downloadable RPM uses a new Git build identifier.

The reported Fedora 43 layout has a 600 MiB EFI partition, 1 GiB ext4 `/boot`,
and a final Btrfs root/home partition. Its `/boot` has GPT type
`BC13C2FF-59E6-4262-A352-B275FD6F7172`, the standard extended boot loader
partition type. The planner previously required generic Linux data for both
`/boot` and root and incorrectly returned `partition_table_invalid`.
The type definition is recorded in the
[UAPI Discoverable Partitions Specification](https://uapi-group.org/specifications/specs/discoverable_partitions_specification/).

The planner now accepts either Linux data or XBOOTLDR for partition 2 only.
The EFI and root type checks, filesystem and mount checks, partition identities,
geometry checks, AC requirement and write boundaries remain enforced. Source
partition types are not rewritten. The disk card matches the planner's source
path against inventory devices, so an additional zram device does not hide
the NVMe model, path and capacity. Conflicting or missing target identity does
not cause a different disk to be displayed.

Validation:

- The regression first failed with the original partition-type rejection,
  then passed for uppercase and lowercase XBOOTLDR identifiers.
- Real `sfdisk` ran against temporary sparse **regular files**, once for each
  accepted boot type. After the end-only root shrink, the complete GPT matched
  the original except for the root size. Boot type, names, attributes and UUIDs
  were retained, and independently seeded data in all three partitions matched.
- Using the actual diagnostic in memory, the fixed planner reports only
  `ac_required`. In a copied fixture with AC simulated as connected, it reports
  a supported 128 GiB allocation with no blockers. These replays execute no
  system commands and leave the input unchanged. The original report contains
  personal device information and is not included in the repository.
- Regression coverage also rejects XBOOTLDR in the EFI/root slots, unrelated
  `/boot` types, and a changed boot type during post-reboot verification.
- All 131 installer tests passed in 6.551 seconds after the final display fix.
  The target summary accepts both 512-byte and 4096-byte sector plans and
  shows ESP, boot and root sizes without inventing a separate home partition.
  A native GTK rendering check used a synthetic NVMe-plus-zram inventory;
  replay of the reported layout shows `128 / 1 / 2 / 125 GiB` as expected.

This correction does not establish physical laptop installation, occupied-disk
shrink timing, or power-loss qualification. The earlier disposable VM
[qualification record](README.md) remains the installation baseline. The RPM
publication includes build-specific checksums and a signed verification receipt
covering its Fedora upgrade and public-download checks.
