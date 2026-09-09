# Zeus OS

A focused Fedora bootc laptop desktop with a macOS-inspired layout, original Zeus artwork, and native GNOME security and accessibility.

The current version is **0.1.0-preview.2**. It includes a translucent top bar and floating dock, original icons, traffic-light window controls, compact application search (Super+Space), a New York dusk wallpaper, and a branded native login screen. GNOME/Wayland runs Files, Firefox, Ptyxis, Settings, and Welcome. The Rust `zeus` helper provides diagnostics, safe desktop fallback, signed updates, and validated SSH launch.

The latest build brings **New York at dusk** to the desktop, lock screen and native login, with a larger, lighter lock clock and restrained translucent login controls. City and gradient backgrounds remain selectable in **Settings → Appearance**. [Screenshots and verification](docs/iterations/git-31f0851a9d07/README.md) record the signed update, accessibility, preservation and performance checks. The change adds no packages or background services.

![New York desktop on the deployed preview](docs/iterations/git-31f0851a9d07/desktop.png)

**Zeus Settings** remains available from the Zeus menu, Welcome or application search for network, Bluetooth, displays, power, sound, appearance, Temp and Updates. Physical battery remains unmeasured on the review VM; see the [metrics history](docs/metrics.md).

Deployment and measured results are recorded in the [preview VM runbook](docs/preview-vm.md) and [release notes](docs/releases/preview-2.md). An image build alone is not a graphical or performance test.

The [timestamped metrics history](docs/metrics.md) keeps measurements, testing
conditions and regressions together across builds.

We keep this version fixed while iterating. Each update has its own Git build ID; see the [iteration build policy](docs/iteration-builds.md).

## Build

Build on a dedicated Linux VM with rootful Podman, Python 3, Git and QEMU tools. Image assembly needs privileged container access; use the isolated builder, not a production host or the development VM.

```sh
git clone https://github.com/KanterLabs/zeusos.git
cd zeusos
sudo ./scripts/build.sh
sudo ./scripts/build-disk.sh
```

[Image inputs](image/inputs.json) pin the Fedora base and maintained osbuild builder by digest. Fedora RPM signatures are checked during assembly; the exact resolved package inventory is captured in `out/packages.lock`. RPM repositories are not snapshot-pinned yet, so later rebuilds can resolve newer packages. The source revision and installed package inventory are also embedded under `/usr/share/zeus/`.

The output `out/disk/qcow2/disk.qcow2` contains no owner password or SSH key. Initial owner setup uses a private Proxmox NoCloud seed; see [deployment instructions](docs/preview-vm.md). Keep account secrets outside Git and preserve the existing guest and owner state on later updates.

## Desktop tools

```sh
zeus version
zeus doctor --json
zeus desktop safe
zeus desktop restore
zeus update status
zeus update check
zeus update install
zeus dev --target user@your-dev-host
```

Safe desktop disables the optional Zeus shell, dock and lock effects and removes managed GTK styling imports; native GNOME remains usable and personal files and preferences stay in place. Applying an OS update or reboot is always explicit. The preview does not configure an unattended update channel.

Open **Updates** from application search or **Welcome → Open Updates** to check
for a signed preview build, review its notes, and install it after administrator
authentication. Installation continues after closing the window. **Restart to
Apply** is a separate action; the normal Temp cleanup policy also applies to
update reboots. See the [updater contract](docs/features/os-updater.md).

## Validation

```sh
cargo fmt --manifest-path zeus/Cargo.toml --check
cargo test --manifest-path zeus/Cargo.toml --locked --offline
python3 -m unittest discover -s tests -v
node --test tests/lock_background.test.mjs
```

GitHub Actions uses `homelab` for image policy checks and `homelab-heavy` for Rust compilation/tests. Runtime validation uses the actual review VM, including native login, application launches, stock-shell fallback, data preservation and idle sampling. [Measurement budgets](docs/releases/preview-1-budgets.md) were declared before the first desktop boot.

## Roadmap

The [additions plan](docs/next-iteration.md) tracks the delivered Settings work
and separate implementation/testing checklists for [AirPods support](docs/features/airpods.md),
better search and optional app installation. AirPods software prerequisites are
verified; real-device qualification and optional battery/noise controls remain open.

The review VM now has a qualified signed preview updater for owner-triggered Updates. Production release channels, release promotion, signing-key rotation and recovery policy, the full remote T3/Codex workflow, personal profile recovery, encrypted interactive installer, and physical laptop qualification remain separate implementation work. Preview qualification does not define production release or key-rotation policy.

- [Preview strategy](docs/preview-strategy.md)
- [Implementation checklist](docs/implementation-plan.md)
- [Testing checklist](docs/testing-plan.md)
- [Helm backlog](docs/helm-backlog.md)
- [Original design](docs/design-v0.1.md)
- [Planning overview](docs/backlog-overview.md)

Project code is MIT licensed. Original desktop artwork is CC0-1.0; Fedora packages retain their upstream licenses.
