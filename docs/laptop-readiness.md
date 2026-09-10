# First-laptop install readiness

This is the finite handoff checklist for installing **0.1.0-preview.2** on one physical Zeus OS laptop. It is a preview qualification record, not a stable release plan. The Zeus candidate baseline is x86_64 Fedora 44 bootc with GNOME 50.4 ([inputs](../image/inputs.json), [GNOME package lock](releases/preview-2/packages.lock)).
The current owner inventory is a Nimo Direct Inc. N154G: x86_64, about 31 GiB RAM, 931.5 GiB NVMe, Fedora 43, UEFI with Secure Boot disabled. It reports a 600 MiB vfat ESP, 1 GiB ext4 `/boot` and 929.9 GiB Btrfs for `/` and `/home` (about 328 GiB used and 601 GiB free); no crypt mapper was shown. Reported devices are AlderLake-UP3 GT1 Intel UHD graphics `8086:46b3`, AlderLake-P CNVi Wi-Fi `8086:51f0` and HD audio `8086:51c8`; the Bluetooth controller/driver still needs confirmation. Treat it as conventional Fedora until its edition and bootc/rpm-ostree status are confirmed; Btrfs subvolumes/fstab, encryption and recovery ownership remain unknown. Preserve Fedora's existing account UID, home and profile. Create an independent Zeus home; do not assume VM 115's `shane` account.
VM 115 is useful evidence but is not a laptop: it has 4 vCPUs, 8 GiB RAM, a 64 GiB VirtIO disk, UEFI/OVMF and virtual display/network devices ([VM record](preview-vm.md#current-environment)). Its Secure Boot and enforcing SELinux results must not be presented as physical-laptop qualification.

## Current status

The implementation and testing plans are proposals and explicitly leave their items unchecked ([implementation](implementation-plan.md), [testing](testing-plan.md)).
The dated receipts provide the current evidence:

VM115 currently runs `git-f080c2d9bc53`, still `0.1.0-preview.2`, with Chrome
153.0.8010.36 and standalone Codex CLI 0.154.0. Earlier feature receipts below
remain their original qualification records.

| Area | Status | Evidence and limit |
| --- | --- | --- |
| Image and desktop | **VM verified** | Build `git-31f0851a9d07`, exact OCI digest, 1,011 packages, native GDM/Wayland, lock/login, scaling, accessibility and safe fallback are recorded ([receipt](iterations/git-31f0851a9d07/build-receipt.json), [UI checks](iterations/git-31f0851a9d07/installed-ui-verification.json)). |
| Signed update | **VM verified; physical rollback pending** | Signed Updates install, explicit restart and retained predecessor passed; the city iteration did not repeat a full rollback cycle ([updater](features/os-updater.md), [receipt](iterations/git-31f0851a9d07/build-receipt.json)). |
| Install artifacts | **Partial** | OCI and QCOW2 build scripts exist ([build](../scripts/build.sh), [QCOW2](../scripts/build-disk.sh)), but the latest iteration made no installer/QCOW2; no checked-in interactive ISO, target-selection result or same-disk migration rehearsal exists ([iteration](iterations/git-31f0851a9d07/README.md), [disk definition](../image/disk.toml)). |
| Encryption/disk safety | **Untested** | No encrypted-install, recovery-key, explicit-target, cancellation, or two-disk sentinel result exists ([Q07](testing-plan.md#harness-and-fixture-construction), [Q15](testing-plan.md#acceptance-execution)). |
| Physical hardware | **Untested** | VM 115 has no physical Wi-Fi/Bluetooth controller, backlight, battery, laptop GPU, trackpad or suspend path ([Settings limits](features/laptop-settings.md#qualification), [metrics](metrics.md#history)). |
| Bluetooth/AirPods | **Software only** | BlueZ/PipeWire/WirePlumber and codecs pass software checks, but no pair, audio, microphone, battery, ANC or reconnect result exists ([feature contract](features/airpods.md), [installed check](features/airpods/installed-image-check.json)). The current pair's exact model and firmware are unknown. |
| Backup/recovery | **Partial / missing** | VM 115 has verified VMA archive integrity and preserved-file hashes, but `restore_performed: false`; profile restore, independent enrollment and replacement automation are later work ([backup](iterations/git-31f0851a9d07/backup-verification.json), [M2 plan](implementation-plan.md#m2-make-a-replacement-laptop-recoverable)). |
| Browser | **VM verified** | Build `git-9c2cfbdcb703` deploys official Chrome `153.0.8010.36` in a 1,023-package image; native defaults, HTTPS, sandbox, recommended Temp download, active-download/Keep, process exit and dated boot/idle samples passed; physical qualification remains pending ([Chrome receipt](iterations/git-9c2cfbdcb703/README.md), [package diff](iterations/git-9c2cfbdcb703/package-diff.json), [tests](iterations/git-9c2cfbdcb703/browser-tests.txt), [Chrome contract](features/google-chrome.md)). |
| Codex CLI | **VM verified; account use pending** | Standalone 0.154.0 is included without Node/npm or a boot service. Native Terminal sign-in choices, offline help, actual sandbox restrictions and clean exit passed. No model request or account sign-in was performed; the remote T3/Codex workflow remains separate ([Codex receipt](iterations/git-f080c2d9bc53/README.md)). |

## Installation decision (updated 2026-09-10)

Shane now wants **dual boot: retain normal Fedora and add Zeus**, launched from
Fedora without USB or firmware setup where supported. This supersedes the earlier
full-replacement preference. The [dual-boot installer plan](features/fedora-dual-boot-installer.md)
defines isolated storage, a staged installer, implementation work and preservation
tests. The [implemented installer](../installer/README.md) has now passed a
clean VM118 install, repeated offline boots, and Fedora update checks. VM117
also passed a synthetic Zeus update and rollback with user files and Fedora
boot files preserved. The [dated qualification record](iterations/installer-20260910/README.md)
separates these results from the still-untested laptop. No physical installation
has been performed.

ZOS-76 describes the earlier replacement investigation and is superseded for this
laptop by the dual-boot work tracked from ZOS-78. Do not execute its existing-root
route against the laptop: bootc's `alongside` option overwrites `/boot`.
The backup/recovery and physical qualification gates below remain applicable;
references to migration now mean preserving Fedora while adding a separate Zeus
installation, not replacing the current root or sharing mutable home profiles.

## Ordered first-laptop gate

Keep one evidence directory keyed by the exact build ID and laptop model.
`unknown`, `pending` and `failed` are not passes.

1. **Confirm the target.** Start with the N154G inventory above; capture exact model/SKU, Fedora edition/kernel, CPU/GPU/display, Wi-Fi/Bluetooth adapters and drivers, firmware, trackpad, battery capacity, disk/layout, UEFI/Secure Boot state, encryption/subvolumes/fstab and recovery ownership. Pass when every field is known or explicitly unavailable and the backup destination is named ([D01](implementation-plan.md#decisions-to-resolve-before-dependent-work)).
2. **Verify candidate and route.** Bind version, source/build ID, OCI/ISO/QCOW2 digests, package inventory and browser choice to one record; independently verify detached signature and SHA-256. For same-disk download/reboot, verify the staged candidate and rehearsal prerequisites; for media, verify x86_64 UEFI boot and an embedded payload with networking blocked. Pass only when the selected route has matching signatures, digests and independent recovery ([build runbook](preview-vm.md#building-a-candidate), [iteration policy](iteration-builds.md)).
3. **Rehearse the selected install.** For same-disk download/reboot, use a clone or spare matching the N154G Btrfs layout and verify isolated Zeus boot/root/home, preserved Fedora files and boot entries, plus both-system update and recovery behavior. For media, use a named UEFI target and sentinel-filled second disk; show model/capacity and require explicit target confirmation. Pass when cancellation is safe, the target boots twice offline, encryption/recovery-key behavior matches the decision, backup/recovery remains usable and a media non-target hash/table is unchanged where applicable; never use VM 115 ([safety boundary](preview-vm.md#safety-boundaries)).
4. **Run physical desktop smoke.** Boot native GDM/Wayland and retain `zeus version`, `zeus doctor --json`, `getenforce`, `mokutil --sb-state`, `systemctl is-active gdm NetworkManager` and `systemctl --failed`. Exercise display, keyboard, trackpad, audio, Wi-Fi, Bluetooth, Files, Ptyxis, Settings, Welcome, search, Super+Space, Super+Return, Ctrl+C, lock/unlock and safe desktop restore. Pass when candidate identity matches, required units are healthy, each present device works and unavailable hardware has a support decision ([desktop review](preview-strategy.md#lightweight-responsiveness-gate)).
5. **Qualify hardware and battery.** Run three Wi-Fi reconnect cycles, three Bluetooth pair/reconnect cycles, internal display/GPU checks, trackpad click/tap/scroll/gesture checks and three locked suspend/resume cycles; after each resume verify display, input, radios and audio. Run three clean-Fedora and three Zeus discharging measurements with matched conditions using [`measure-battery.py`](../scripts/measure-battery.py). Pass hardware with no crash, input loss or required reboot; pass power with no material regression and median energy ≤105% of clean Fedora. Record the eight-hour `<1% capacity/hour` suspend diagnostic separately; ≥10% runtime gain is aspirational ([battery protocol](performance/battery-test-protocol.md), [power plan](implementation-plan.md#m3-add-advanced-polish-and-measured-power-policy)). If AirPods are tested, record the current pair's exact model, case model, firmware and adapter/driver first; do not reuse the superseded model note ([AirPods contract](features/airpods.md)).
6. **Verify current-laptop recovery and rollback.** Before changing the laptop, independently verify a user-data backup and a recovery path that survives root replacement (media, alternate boot or another separately verified route). Install one newer signed candidate, confirm staged identity, use only the explicit restart action, compare user files/preferences, then boot the retained predecessor and forward candidate. Pass when recovery-key unlock, post-backup sentinel data and user files remain intact, and OS rollback stays separate from mutable profile/database restore ([updater rules](features/os-updater.md), [backup](iterations/git-31f0851a9d07/backup-verification.json)).
7. **Hand off or defer.** Handoff requires gates 1–6 to pass for the same candidate and physical model with dated evidence. Otherwise keep using VM 115 or disposable media and label the result `preview test only`. Independent enrollment, profile restore, remote-work continuity, isolated backup restore and the ≤30-minute wiped-client journey remain later M2/M4 work, not gates for this first personal-laptop test ([M2 acceptance](testing-plan.md#acceptance-execution)).

## Planned implementation

The following cards are unclaimed Backlog work. Existing installer, power and recovery cards remain the implementation owners; these additions coordinate the first physical model and browser security servicing.

| Card | Deliverable |
| --- | --- |
| ZOS-74 — Qualify the first physical Zeus laptop | Dated model-specific hardware, battery and install/recovery evidence, extending ZOS-8, ZOS-13, ZOS-23, ZOS-36/37/45/50/51 and AirPods ZOS-70. |
| ZOS-75 — Keep Chrome current through signed Zeus updates | Detect newer official Stable packages, qualify and publish signed images on a documented cadence, record servicing timestamps and surface stale or failed checks; extends ZOS-24 and ZOS-28. |
| ZOS-76 — Earlier replacement investigation (superseded for this laptop) | Read-only N154G layout preflight; disposable Fedora 43 clone with 600 MiB ESP, 1 GiB `/boot` and Btrfs root/home; independent signed archive verification; preserve files, UID, keyring and network; retain old root unless explicitly cleaned; test cancellation, non-target safety, offline boot/update/recovery before exact-target replacement. |

First hardware checks should use a clone or spare matching the current Btrfs layout, or a verified independent recovery route. Before changing partitions or boot entries, verify backup/recovery, the target disk/layout and the dual-boot rehearsal. USB is optional when that independent route is verified. The full hardware, battery and update/rollback evidence then determines readiness for daily use; it does not prevent an earlier safe test installation on disposable storage.
