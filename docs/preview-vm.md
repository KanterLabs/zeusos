# Zeus OS review VM

Provisioned September 8, 2026 on Shane's existing homelab Proxmox.

| Setting | Value |
| --- | --- |
| Node | `pve-homelab` — `10.0.0.20` |
| VM | **115 — zeusos-preview** |
| CPU | 4 vCPUs, one socket, host CPU |
| RAM | 8 GiB fixed; ballooning disabled |
| System disk | 64 GiB thin disk on `local-lvm`, VirtIO SCSI |
| Firmware | UEFI/OVMF, q35; enrolled firmware keys |
| Display | VirtIO graphics, 64 MiB display memory |
| Network | VirtIO NIC on private LAN bridge `vmbr0`; guest DHCP once installed |
| Protection | VM deletion protection enabled; automatic host-start disabled |
| Current state | Powered off; no OS installed and no installation image mounted |

## Access

Open [Proxmox](https://10.0.0.20:8006), sign in with your existing account, select **pve-homelab → 115 (zeusos-preview) → Console**. Access requires the existing private-network route. No new public service or port was exposed. There is no guest IP or guest login yet.

## What has been verified

- Proxmox accepted the configuration and reported 4 CPUs, 8,589,934,592 bytes RAM and a 68,719,476,736-byte system disk.
- QEMU started successfully with the configured UEFI/q35 machine; this was a firmware-only smoke check. The empty guest was then stopped.
- All 27 pre-existing QEMU/LXC guest configuration hashes remained unchanged after provisioning.
- This does **not** establish that Zeus boots, its desktop renders correctly, or Secure Boot accepts the future image. Those checks require an actual build. Guest-agent support is enabled in Proxmox, but no guest agent is installed yet.

Provisioning evidence is retained on the Proxmox host under `/var/lib/zeusos/provisioning/`. The first deliverable for this VM is a bootable Zeus preview with the desktop baseline described in [preview strategy](preview-strategy.md).

## Keep the review environment persistent

Use this same VM for successive owner previews, preserving the user account, files and settings. Record the build identifier, image digest, known limitations and validation results for every delivery. Do not reset or recreate it as an upgrade procedure.

Before updating a populated preview, verify a pre-upgrade backup and confirm that the retained rollback version can still read its state. Use snapshots only as an additional convenience, not an independent backup. VM 115 is eligible for the existing all-guests nightly SATA backup job, but no successful backup or restore has yet been verified for it. Destructive restore needs explicit authorization and an exact backup target.

Run clean-install, corruption, destructive recovery and other destructive tests on separate disposable guests. VM review covers layout and interaction; physical laptop tests still own battery life, touchpad feel, suspend, Wi-Fi and hardware graphics behavior.
