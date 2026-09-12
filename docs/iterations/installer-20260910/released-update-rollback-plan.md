# ZOS-83 released update, rollback, and removal qualification plan

Status: source and review-contract work complete; live released-update and
rollback qualification is pending. Destructive data removal remains outside
this run because it requires separate, explicit authorization. This document
separates accepted evidence from the remaining state-changing qualification
so a reviewer cannot mistake the synthetic rehearsal for a released claim.

## Known evidence and exact disposable target

VM118 (`zeusos-dualboot-clean-test`, DMI UUID
`29f40b00-bc18-4ff3-82da-1d9006d7db4c`) is the disposable two-ESP target. Its
current pre-change backup was captured directly from VM118 and passed a full
`zstd -t` integrity check on 2026-09-12:

```text
/mnt/pve/sata-ssd/dump/vzdump-qemu-118-2026_09_12-02_55_35.vma.zst
compressed_bytes 26310457372
uncompressed_bytes 31834179072
```

The older VM117 VMA and its checksum remain in
[`vm118-preinstall-proof.json`](vm118-preinstall-proof.json) as provenance for
the original restored baseline; they are not the rollback target for this run.

The restored VM118 baseline and its 16-file manifest are recorded in
[`vm118-baseline.json`](vm118-baseline.json). The clean install and real
Fedora kernel/GRUB update are recorded in
[`vm118-installation.json`](vm118-installation.json) and
[`vm118-fedora-update-efi-proof.json`](vm118-fedora-update-efi-proof.json).
The VM117 update/rollback receipt is useful lifecycle evidence, but its
candidate is explicitly a synthetic marker-only fixture:
[`vm117-update-rollback.json`](vm117-update-rollback.json).

VM118 currently boots installed release `git-ad091ecc2978`. At the start of
this qualification the promoted signed feed identifies
`git-89de745be436` (version `0.1.0-preview.2`, archive SHA-256
`37b81a53e32a7f8a87db509c2493e7c5950441cbee9a80fef6945c62ac1396a3`).
The feed will advance again when the integrated image is published. Record the
exact final archive checksum, signed metadata verification output, and booted
manifest in a new receipt before calling a release run accepted.

## Released signed Zeus update and rollback

The owner-authorized operator should run this sequence only after checking
that VM118 is the target and the backup above is still available. These are
state-changing commands and are a qualification plan, not commands run by
this source-review task.

1. Capture a read-only baseline from Fedora and Zeus: exact GPT table and
   partition GUIDs, both ESP tree hashes, both `/boot` trees, Fedora BLS and
   boot entries, Fedora and Zeus home sentinels, and Temp policy/status. Prove
   that the first ESP is Fedora's and the second ESP is Zeus's.
2. Verify the released signed feed and archive through the existing updater
   path. Retain the prior Zeus deployment and record the staged build ID and
   manifest before choosing **Restart to Apply**. The UI/service must not
   reboot without that explicit owner action.
3. Boot Fedora through its existing default path, then select Zeus from the
   existing menu. After login, record `bootc status --json`, the release
   manifest, the independent Zeus home sentinel, and Temp policy. Verify the
   previous deployment is retained for rollback.
4. Queue the retained deployment with the pinned `bootc rollback` recovery
   path and use the explicit restart action. Boot Zeus again and record the
   same status, home, and Temp checks.
5. Compare both transitions with the baseline. Pass only when the release
   signature/digest, both boot choices, independent Fedora/Zeus homes, Temp
   isolation, Fedora ESP/`/boot`/BLS hashes, Zeus ESP identity, and retained
   rollback deployment all match the contract. A synthetic candidate does not
   satisfy this row.

Required receipt fields:

```text
released_archive_sha256
released_manifest_digest
signature_verified
staged_build_id
post_update_build_id
rollback_build_id
fedora_boot_choice_after_update
zeus_boot_choice_after_update
fedora_boot_choice_after_rollback
zeus_boot_choice_after_rollback
fedora_esp_unchanged_update
fedora_esp_unchanged_rollback
fedora_boot_bls_unchanged_update
fedora_boot_bls_unchanged_rollback
zeus_esp_identity_unchanged_update
zeus_esp_identity_unchanged_rollback
fedora_home_unchanged_update
zeus_home_unchanged_update
fedora_home_unchanged_rollback
zeus_home_unchanged_rollback
temp_fedora_isolated_update
temp_zeus_isolated_update
temp_fedora_isolated_rollback
temp_zeus_isolated_rollback
```

The source contract deliberately leaves this receipt open: the existing
`vm117-update-rollback.json` sets `released_efi_artifact` false and calls its
candidate synthetic. No released signed Zeus update/rollback has been claimed
here.

## Safe removal qualification

The safe review model defaults to menu-only removal. It must be exercised from
Fedora with a fresh read-only inventory and the real root-owned journal:

- Review the journal-bound disk, Fedora partitions 1--3, and Zeus partitions
  4--6. The review must show Fedora as the default boot owner and must not
  substitute a disk or partition by label.
- Choose **Keep Zeus data / remove menu entry only** to emit exactly the owned
  GRUB script removal and fixed `grub2-mkconfig --no-grubenv-update` operation.
  Cancellation emits no operation. Verify the Fedora ESP, `/boot`, root,
  grubenv, unrelated scripts, GPT identities, Fedora home, Zeus home, and Temp
  state remain unchanged. This is the accepted live rehearsal in
  [`vm118-menu-removal.json`](vm118-menu-removal.json).
- A full removal review must explicitly say **Delete Zeus data with its three
  journal-owned partitions**, display the exact plan ID, require that ID as
  confirmation, and require the documented backup and VM qualification gate.
  It must prove all three Zeus partitions are unmounted immediately before the
  fixed deletion operation. The executor may delete only the recorded GUIDs;
  it must retain Fedora partitions 1--3 and never issue a grow, resize, or
  reallocation operation.
- After any separately authorized destructive run, re-read the GPT table from
  Fedora, verify Fedora's boot entry and default path still work, and record
  that no free space was automatically assigned to Fedora. Do not run that
  destructive step as part of documentation or source tests.

The source planner now encodes these requirements as `data_policy`,
`zeus_resources`, `fedora_preservation`, and `space.automatic_reclaim: false`.
`launcher.py` exposes the same facts for a GTK-independent owner review; the
privileged integration must still use the fixed root-owned backend seam.
