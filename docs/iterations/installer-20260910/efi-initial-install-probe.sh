#!/bin/bash
set -euo pipefail
cd /work
[[ ! -e efi-probe.img ]]
truncate -s 2G efi-probe.img
sfdisk efi-probe.img <<'GPT'
label: gpt
size=256M,type=U
size=256M,type=U
size=256M,type=L
type=L
GPT
for minor in $(seq 0 31); do
  [[ -e /dev/loop${minor} ]] || mknod /dev/loop${minor} b 7 "$minor"
done
probe_loop=$(losetup --find --show --partscan /work/efi-probe.img)
cleanup() {
  umount /target/boot/efi /target/boot /target /fedora 2>/dev/null || true
  losetup -d "$probe_loop"
}
trap cleanup EXIT
[[ "$probe_loop" =~ ^/dev/loop[0-9]+$ ]]
for n in 1 2 3 4; do
  sysnode="/sys/class/block/$(basename "$probe_loop")p${n}/dev"
  if [[ -e "$sysnode" && ! -e "${probe_loop}p${n}" ]]; then
    IFS=: read -r major minor < "$sysnode"
    mknod "${probe_loop}p${n}" b "$major" "$minor"
  fi
  for attempt in $(seq 1 20); do
    [[ -b "${probe_loop}p${n}" ]] && break
    sleep 0.2
  done
done
mkfs.vfat "${probe_loop}p1"
mkfs.vfat "${probe_loop}p2"
mkfs.ext4 -q "${probe_loop}p3"
mkfs.ext4 -q "${probe_loop}p4"
mkdir -p /target /fedora
mount "${probe_loop}p1" /fedora
mkdir -p /fedora/EFI/fedora
printf 'FEDORA EFI SENTINEL\n' > /fedora/EFI/fedora/shimx64.efi
sha256sum /fedora/EFI/fedora/shimx64.efi > /work/fedora-efi-before.sha256
mount "${probe_loop}p4" /target
mkdir -p /target/boot
mount "${probe_loop}p3" /target/boot
mkdir -p /target/boot/efi
mount "${probe_loop}p2" /target/boot/efi
bootupctl backend install --component EFI --write-uuid /target
sha256sum --check /work/fedora-efi-before.sha256
mount "${probe_loop}p2" /target/boot/efi
find /target/boot -maxdepth 4 -type f | sort
findmnt -J /target/boot/efi
sync
