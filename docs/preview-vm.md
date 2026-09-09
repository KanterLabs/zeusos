# Zeus OS preview VM runbook

This is the operating record for the populated Zeus desktop preview. It is a
small, manual review environment for the first desktop slice described in the
[preview strategy](preview-strategy.md). It is not a disposable install test,
a production laptop, or a completed recovery service.

## Current environment

The review guest is already installed and owner state must be preserved between
preview candidates.

| Item | Current value |
| --- | --- |
| Proxmox node | `pve` / `pve-homelab` — `10.0.0.20` |
| Proxmox UI | [https://10.0.0.20:8006](https://10.0.0.20:8006) |
| Review guest | VM **115**, `zeusos-preview` |
| Guest address | `10.0.0.95` on the private LAN; DHCP may change it |
| Guest account | `shane`; native GDM login; requested default password `root` |
| Compute | 4 vCPUs, 8 GiB fixed RAM, no ballooning |
| Storage | 64 GiB VirtIO system disk |
| Firmware | UEFI/OVMF with enrolled Secure Boot keys |
| Display/network | VirtIO graphics and VirtIO NIC on the private bridge |
| Security observed | Secure Boot enabled and SELinux enforcing |
| Build guest | VM **116**, builder address `10.0.0.56` |

The Proxmox host and the guest are private-network resources. The owner-triggered
updater reads a public, signed preview feed and immutable release assets, but
that publication path does not expose SSH, Proxmox, VM, or guest access. Use the
Proxmox console for the native GDM path and `ssh shane@10.0.0.95` for non-GUI
checks when the address is current.

## Safety boundaries

- VM 115 is populated. Never rerun the first-install provisioning command,
  attach a new seed, wipe the disk, or recreate this guest as an update step.
  Use a named disposable guest for clean installs, partitioning, corruption,
  full-disk, interrupted-update, and restore tests.
- The generic image contains no owner password, private key, machine identity,
  or personal profile. The one-time seed uses the owner-requested default password `root` and
  sends only its hash to the guest. The local login record stays in the file
  `/home/shane/.local/state/zeusos/preview-1-credentials.json`, mode `0600`;
  do not copy or print it in the repository, terminal transcript, screenshots,
  or chat.
- Keep the review guest's owner files and settings across candidates. A
  Proxmox snapshot is a convenience point and is not a backup. Verify the
  backup gate below before any update or state migration.
- Native Updates is qualified on VM 115 for signed build `git-fd2125f63159`
  after a native Updates installation and explicit reboot. The post-reboot
  status reported `The selected update is installed.` There is no unattended
  update or automatic reboot.
- The first updater-capable image was a bootstrap transition for this populated
  guest. Normal owner-triggered updates now use Welcome → **Open Updates** or
  the `zeus update` commands. Keep the manual bootc and rollback procedure as
  the recovery fallback.
- Bootc status on an older bootstrap image requires root; an unprivileged doctor
  report records that limitation without elevating itself.

## One-time owner provisioning

The initial seed was prepared with the repository's
[`provision-preview.py`](../scripts/provision-preview.py) helper while VM 115
was stopped. The helper requires a single Ed25519 public key, a new local
credentials output path, and the explicit `--fresh-install` flag. It refuses a
running VM and creates the remote seed at the fixed private Proxmox snippets
path.

The following is the historical command used before VM 115 became populated;
it is retained to make the seed contract reviewable and must not be run again
against VM 115:

```sh
python3 scripts/provision-preview.py \
  --host pve \
  --vmid 115 \
  --public-key "$HOME/.ssh/id_ed25519.pub" \
  --credentials /home/shane/.local/state/zeusos/preview-1-credentials.json \
  --fresh-install
```

The command's `--credentials` file is created with exclusive creation and mode
`0600`; an existing file is a stop condition. On that initial boot, the guest
was started from Proxmox, NoCloud completed, and `shane` authenticated through
native GDM. The seed CD was subsequently ejected and cloud-init disabled after
first provisioning; the private seed remains on the host for its audit record. If the guest address changes, find the new DHCP lease or use the
Proxmox console; do not reseed the populated VM. A future disposable install
needs its own reviewed guest and storage procedure.

## Read-only guest checks

From the guest console or an SSH session, capture the following after a normal
boot. These commands do not update the image or reboot the guest:

```sh
zeus version
zeus doctor --json
getenforce
mokutil --sb-state
systemctl is-active gdm NetworkManager
```

The doctor report intentionally exposes session, terminal, and bootc readiness
without dumping environment variables, credentials, pairing links, or prompt
contents. Record its output as redacted evidence if it is retained.

The current local desktop actions are also reversible and narrow:

```sh
zeus desktop safe       # disable Zeus shell/dock and managed GTK imports
zeus desktop restore    # restore the Zeus presentation
zeus update status --json  # local updater state; read-only and no reboot
zeus update check --json   # fetch signed metadata only; no image download
/usr/libexec/zeus-welcome
```

The safe action checks for a usable `ptyxis`/GNOME terminal before changing the
dock and leaves credentials, personal files, wallpaper, and user preferences
in place. The stock GNOME shell and native authentication remain available.

## Normal Updates path

Use this path for normal owner-triggered updates. Open Welcome → **Open Updates**
(or launch the **Updates** application from GNOME search), or use the equivalent
CLI:

```sh
zeus update status --json   # read local state
zeus update check --json    # fetch and verify the small signed feed metadata
zeus update install --json  # request owner authentication and start the job
```

`install` invokes `pkexec` for Polkit authentication, then starts the
background root systemd service. The verified image is downloaded and staged
for the next restart; closing the window does not cancel that service. It never
reboots on its own. When the Updates window reports **Ready**, save work and use its
**Restart to Apply** action, which goes through the native GNOME confirmation.
If Temp is set to **On boot**, its eligible-file cleanup also runs for that
update reboot.

The implementation and command contract are recorded in the
[updater feature notes](features/os-updater.md). Use the manual procedure below
for bootstrap, recovery, or any image where the Updates app is not yet
qualified.

## Building a candidate

Build only on VM 116 (`10.0.0.56`), never on the Proxmox host, VM 115, or the
development VM. On the builder checkout, the scripts require rootful Podman
and must be run as root; they take no positional arguments:

```sh
cd /path/to/zeusos
sudo ./scripts/build.sh
sudo ./scripts/build-disk.sh
```

The first script builds the local bootc image from the digest-pinned inputs in
[`image/inputs.json`](../image/inputs.json). The second emits the QCOW2 under
`out/disk/qcow2/disk.qcow2` and writes `out/SHA256SUMS`.
Resolved package inventory and image metadata are generated under `out/` and
must be retained with the candidate record. A QCOW2 build is an artifact
receipt; it does not prove a booted desktop or a successful update.

Before transferring any candidate, independently verify the detached SSH
signature and the SHA-256 checksum for the exact same files. The checked-in
release receipt uses namespace `zeusos-release` and signer identity
`zeusos-preview`:

```sh
ssh-keygen -Y verify \
  -f security/allowed_signers \
  -I zeusos-preview \
  -n zeusos-release \
  -s docs/releases/preview-2/SHA256SUMS.sig \
  < docs/releases/preview-2/SHA256SUMS

repo_dir=/path/to/zeusos
# Place the named archive in /path/to/verified-artifacts first.
(cd /path/to/verified-artifacts && \
  sha256sum -c "$repo_dir/docs/releases/preview-2/SHA256SUMS")
```

Run the signature check from an independently obtained checkout or verification
host and retain the command output. A checksum file must be updated for the
candidate before this gate is considered complete; an old valid signature does
not authenticate a new artifact. The separate feed publication and
`zeusos-update` signature procedure is documented in the
[iteration build policy](iteration-builds.md#signed-preview-update-publication).
Complete the source, image, signature, and release-asset gates before updating
the feed; record native installation and explicit-restart qualification before
handing the preview to users. The current final record is in the [signed
Settings iteration receipt](iterations/git-fd2125f63159/README.md).

## Bootstrap and manual update path

This is the historical manual path for the first updater-capable image and for
recovery. Use a verified root-owned OCI archive with bootc for bootstrap, and
retain this path as the fallback after normal Updates use is available. Do the
following in order. Stop and keep the current deployment if any gate fails.

1. Confirm the guest is VM 115 and capture the current `zeus doctor --json`,
   `bootc status`, owner-file inventory, and candidate identifier. Do not run a
   clean-install or provisioning helper against this guest.
2. Verify the existing populated backup on the Proxmox host. The recorded
   backup is:

   ```text
   /mnt/pve/sata-ssd/dump/vzdump-qemu-115-2026_09_08-15_51_51.vma.zst
   ```

   The recorded storage reference is
   `sata-ssd:backup/vzdump-qemu-115-2026_09_08-15_51_51.vma.zst`. The checks
   below are the exact integrity checks used for this backup:

   ```sh
   ssh pve 'zstd -t /mnt/pve/sata-ssd/dump/vzdump-qemu-115-2026_09_08-15_51_51.vma.zst'
   # First decompress on the roomy SATA volume, with restrictive permissions.
   ssh pve 'umask 077; zstd -dc /mnt/pve/sata-ssd/dump/vzdump-qemu-115-2026_09_08-15_51_51.vma.zst > /mnt/pve/sata-ssd/zeusos/verification/temp-preview.vma'
   ssh pve 'vma verify /mnt/pve/sata-ssd/zeusos/verification/temp-preview.vma'
   ```

   Both archive checks are recorded as passed. They verify archive integrity,
   not an isolated restore. Preservation files written before backup, after
   backup and after update all survived update, rollback and roll-forward.
3. After the independent signature and checksum gate, place the verified OCI
   archive at a root-owned path on the guest. Do not put an owner password or
   private key in the archive or its command line. Confirm its ownership,
   permissions, and checksum before switching:

   ```sh
   ROOTOWNED_ARCHIVE=/var/lib/zeus/updates/preview-2-final/zeusos-preview-2.oci
   sudo stat -c '%U:%G %a %n' "$ROOTOWNED_ARCHIVE"
   sudo sha256sum "$ROOTOWNED_ARCHIVE"
   ```

4. Stage the image with the explicit local OCI archive transport. `--retain`
   keeps the current image reference available for review/rollback; omitting
   `--apply` is deliberate because staging must not reboot the desktop:

   ```sh
   sudo bootc switch --transport oci-archive --retain "$ROOTOWNED_ARCHIVE"
   sudo bootc status
   ```

5. Reboot only after the operator has reviewed the staged status and recorded
   the restart action:

   ```sh
   sudo systemctl reboot
   ```

   This restart is explicit. If Temp uses the **On boot** policy, its eligible
   cleanup also applies to this update reboot.

6. After GDM returns, run `zeus doctor --json`, verify owner files and the
   desktop smoke matrix, and retain the previous deployment until the candidate
   is reviewed. OS rollback does not roll back mutable `/var` data or a remote
   database; those are separate state checks. `/etc` returns to its previous
   deployment state on rollback, so later system configuration changes need
   separate review. Owner home files and dconf preferences live under `/var`.

If the desktop shell is broken, use a Proxmox text console or SSH and select a
known-good deployment manually:

```sh
sudo bootc status
sudo bootc rollback
sudo bootc status
sudo systemctl reboot
```

Record the selected deployment and reboot. Do not use `dnf`, rpm layering, or a
remote update service to repair this image.

## Desktop and performance review

The scripts below use the actual argument contracts in this repository. Run
them against the running VM 115 and save outputs under the local ignored
`out/evidence/` directory:

```sh
mkdir -p out/evidence

python3 scripts/proxmox-screenshot.py \
  --host pve \
  --vmid 115 \
  --output out/evidence/preview-1-desktop.png

python3 scripts/proxmox-input.py \
  --host pve \
  --vmid 115 \
  --keys ctrl-alt-f2

ssh shane@10.0.0.95 'python3 - --settle 60 --label preview-1-zeus' \
  < scripts/measure-idle.py \
  > out/evidence/idle-preview-1-zeus.json

ssh shane@10.0.0.95 'python3 -' \
  < scripts/validate-desktop.py \
  > out/evidence/desktop-schema-preview-1.txt
```

`proxmox-screenshot.py` requires `--vmid` and `--output`; it captures the
existing display through the private QMP socket and converts the returned PPM
to PNG. `proxmox-input.py` accepts repeatable `--keys` combinations and keeps
text input on stdin when `--text-stdin` is used; never put a password in an
argument. `measure-idle.py` accepts `--settle` from 0 through 300 seconds and a
required `--label`. `validate-desktop.py` must run inside the Fedora guest so
its installed GNOME schemas and desktop launchers are authoritative.

Review the native GDM/login path, the centered dock and wallpaper, Files,
Ptyxis, Settings, Firefox, Welcome, Super+Space search, Super+Return terminal,
terminal `Ctrl+C`, notification privacy, keyboard navigation, on-screen
keyboard, and 100/125/150% scaling. Run `zeus desktop safe`, log out/in if
needed, verify stock GNOME and terminal access, then run `zeus desktop restore`
and verify the dock returns. A screenshot cannot establish idle redraw,
performance, battery, suspend, Wi-Fi, or physical input behavior.

The declared comparison and budgets are in
[`preview-1-budgets.md`](releases/preview-1-budgets.md). Record actual values
and variability; the 4-vCPU/8-GiB VM allocation is not performance evidence.

## Current signed Settings iteration

VM 115 now runs signed build `git-fd2125f63159`, still version
`0.1.0-preview.2`, with booted manifest
`sha256:ae59a68dbfc9eb6f5f22fe7000f1073b266d578b49cb2c7d490ab38975dd023f`.
Open **Zeus → Settings** or **Welcome → Open Settings** for everyday controls.
The [final iteration receipt](iterations/git-fd2125f63159/README.md) records
the source, image, signature and release-asset gates, the native installation
through **Open Updates**, explicit reboot, and the post-reboot status. The
[earlier updater qualification](iterations/git-17d205103f3a/README.md) separately
records the background-install and authentication-cancellation tests.
The booted image is
`/var/lib/zeus/updater/downloads/zeusos-0.1.0-preview.2-git-fd2125f63159.oci`.
The previous build `git-17d205103f3a` remains in the rollback slot; no rollback
cycle was run. See the [timestamped metrics history](metrics.md) for runtime
observations; physical battery remains unmeasured on this VM.

The current verified pre-Settings backup is
`/mnt/pve/sata-ssd/zeusos/backups/settings-20260909/vzdump-qemu-115-2026_09_09-03_53_43.vma.zst`.
Its zstd and full decompressed VMA checks passed. The previous verified
pre-updater backup remains at
`/mnt/pve/sata-ssd/zeusos/backups/updater-20260909/vzdump-qemu-115-2026_09_09-01_16_44.vma.zst`.
Its zstd and decompressed VMA checks passed. The archive name follows the
Proxmox host clock; do not infer an event time from it. The earlier populated
pre-upgrade backup remains separately as a historical record at
`/mnt/pve/sata-ssd/zeusos/backups/boot-power-20260909/vzdump-qemu-115-2026_09_08-17_06_26.vma.zst`; its zstd and decompressed VMA checks also passed.
No previous backup or global retention policy was changed. Temp was held at
Never during repeated test boots and restored through its normal policy API for
handoff.

## Previous Preview 2 visual deployment (historical)

This historical deployment ran source `c11e57b8569b2332bbd570b6aef51d79b5c38cec`, booted
manifest `sha256:c35a6a8a2a42a42b508c74bf55e0ae5022d4848d0cfaff11816d56295aec22e5`.
The [Preview 2 notes](releases/preview-2.md) and
[receipt](releases/preview-2/build-receipt.json) record the six visual changes,
console checks, signed OCI artifact and idle sample.

The final archive is root-owned under `/var/lib/zeus/updates/preview-2-final/`
and retained on Proxmox at
`/mnt/pve/sata-ssd/zeusos/releases/0.1.0-preview.2/`.
The populated backup above passed both zstd and VMA verification. Four owner
files survived, including one created after that backup. No restore or disk
replacement was performed. The previous backup was pruned by the existing
keep-last-one policy; historical Preview 1 backup paths are not current targets.

At that deployment, the bootc rollback slot contained the initial Preview 2 candidate,
which predates the shell startup fixes. This revision did not repeat a
rollback/re-forward cycle; use the safe-desktop command for a presentation
fallback. The separately retained Preview 1 release archive is still available
for a reviewed manual OS recovery; it does not require restoring owner data.

## Preview 1 historical qualification

The previous Preview 1 release used source `38bb7fc53002a76dbfa4a5c891c3d1b1bbdb4f20`
and booted OCI manifest `sha256:3fa00c6a1d5eaa5529deb6c6138ffedfedb354d999da9735d268e70e808cd06b`.
The exact QCOW2 and OCI archive hashes, sizes, package inventory and validation
results are in the [release receipt](releases/preview-1/build-receipt.json) and
[release notes](releases/preview-1.md).

- Native GDM login, Wayland desktop, lock/unlock, Welcome actions, Files,
  Settings, Terminal, Firefox HTTPS browsing and search/terminal shortcuts passed.
- Secure Boot and SELinux are enabled; no system services are failed. SSH
  password and root login are disabled. Unattended bootc apply is masked.
- The image update retained the owner account, a dconf preference, and three
  files written before backup, after backup, and after update. Actual rollback
  and roll-forward retained those writes. The final icon-cache-only update
  also retained all three files. No disk replacement or backup restore was used
  once the guest contained owner state.
- Signed artifact checksums passed locally, on the guest for update, and on
  the Proxmox artifact store. QCOW2 structure validation passed.
- Cold start to visible GDM was 21.83 seconds. Zeus idle was 908.1 MiB RAM and
  0.188% aggregate CPU median; the same guest with standard presentation was
  882.1 MiB and 0.187%. Full protocol and sample limits are in the release notes.
- Safe desktop and restore passed with the optional dock inactive/active and
  owner-file checksums unchanged. The Zeus presentation is restored.
- Isolated backup restore, physical laptop hardware/battery, full fractional
  scaling/accessibility, compositor tracing, and automatic signed registry
  distribution are not qualified by this preview.

The final generic artifacts and signed checksums are retained on Proxmox at
`/mnt/pve/sata-ssd/zeusos/releases/0.1.0-preview.1/release/`. The isolated builder
is shut down after assembly; VM 115 remains running for review. Its native
idle lock remains enabled, so use the requested preview password if it locks.

This work changed only the dedicated Zeus guests and related artifact/backup
paths. The final comparison found 21 unrelated guest configuration hashes
unchanged; five Hostlet guest configurations changed and one disappeared
concurrently. This task made no changes to those guests and does not claim
those six configurations remained static throughout the shared-host session.
