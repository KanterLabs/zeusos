#!/usr/bin/env bash
# VM117-only qualification probe for a synthetic bootc deployment update.
#
# This file is an evidence helper, not part of the installed OS.  It is copied
# to /var/lib/zeus on the disposable VM and run as root in separate phases so
# that the reboot boundaries are explicit.  The candidate archive contains
# only a marker added to the pinned preview image; it is not a release.

set -u
set -o pipefail

STATE=/var/lib/zeus/vm117-update-rollback-20260910
EXPORT="$STATE/export"
BACKUP="$STATE/backup"
CANDIDATE=/var/lib/zeus/updater/downloads/zeusos-vm117-synthetic-update-20260910.oci
USER_FILE=/var/home/shane/vm117-update-rollback-user.txt
CONFIG=/etc/zeus/efi-update.json
ESP_MOUNT=/boot/efi
FEDORA_ESP=/dev/sda1
FEDORA_BOOT=/dev/sda2
ZEUS_ESP=/dev/sda4
ZEUS_BOOT=/dev/sda5
ZEUS_ROOT=/dev/sda6
MARKER=/usr/share/zeus/installer-validation
MARKER_TEXT='VM117 synthetic OS update marker; not a release artifact'

SCOPE_FILES=(
  /etc/zeus/efi_update.py
  /etc/zeus/efi-update.json
  /etc/systemd/system/bootloader-update.service.d/zeus-efi.conf
)

mkdir -p "$STATE" "$EXPORT" "$BACKUP" 2>/dev/null || exit 70
chmod 0755 "$STATE" 2>/dev/null || exit 70
chmod 0700 "$BACKUP" 2>/dev/null || exit 70
chmod 0755 "$EXPORT" 2>/dev/null || exit 70

say() {
  # Phase output is intentionally limited to booleans, paths, and return
  # codes.  Digest-bearing status and manifest files are exported separately.
  printf '%s\n' "$*"
}

copy_export() {
  local src=$1
  local dest=$2
  local parent
  parent=${dest%/*}
  mkdir -p "$parent" 2>/dev/null || return 1
  chmod 0755 "$parent" 2>/dev/null || true
  install -m 0644 "$src" "$dest" 2>/dev/null || return 1
  return 0
}

file_digest() {
  local src=$1
  local dest=$2
  /usr/bin/sha256sum "$src" 2>/dev/null | /usr/bin/awk '{print $1}' > "$dest"
}

tree_digest() {
  local root=$1
  local dest=$2
  python3 - "$root" > "$dest" <<'PY'
import hashlib
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
entries = []
for path in root.rglob("*"):
    relative = path.relative_to(root).as_posix().encode()
    if path.is_symlink():
        payload = os.readlink(path).encode()
        entries.append((b"L", relative, payload))
    elif path.is_file():
        entries.append((b"F", relative, path.read_bytes()))

digest = hashlib.sha256()
for kind, relative, payload in sorted(entries):
    digest.update(kind)
    digest.update(b"\0")
    digest.update(relative)
    digest.update(b"\0")
    digest.update(payload)
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

mount_at_target() {
  local target=$1
  local mounted_target
  mounted_target=$(/usr/bin/findmnt -rn -o TARGET --target "$target" 2>/dev/null || true)
  [[ "$mounted_target" == "$target" ]]
}

read_config_uuid() {
  python3 - "$CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)["esp_uuid"]
print(str(value).upper())
PY
}

write_status_summary() {
  local status_file=$1
  local summary_file=$2
  python3 - "$status_file" "$summary_file" <<'PY'
import json
from pathlib import Path
import sys

status_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
try:
    document = json.loads(status_path.read_text(encoding="utf-8"))
    root = document.get("status", document)
except Exception:
    summary_path.write_text("status_json_valid=false\n", encoding="utf-8")
    raise SystemExit(0)

def normalize(key):
    return str(key).replace("_", "").replace("-", "").lower()

def find_key(value, names):
    if isinstance(value, dict):
        for key, child in value.items():
            if normalize(key) in names:
                return True, child
        for child in value.values():
            found, result = find_key(child, names)
            if found:
                return True, result
    elif isinstance(value, list):
        for child in value:
            found, result = find_key(child, names)
            if found:
                return True, result
    return False, None

def truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none", "null"}
    return bool(value)

def present(names):
    found, value = find_key(root, names)
    return found and value is not None and truthy(value)

booted_found, booted = find_key(root, {"booted"})
staged_found, staged = find_key(root, {"staged"})
rollback_found, rollback = find_key(root, {"rollback"})
queued_found, queued = find_key(root, {"rollbackqueued"})
lines = [
    "status_json_valid=true",
    f"booted_present={str(booted_found and booted is not None).lower()}",
    f"staged_present={str(staged_found and staged is not None and truthy(staged)).lower()}",
    f"rollback_present={str(rollback_found and rollback is not None and truthy(rollback)).lower()}",
    f"rollback_queued={str(queued_found and truthy(queued)).lower()}",
]
summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

snapshot_fedora_tree() {
  local phase=$1
  local label dev fstype mountpoint details source actual_type options ok
  local overall=0
  for label in fedora-esp fedora-boot; do
    if [[ "$label" == fedora-esp ]]; then
      dev=$FEDORA_ESP
      fstype=vfat
    else
      dev=$FEDORA_BOOT
      fstype=ext4
    fi
    mountpoint="$STATE/mnt-$label"
    mkdir -p "$mountpoint" 2>/dev/null || overall=1
    if mount_at_target "$mountpoint"; then
      overall=1
    fi
    if ! /usr/bin/mount -o ro "$dev" "$mountpoint" > "$STATE/mount-$phase-$label.log" 2>&1; then
      overall=1
      continue
    fi
    details=$(/usr/bin/findmnt -rn -o SOURCE,FSTYPE,OPTIONS --target "$mountpoint" 2>/dev/null || true)
    printf '%s\n' "$details" > "$STATE/mount-$phase-$label.details"
    source=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $1}')
    actual_type=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $2}')
    options=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $3}')
    [[ "$source" == "$dev" ]] || overall=1
    [[ "$actual_type" == "$fstype" ]] || overall=1
    case ",$options," in *,ro,*) ;; *) overall=1 ;; esac
    if ! tree_digest "$mountpoint" "$STATE/$phase-$label-tree.sha256"; then
      overall=1
    fi
    if [[ "$label" == fedora-boot ]]; then
      if [[ -d "$mountpoint/loader/entries" ]]; then
        tree_digest "$mountpoint/loader/entries" "$STATE/$phase-fedora-bls-tree.sha256" || overall=1
        printf 'true\n' > "$STATE/$phase-fedora-bls-present"
      else
        printf 'absent\n' > "$STATE/$phase-fedora-bls-tree.sha256"
        printf 'false\n' > "$STATE/$phase-fedora-bls-present"
        overall=1
      fi
    fi
    if ! /usr/bin/umount "$mountpoint" > "$STATE/umount-$phase-$label.log" 2>&1; then
      overall=1
    fi
    if mount_at_target "$mountpoint"; then
      overall=1
    fi
    rmdir "$mountpoint" 2>/dev/null || true
  done
  return "$overall"
}

snapshot_zeus_esp() {
  local phase=$1
  local details source actual_type uuid expected_uuid
  details=$(/usr/bin/findmnt -rn -o SOURCE,FSTYPE,UUID --target "$ESP_MOUNT" 2>/dev/null || true)
  printf '%s\n' "$details" > "$STATE/$phase-zeus-esp-mount.details"
  source=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $1}')
  actual_type=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $2}')
  uuid=$(printf '%s\n' "$details" | /usr/bin/awk 'NR == 1 {print $3}')
  expected_uuid=$(cat "$STATE/esp-uuid.txt" 2>/dev/null || true)
  [[ "$source" == "$ZEUS_ESP" ]] || return 1
  [[ "$actual_type" == vfat ]] || return 1
  [[ "${uuid^^}" == "${expected_uuid^^}" ]] || return 1
  tree_digest "$ESP_MOUNT" "$STATE/$phase-zeus-esp-tree.sha256" || return 1
  return 0
}

scope_check() {
  local src base expected_meta actual_meta
  for src in "${SCOPE_FILES[@]}"; do
    base=${src##*/}
    cmp -s "$BACKUP/$base" "$src" || return 1
    expected_meta=$(cat "$STATE/baseline-$base.meta" 2>/dev/null || true)
    actual_meta=$(stat -c '%u:%g:%a' "$src" 2>/dev/null || true)
    [[ "$actual_meta" == "$expected_meta" ]] || return 1
  done
  cmp -s "$BACKUP/user-file" "$USER_FILE" || return 1
  expected_meta=$(cat "$STATE/baseline-user-file.meta" 2>/dev/null || true)
  actual_meta=$(stat -c '%u:%g:%a' "$USER_FILE" 2>/dev/null || true)
  [[ "$actual_meta" == "$expected_meta" ]] || return 1
  return 0
}

service_check() {
  local phase=$1
  local rc=0
  /usr/bin/systemctl cat bootloader-update.service > "$STATE/service-$phase.cat" 2>/dev/null || rc=1
  /usr/bin/systemctl show bootloader-update.service \
    -p LoadState -p ActiveState -p SubState -p ExecMainStatus \
    > "$STATE/service-$phase.show" 2>/dev/null || rc=1
  /usr/bin/grep -Fq '/etc/zeus/efi_update.py --config /etc/zeus/efi-update.json' "$STATE/service-$phase.cat" || rc=1
  /usr/bin/grep -Fq 'RequiresMountsFor=/boot/efi' "$STATE/service-$phase.cat" || rc=1
  /usr/bin/grep -Fq 'MountFlags=slave' "$STATE/service-$phase.cat" || rc=1
  /usr/bin/grep -Fxq 'LoadState=loaded' "$STATE/service-$phase.show" || rc=1
  /usr/bin/grep -Fxq 'ActiveState=active' "$STATE/service-$phase.show" || rc=1
  /usr/bin/grep -Fxq 'SubState=exited' "$STATE/service-$phase.show" || rc=1
  /usr/bin/grep -Fxq 'ExecMainStatus=0' "$STATE/service-$phase.show" || rc=1
  return "$rc"
}

safe_copy_phase_evidence() {
  local phase=$1
  local source base
  for source in \
    "$STATE/bootc-status-$phase.json" \
    "$STATE/bootc-status-$phase.summary" \
    "$STATE/$phase-result.json" \
    "$STATE/$phase-zeus-esp-tree.sha256" \
    "$STATE/$phase-fedora-esp-tree.sha256" \
    "$STATE/$phase-fedora-boot-tree.sha256" \
    "$STATE/$phase-fedora-bls-tree.sha256" \
    "$STATE/$phase-fedora-bls-present" \
    "$STATE/$phase-zeus-esp-mount.details" \
    "$STATE/service-$phase.cat" \
    "$STATE/service-$phase.show"; do
    if [[ -f "$source" ]]; then
      base=${source##*/}
      copy_export "$source" "$EXPORT/$base" || true
    fi
  done
}

phase_baseline() {
  local rc=0 src base meta
  if [[ -f "$STATE/baseline.complete" ]]; then
    say 'phase=baseline already_complete=true'
    return 1
  fi
  [[ "$(id -u)" == 0 ]] || rc=1
  [[ -f "$CANDIDATE" ]] || rc=1
  [[ ! -e "$MARKER" ]] || rc=1
  [[ ! -e "$USER_FILE" ]] || rc=1
  if [[ "$rc" == 0 ]]; then
    printf '%s\n' "$MARKER_TEXT" > "$USER_FILE" 2>/dev/null || rc=1
    chown 1000:1000 "$USER_FILE" 2>/dev/null || rc=1
    chmod 0600 "$USER_FILE" 2>/dev/null || rc=1
  fi
  for src in "${SCOPE_FILES[@]}"; do
    base=${src##*/}
    [[ -f "$src" ]] || rc=1
    if [[ -f "$src" ]]; then
      meta=$(stat -c '%u:%g:%a' "$src" 2>/dev/null || true)
      printf '%s\n' "$meta" > "$STATE/baseline-$base.meta"
      install -m 0644 "$src" "$BACKUP/$base" 2>/dev/null || rc=1
    fi
  done
  if [[ -f "$USER_FILE" ]]; then
    stat -c '%u:%g:%a' "$USER_FILE" > "$STATE/baseline-user-file.meta" 2>/dev/null || rc=1
    install -m 0644 "$USER_FILE" "$BACKUP/user-file" 2>/dev/null || rc=1
  fi
  if ! uuid=$(read_config_uuid 2>/dev/null); then
    rc=1
  else
    printf '%s\n' "$uuid" > "$STATE/esp-uuid.txt"
  fi
  file_digest "$CANDIDATE" "$STATE/candidate.sha256" || rc=1
  stat -c '%s' "$CANDIDATE" > "$STATE/candidate.size" 2>/dev/null || rc=1
  tree_digest /boot "$STATE/zeus-boot-before-tree.sha256" || rc=1
  snapshot_zeus_esp baseline || rc=1
  snapshot_fedora_tree baseline || rc=1
  /usr/bin/bootc status --json > "$STATE/bootc-status-before.json" 2> "$STATE/bootc-status-before.err"
  [[ "$?" == 0 ]] || rc=1
  write_status_summary "$STATE/bootc-status-before.json" "$STATE/bootc-status-before.summary" || rc=1
  /usr/bin/systemctl cat bootloader-update.service > "$STATE/service-baseline.cat" 2>/dev/null || rc=1
  /usr/bin/systemctl show bootloader-update.service \
    -p LoadState -p ActiveState -p SubState -p ExecMainStatus \
    > "$STATE/service-baseline.show" 2>/dev/null || rc=1
  for src in "${SCOPE_FILES[@]}" "$USER_FILE"; do
    base=${src##*/}
    file_digest "$src" "$STATE/baseline-$base.sha256" || rc=1
  done
  for src in "$STATE"/baseline-*.sha256 "$STATE"/baseline-*.meta "$STATE"/esp-uuid.txt \
    "$STATE"/candidate.sha256 "$STATE"/candidate.size "$STATE"/zeus-boot-before-tree.sha256 \
    "$STATE"/baseline-zeus-esp-tree.sha256 "$STATE"/baseline-fedora-esp-tree.sha256 \
    "$STATE"/baseline-fedora-boot-tree.sha256 "$STATE"/baseline-fedora-bls-tree.sha256 \
    "$STATE"/baseline-fedora-bls-present; do
    [[ -f "$src" ]] || continue
    base=${src##*/}
    copy_export "$src" "$EXPORT/$base" || rc=1
  done
  local backup_base
  for src in "${SCOPE_FILES[@]}" "$USER_FILE"; do
    base=${src##*/}
    backup_base=$base
    [[ "$src" == "$USER_FILE" ]] && backup_base=user-file
    copy_export "$BACKUP/$backup_base" "$EXPORT/backup/$backup_base" || rc=1
  done
  copy_export "$STATE/bootc-status-before.json" "$EXPORT/bootc-status-before.json" || rc=1
  copy_export "$STATE/service-baseline.cat" "$EXPORT/service-baseline.cat" || rc=1
  copy_export "$STATE/service-baseline.show" "$EXPORT/service-baseline.show" || rc=1
  if [[ "$rc" == 0 ]]; then
    printf '%s\n' complete > "$STATE/baseline.complete"
    copy_export "$STATE/baseline.complete" "$EXPORT/baseline.complete" || rc=1
  fi
  local complete=false
  [[ "$rc" == 0 ]] && complete=true
  say "phase=baseline complete=$complete"
  return "$rc"
}

phase_stage() {
  local switch_rc=0 status_rc=0
  [[ -f "$STATE/baseline.complete" ]] || { say 'phase=stage baseline_complete=false'; return 1; }
  /usr/bin/bootc switch --transport oci-archive --retain "$CANDIDATE" 2>&1 \
      | sed -E 's/sha256:[0-9a-fA-F]{64}/sha256:<digest-redacted>/g' > "$STATE/switch-scrubbed.log"
  switch_rc=${PIPESTATUS[0]}
  # The command's output was captured only through the redacting filters.
  copy_export "$STATE/switch-scrubbed.log" "$EXPORT/switch-scrubbed.log" || true
  /usr/bin/bootc status --json > "$STATE/bootc-status-stage.json" 2> "$STATE/bootc-status-stage.err"
  status_rc=$?
  write_status_summary "$STATE/bootc-status-stage.json" "$STATE/bootc-status-stage.summary" || status_rc=1
  safe_copy_phase_evidence stage
  local staged=false valid=false
  /usr/bin/grep -Fxq 'status_json_valid=true' "$STATE/bootc-status-stage.summary" && valid=true
  /usr/bin/grep -Fxq 'staged_present=true' "$STATE/bootc-status-stage.summary" && staged=true
  printf '%s\n' "$switch_rc" > "$STATE/switch.rc"
  copy_export "$STATE/switch.rc" "$EXPORT/switch.rc" || true
  say "phase=stage switch_rc=$switch_rc status_rc=$status_rc status_json_valid=$valid staged_present=$staged"
  [[ "$switch_rc" == 0 && "$status_rc" == 0 && "$valid" == true && "$staged" == true ]]
}

phase_reboot() {
  say 'phase=reboot requested=true'
  /usr/bin/systemctl reboot > /dev/null 2>&1
  local rc=$?
  say "phase=reboot systemctl_rc=$rc"
  return 0
}

write_result() {
  local phase=$1
  shift
  {
    printf '{\n'
    local first=true pair key value
    for pair in "$@"; do
      key=${pair%%=*}
      value=${pair#*=}
      if [[ "$first" == true ]]; then first=false; else printf ',\n'; fi
      printf '  "%s": %s' "$key" "$value"
    done
    printf '\n}\n'
  } > "$STATE/$phase-result.json"
  copy_export "$STATE/$phase-result.json" "$EXPORT/$phase-result.json" || true
}

phase_post_update() {
  local rc=0 status_rc=0 fedora_rc=0
  [[ -f "$STATE/baseline.complete" ]] || rc=1
  /usr/bin/bootc status --json > "$STATE/bootc-status-post-update.json" 2> "$STATE/bootc-status-post-update.err"
  status_rc=$?
  write_status_summary "$STATE/bootc-status-post-update.json" "$STATE/bootc-status-post-update.summary" || status_rc=1
  local status_valid=false rollback_present=false staged_present=true queued=true
  /usr/bin/grep -Fxq 'status_json_valid=true' "$STATE/bootc-status-post-update.summary" && status_valid=true
  /usr/bin/grep -Fxq 'rollback_present=true' "$STATE/bootc-status-post-update.summary" && rollback_present=true
  /usr/bin/grep -Fxq 'staged_present=true' "$STATE/bootc-status-post-update.summary" && staged_present=true || staged_present=false
  /usr/bin/grep -Fxq 'rollback_queued=true' "$STATE/bootc-status-post-update.summary" && queued=true || queued=false
  local marker_present=false
  [[ -f "$MARKER" ]] && /usr/bin/grep -Fxq "$MARKER_TEXT" "$MARKER" && marker_present=true
  [[ "$marker_present" == true ]] || rc=1
  local scope_preserved=false
  scope_check && scope_preserved=true || rc=1
  local zeus_esp_mounted=false
  snapshot_zeus_esp post-update && zeus_esp_mounted=true || rc=1
  tree_digest /boot "$STATE/post-update-zeus-boot-tree.sha256" || rc=1
  snapshot_fedora_tree post-update && fedora_rc=0 || { fedora_rc=1; rc=1; }
  local fedora_esp_unchanged=false fedora_boot_unchanged=false fedora_bls_unchanged=false
  cmp -s "$STATE/baseline-fedora-esp-tree.sha256" "$STATE/post-update-fedora-esp-tree.sha256" && fedora_esp_unchanged=true
  cmp -s "$STATE/baseline-fedora-boot-tree.sha256" "$STATE/post-update-fedora-boot-tree.sha256" && fedora_boot_unchanged=true
  cmp -s "$STATE/baseline-fedora-bls-tree.sha256" "$STATE/post-update-fedora-bls-tree.sha256" && fedora_bls_unchanged=true
  [[ "$fedora_esp_unchanged" == true && "$fedora_boot_unchanged" == true && "$fedora_bls_unchanged" == true ]] || rc=1
  local service_ok=false
  service_check post-update && service_ok=true || rc=1
  write_result post-update \
    "status_json_valid=$status_valid" \
    "rollback_present=$rollback_present" \
    "staged_present=$staged_present" \
    "rollback_queued=$queued" \
    "synthetic_marker_present=$marker_present" \
    "scope_and_user_preserved=$scope_preserved" \
    "zeus_esp_mounted=$zeus_esp_mounted" \
    "fedora_esp_unchanged=$fedora_esp_unchanged" \
    "fedora_boot_unchanged=$fedora_boot_unchanged" \
    "fedora_bls_unchanged=$fedora_bls_unchanged" \
    "bootloader_update_service_ok=$service_ok"
  safe_copy_phase_evidence post-update
  say "phase=post-update status_rc=$status_rc status_json_valid=$status_valid synthetic_marker_present=$marker_present scope_and_user_preserved=$scope_preserved zeus_esp_mounted=$zeus_esp_mounted fedora_esp_unchanged=$fedora_esp_unchanged fedora_bls_unchanged=$fedora_bls_unchanged service_ok=$service_ok"
  [[ "$rc" == 0 && "$status_rc" == 0 && "$fedora_rc" == 0 ]]
}

phase_queue_rollback() {
  local rollback_rc=0 status_rc=0
  /usr/bin/bootc rollback 2>&1 \
      | sed -E 's/sha256:[0-9a-fA-F]{64}/sha256:<digest-redacted>/g' > "$STATE/rollback-scrubbed.log"
  rollback_rc=${PIPESTATUS[0]}
  copy_export "$STATE/rollback-scrubbed.log" "$EXPORT/rollback-scrubbed.log" || true
  /usr/bin/bootc status --json > "$STATE/bootc-status-rollback-queued.json" 2> "$STATE/bootc-status-rollback-queued.err"
  status_rc=$?
  write_status_summary "$STATE/bootc-status-rollback-queued.json" "$STATE/bootc-status-rollback-queued.summary" || status_rc=1
  safe_copy_phase_evidence rollback-queued
  local queued=false valid=false
  /usr/bin/grep -Fxq 'status_json_valid=true' "$STATE/bootc-status-rollback-queued.summary" && valid=true
  /usr/bin/grep -Fxq 'rollback_queued=true' "$STATE/bootc-status-rollback-queued.summary" && queued=true
  say "phase=queue-rollback rollback_rc=$rollback_rc status_rc=$status_rc status_json_valid=$valid rollback_queued=$queued"
  [[ "$rollback_rc" == 0 && "$status_rc" == 0 && "$valid" == true && "$queued" == true ]]
}

phase_post_rollback() {
  local rc=0 status_rc=0 fedora_rc=0
  /usr/bin/bootc status --json > "$STATE/bootc-status-post-rollback.json" 2> "$STATE/bootc-status-post-rollback.err"
  status_rc=$?
  write_status_summary "$STATE/bootc-status-post-rollback.json" "$STATE/bootc-status-post-rollback.summary" || status_rc=1
  local status_valid=false rollback_present=false staged_present=true queued=true
  /usr/bin/grep -Fxq 'status_json_valid=true' "$STATE/bootc-status-post-rollback.summary" && status_valid=true
  /usr/bin/grep -Fxq 'rollback_present=true' "$STATE/bootc-status-post-rollback.summary" && rollback_present=true
  /usr/bin/grep -Fxq 'staged_present=true' "$STATE/bootc-status-post-rollback.summary" && staged_present=true || staged_present=false
  /usr/bin/grep -Fxq 'rollback_queued=true' "$STATE/bootc-status-post-rollback.summary" && queued=true || queued=false
  local marker_absent=false
  [[ ! -e "$MARKER" ]] && marker_absent=true
  [[ "$marker_absent" == true ]] || rc=1
  local scope_preserved=false
  scope_check && scope_preserved=true || rc=1
  local zeus_esp_mounted=false
  snapshot_zeus_esp post-rollback && zeus_esp_mounted=true || rc=1
  tree_digest /boot "$STATE/post-rollback-zeus-boot-tree.sha256" || rc=1
  snapshot_fedora_tree post-rollback && fedora_rc=0 || { fedora_rc=1; rc=1; }
  local fedora_esp_unchanged=false fedora_boot_unchanged=false fedora_bls_unchanged=false
  cmp -s "$STATE/baseline-fedora-esp-tree.sha256" "$STATE/post-rollback-fedora-esp-tree.sha256" && fedora_esp_unchanged=true
  cmp -s "$STATE/baseline-fedora-boot-tree.sha256" "$STATE/post-rollback-fedora-boot-tree.sha256" && fedora_boot_unchanged=true
  cmp -s "$STATE/baseline-fedora-bls-tree.sha256" "$STATE/post-rollback-fedora-bls-tree.sha256" && fedora_bls_unchanged=true
  [[ "$fedora_esp_unchanged" == true && "$fedora_boot_unchanged" == true && "$fedora_bls_unchanged" == true ]] || rc=1
  local service_ok=false
  service_check post-rollback && service_ok=true || rc=1
  write_result post-rollback \
    "status_json_valid=$status_valid" \
    "rollback_present=$rollback_present" \
    "staged_present=$staged_present" \
    "rollback_queued=$queued" \
    "synthetic_marker_absent=$marker_absent" \
    "scope_and_user_preserved=$scope_preserved" \
    "zeus_esp_mounted=$zeus_esp_mounted" \
    "fedora_esp_unchanged=$fedora_esp_unchanged" \
    "fedora_boot_unchanged=$fedora_boot_unchanged" \
    "fedora_bls_unchanged=$fedora_bls_unchanged" \
    "bootloader_update_service_ok=$service_ok"
  safe_copy_phase_evidence post-rollback
  say "phase=post-rollback status_rc=$status_rc status_json_valid=$status_valid synthetic_marker_absent=$marker_absent scope_and_user_preserved=$scope_preserved zeus_esp_mounted=$zeus_esp_mounted fedora_esp_unchanged=$fedora_esp_unchanged fedora_bls_unchanged=$fedora_bls_unchanged service_ok=$service_ok"
  [[ "$rc" == 0 && "$status_rc" == 0 && "$fedora_rc" == 0 ]]
}

phase_cleanup() {
  local rc=0
  [[ ! -e "$MARKER" ]] || rc=1
  [[ -e "$USER_FILE" ]] && rm -f "$USER_FILE" || true
  [[ ! -e "$USER_FILE" ]] || rc=1
  [[ -e "$CANDIDATE" ]] && rm -f "$CANDIDATE" || true
  [[ ! -e "$CANDIDATE" ]] || rc=1
  printf '%s\n' "$rc" > "$STATE/cleanup.rc"
  copy_export "$STATE/cleanup.rc" "$EXPORT/cleanup.rc" || true
  local user_removed=false candidate_removed=false
  [[ ! -e "$USER_FILE" ]] && user_removed=true
  [[ ! -e "$CANDIDATE" ]] && candidate_removed=true
  say "phase=cleanup synthetic_user_removed=$user_removed candidate_removed_vm117=$candidate_removed"
  return "$rc"
}

phase=${1:-}
case "$phase" in
  baseline) phase_baseline ;;
  stage) phase_stage ;;
  reboot) phase_reboot ;;
  post-update) phase_post_update ;;
  queue-rollback) phase_queue_rollback ;;
  post-rollback) phase_post_rollback ;;
  cleanup) phase_cleanup ;;
  *) say 'usage=baseline|stage|reboot|post-update|queue-rollback|post-rollback|cleanup'; exit 64 ;;
esac
