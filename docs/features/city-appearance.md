# New York desktop, lock screen and login

Status: **ZOS-72 in progress**. Shane selected **New York skyline at dusk** for
the desktop background, lock screen and pre-login screen. Continue using
**0.1.0-preview.2** with separate Git build IDs.

## Appearance

- One still city image fills the desktop, lock screen and native GDM backdrop.
- Warm skyline lights and a blue dusk sky accompany the existing translucent
  Zeus panel, floating dock and small window controls.
- The native lock clock uses larger, lighter type. Login controls use restrained
  translucent surfaces with visible keyboard focus.
- The city and retained Zeus gradient artwork are available in GNOME's native
  Appearance picker. Wallpaper defaults remain editable and unlocked.

The [artwork record](../artwork/new-york-dusk.md) includes the saved asset, actual
pixel dimensions, checksum, generation prompts and provenance. This is generated
photographic-style artwork. No video wallpaper or continuous animation is added.

## Implementation checklist

- [x] Save the New York artwork in the image overlay and record its provenance.
- [x] Set desktop and screensaver defaults, retaining the previous assets.
- [x] Add a native Appearance catalog for the city and existing gradients.
- [x] Style the native greeter and clock using GNOME 50 selectors.
- [x] Add a small visual-only lock effect adjustment so the skyline stays visible.
  Change only existing background blur/brightness properties, restore them on
  disable/unlock, and retain native effects for high-contrast mode or unavailable
  internals. Authentication, input handlers and password widgets remain native.
- [x] Include the optional lock effect in safe-desktop disable/restore behavior.
- [ ] Build and deploy a signed preview update through the existing updater.

Nine lifecycle tests exercise property restoration, partial failures, high contrast,
monitor replacement, scaling and cancellation. The extension uses GNOME 50's
private background actor path; a changed or unavailable path keeps native blur.
It uses signals and cancellable one-shot idle work, with no recurring timer.

The review owner's existing explicit extension list needs an additive entry for
the new lock appearance. Preserve its other entries, wallpaper choices and
unrelated preferences. The requested city replaces Zeus's current default
wallpapers; updates must not lock out later owner wallpaper choices.

## Testing checklist

- [x] Record pre-change GNOME preferences and seven populated-file hashes.
- [x] Verify a fresh populated VM backup with zstd and full VMA verification.
- [ ] Pass source checks, real-image GNOME/theme validation and image identity checks.
- [ ] Verify native pre-login user selection, password entry and successful login.
- [ ] Verify native lock clock, unlock prompt and successful unlocking.
- [ ] Verify lock effect teardown, safe fallback, high contrast and repeated locking.
- [ ] Check layout, focus and readable contrast at normal and 200% scaling;
  restore temporary appearance, scaling and accessibility choices afterward.
- [ ] Check all populated-file hashes and unrelated preferences after updating.
  Temp cleanup is held at Never during qualification reboots, then restored to
  the original On boot policy. No backup restore or owner reseed is an upgrade step.
- [ ] Compare three cold starts and a closed-desktop idle window before/after;
  record all results and limits in [metrics.md](../metrics.md).
- [ ] Capture actual desktop, lock and pre-login screenshots from VM 115.

Declared review thresholds: no added runtime package or service, less than
10 MiB full OCI archive growth, at most +1 s median OS startup, +0.25 idle CPU
percentage points and +64 MiB idle memory. The measurement protocol is 20 seconds
settling followed by 120 seconds sampling, with the same display, Temp held at
Never, builder stopped and backup work complete during performance samples.
These VM samples do not qualify physical battery runtime or laptop hardware.
