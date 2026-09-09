# Updater bootstrap — September 9, 2026

This signed image bootstraps the in-OS updater on the existing VM 115. Version
remains **0.1.0-preview.2**, build **git-21a465760f53**. Native Updates and Welcome
launch, signed checks report this build current, and administrator-only requests
reject an unprivileged direct invocation. Source CI and 123 repository tests
pass; installed checks validate 82 GNOME settings, both GTK themes, the Updates
stylesheet, Polkit policy and root service definition.

![Native update check](updates-current.png)

The fresh populated backup passed zstd and decompressed VMA verification before
staging. Six permanent fixture hashes and the existing 48-byte Temp file remain
intact. Temp was held at Never for qualification boots; no backup restore, disk
replacement, reinstall or account reseed was performed.

The [first update boot](first-update-boot.json) records an explicit warm reboot:
25.093 seconds to changed-boot-ID SSH/GDM readiness and 8.774 seconds of reported
OS startup. This is one update boot, not a cold-start comparison or first-pixel
measurement. The [pre-updater idle sample](idle-before-updater.json) used a long
running session, 20 seconds settling, 120 seconds sampling, closed apps, an awake
display and Temp set to Never. It is an observation, not a controlled claim that
the updater improves performance. The VM has no physical battery.

[Build identity](build-info.json), [payload CI](payload-ci.json),
[published asset hashes](asset-verification.json), and the signed per-build
manifest are retained here. This image is the starting point for the native
update qualification of its successor; these checks alone do not establish that
an installation through the Updates window succeeded.
