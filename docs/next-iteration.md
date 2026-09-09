# Next Zeus OS additions

Planned on **2026-09-09 UTC**, after the deployed `git-17d205103f3a` build.
Shane selected **everyday laptop controls**. Zeus Settings is now implemented
and deployed to VM 115 as **git-fd2125f63159**; search and Apps remain unclaimed
Backlog work. The [Settings receipt](iterations/git-fd2125f63159/README.md) records
the native checks, signed update, screenshots and measured results.
Continue using **0.1.0-preview.2** with a separate Git build ID for each payload.

The next goal is easier everyday use while preserving the attractive desktop,
quick boot and low idle activity. Start with Settings, then improve search, then
add optional app installation. Each feature gets its own testable preview build
and actual VM screenshots.

| Order | Feature | What Shane will be able to do | Helm |
| --- | --- | --- | --- |
| Delivered | Zeus Settings | Reach laptop controls, appearance, Temp and Updates from one compact window. | ZOS-67 |
| 2 | Better search | Press Super+Space and find a setting or useful shortcut as well as an app. | ZOS-68 |
| 3 | Apps | Browse a small app catalog and explicitly install, update or remove optional apps. | ZOS-69 |

## What is already available

The desktop already retains GNOME's native Quick Settings. The image explicitly
includes NetworkManager Wi-Fi/Bluetooth support, BlueZ, GNOME Settings and
power-profiles-daemon. GNOME supplies the underlying network, sound, display and
power controls. Battery defaults, Temp and signed OS Updates are already shipped.
See [GNOME's Quick Settings documentation](https://help.gnome.org/gnome-help/quick-settings.html).

Current Super+Space search finds installed applications and caches the app list.
The Zeus package list has no explicit Flatpak or software-store setup; inherited
base packages still need checking before adding dependencies. These findings come
from the [package list](../image/packages.txt),
[shell extension](../desktop/rootfs/usr/share/gnome-shell/extensions/zeus-shell@kanterlabs/extension.js),
[Welcome app](../zeus/assets/welcome.py), and
[power defaults](../desktop/rootfs/etc/dconf/db/local.d/01-zeus-power).

## 1. Zeus Settings — ZOS-67

**Implementation checklist**

- [x] Add a small native window matching the existing light/dark style, spacing,
  icons and compact window controls.
- [x] Provide Appearance, Network/Wi-Fi, Bluetooth, Sound, Displays and Power
  destinations that open the corresponding installed GNOME panels. Determine
  supported panel IDs from the built image; retain a native Settings fallback.
- [x] Link to the existing Temp and Updates apps and show local version/build
  information. Opening Settings must not check the network or install updates.
- [x] Expose the window through Welcome, the Zeus menu and application search.
  Keep a clear route to all native GNOME settings.
- [x] Handle missing hardware/panels honestly. Use native controls for changing
  preferences; avoid new hardware daemons or recurring status polling.

**Testing checklist**

- [x] Open every destination from the actual GNOME session; verify repeated
  activation, focus, keyboard navigation and missing-panel fallback.
- [x] Review screenshots in light/dark at 100% and 200% scale. Check readable
  labels, accessible names and comfortable click targets.
- [x] Compare owner preferences, Temp policy, permanent-file hashes and staged
  update state before/after opening and closing the window: all must be unchanged.
- [x] Complete the shared performance and deployment checks below. Mark physical
  Wi-Fi, Bluetooth and battery behavior as pending hardware qualification.

Missing-panel fallback is covered by source fixtures; all native destinations
are present in the installed image. Physical radios, brightness adjustment,
battery life and laptop suspend/resume remain **pending hardware qualification**.

Implemented code scope: a native app and desktop entry under `desktop/rootfs`,
Welcome's launchers, and the Zeus menu in the existing shell extension.

## 2. Better Super+Space search — ZOS-68

**Implementation checklist**

- [ ] Add clearly labelled settings destinations and safe shortcuts such as
  opening Temp, Updates and Files, with useful aliases such as "wifi" and "power".
- [ ] Keep installed-app results, a bounded list, sensible ranking and no duplicate
  destinations. Retain cache invalidation when applications change.
- [ ] Represent actions with an allowlist and fixed argument arrays. Query text
  never becomes an executable command; search does not delete files, install
  updates or restart the computer.
- [ ] Preserve arrows/Enter/Escape behavior and focus restoration. Keep all work
  local, with no full-home file index or recurring work while search is closed.

**Testing checklist**

- [ ] Test ranking, deduplication, empty queries, special characters, missing app
  IDs and fast query changes. Verify app install/remove invalidates cached results.
- [ ] In native GNOME, search for and launch apps, settings and shortcuts; verify
  keyboard selection, Escape, focus restoration and the existing key bindings.
- [ ] Check light/dark, both scales, reduced motion and safe desktop fallback.
- [ ] Measure at least 30 warm queries and report median/p95 response time against
  the previous build under matched conditions; report cold opening separately.
  Complete the shared checks below.

Likely code scope: the existing shell extension's result model and dialog,
desktop-entry keywords, and meaningful search/interaction tests.

## 3. On-demand Apps — ZOS-69

**Implementation checklist**

- [ ] Inspect the actual image package inventory, then add the supported Flatpak
  backend and a compact native Apps window. Record dependency and image-size cost.
- [ ] Start with a small explicit catalog and installed-app list. Use a user-scoped
  repository with verified metadata; present the repository choice before setup.
  Flatpak supplies app transactions; see its
  [usage documentation](https://docs.flatpak.org/en/latest/using-flatpak.html).
- [ ] Before an explicit install, show app identity, publisher/source, permissions
  and available download-size estimates. Distinguish missing metadata from known
  values. Respect sandbox permissions and the existing file portal.
- [ ] Provide install, launch, manual app update and removal. Retain application
  data on removal. Keep these operations clearly separate from signed OS Updates.
- [ ] Show progress without freezing the window; serialize conflicting operations
  and handle supported cancellation, interruption, offline and full-disk states.
- [ ] Refresh catalogs on demand. Add no scheduled app update/refresh jobs or
  persistent Zeus Apps process when the window is closed.

**Testing checklist**

- [ ] In a disposable profile/VM, exercise repository setup, install, launch,
  search/dock discovery, manual update and removal with app data retained.
- [ ] Verify downloaded-file/file-picker behavior with the configured Temp
  location, without granting broad filesystem access merely for integration.
- [ ] Test canceled/interrupted transactions, unavailable metadata, disconnection,
  insufficient space and repeated clicks. Report the actual resulting app state.
- [ ] Compare permanent-file hashes and OS staged/rollback state before and after
  app transactions. Exercise failure and rollback fixtures away from Shane's data.
- [ ] Check populated app-data preservation across OS update/rollback in a
  disposable VM. An older OS image without Flatpak may lack app launch support;
  retained app data and a working native desktop must survive until forward update.
- [ ] Verify keyboard/accessibility, light/dark and both scales; complete the
  shared performance and deployment checks below.

Likely code scope: image packages, a native Apps app and backend adapter under
`desktop/rootfs`, desktop entries, Welcome integration and transaction tests.

## Shared performance and preview checks

The checks below are complete for **Settings / git-fd2125f63159**. Repeat them
for each future search or Apps build; these marks do not qualify those features.

- [x] Keep the current version and build only on dedicated builder VM 116.
- [x] Run relevant source checks and native UI tests for the changed feature.
  Check that safe desktop mode, native login and existing shortcuts still work.
- [x] Measure three cold boots and a closed-app idle run on the same 4-vCPU/8-GiB
  VM, with the builder stopped and matched login, workload and collection settings.
  Use 20 seconds settling and 120 seconds sampling for idle, as in recent entries.
- [x] Record UTC timestamps, source/build, conditions, raw evidence, dependencies,
  image size, boot medians, CPU, memory and process activity in the append-only
  [metrics history](metrics.md). Separate OS boot time from host-start-to-ready time.
- [x] Investigate persistent regressions greater than **0.25 CPU percentage
  points**, **64 MiB closed-app idle memory**, or **1 second median OS boot time**
  before promotion. These are new review thresholds, not measured improvements.
  Remeasure under matched conditions; document a fix or an explicit tradeoff.
- [x] Require no new recurring Zeus background work for Settings or closed search,
  and no scheduled Apps catalog/update work. Preserve existing user power choices.
- [x] Before each preview deployment, verify a fresh backup and record owner-file,
  preference and Temp-policy preservation checkpoints. Update the existing VM 115
  through the signed updater; never reseed or replace the owner's VM.
- [x] Verify the booted build and native UI, attach real screenshots, and report
  remaining limits. The VM does not establish physical battery life, radio behavior
  or laptop suspend/resume reliability.

The current observations are recorded in the
[deployed iteration receipt](iterations/git-fd2125f63159/README.md).
Production release policy, the encrypted installer, remote development workflow
and physical laptop qualification retain their separate backlog scope.
