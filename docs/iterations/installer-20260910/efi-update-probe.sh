#!/bin/bash
set -Eeuo pipefail

cd /work
exec > /work/efi-update-probe.txt 2>&1

image=/work/efi-update-probe.img
[[ ! -e "$image" ]]

echo "PROBE image=$image"
echo "PROBE image_tag=localhost/zeusos:0.1.0-preview.2"
echo "PROBE bootupd_expected=0.2.35"
echo "PROBE payload_fixture=synthetic_one_byte_mutation_of_preview_shim"
echo "PROBE metadata_fixture=synthetic_future_package_versions"
echo "PROBE efi_bootability_qualification=false"
truncate -s 2G "$image"
sfdisk "$image" <<'GPT'
label: gpt
size=256M,type=U
size=256M,type=U
size=256M,type=L
type=L
GPT

for minor in $(seq 0 31); do
  [[ -e /dev/loop${minor} ]] || mknod /dev/loop${minor} b 7 "$minor"
done

probe_loop=$(losetup --find --show --partscan "$image")
[[ "$probe_loop" =~ ^/dev/loop[0-9]+$ ]]
echo "PROBE loop=$probe_loop"

cleanup_rc=0
cleanup() {
  set +e
  for target in /boot/efi /boot /fedora; do
    if findmnt -n --mountpoint "$target" >/dev/null 2>&1; then
      umount "$target" || cleanup_rc=1
    fi
  done
  loop_detached=false
  if [[ -n "${probe_loop:-}" ]]; then
    losetup -d "$probe_loop" || cleanup_rc=1
    if ! losetup "$probe_loop" >/dev/null 2>&1; then
      loop_detached=true
    fi
  else
    loop_detached=true
  fi
  echo "CLEANUP unmount_and_detach_rc=$cleanup_rc loop_detached=$loop_detached"
}
trap cleanup EXIT

for n in 1 2 3 4; do
  sysnode="/sys/class/block/$(basename "$probe_loop")p${n}/dev"
  if [[ -e "$sysnode" && ! -e "${probe_loop}p${n}" ]]; then
    IFS=: read -r major minor < "$sysnode"
    mknod "${probe_loop}p${n}" b "$major" "$minor"
  fi
  for attempt in $(seq 1 30); do
    [[ -b "${probe_loop}p${n}" ]] && break
    sleep 0.2
  done
  [[ -b "${probe_loop}p${n}" ]]
done

mkfs.vfat -n FEDORA-ESP "${probe_loop}p1"
mkfs.vfat -n ZEUS-ESP "${probe_loop}p2"
mkfs.ext4 -q -L ZEUS-BOOT "${probe_loop}p3"
mkfs.ext4 -q -L ZEUS-ROOT "${probe_loop}p4"

assert_unmounted() {
  local target=$1
  if findmnt -n --mountpoint "$target" >/dev/null 2>&1; then
    echo "ERROR target is unexpectedly mounted before setup: $target" >&2
    return 1
  fi
  echo "MOUNT preflight target=$target state=unmounted"
}

assert_loop_mount() {
  local target=$1 expected_source=$2 expected_fstype=$3
  local source fstype
  source=$(findmnt -n -o SOURCE --mountpoint "$target")
  fstype=$(findmnt -n -o FSTYPE --mountpoint "$target")
  [[ "$source" == "$expected_source" ]]
  [[ "$source" == /dev/loop* ]]
  [[ "$fstype" == "$expected_fstype" ]]
  echo "MOUNT verified target=$target source=$source fstype=$fstype"
}

mkdir -p /fedora
assert_unmounted /fedora
mount "${probe_loop}p1" /fedora
assert_loop_mount /fedora "${probe_loop}p1" vfat
mkdir -p /fedora/EFI/fedora
printf 'FEDORA EFI SENTINEL\n' > /fedora/EFI/fedora/shimx64.efi
sha256sum /fedora/EFI/fedora/shimx64.efi > /work/fedora-efi-before.sha256

mkdir -p /boot
assert_unmounted /boot
mount "${probe_loop}p3" /boot
assert_loop_mount /boot "${probe_loop}p3" ext4
mkdir -p /boot/efi

assert_unmounted /boot/efi
mount "${probe_loop}p2" /boot/efi
assert_loop_mount /boot/efi "${probe_loop}p2" vfat
echo "MOUNT preflight all disposable mounts are loop-owned before bootupd install"

zeus_uuid=$(blkid -s UUID -o value "${probe_loop}p2")
[[ "$zeus_uuid" =~ ^[0-9A-F]{4}-[0-9A-F]{4}$ ]]
echo "PROBE zeus_fat_uuid=$zeus_uuid"

echo "STEP initial bootupd backend install against the mounted Zeus ESP"
bootupctl backend install --component EFI --write-uuid /

# bootupd owns and drops its Efi mount guard at process exit.  Remount the
# disposable Zeus ESP before taking the pre-update hashes and running the
# service wrapper, while leaving the Fedora fixture mounted for its sentinel.
if ! findmnt -n --mountpoint /boot/efi >/dev/null 2>&1; then
  mount "${probe_loop}p2" /boot/efi
fi
assert_loop_mount /fedora "${probe_loop}p1" vfat
assert_loop_mount /boot "${probe_loop}p3" ext4
assert_loop_mount /boot/efi "${probe_loop}p2" vfat

zeus_shim=/boot/efi/EFI/fedora/shimx64.efi
zeus_efi_uuid=/boot/efi/EFI/fedora/bootuuid.cfg
zeus_boot_uuid=/boot/grub2/bootuuid.cfg
for path in "$zeus_shim" "$zeus_efi_uuid" "$zeus_boot_uuid"; do
  [[ -f "$path" ]]
done
sha256sum "$zeus_shim" > /work/zeus-shim-before.sha256
sha256sum "$zeus_efi_uuid" "$zeus_boot_uuid" > /work/zeus-static-uuid-before.sha256
echo "PROBE initial Zeus payload and static UUID hashes captured"

etc_mode_before=$(stat -c %a /etc)
# The pinned preview image carries /etc as 0775.  A deployed target must have
# a root-only configuration parent, so normalize this container overlay before
# creating the installed static scope file and record the image quirk.
chmod go-w /etc
etc_mode_after=$(stat -c %a /etc)
echo "PROBE image_etc_mode_before=$etc_mode_before image_etc_mode_after=$etc_mode_after"

PYTHONPATH=/work python3 - "$zeus_uuid" <<'PY'
import sys

from efi_update import write_scope_config

write_scope_config("/etc/zeus/efi-update.json", sys.argv[1])
PY

echo "STEP create a synthetic update fixture in the container overlay"
PYTHONPATH=/work python3 - <<'PY'
import json
from pathlib import Path

# This deliberately exercises bootupd's file-tree update path with a
# one-byte mutation of the image's existing preview shim.  It is not a
# released shim artifact and does not qualify firmware or OS bootability.
payload = Path("/usr/lib/efi/shim/16.1-5/EFI/fedora/shimx64.efi")
data = bytearray(payload.read_bytes())
if not data:
    raise SystemExit("EFI payload fixture is empty")
data[0] ^= 1
payload.write_bytes(data)

metadata_path = Path("/usr/lib/bootupd/updates/EFI.json")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
metadata["timestamp"] = "2099-01-01T00:00:00Z"
metadata["version"] = "grub2-1:2.12-65.fc44,shim-16.1-6"
for module in metadata.get("versions", []):
    if module.get("name") == "grub2":
        module["rpm_evr"] = "1:2.12-65.fc44"
    elif module.get("name") == "shim":
        module["rpm_evr"] = "16.1-6"
metadata_path.write_text(json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8")
PY

bootupctl status --json > /work/status-before-update.json
echo "PROBE status before scoped update:"
cat /work/status-before-update.json

# Recheck every fixture mount immediately before invoking the wrapper.  This
# proves that all writes above remain confined to the disposable loop image
# and the container overlay, with no host ESP mounted in the namespace.
assert_loop_mount /fedora "${probe_loop}p1" vfat
assert_loop_mount /boot "${probe_loop}p3" ext4
assert_loop_mount /boot/efi "${probe_loop}p2" vfat
echo "MOUNT preflight all disposable mounts are loop-owned before scoped update"

echo "STEP run the UUID-scoped wrapper"
set +e
INVOCATION_ID=efi-update-probe PYTHONPATH=/work python3 /work/efi_update.py \
  --config /etc/zeus/efi-update.json --json > /work/efi-update-wrapper.json
wrapper_rc=$?
set -e
echo "PROBE wrapper_rc=$wrapper_rc"
[[ "$wrapper_rc" -eq 0 ]]

assert_loop_mount /fedora "${probe_loop}p1" vfat
assert_loop_mount /boot "${probe_loop}p3" ext4
assert_loop_mount /boot/efi "${probe_loop}p2" vfat
sha256sum --check /work/fedora-efi-before.sha256
sha256sum --check /work/zeus-static-uuid-before.sha256
sha256sum "$zeus_shim" > /work/zeus-shim-after.sha256
before_shim=$(cut -d' ' -f1 /work/zeus-shim-before.sha256)
after_shim=$(cut -d' ' -f1 /work/zeus-shim-after.sha256)
[[ "$before_shim" != "$after_shim" ]]
grep -q '"mounted_for_update": false' /work/efi-update-wrapper.json

PYTHONPATH=/work python3 - "$zeus_uuid" "$probe_loop" "$before_shim" "$after_shim" "$etc_mode_before" "$etc_mode_after" <<'PY'
import json
from pathlib import Path
import sys

wrapper = json.loads(Path("/work/efi-update-wrapper.json").read_text(encoding="utf-8"))
status = json.loads(Path("/work/status-before-update.json").read_text(encoding="utf-8"))
payload = {
    "date": "2026-09-10",
    "environment": "VM116 dedicated builder; disposable 2GiB loop image in isolated privileged container",
    "image": "localhost/zeusos:0.1.0-preview.2",
    "bootupd": "0.2.35",
    "layout": "first ESP Fedora sentinel; second ESP Zeus target; one backing loop disk",
    "payload_fixture": "synthetic one-byte mutation of the preview image shim",
    "metadata_fixture": {
        "kind": "synthetic future package versions",
        "base_version": "grub2-1:2.12-64.fc44,shim-16.1-5",
        "fixture_version": "grub2-1:2.12-65.fc44,shim-16.1-6",
        "timestamp": "2099-01-01T00:00:00Z",
    },
    "released_efi_upgrade_tested": False,
    "efi_bootability_qualified": False,
    "loop_device": sys.argv[2],
    "zeus_esp_fat_uuid": sys.argv[1],
    "wrapper_command": ["/usr/bin/python3", "/work/efi_update.py", "--config", "/etc/zeus/efi-update.json", "--json"],
    "wrapper_result": wrapper,
    "status_before_update": status,
    "fedora_sentinel_sha256_check": "passed",
    "zeus_static_uuid_sha256_check": "passed",
    "zeus_shim_sha256_before": sys.argv[3],
    "zeus_shim_sha256_after": sys.argv[4],
    "zeus_payload_changed": True,
    "all_preflight_mounts_loop_owned": True,
    "host_efi_mounted": False,
    "image_etc_mode_before": sys.argv[5],
    "image_etc_mode_after": sys.argv[6],
    "firmware_changes_requested": False,
    "cleanup": "see efi-update-probe.txt CLEANUP line",
}
Path("/work/efi-update-probe.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

sync
echo "PROBE PASS Fedora sentinel and static UUID files unchanged; Zeus EFI payload changed"
