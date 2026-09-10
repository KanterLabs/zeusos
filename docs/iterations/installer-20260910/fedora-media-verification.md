# Fedora 43 media verification

Recorded 2026-09-10 UTC for the VM117 fixture work.

The installer source is the official Fedora 43 Everything x86_64 netinstall
image:

```text
Fedora-Everything-netinst-x86_64-43-1.6.iso
size: 1172207616 bytes
sha256: f4d06a40ce4fb4a84705e1a1f01ca1328f95a2120d422ba5f719af5df62d0099
```

The signed checksum record was checked with the Fedora 43 primary key. The
key fingerprint is:

```text
C6E7 F081 CF80 E131 4667 6E88 829B 6066 3164 5531
```

The exact signed checksum line is:

```text
SHA256 (Fedora-Everything-netinst-x86_64-43-1.6.iso) = f4d06a40ce4fb4a84705e1a1f01ca1328f95a2120d422ba5f719af5df62d0099
```

The detached evidence copies are kept in [evidence/fedora43-checksum.txt](evidence/fedora43-checksum.txt) and [evidence/fedora43-primary.asc](evidence/fedora43-primary.asc). Their SHA256 values are, respectively, `31d375ce5165192b0455148e8f35345c29a395c88d66c97af29ca930971a08f1` and `2b1449a082d3264dda8e18369f04e9ac4163bf3f8cb530b0783dc2ab064a08ec`.

Verification was performed in an isolated GnuPG home on the workstation and
again in an isolated GnuPG home on `pve`; both reported a good signature from
the Fedora 43 primary key. The downloaded ISO was then hashed on `pve` and
matched the signed SHA256 above. The verified source image remains at:

```text
/mnt/pve/sata-ssd/template/iso/zeusos-fixture-117/Fedora-Everything-netinst-x86_64-43-1.6.iso
```

For the interrupted VM117 attempt, the source image was remastered with the
fixture Kickstart and EFI GRUB menu and attached as:

```text
/mnt/pve/sata-ssd/template/iso/Fedora-Everything-netinst-x86_64-43-1.6-vm117-final.iso
```

Anaconda loaded the Kickstart, brought up `ens18`, created the ESP, ext4
`/boot`, Btrfs root filesystem, and sentinel disk partition, and reached RPM
installation (`349/505`) before the console session was paused. Fedora first
boot, SSH/IP validation, snapshot, and backup verification remain pending with
the parent agent; this record does not claim those steps are complete.
