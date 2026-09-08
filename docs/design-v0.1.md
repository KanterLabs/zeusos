# Zeus OS — Product & Technical Design

**Owner:** Shane Kanterman / KanterLabs  
**Version:** 0.1 — Proposed design baseline  
**Date:** September 8, 2026  
**Working name:** Zeus OS

> A minimal, macOS-like Fedora workstation for development on the Proxmox homelab. The laptop is replaceable; the dev VM owns development state.

Recovery and performance figures are acceptance targets, not measured results.

## 01  Product definition

*A personal workstation, designed to be replaced rather than rebuilt.*

Zeus OS is a proposed Fedora-based laptop operating system for Shane Kanterman. It combines a macOS-like desktop, a deliberately small application set and a reproducible personal setup. All development projects remain on the existing Proxmox homelab dev VM; the laptop provides the interface, connectivity and device-specific credentials.

> Primary outcome: install the ISO on a supported replacement laptop, authenticate, restore the personal profile and resume remote development within 30 minutes, without losing work already saved on the healthy dev VM.

### Confirmed requirements

| ID | Requirement |
| --- | --- |
| R01 | Provide a bootable, installable ISO and disposable VM test images. |
| R02 | Deliver a desktop that closely resembles macOS in layout and interaction. |
| R03 | Prioritize long battery life, low background activity and minimal local compute. |
| R04 | Preinstall T3 Code, Codex CLI and Git, plus the connectivity tools they need. |
| R05 | Keep project working trees, builds and agent execution on the homelab dev VM. |
| R06 | Restore a replacement laptop within the defined 30-minute recovery target. |
| R07 | Support tested, signed, image-based updates and an accessible rollback path. |

### Proposed baseline

Use Fedora 44 bootc, GNOME on Wayland and an x86_64 UEFI installer. Fedora 44 is the current stable Fedora release identified during research. Pin the actual base digest, GNOME version and tools in a release manifest; do not build production releases from floating latest tags. Zeus OS is a working name, not an implemented OS release. [1]

### Non-goals

No macOS application compatibility, Apple Silicon support in v0.1, local development VM, mandatory cloud-hosted dev environment, custom package manager or replacement GNOME compositor. Do not bundle local SDK collections, container workloads, databases or LLM runtimes. Preserve necessary desktop runtimes and hardware support even when they increase package count.

This document is a design baseline, not evidence of a completed build. “Required” denotes a release condition; numeric performance and recovery figures are engineering targets until measured.

## 02  Architecture & ownership

*The laptop is replaceable. The dev VM owns development state.*

| Boundary | Responsibilities |
| --- | --- |
| Laptop / Zeus OS | Display and input; GNOME; T3 Code client; terminal; browser; installed CLI tools; personal configuration; unique device identity. |
| Private transport | SSH over a reachable LAN or Tailscale path. Mosh is optional for terminal sessions; SSH remains available for tunnels and fallback. |
| Proxmox dev VM | Project files and uncommitted changes; Git operations on projects; T3 Code server; Codex execution; SDKs; builds; databases; persistent terminal sessions. |
| Recovery services | Encrypted configuration/recovery material; Git remotes; independently stored dev-VM backups; retained OS releases and installation media. |

```text
Laptop: T3 Code / terminal / browser
                 |
          encrypted connection
                 |
Dev VM: T3 service + Codex + tmux + project files
                 |
       Git remotes + independent backups
```

### Execution boundary

A locally installed Codex executable is not automatically a remote agent. The default Codex launcher must enter the dev VM and run Codex there. Local execution remains an explicit utility mode, never a silent fallback when the network fails. The T3 Code client must visibly identify its remote environment before a task starts.

### OS and state boundary

Ship software and defaults in image-managed locations, primarily /usr. Keep machine configuration in the base-supported persistent /etc model and runtime state in /var. Discover the base image’s home-directory layout instead of assuming /home and /var/home are interchangeable. bootc preserves mutable state separately from the OS deployment; application data is not a versioned OS layer. [2]

### Operational separation

Build images in a dedicated builder VM, not directly on the Proxmox host or the live dev VM. Test installs in disposable VMs with disposable disks. Build credentials must not grant unrestricted access to development projects or backups. A failure in the image pipeline must not interrupt ongoing development.

> The laptop must not be the sole authoritative location for any important project file, accepted agent task or recoverable personal setting.

## 03  Desktop experience

*macOS-like presentation without replacing the security or desktop foundations.*

The visual target is close macOS resemblance: a quiet top bar, centered floating dock, left-side window controls, coherent typography, restrained translucency and a clean lock screen. Treat this as a visual and interaction specification, not a promise of pixel-identical rendering across every Linux application.

| Surface | Design specification |
| --- | --- |
| Top bar | Approximately 28–32 logical pixels; system menu and active app label at left; Wi-Fi, battery and time at right. No decorative fake application menus. |
| Dock | Centered bottom placement; 44–48 logical-pixel icons; running indicators; intelligent hide. Default entries: Files, Terminal, T3 Code, Dev, Browser. |
| Windows | Close, minimize and maximize controls on the left where supported. Consistent spacing and modest shadows; retain application-specific behavior. |
| Navigation | GNOME Overview as the workspace view. Super+Space opens search; Super+Return opens a terminal. Preserve terminal Ctrl+C and standard editing keys. |
| Lock screen | Large clock and user avatar; minimal essential status. Hide notification contents. A single cached blur transition may accompany password focus. |
| Login / GDM | Coordinated wallpaper and restrained branding. Authentication stays native; login-screen changes are a separately tested surface. |
| Visual assets | Original or redistribution-cleared wallpaper, icons and fonts. Use the upstream UI font and an open monospace font; do not redistribute Apple assets. |
| Accessibility | Readable contrast, reduced motion, keyboard navigation, scaling and a functional on-screen keyboard. Meaning must not depend on color alone. |

### Implementation strategy

Start with GNOME settings, a small audited dock dependency and a thin Zeus shell integration layer. Pin extension versions to the tested GNOME release. Keep dock, status presentation and lock styling independently disableable. Do not fork GNOME or force unsupported toolkit-wide CSS overrides merely to match an application screenshot. Use supported libadwaita style and color facilities where applicable. [8]

Lock-screen extension behavior must explicitly support the appropriate session mode and must not intercept password input. GNOME documents separate user and unlock-dialog behavior; extension lifecycle cleanup and keyboard-signal restrictions are security-relevant. [9, 9a]

> A broken cosmetic module must leave a usable stock GNOME session. Universal global menus and exact macOS application chrome are outside v0.1.

## 04  Personal development suite

*Install the client tools locally; keep the actual development workload remote.*

| Component | Laptop role | Dev VM role |
| --- | --- | --- |
| T3 Code | Preinstalled desktop client and launcher; opens the configured homelab environment. | Persistent user service; project access, agent orchestration and durable application state. |
| Codex CLI | Preinstalled standalone executable; default launcher explicitly opens remote Codex. | Authoritative agent execution, provider authentication, configuration and session history. |
| Git + gh | Profile management and optional local utility work. | Project commits, branches, worktrees, remotes and repository credentials. |
| SSH / Mosh / tmux | SSH client and tunnels; Mosh client; tmux available as a utility. | SSH server; optional Mosh server; tmux server owns persistent terminal sessions. |
| Tailscale | Private homelab connectivity; fresh device enrollment. | Private endpoint with least-privilege access rules. |
| Desktop essentials | Browser, terminal, Files, Settings, keyring, firmware tooling and small CLI utilities. | Existing SDKs, build systems and development services; not replicated to the laptop. |

### T3 Code integration

Upstream T3 Code documents desktop-managed SSH connections that start or reuse a remote server and forward its port. Use that supported path with a verified persistent service on the dev VM. Do not assume the moving upstream documentation matches an older packaged release: verify the exact client/server pair before shipping. [5]

T3 Code’s Linux background-service support uses systemd user services and lingering. Provision this once on the dev VM, under the normal development user. Client reconnects must attach without creating duplicate services or restarting active work. Remote updates must be explicit because service updates restart the server. [6]

### Packaging contract

Include versioned T3 Code Linux application artifacts and a standalone Codex binary in the OS image, with their required runtimes. An application runtime such as Electron is not a local SDK collection; its actual memory and power cost must still be measured. Codex offers standalone Linux installation, so Node is not required solely to launch its local CLI. [7]

Include git, gh, openssh-clients, mosh, tmux, ripgrep, fd-find, fzf, jq, curl and rsync. Prefer one browser and one terminal. Flatpak is available for optional user-installed apps, but the core recovery workflow must not depend on downloading additional apps after installation. Disable self-updaters for image-managed tools so the release manifest remains accurate.

> Do not mount projects with SSHFS as the primary workflow, silently mirror working trees, or launch builds locally when a remote host is unavailable.

## 05  Configuration & persistent state

*Separate portable preferences, device identity and development data.*

| State class | Storage / owner | Restore and update policy |
| --- | --- | --- |
| System baseline | OS image and release manifest | Reinstall or bootc update. No personal secrets or host-specific identities. |
| Portable preferences | Private workstation-config repository | Versioned shell, terminal, Git, GNOME and launcher settings. Apply a reviewed revision. |
| Device identity | Local encrypted state | Fresh machine identity, SSH device key and Tailscale enrollment. Never clone from another laptop. |
| Secret recovery | Independent encrypted vault / recovery kit | Interactive unlock and authorization. Recovery access must not depend only on the lost laptop. |
| Development state | Homelab dev VM | Do not restore to the laptop. Reconnect to the existing projects and persistent services. |
| Local exceptions | Explicit encrypted backup allowlist | Optional notes, browser bookmarks and unsent drafts. Caches are excluded; coverage is visible. |

### Profile precedence

Resolve settings in this order: image defaults, approved personal profile, device overrides, then explicit user choices. Apply only namespaced or allowlisted GNOME keys. A profile update must not replay a full dconf dump and overwrite unrelated choices. Store theme assets in the image; use the profile for selection and preferences.

### Idempotent restore

The restore engine validates the profile schema and revision, previews changes, backs up managed files, writes atomically and records a checkpoint after each successful phase. Running it again must not duplicate aliases, keys, services or desktop entries. Conflicts and unsupported settings require a visible decision, not silent replacement.

Keep the last-known-good profile revision with its applicable OS release range. Configuration migrations must support the preceding supported OS deployment. Do not treat OS rollback as automatic rollback of the user’s profile or application databases.

### Capture is deliberate

Provide a proposed zeus config export command that exports approved settings and shows a diff before committing. Regular encrypted backups cover explicitly allowed non-Git state. A local setting not exported or backed up is not recoverable by definition; the doctor report must show the most recent successful export and backup, not imply continuous synchronization.

> Never embed SSH private keys, OAuth caches, Wi-Fi passwords, GitHub tokens, Tailscale state, machine-id, or user passwords in the ISO, OCI layers or profile repository.

## 06  Replacement-laptop recovery

*Target recovery time: 30 minutes under explicitly tested conditions.*

Start the clock when the replacement laptop boots the prepared USB installer. Stop it when the configured desktop is available, the dev VM is reachable, T3 Code opens the remote environment and the terminal reattaches to the existing session. Count authentication and both installation boots. USB download and creation are recorded separately; keep current recovery media ready.

| Stage | Budget | Required outcome |
| --- | --- | --- |
| Install | 15 min | Select the target disk explicitly; install the embedded OS payload; create a user; enable disk encryption; boot the installed system. |
| Connect and enroll | 5 min | Connect Wi-Fi; unlock recovery access; approve a new Tailscale identity; authorize a fresh SSH device key. |
| Restore preferences | 5 min | Fetch and verify the selected private profile; apply settings and app connection definitions; preserve device-specific configuration. |
| Validate and resume | 5 min | Run doctor; open the remote T3 environment; attach tmux; verify saved working-tree changes and a running task. |

### Prerequisites and failure behavior

The target assumes a supported laptop, functional installer media, usable networking, available identity providers and a healthy, reachable homelab dev VM. Recovery factors must be available independently, such as a second enrolled device or offline recovery codes. Unknown hardware, broken Wi-Fi, homelab outages and unavailable authentication services can exceed the target; record the blocker rather than reporting success.

### First-run workflow

A single Setup Workstation launcher invokes the restore engine. Present progress for network, device enrollment, profile revision, SSH trust, remote tools and final validation. Retry interrupted steps from checkpoints. Offer a complete stock desktop and terminal even when personalization cannot finish. Do not require manual package installation or undisclosed post-install commands.

Preserve known SSH host fingerprints through a trusted profile or separate verification path. A changed host key blocks automatic connection. Enrollment must create a new key on the replacement device or use an approved hardware-backed identity; the recovery kit must support granting that new public key access without the old laptop.

### Meaning of no work lost

For laptop-only loss while the dev VM remains healthy, work already saved remotely should be unchanged, including uncommitted and untracked files. Unsaved editor buffers, unsubmitted prompts and laptop-only drafts are not automatically protected. The UI must distinguish submitted work from local drafts; test their actual persistence rather than promising it.

> The recovery drill passes only from a wiped client, with the old client unavailable and without rebuilding or resetting the dev VM.

## 07  Image updates & release channels

*One OS update engine, one tested artifact, explicit restarts.*

Choose bootc as the public OS lifecycle interface. Do not mix rpm-ostree layering or live dnf upgrades into the supported client workflow. Image builds may use package managers inside the build environment; installed clients consume whole tested deployments. bootc stages updates for a later boot and supports selecting the preceding deployment through rollback. [3]

| Channel | Audience | Promotion rule |
| --- | --- | --- |
| nightly | Disposable test VMs | Build from reviewed changes and scheduled upstream refreshes; never required on the daily-use laptop. |
| beta | Hardware validation | Promote the same tested digest after install, connectivity, desktop and rollback checks. |
| stable | Daily-use laptop | Manual approval after the beta soak and recovery checks. Retain release metadata and previous known-good artifacts. |

### Client update policy

Check for updates at a proposed six-hour interval with jitter while awake. Download and stage on AC and an unmetered connection by default; provide a user override. Show the version, digest, release notes and pending reboot. Never reboot a desktop without explicit user action. Inspect and disable or override any inherited auto-apply timer that conflicts with this policy. [3]

### Compatibility and rollback

Version T3 Code client, remote server compatibility, Codex, shell modules and profile schema in one release manifest. A laptop OS update must not silently restart or upgrade the remote agent service. Promote compatible client/server versions together operationally, with the remote restart scheduled when tasks are finished.

Retain a known-good boot entry and document recovery from both the desktop and a text console. OS rollback does not reverse /var application data or remote database migrations. Test old binaries against retained state; use separate state backups before incompatible migrations. Revoke a bad channel release and publish a tested replacement rather than rebuilding an old tag in place. [2]

### Security cadence

Evaluate upstream updates daily. Define an expedited path for relevant high-severity fixes, retaining install, connectivity and signature gates even when cosmetic changes are deferred. Block major Fedora or GNOME transitions until extension and hardware tests pass. Optional Flatpak apps and firmware have separate update records and are not claimed to roll back with the OS.

> Release promotion changes channel references to an existing digest. It must not rebuild the candidate or silently substitute different package versions.

## 08  Build, installer & supply chain

*Build on the homelab; publish reproducible inputs and verifiable outputs.*

### Build pipeline

```text
Reviewed Git revision + locked inputs
  -> build bootc desktop image
  -> lint, dependency inventory, license/secret checks
  -> sign candidate OCI digest
  -> produce installer ISO + QCOW2 from that payload
  -> install, update and recovery tests
  -> promote exact digest to beta, then stable
```

Use the maintained OSBuild image-builder project and its bootc-image-builder tooling. The old standalone bootc-image-builder repository has been archived after migration. Pin the builder container by digest and record the invoked interface. Generate both a fresh-install ISO and a QCOW2 artifact; importing QCOW2 is not a substitute for testing the actual installer. [4, 4a]

### Installer safety contract

The end-user ISO must be interactive: show disk model and capacity, request an explicit target selection and require confirmation before destructive partitioning. Never ship an unattended “first disk found” policy as personal recovery media. Include the OS payload so base installation does not require pulling the full image over Wi-Fi. Verify these behaviors on the chosen builder/Anaconda path. [4]

Target x86_64 UEFI, an encrypted user-data/root layout and a visible fallback boot entry. Keep Fedora’s supported boot components and avoid custom kernel modules in v0.1. Secure Boot and encrypted installation are hardware release gates, not capabilities inferred merely from using Fedora. A full live desktop is optional; the installer and installed desktop are mandatory.

### Release record

Each release records source commit, Fedora major version, base digest, resolved RPM versions, third-party artifact hashes, extension versions, builder digest, configuration schema, T3 client/server compatibility, test results and licenses. Generate a software bill of materials, signed checksums for ISO/QCOW2 and an OCI signature. Archive the inputs and artifacts required for a rebuild; byte-for-byte reproducibility is a separate test, not assumed.

### Registry and access

Prefer a publicly readable, secret-free OS image in a proposed KanterLabs registry namespace, with private personal configuration fetched only after enrollment. Restrict publishing and signing credentials to the release pipeline. Isolate privileged builds; test releases without production secrets. Keep at least one verified installer outside the homelab so homelab loss cannot remove every recovery path.

> Two independent gates: the image must be signed, and an installed client must actually reject an unsigned or wrong-signer update. Signing alone is insufficient. [10, 10a]

## 09  Security & identity

*Protect the device, the update channel and the authoritative remote workspace.*

| Area | Required control |
| --- | --- |
| Disk and login | Encrypted installation; lock on suspend; native authentication; no automatic login; essential accessibility retained. |
| OS protections | SELinux enforcing, firewall enabled, supported kernel and firmware. No broad passwordless sudo, disabled sandbox or blanket security bypass. |
| Remote access | Private network or SSH tunnel only. Do not publish a development agent server or Proxmox management interface to the public internet. |
| SSH trust | Unique device key, verified host identity and no agent forwarding by default. Do not distribute the dev VM’s Git credentials to clients unnecessarily. |
| Agent permissions | Run agents as the development user. Retain approval/sandbox controls; do not grant the agent Proxmox-admin or backup-admin credentials. |
| Update trust | Enforced signature policy scoped to the expected image identity and signer; negative tests for unsigned and incorrectly signed candidates. |
| Logging | Redact tokens, pairing links, secrets and prompt contents. Diagnostics are local by default and exported only with review. |

### Credential placement

T3 Code provider credentials and Codex authentication belong on the dev VM because that is where agents run. Replacing the laptop should not require reauthenticating a healthy remote provider session. When remote reauthentication is needed, use the provider’s supported flow; Codex documents device-code authentication for headless environments, subject to account or workspace availability. [7a]

### Device enrollment and revocation

The restore workflow must authenticate the owner, register a new device identity and grant only the required dev-VM access. Do not copy Tailscale state, machine-id or another device’s private key into a new installation. A lost-device runbook revokes its network enrollment, SSH authorization and T3 client session while preserving the active dev VM.

### Update trust bootstrap

Install trusted verification material and container signature policy with the verified ISO. Validate bootc’s enforcement path for the pinned release, including its enforce-container-sigpolicy behavior and signature-discovery configuration. A recovery key-rotation plan must exist before publishing stable updates. Image signing, Secure Boot and encrypted storage protect different boundaries and must be tested independently. [10, 10a]

> Secret-manager selection remains open. The non-negotiable requirement is independent recovery access: losing the laptop must not also lose the only way to unlock its replacement.

## 10  Power & performance

*Optimize measured energy use, not the number of packages on disk.*

No absolute battery-life claim is possible before selecting and measuring the target laptop. Compare Zeus OS against clean Fedora on the same hardware, firmware, brightness, refresh rate, network and workload. Remote execution removes local builds, but the screen, browser, T3 client, Wi-Fi and graphics stack still consume power.

| Area | Baseline policy |
| --- | --- |
| Power manager | Use one Fedora-supported power stack. Fedora moved GNOME’s default power-profile management to TuneD/tuned-ppd; verify the selected base. Do not stack competing power daemons. [11] |
| Display and effects | Use a stable 60 Hz battery mode where supported; avoid animated wallpaper and continuous blur. Cache lock blur and stop animation work once transitions finish. |
| Background activity | No local dev containers, project indexing, agent server or SDK watchers at startup. Audit printing/discovery/indexing services individually instead of indiscriminately deleting dependencies. |
| Update traffic | Batch background work; prefer AC and unmetered networks. No tight polling loops for host status. Back off while offline or suspended. |
| Sleep and connectivity | Lock before suspend; restore network and tunnels after resume. Do not keep the laptop awake merely to preserve a remote job. |
| Hardware support | Retain Wi-Fi firmware, audio, Bluetooth, portals, keyring and firmware updates. Avoid experimental blanket kernel or PowerTOP tuning. |

### Candidate acceptance budgets

On the reference laptop, target at least 10% longer runtime than clean Fedora in the standardized remote-dev workload; require no material regression to promote stable. Initial guardrail: median energy use must not exceed the baseline by more than 5% across repeated equivalent runs. The 10% gain is an aspirational product target, not an achieved result.

Diagnostic targets: under 2% aggregate CPU utilization at a settled idle desktop; no recurring custom-shell redraw when idle; under 1% battery capacity per hour during an eight-hour supported suspend test. Treat failures as investigation triggers and record the hardware’s inherent limits. Package count and idle RAM are diagnostic measurements, not proxies for battery life.

### Measurement protocol

Record laptop model, battery full-charge capacity, firmware, kernel, display settings, room conditions and wireless path. Run at least three comparable energy tests after warm-up, including terminal-only and T3-plus-browser workloads. Report median watt-hours consumed and runtime with variability. VM results cannot validate physical battery, suspend drain, thermals or trackpad quality.

## 11  Dev VM durability & backup

*A replaceable laptop does not make the homelab failure-proof.*

### Persistence model

Keep T3 Code’s service, Codex history and all working trees on the dev VM. Use systemd user-service persistence for T3 Code and remote tmux for terminal sessions. Laptop disconnects must not terminate a submitted remote task. A VM reboot is different: processes stop, and continuity depends on saved application state and supported resume behavior, not merely on tmux.

| Failure scenario | Recovery objective | Boundary |
| --- | --- | --- |
| Laptop destroyed; VM healthy | RTO ≤30 min; RPO zero for work already saved remotely. | Does not include unsent prompts or unsaved client buffers. |
| Network interruption | Reconnect without duplicated jobs once transport returns. | No claim of remote access while both paths are unavailable. |
| Dev VM or storage failure | Proposed project-data RPO ≤1 hour; VM-restore RTO measured in a drill. | Must include working-tree files, untracked files and application state. |
| Whole homelab lost | Restore from an independently stored encrypted copy. | Offsite copy age defines actual RPO; no 30-minute guarantee. |

### Proposed backup policy

Take hourly encrypted file-level backups of project roots and relevant agent configuration/state; take a nightly full dev-VM backup. Retain proposed tiers of 24 hourly, 14 daily and 8 weekly recovery points, subject to measured storage capacity. Capture database state through supported backup/export or quiescence procedures, not a casual file copy of an active database.

Enable and verify the QEMU guest agent for VM backup coordination. Proxmox documents filesystem freeze/thaw to improve live-backup consistency; application-specific databases still need appropriate consistency handling. Back up every volume that actually contains project or application data. [12]

Keep one copy outside the dev VM’s storage failure domain and an encrypted offsite copy. A backup on the same Proxmox disk is not sufficient protection from host storage loss. Git remotes complement these backups but do not cover uncommitted files, untracked files, local databases or every session artifact.

### Verification and health visibility

Restore a representative backup to an isolated VM at least monthly and verify projects, Git state, agent history and database integrity. Prevent the restored test VM from advertising the production hostname or running production jobs. The laptop doctor command reports last successful backup time, copy age and restore-test date through a read-only health source; it never holds backup administrator credentials.

> The zero-work-loss promise is scoped to laptop failure. Homelab data loss is bounded by the tested backup recovery point, not by the OS installer.

## 12  Verification & acceptance

*A release passes only when the user journeys survive failure.*

Use Proxmox disposable VMs for installation, update and recovery tests. Initial profile: 4 vCPU, 8 GiB RAM, 64 GiB disposable disk, UEFI/OVMF and VirtIO devices. Keep a second low-resource profile for regression checks. GPU animation, Wi-Fi, suspend and battery acceptance require real hardware.

| Test | Pass condition | Req. |
| --- | --- | --- |
| T01 / install | ISO installs offline from its embedded payload and boots with the required suite present. | R01/04 |
| T02 / disk safety | Two-disk VM requires explicit disk selection; the non-target disk remains unchanged. | R01 |
| T03 / personalization | Wiped-client restore completes from independent credentials in ≤30 minutes on the reference setup. | R06 |
| T04 / continuity | Kill the client during a remote task; reconnect without duplicate execution or changed saved working-tree files. | R05/06 |
| T05 / transport | Suspend and resume; interrupt Wi-Fi; test SSH fallback; explain unreachable and host-key errors clearly. | R05 |
| T06 / update | Upgrade from previous stable and oldest supported installer; reboot; confirm image and profile versions. | R07 |
| T07 / rollback | Fault-inject a shell failure; select previous deployment; recover a usable terminal and remote access. | R07 |
| T08 / trust | Reject unsigned/wrong-signer updates; secret scan finds no embedded credentials; identity is unique per install. | R07 |
| T09 / failure safety | Interrupt restore/download; simulate full disk; preserve the working deployment and resumable checkpoints. | R06/07 |
| T10 / desktop | Review shell, lock, GDM, keyboard, 100/125/150% scaling and external-display behavior. | R02 |
| T11 / power | Meet measured baseline guardrails and suspend/resume checks on each supported laptop. | R03 |
| T12 / backup | Restore a dev-VM backup in isolation; validate uncommitted/untracked work and application data. | R05/06 |

### Test evidence

Record image digest, profile commit, tool versions, test configuration, results and relevant logs/screenshots. A doctor check is not proof of backup restorability or battery performance; it may display those results only when a dated test record exists. Treat skipped critical tests as failures for stable promotion.

> Release gate: install, connect, restore, update, rollback and preserve remote work before spending additional effort on cosmetic refinements.

## 13  Implementation & repository

*Build the narrow usable slice before expanding the desktop.*

| Milestone | Deliverable and exit gate |
| --- | --- |
| M0 / feasibility | Pin Fedora/bootc and T3 artifacts; prove a GNOME session, persistent remote T3 connection, interactive encrypted install and rollback. |
| M1 / usable image | Ship the core suite, supported transport, installer and QCOW2. Pass install, disk safety and remote-execution tests. |
| M2 / personal recovery | Implement profile restore and device enrollment. Pass a wiped-client recovery drill with the old laptop unavailable. |
| M3 / desktop + power | Apply the dock, shell and lock design. Validate failure fallback, real-hardware suspend and energy baseline. |
| M4 / maintained beta | Automate signed builds, channel promotion, rejection tests and backup visibility; complete client/server compatibility testing. |

### Proposed project layout

```text
zeus-os/
  image/          Containerfile, package and version locks
  installer/      interactive installer definition
  desktop/        shell modules, dconf defaults, assets
  zeus/           restore, dev, doctor and update integration
  profiles/       schema and non-personal example
  remote/         dev-VM service and compatibility definition
  security/       update policy, public verification material
  tests/          installer, integration, recovery, hardware
  docs/           design, decisions, runbooks
  .github/        build, verify and promote workflows
workstation-config/   separate private personal repository
```

### Proposed command contract — not yet implemented

zeus restore applies the approved personal profile; zeus dev opens the remote workspace; zeus doctor reports actionable readiness and dated backup status; zeus update exposes bootc status/staging; zeus config export captures an allowlisted preference diff. Add a safe-desktop recovery action that disables cosmetic modules without removing connectivity or credentials.

Use a small Rust CLI for orchestration and structured validation; use GJS only inside GNOME shell modules. Invoke trusted system tools with argument arrays and bounded timeouts, never shell-interpolated host strings. Keep privileged operations narrow. A native first-run window may wrap the same engine later; it must not duplicate setup logic.

> v0.1 definition of done: a usable installer, required dev suite, remote-first launchers, tested personal restore and manual bootc rollback. Advanced blur, a custom settings app and full login-screen redesign are not prerequisites.

## 14  Decisions & unresolved gates

*Make uncertainty explicit before committing to a daily-use release.*

| Decision | Selected direction | Reason / limit |
| --- | --- | --- |
| OS lifecycle | Fedora bootc image deployments. | Single client update model; do not fork Fedora or introduce package layering. |
| Desktop | GNOME / Wayland. | Fits the chosen interaction model; not an assertion that GNOME is universally the most power-efficient option. |
| Compute placement | Existing homelab dev VM. | No cloud dev provider or duplicate local environment required. |
| Personalization | Private versioned profile plus independent secret recovery. | No personal credentials or machine identities in published OS artifacts. |
| App delivery | Core tools pinned in the image. | Consistent recovery; optional apps can have a separate Flatpak lifecycle. |
| Connectivity | SSH with optional Tailscale/Mosh. | No custom transport; private access and visible remote execution. |
| Visual fidelity | Close macOS-like shell; native authentication and app semantics. | Do not trade reliability, accessibility or battery for fake menus or invasive toolkit patches. |

### Open gates before hardware beta

Select the reference laptop and record its display, GPU, Wi-Fi and battery characteristics. Finalize the secret-recovery provider and new-device enrollment method. Select exact T3 Code artifacts and prove remote-service compatibility and session persistence. Validate encrypted interactive installation and Secure Boot on real hardware. Confirm asset redistribution rights and measured backup capacity.

### Principal risks and mitigation

GNOME upgrades can break shell integrations: pin and test them, with a stock-desktop fallback. T3 client/server drift can interrupt work: version the pair and make remote restarts explicit. Homelab dependence can block work during outages: retain secure alternate access and tested independent backups. Excessive UI effects can erase power savings: require physical energy measurements.

Credential recovery can become circular: perform recovery drills without the old device. Mutable configuration can drift from the image: expose versions, supported settings and last export. A signed image can still be trusted incorrectly: test actual signature rejection, identity scoping and key rotation. Do not label any of these resolved until evidence is recorded.

> Success is demonstrated by replacing the client, not by a screenshot: the same remote projects, saved changes and persistent sessions must remain available after a clean installation.

## 15 References

Primary documentation reviewed September 8, 2026. Architecture choices and performance thresholds are proposed requirements.

**[1] Fedora Project — [Fedora Workstation 44 download](https://fedoraproject.org/workstation/download/).** Stable-release baseline. The download page identifies Fedora 44 and its April 28, 2026 release date.

**[2] bootc project — [Filesystem](https://bootc.dev/bootc/filesystem.html).** Image-managed paths, mutable /etc, persistent /var and the limits of OS rollback for application data.

**[3] bootc project — [Managing upgrades](https://bootc.dev/bootc/upgrades.html).** Staging, upgrade, switch and rollback behavior; upstream update timer. Validate command behavior against the pinned binary.

**[4] OSBuild project — [bootc-image-builder documentation and repository migration](https://github.com/osbuild/image-builder/tree/main/bootc-image-builder).** Installer and QCOW2 output options, embedded payloads and installer customization. This is the maintained repository path.

**[5] T3 Code maintainers — [Remote access](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md).** Desktop-managed SSH, remote execution and credential placement. Moving upstream documentation; the chosen packaged release still requires testing.

**[6] T3 Code maintainers — [Running T3 Code in the background](https://github.com/pingdotgg/t3code/blob/main/docs/user/background-service.md).** Linux user-service persistence, lingering, pinned service updates and service restart behavior.

**[7] OpenAI — [Codex CLI and authentication](https://learn.chatgpt.com/docs/codex/cli).** Standalone installation and CLI behavior. The linked authentication reference below covers headless sign-in.

**[7a] OpenAI — [Authentication](https://learn.chatgpt.com/docs/auth).** Device-code sign-in availability and headless authentication. Credentials remain on the machine executing the agent.

**[8] GNOME / libadwaita — [Style classes](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.3/style-classes.html).** Supported application styling facilities. Does not establish universal macOS theming or global-menu compatibility.

**[9] GNOME JavaScript documentation — [Session modes](https://gjs.guide/extensions/topics/session-modes.html).** Separate user and unlock-dialog modes and extension lifecycle behavior.

**[9a] GNOME JavaScript documentation — [Extension review guidelines](https://gjs.guide/extensions/review-guidelines/review-guidelines.html).** Lock-screen session restrictions and cleanup expectations, including keyboard-signal handling.

**[10] bootc project — [bootc-switch manual](https://bootc.dev/bootc/man/bootc-switch.8.html).** Digest targeting and the enforce-container-sigpolicy option. Confirm effective trust policy in integration tests.

**[10a] containers/image maintainers — [containers-policy.json manual](https://github.com/containers/image/blob/main/docs/containers-policy.json.5.md).** Trusted image identities, policy scopes and signature-verification requirements.

**[11] Fedora Project — [Fedora 41 system-administrator release notes](https://docs.fedoraproject.org/en-US/fedora/f41/release-notes/sysadmin/).** Documents the TuneD power-management transition for GNOME. Verify the packages selected in the actual Fedora 44 image.

**[12] Proxmox Server Solutions — [Backup and Restore](https://pve.proxmox.com/pve-docs/chapter-vzdump.html).** VM backup modes and guest-agent freeze/thaw coordination. Used through the indexed official documentation; direct fetch was restricted.

**[4a] OSBuild project — [Archived bootc-image-builder repository](https://github.com/osbuild/bootc-image-builder).** Migration notice directing development to osbuild/image-builder; avoid treating the archived repository as the maintained project.

### Validation notes

No live Proxmox configuration, backup status, laptop hardware inventory or working T3 installation was inspected. Validate the pinned versions against current upstream documentation.

RTO = recovery time objective. RPO = recovery point objective. Laptop-only recovery and homelab disaster recovery have different targets.
