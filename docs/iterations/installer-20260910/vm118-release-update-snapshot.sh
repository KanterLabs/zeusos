#!/usr/bin/env bash
# Read-only phase snapshot for the released VM118 update/rollback qualification.

set -euo pipefail

PHASE=${1:-}
case "$PHASE" in
  baseline|bridge|updated|rollback|forward) ;;
  *) echo "usage: $0 {baseline|bridge|updated|rollback|forward}" >&2; exit 64 ;;
esac

STATE=/var/lib/zeus/qualification/vm118-release-update-20260912
OUT="$STATE/$PHASE"
EXPECTED_DMI=29f40b00-bc18-4ff3-82da-1d9006d7db4c
OWNER_SENTINEL=/var/home/shane/.local/share/zeus/qualification/update-rollback-sentinel.txt

[[ $(id -u) == 0 ]] || { echo "must run as root" >&2; exit 77; }
[[ $(cat /sys/class/dmi/id/product_uuid | tr '[:upper:]' '[:lower:]') == "$EXPECTED_DMI" ]] || {
  echo "refusing unexpected VM identity" >&2
  exit 65
}

install -d -m 0755 "$OUT"
if [[ "$PHASE" == baseline && ! -e "$OWNER_SENTINEL" ]]; then
  install -d -m 0700 -o 1000 -g 1000 "${OWNER_SENTINEL%/*}"
  printf 'VM118 released update preservation sentinel, 2026-09-12\n' > "$OWNER_SENTINEL"
  chown 1000:1000 "$OWNER_SENTINEL"
  chmod 0600 "$OWNER_SENTINEL"
fi
[[ -f "$OWNER_SENTINEL" ]] || { echo "owner sentinel missing" >&2; exit 66; }

tree_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix().encode()
    if path.is_symlink():
        kind, payload = b"L", os.readlink(path).encode()
    elif path.is_file():
        kind, payload = b"F", path.read_bytes()
    else:
        continue
    digest.update(kind + b"\0" + relative + b"\0" + payload + b"\0")
print(digest.hexdigest())
PY
}

mounted_tree() {
  local device=$1 options=$2 name=$3 mountpoint
  mountpoint="$STATE/mnt-$name"
  install -d -m 0700 "$mountpoint"
  mount -o "$options" "$device" "$mountpoint"
  tree_digest "$mountpoint" > "$OUT/$name-tree.sha256"
  if [[ "$name" == fedora-boot && -d "$mountpoint/loader/entries" ]]; then
    tree_digest "$mountpoint/loader/entries" > "$OUT/fedora-bls-tree.sha256"
  fi
  findmnt --json --target "$mountpoint" > "$OUT/$name-mount.json"
  umount "$mountpoint"
}

sfdisk --json /dev/sda > "$OUT/gpt.json"
lsblk --bytes --json --output NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,PARTTYPE,START,PARTN /dev/sda > "$OUT/lsblk.json"
bootc status --json > "$OUT/bootc-status.json"
cat /proc/sys/kernel/random/boot_id > "$OUT/boot-id.txt"
cat /usr/share/zeus/build-id > "$OUT/build-id.txt"
cat /usr/share/zeus/source-commit > "$OUT/source-commit.txt"
cat /usr/share/zeus/update-sequence > "$OUT/update-sequence.txt"
sha256sum "$OWNER_SENTINEL" | awk '{print $1}' > "$OUT/zeus-home-sentinel.sha256"

TEMP_POLICY=/var/home/shane/.config/zeus/temp-policy.json
if [[ -f "$TEMP_POLICY" ]]; then
  cp --preserve=mode,timestamps "$TEMP_POLICY" "$OUT/temp-policy.json"
  sha256sum "$TEMP_POLICY" | awk '{print $1}' > "$OUT/temp-policy.sha256"
else
  printf '{"state":"default","policy_file_present":false}\n' > "$OUT/temp-policy.json"
  printf 'absent\n' > "$OUT/temp-policy.sha256"
fi

tree_digest /boot/efi > "$OUT/zeus-esp-tree.sha256"
tree_digest /boot > "$OUT/zeus-boot-tree.sha256"
findmnt --json --target /boot/efi > "$OUT/zeus-esp-mount.json"
findmnt --json --target /boot > "$OUT/zeus-boot-mount.json"

mounted_tree /dev/sda1 ro fedora-esp
mounted_tree /dev/sda2 ro fedora-boot

FEDORA_HOME="$STATE/mnt-fedora-home"
install -d -m 0700 "$FEDORA_HOME"
mount -o ro,subvol=home /dev/sda3 "$FEDORA_HOME"
for path in zeus-fixture-home-sentinel.txt zeus-fixture-data.bin; do
  if [[ -f "$FEDORA_HOME/$path" ]]; then
    sha256sum "$FEDORA_HOME/$path" | awk -v p="$path" '{print $1 "  " p}'
  else
    printf 'missing  %s\n' "$path"
  fi
done > "$OUT/fedora-home.sha256"
findmnt --json --target "$FEDORA_HOME" > "$OUT/fedora-home-mount.json"
umount "$FEDORA_HOME"

systemctl is-system-running > "$OUT/system-state.txt" || true
systemctl --failed --no-legend > "$OUT/failed-units.txt" || true
printf 'phase=%s build=%s\n' "$PHASE" "$(cat "$OUT/build-id.txt")"
