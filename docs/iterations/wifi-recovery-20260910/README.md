# Existing Zeus Wi-Fi recovery — 2026-09-10

Version stays **0.1.0-preview.2**, image **git-ad091ecc2978**.

The previous image had the Intel kernel driver and NetworkManager Wi-Fi
plugin, but no iwlwifi firmware. The new image includes
`iwlwifi-mvm-firmware`; image builds now validate the driver, firmware,
NetworkManager daemon/plugin, nmcli and wpa_supplicant.

## Install from Fedora

Install the updated Zeus Installer RPM and run:

```sh
zeus-installer update-existing
```

The separate update window downloads the signed image using Fedora's network.
It checks the completed installer journal, exact partition table, filesystem
identities and installed Zeus identity before copying an offline update into
Zeus. It does not repartition, format, or reset user accounts or files.

Boot Zeus from the existing menu. The offline service verifies and stages the
image before the login screen, without a network. Open Updates, then restart
and choose Zeus again to activate it. Fedora remains the default boot choice.
The old image remains available for rollback. Ordinary Temp cleanup still
applies during these boots.

Repeated preparation of the same pending image is supported; different pending
images and changed disk identities are rejected. Once applied, the pending
marker is consumed so subsequent boots and rollback do not silently reapply it.
The shared status directories are readable by the desktop, while the image
cache stays private.

## Qualification

[Machine-readable receipt](qualification.json) records the populated VM118
trial: 4 vCPUs, 8 GiB RAM, verified vzdump backup, network disconnected during
initial offline staging, successful corrected-image boot, and unprivileged
Updates reporting up to date. All 27 tracked files (Fedora data and boot files,
Zeus documents/application data and EFI files) and the exact partition table
were unchanged. The old image was retained by bootc and booted in a rollback trial. Its
native updater still read the shared state, the Zeus document was intact,
and the consumed offline request did not reapply itself.

The corrected running image contains 342 Intel firmware files and passes the
Wi-Fi prerequisite validator. This VM has no physical Wi-Fi radio: association,
signal quality, AirPods, suspend and battery behavior still require laptop
hardware testing. No physical Wi-Fi connection or battery result is claimed.

Fresh-install qualification continues to use the earlier bootc 1.16.10 and
bootupd 0.2.35 storage boundary trials; these tool versions are unchanged in
this image. The earlier signed artifact remains allowlisted for interrupted
installations. The existing-install path is the real VM upgrade exercised here.
