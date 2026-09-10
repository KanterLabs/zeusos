# Resume final boot-menu setup

Product remains `0.1.0-preview.2`. A reported installation successfully resized
Fedora, created Zeus partitions, deployed the OS and installed Zeus EFI files,
then failed reading Fedora's group-writable `/etc/default/grub`. Correcting that
file's permissions exposed a second issue: the GUI offered Continue while the
backend rejected every error-phase journal as not prepared.

The continuation path is limited to the recorded final boot-menu boundary.
It requires a qualified executor, the existing verified image, original plan,
and matching installed target evidence. It does not restart partitioning,
formatting, image deployment or EFI installation. Other incomplete stage-two
boundaries remain ineligible for replay.

The GUI consumes the backend's explicit finalization capability. A failed
call clears stale installation eligibility until a fresh status review.
The protected-file error identifies the file whose metadata was rejected.

Validation results and physical-hardware limits are recorded in the signed
build-specific release receipt. Agent-run Fedora package testing preserves
the existing populated journal and boot configuration using an independently
verified copy and full file/GPT comparisons. No laptop write is performed by
the agent, and no successful laptop boot is claimed.

Parent verification: 180 installer tests passed in 12.524 seconds, including
the real backend/executor finalization integration. Its injected command
runner records no repeated partition, format, mount, image or EFI install
commands on retry. Fresh disk, mount, boot and saved identity mismatches are
refused. Installed-RPM fixture verification is recorded separately in the
release receipt; it is not a physical laptop boot test.
