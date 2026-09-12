#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Qualify the systemd-sysext lifecycle on one already-installed, disposable
# Fedora 44 Zeus guest.  This helper deliberately never starts a VM, reboots,
# stages an image, or calls the updater.  The operator owns the reboot between
# prepare and verify.

set -Eeuo pipefail
IFS=$'\n\t'

SCHEMA='zeus-developer-sysext-qualification-v1'
SCRIPT_VERSION='1'
EXTENSION_NAME='zeus-qualification-fixture'
EXTENSION_PARENT='/var/lib/extensions'
EXTENSION_ROOT="${EXTENSION_PARENT}/${EXTENSION_NAME}"
BAD_VERSION_NAME="${EXTENSION_NAME}-bad-version"
BAD_LEVEL_NAME="${EXTENSION_NAME}-bad-level"
SENTINEL='/usr/share/zeus/developer-mode-qualification-sentinel'
OVERRIDE_CONTENT='Zeus Developer Mode systemd-sysext qualification override v1'
STATE_DIR_DEFAULT='/var/lib/zeus/qualification/developer-sysext'
STATE_DIR_ROOT='/var/lib/zeus/qualification'
UNIT_NAME='zeus-developer-sysext-qualification.service'
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
PROTECTED_PREFIXES=$'/home\n/var/home\n/var/lib/zeus/updates\n/boot\n/boot/efi\n/efi\n/var/lib/bootc\n/usr\n/etc\n/proc\n/sys\n/dev'

MODE='prepare'
DRY_RUN=0
OUTPUT=''
OUTPUT_EXPLICIT=0
STATE_DIR="$STATE_DIR_DEFAULT"
IDLE_SETTLE_SECONDS=5
IDLE_SECONDS=10
IDLE_INTERVAL_SECONDS=2

RUN_TMP=''
STATE_JSON=''
LOCK_PATH=''
BOOT_ID=''
PRE_BOOT_ID=''
CURRENT_BOOT_ID=''
BASE_ID=''
BASE_VERSION_ID=''
BASE_SYSEXT_LEVEL=''
BASE_ARCHITECTURE=''
BASE_SENTINEL_HASH=''
BASE_SENTINEL_MODE=''
BASE_SENTINEL_UID=''
BASE_SENTINEL_GID=''
BASE_SENTINEL_CONTEXT=''
BASE_LIST_HASH=''
SELINUX_MODE=''
SYSTEMD_VERSION=''
SYSTEMD_SYSEXT=''
GETENFORCE=''
RESTORECON=''
ZEUS_CLI=''
SAFE_DESKTOP_HELPER='/usr/libexec/zeus-desktop-safe'
SAFE_DESKTOP_OUTPUT_SHA256=''
SAFE_DESKTOP_HELPER_SHA256=''
SAFE_DESKTOP_HELPER_CONTEXT=''
SAFE_DESKTOP_CHECKED=0
CUSTOM_UNIT_CREATED=0
EXTENSIONS_PARENT_CREATED=0
EXTENSION_PRESENT=0
SYSEXT_MAY_BE_MERGED=0
CLEANUP_DONE=0
KEEP_FIXTURE=0
FINISHED=0
ERROR_CODE=''
ERROR_MESSAGE=''
RESULT='failed'
REBOOT_OBSERVED=0
BOOT_ACTIVATION_SOURCE=''
OPERATION_START_NS=0
VERIFY_START_NS=0
AVC_OPERATION_COUNT=0
AVC_BOOT_COUNT=0
LAST_RETURN_CODE=0
CURRENT_PHASE=''
COMMAND_INDEX=0

usage() {
    cat <<'EOF'
Usage: qualify-developer-sysext.sh [OPTIONS]

Qualify the disposable Fedora 44 Zeus systemd-sysext fixture.  Run
--mode prepare, reboot the disposable guest through the operator's VM
workflow, then run --mode verify.  This script never reboots the guest.

Options:
  --mode MODE                    prepare or verify (default: prepare)
  --output PATH                  root-owned JSON evidence path, or - for stdout
  --state-dir PATH               root-owned phase state directory
  --idle-settle-seconds N        idle settle period, 0..300 (default: 5)
  --idle-seconds N               idle measurement period, 0..300 (default: 10)
  --sample-interval-seconds N    idle sample spacing, 1..60 (default: 2)
  --dry-run                      print the contract without touching the guest
  --help                         show this help
  --version                      show the fixture version
EOF
}

die() {
    ERROR_CODE=$1
    ERROR_MESSAGE=$2
    printf 'qualify-developer-sysext: %s: %s\n' "$ERROR_CODE" "$ERROR_MESSAGE" >&2
    exit 1
}

trim_newlines() {
    local value=$1
    value=${value//$'\n'/}
    value=${value//$'\r'/}
    printf '%s' "$value"
}

is_bad_prefix() {
    local candidate=$1
    case "$candidate" in
        /home|/home/*|/var/home|/var/home/*|/var/lib/zeus/updates|/var/lib/zeus/updates/*|\
        /boot|/boot/*|/boot/efi|/boot/efi/*|/efi|/efi/*|/var/lib/bootc|/var/lib/bootc/*|\
        /proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/usr|/usr/*|/etc|/etc/*)
            return 0
            ;;
    esac
    return 1
}

validate_path_argument() {
    local label=$1
    local candidate=$2
    [[ "$candidate" == /* ]] || die invalid_path "$label must be an absolute path"
    [[ "$candidate" != *$'\n'* && "$candidate" != *$'\r'* ]] || die invalid_path "$label contains a newline"
    case "$candidate" in
        *'//'*) die invalid_path "$label contains an empty path component" ;;
        *'/./'*|*/.) die invalid_path "$label contains a dot path component" ;;
        *'/../'*|*/..) die invalid_path "$label contains a parent path component" ;;
    esac
    if is_bad_prefix "$candidate"; then
        die protected_path "$label is below a protected path"
    fi
}

validate_integer() {
    local label=$1
    local value=$2
    [[ "$value" =~ ^[0-9]+$ ]] || die invalid_option "$label must be a non-negative integer"
}

parse_args() {
    while (($#)); do
        case "$1" in
            --mode)
                (($# >= 2)) || die invalid_option '--mode requires a value'
                MODE=$2
                shift 2
                ;;
            --mode=*)
                MODE=${1#*=}
                shift
                ;;
            --output)
                (($# >= 2)) || die invalid_option '--output requires a value'
                OUTPUT=$2
                OUTPUT_EXPLICIT=1
                shift 2
                ;;
            --output=*)
                OUTPUT=${1#*=}
                OUTPUT_EXPLICIT=1
                shift
                ;;
            --state-dir)
                (($# >= 2)) || die invalid_option '--state-dir requires a value'
                STATE_DIR=$2
                shift 2
                ;;
            --state-dir=*)
                STATE_DIR=${1#*=}
                shift
                ;;
            --idle-settle-seconds)
                (($# >= 2)) || die invalid_option '--idle-settle-seconds requires a value'
                IDLE_SETTLE_SECONDS=$2
                shift 2
                ;;
            --idle-settle-seconds=*)
                IDLE_SETTLE_SECONDS=${1#*=}
                shift
                ;;
            --idle-seconds)
                (($# >= 2)) || die invalid_option '--idle-seconds requires a value'
                IDLE_SECONDS=$2
                shift 2
                ;;
            --idle-seconds=*)
                IDLE_SECONDS=${1#*=}
                shift
                ;;
            --sample-interval-seconds)
                (($# >= 2)) || die invalid_option '--sample-interval-seconds requires a value'
                IDLE_INTERVAL_SECONDS=$2
                shift 2
                ;;
            --sample-interval-seconds=*)
                IDLE_INTERVAL_SECONDS=${1#*=}
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            --version|-V)
                printf '%s\n' "$SCRIPT_VERSION"
                exit 0
                ;;
            prepare|verify)
                [[ "$MODE" == 'prepare' ]] || die invalid_option 'mode was supplied twice'
                MODE=$1
                shift
                ;;
            *)
                die invalid_option "unknown option or mode: $1"
                ;;
        esac
    done

    [[ "$MODE" == 'prepare' || "$MODE" == 'verify' ]] || die invalid_mode 'mode must be prepare or verify'
    validate_integer idle-settle-seconds "$IDLE_SETTLE_SECONDS"
    validate_integer idle-seconds "$IDLE_SECONDS"
    validate_integer sample-interval-seconds "$IDLE_INTERVAL_SECONDS"
    ((IDLE_SETTLE_SECONDS <= 300)) || die invalid_option 'idle-settle-seconds must be at most 300'
    ((IDLE_SECONDS <= 300)) || die invalid_option 'idle-seconds must be at most 300'
    ((IDLE_INTERVAL_SECONDS >= 1 && IDLE_INTERVAL_SECONDS <= 60)) || die invalid_option 'sample-interval-seconds must be 1..60'

    validate_path_argument state-dir "$STATE_DIR"
    case "$STATE_DIR" in
        "$STATE_DIR_ROOT"|"$STATE_DIR_ROOT"/*) ;;
        *) die protected_path 'state-dir must remain below /var/lib/zeus/qualification' ;;
    esac
    if [[ -z "$OUTPUT" ]]; then
        OUTPUT="${STATE_DIR}/${MODE}.json"
    elif [[ "$OUTPUT" != '-' ]]; then
        validate_path_argument output "$OUTPUT"
        case "$OUTPUT" in
            "$STATE_DIR"|"$STATE_DIR"/*) ;;
            *) die protected_path 'output must remain below state-dir or be - for stdout' ;;
        esac
    fi
}

dry_run() {
    local output=$OUTPUT
    ((OUTPUT_EXPLICIT)) || output='-'
    /usr/bin/python3 - "$MODE" "$STATE_DIR" "$output" "$EXTENSION_ROOT" "$UNIT_PATH" "$SENTINEL" "$PROTECTED_PREFIXES" <<'PY'
import json
import os
import sys

mode, state_dir, output, extension, unit, sentinel, protected = sys.argv[1:]
payload = {
    "schema": "zeus-developer-sysext-qualification-v1",
    "schema_version": 1,
    "fixture_version": 1,
    "mode": mode,
    "result": "dry_run",
    "qualification_passed": False,
    "reboot": {
        "required": mode == "prepare",
        "observed": False,
        "claim": "not_observed",
    },
    "would_read": [
        "/usr/lib/os-release",
        "/proc/stat",
        "/proc/meminfo",
        "/proc/sys/kernel/random/boot_id",
        "/var/log/journal or the current journal boot",
        "/usr/bin/zeus desktop safe --dry-run",
        "/usr/libexec/zeus-desktop-safe",
    ],
    "would_mutate": [extension, state_dir, unit],
    "sentinel": sentinel,
    "protected_write_prefixes": protected.splitlines(),
    "runtime_operations": [
        "compatible systemd-sysext refresh and sentinel replacement",
        "systemd-sysext unmerge and exact sentinel restoration",
        "incompatible VERSION_ID rejection",
        "incompatible SYSEXT_LEVEL rejection",
        "post-reboot boot activation observation" if mode == "verify" else "operator-controlled reboot boundary",
        "SELinux enforcing and AVC denial scan",
        "safe-desktop recovery dry-run with terminal and data-preservation checks",
        "monotonic timing, idle CPU, and idle memory sampling",
        "disabled-mode boot timing and post-cleanup idle sample",
    ],
    "safe_desktop": {
        "command": ["zeus", "desktop", "safe", "--dry-run"],
        "mutation_performed": False,
        "claim": "contract_only",
    },
    "disabled_mode": {
        "boot_timing": {"claim": "observed_before_apply"},
        "idle": {"claim": "observed_after_cleanup"},
    },
    "reboot_command": None,
}
text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
if output == "-" or not output:
    print(text, end="")
else:
    # Dry-run output is the one intentional write.  The guest state, extension
    # path, unit path, and all other operational paths remain untouched.
    with open(output, "x", encoding="utf-8") as stream:
        stream.write(text)
    print(text, end="")
PY
}

require_root_and_tools() {
    ((EUID == 0)) || die not_root 'this fixture must run as root; use --dry-run for a static contract check'
    [[ -x /usr/bin/python3 ]] || die missing_tool '/usr/bin/python3 is required'
    [[ -x /usr/bin/systemctl ]] || die missing_tool '/usr/bin/systemctl is required'
    [[ -x /usr/bin/journalctl ]] || die missing_tool '/usr/bin/journalctl is required'
    [[ -x /usr/bin/systemd-analyze ]] || die missing_tool '/usr/bin/systemd-analyze is required for boot timing'
    [[ -x /usr/bin/sha256sum ]] || die missing_tool '/usr/bin/sha256sum is required'
    [[ -x /usr/bin/stat ]] || die missing_tool '/usr/bin/stat is required'
    [[ -x /usr/bin/mktemp ]] || die missing_tool '/usr/bin/mktemp is required'
    [[ -x /usr/bin/flock ]] || die missing_tool '/usr/bin/flock is required'
    ZEUS_CLI=$(type -P zeus || true)
    [[ -n "$ZEUS_CLI" && -x "$ZEUS_CLI" ]] || die missing_tool 'zeus is required for safe-desktop recovery qualification'
    [[ -x "$SAFE_DESKTOP_HELPER" ]] || die missing_tool 'zeus-desktop-safe is required for safe-desktop recovery qualification'
    [[ ! -L "$SAFE_DESKTOP_HELPER" ]] || die unsafe_path 'zeus-desktop-safe must not be a symlink'
    SAFE_DESKTOP_HELPER_SHA256=$(sha256_file "$SAFE_DESKTOP_HELPER")
    SAFE_DESKTOP_HELPER_CONTEXT=$(/usr/bin/stat -c '%C' "$SAFE_DESKTOP_HELPER")
    SYSTEMD_SYSEXT=$(type -P systemd-sysext || true)
    [[ -n "$SYSTEMD_SYSEXT" && -x "$SYSTEMD_SYSEXT" ]] || die missing_tool 'systemd-sysext is required'
    GETENFORCE=$(type -P getenforce || true)
    [[ -n "$GETENFORCE" && -x "$GETENFORCE" ]] || die missing_tool 'getenforce is required for an enforcing SELinux gate'
    RESTORECON=$(type -P restorecon || true)
    [[ -n "$RESTORECON" && -x "$RESTORECON" ]] || die missing_tool 'restorecon is required for extension labels'
    type -P findmnt >/dev/null 2>&1 || die missing_tool 'findmnt is required for the overlay observation'
    type -P cut >/dev/null 2>&1 || die missing_tool 'cut is required'
    type -P grep >/dev/null 2>&1 || die missing_tool 'grep is required'
}

ensure_safe_directory() {
    local directory=$1
    local label=$2
    if [[ -L "$directory" ]]; then
        die unsafe_path "$label is a symlink"
    fi
    if [[ -e "$directory" ]]; then
        [[ -d "$directory" ]] || die unsafe_path "$label is not a directory"
        local uid mode
        uid=$(/usr/bin/stat -c '%u' "$directory")
        mode=$(/usr/bin/stat -c '%a' "$directory")
        [[ "$uid" == '0' ]] || die unsafe_path "$label is not root-owned"
        [[ $((8#$mode & 22)) -eq 0 ]] || die unsafe_path "$label is group/world writable"
    else
        /usr/bin/install -d -o root -g root -m 0700 "$directory"
    fi
}

ensure_state() {
    if [[ "$MODE" == 'verify' ]]; then
        [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || die missing_state 'verify requires an existing private state directory from prepare'
    fi
    ensure_safe_directory "$STATE_DIR" state-dir
    STATE_JSON="${STATE_DIR}/prepare.json"
    if [[ "$MODE" == 'prepare' ]]; then
        [[ ! -e "$STATE_JSON" && ! -L "$STATE_JSON" ]] || die stale_state 'prepare.json already exists; inspect or remove the previous disposable qualification state'
    else
        [[ -f "$STATE_JSON" && ! -L "$STATE_JSON" ]] || die missing_state 'verify requires prepare.json from the same disposable guest'
        local uid mode
        uid=$(/usr/bin/stat -c '%u' "$STATE_JSON")
        mode=$(/usr/bin/stat -c '%a' "$STATE_JSON")
        [[ "$uid" == '0' && $((8#$mode & 22)) -eq 0 ]] || die unsafe_state 'prepare.json must be root-owned and private'
    fi
    if [[ "$OUTPUT" != '-' ]]; then
        local parent
        parent=${OUTPUT%/*}
        [[ -n "$parent" && -d "$parent" && ! -L "$parent" && ! -L "$OUTPUT" ]] || die invalid_output 'output parent must exist without symlinks and output must not be a symlink'
    fi
}

acquire_lock() {
    LOCK_PATH="${STATE_DIR}/qualification.lock"
    if [[ -L "$LOCK_PATH" ]]; then
        die unsafe_state 'qualification.lock must not be a symlink'
    fi
    if [[ -e "$LOCK_PATH" ]]; then
        [[ -f "$LOCK_PATH" ]] || die unsafe_state 'qualification.lock must be a regular file'
        local uid mode
        uid=$(/usr/bin/stat -c '%u' "$LOCK_PATH")
        mode=$(/usr/bin/stat -c '%a' "$LOCK_PATH")
        [[ "$uid" == '0' && $((8#$mode & 22)) -eq 0 ]] || die unsafe_state 'qualification.lock must be root-owned and private'
    else
        /usr/bin/install -o root -g root -m 0600 /dev/null "$LOCK_PATH"
    fi
    # Keep the descriptor open for the complete phase, including evidence and
    # cleanup.  A second invocation then fails before it can touch the guest.
    exec 9<> "$LOCK_PATH"
    if ! /usr/bin/flock -n 9; then
        die qualification_busy 'another qualification run holds the state lock'
    fi
}

mono_ns() {
    /usr/bin/python3 - <<'PY'
import time
print(time.monotonic_ns())
PY
}

utc_now() {
    /usr/bin/python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
PY
}

read_os_value() {
    /usr/bin/python3 - "$1" "$2" <<'PY'
import shlex
import sys

path, wanted = sys.argv[1:]
for raw in open(path, encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line.startswith(wanted + "="):
        continue
    value = line.split("=", 1)[1]
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        raise SystemExit(2)
    print(parts[0] if parts else "")
    break
PY
}

sha256_file() {
    /usr/bin/sha256sum "$1" | /usr/bin/cut -d ' ' -f1
}

record_command() {
    local label=$1
    local expected=$2
    shift 2
    local out err start end rc out_hash err_hash argv_json
    out="${RUN_TMP}/command-${COMMAND_INDEX}.stdout"
    err="${RUN_TMP}/command-${COMMAND_INDEX}.stderr"
    start=$(mono_ns)
    if "$@" >"$out" 2>"$err"; then
        rc=0
    else
        rc=$?
    fi
    end=$(mono_ns)
    out_hash=$(sha256_file "$out")
    err_hash=$(sha256_file "$err")
    argv_json=$(/usr/bin/python3 - "$@" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1:], separators=(",", ":")))
PY
)
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$label" "$expected" "$rc" "$start" "$end" "$out_hash" "$err_hash" "$argv_json" >> "${RUN_TMP}/commands.tsv"
    LAST_OUT_PATH=$out
    LAST_ERR_PATH=$err
    LAST_RETURN_CODE=$rc
    COMMAND_INDEX=$((COMMAND_INDEX + 1))
    [[ "$expected" == 'must-succeed' && "$rc" -ne 0 ]] && return 1
    return 0
}

run_checked() {
    local label=$1
    shift
    if record_command "$label" must-succeed "$@"; then
        return 0
    fi
    die command_failed "$label failed; see stderr digest in evidence"
}

run_rejection_check() {
    local label=$1
    shift
    # systemd-sysext has emitted "No suitable extensions found" with either a
    # successful no-op or a rejected-operation status across released builds.
    # Accept only those two statuses, then prove rejection from the untouched
    # base sentinel below.
    record_command "$label" expected-rejection "$@" || true
    [[ "$LAST_RETURN_CODE" == '0' || "$LAST_RETURN_CODE" == '1' ]] || die unexpected_rejection_status "$label returned $LAST_RETURN_CODE"
}

record_case() {
    local case_id=$1
    local status=$2
    local started=$3
    local ended=$4
    local message=$5
    printf '%s\t%s\t%s\t%s\t%s\n' "$case_id" "$status" "$started" "$ended" "$message" >> "${RUN_TMP}/cases.tsv"
}

begin_case() {
    CASE_ID=$1
    CASE_START_NS=$(mono_ns)
}

pass_case() {
    local message=$1
    record_case "$CASE_ID" pass "$CASE_START_NS" "$(mono_ns)" "$message"
}

begin_phase() {
    CURRENT_PHASE=$1
    PHASE_START_NS=$(mono_ns)
}

end_phase() {
    printf '%s\t%s\t%s\t%s\n' "$CURRENT_PHASE" "$PHASE_START_NS" "$(mono_ns)" "$(utc_now)" >> "${RUN_TMP}/phases.tsv"
    CURRENT_PHASE=''
}

snapshot_base_sentinel() {
    [[ -e "$SENTINEL" ]] || die missing_sentinel "${SENTINEL} must be supplied by the immutable Zeus base"
    [[ ! -L "$SENTINEL" ]] || die unsafe_sentinel 'the qualification sentinel must not be a symlink'
    [[ -f "$SENTINEL" ]] || die unsafe_sentinel 'the qualification sentinel must be a regular file'
    BASE_SENTINEL_HASH=$(sha256_file "$SENTINEL")
    BASE_SENTINEL_MODE=$(/usr/bin/stat -c '%a' "$SENTINEL")
    BASE_SENTINEL_UID=$(/usr/bin/stat -c '%u' "$SENTINEL")
    BASE_SENTINEL_GID=$(/usr/bin/stat -c '%g' "$SENTINEL")
    BASE_SENTINEL_CONTEXT=$(/usr/bin/stat -c '%C' "$SENTINEL")
}

assert_baseline_sentinel() {
    [[ -f "$SENTINEL" && ! -L "$SENTINEL" ]] || die restoration_failed 'the base sentinel is not a regular file after unmerge'
    [[ "$(sha256_file "$SENTINEL")" == "$BASE_SENTINEL_HASH" ]] || die restoration_failed 'the base sentinel content was not restored exactly'
    [[ "$(/usr/bin/stat -c '%a' "$SENTINEL")" == "$BASE_SENTINEL_MODE" ]] || die restoration_failed 'the base sentinel mode changed'
    [[ "$(/usr/bin/stat -c '%u' "$SENTINEL")" == "$BASE_SENTINEL_UID" ]] || die restoration_failed 'the base sentinel owner changed'
    [[ "$(/usr/bin/stat -c '%g' "$SENTINEL")" == "$BASE_SENTINEL_GID" ]] || die restoration_failed 'the base sentinel group changed'
    if [[ "$BASE_SENTINEL_CONTEXT" != '?' && "$BASE_SENTINEL_CONTEXT" != '' ]]; then
        [[ "$(/usr/bin/stat -c '%C' "$SENTINEL")" == "$BASE_SENTINEL_CONTEXT" ]] || die restoration_failed 'the base sentinel SELinux context changed'
    fi
}

assert_override_sentinel() {
    [[ -f "$SENTINEL" && ! -L "$SENTINEL" ]] || die merge_failed 'the merged sentinel is missing or not a regular file'
    local expected_hash
    expected_hash=$(printf '%s\n' "$OVERRIDE_CONTENT" | sha256_file /dev/stdin)
    [[ "$(sha256_file "$SENTINEL")" == "$expected_hash" ]] || die merge_failed 'the merged sentinel did not contain the extension override'
}

extension_path_for() {
    case "$1" in
        "$EXTENSION_NAME") printf '%s\n' "$EXTENSION_ROOT" ;;
        "$BAD_VERSION_NAME") printf '%s\n' "${EXTENSION_PARENT}/${BAD_VERSION_NAME}" ;;
        "$BAD_LEVEL_NAME") printf '%s\n' "${EXTENSION_PARENT}/${BAD_LEVEL_NAME}" ;;
        *) die unsafe_extension "unknown extension name: $1" ;;
    esac
}

remove_owned_extension() {
    local name=$1
    local path
    path=$(extension_path_for "$name")
    [[ "$path" == "${EXTENSION_PARENT}/${name}" ]] || die unsafe_extension 'extension path did not resolve to the fixed parent'
    if [[ -L "$path" ]]; then
        die unsafe_extension "refusing to remove symlink extension path: $path"
    fi
    if [[ -e "$path" ]]; then
        [[ -d "$path" ]] || die unsafe_extension "extension path is not a directory: $path"
        /usr/bin/rm -rf -- "$path"
    fi
    if [[ "$name" == "$EXTENSION_NAME" ]]; then
        EXTENSION_PRESENT=0
    fi
}

write_extension() {
    local name=$1
    local version_id=$2
    local sysext_level=$3
    local include_level=$4
    local path release target
    path=$(extension_path_for "$name")
    [[ ! -e "$path" && ! -L "$path" ]] || die extension_collision "refusing to overwrite existing extension path: $path"
    /usr/bin/install -d -o root -g root -m 0755 "$path/usr/lib/extension-release.d" "$path/usr/share/zeus"
    release="$path/usr/lib/extension-release.d/extension-release.${name}"
    target="$path/usr/share/zeus/developer-mode-qualification-sentinel"
    {
        printf 'ID=%s\n' "$BASE_ID"
        printf 'VERSION_ID=%s\n' "$version_id"
        if ((include_level)); then
            printf 'SYSEXT_LEVEL=%s\n' "$sysext_level"
        fi
        printf 'ARCHITECTURE=%s\n' "$BASE_ARCHITECTURE"
    } > "$release"
    printf '%s\n' "$OVERRIDE_CONTENT" > "$target"
    /usr/bin/chown root:root "$release" "$target"
    /usr/bin/chmod 0644 "$release" "$target"
    "$RESTORECON" -RF "$path" >/dev/null 2>&1 || die selinux_label_failed "restorecon failed for $path"
    if [[ "$name" == "$EXTENSION_NAME" ]]; then
        EXTENSION_PRESENT=1
    fi
}

ensure_extension_parent() {
    if [[ -L "$EXTENSION_PARENT" ]]; then
        die unsafe_extension_parent "$EXTENSION_PARENT is a symlink"
    fi
    if [[ -e "$EXTENSION_PARENT" ]]; then
        [[ -d "$EXTENSION_PARENT" ]] || die unsafe_extension_parent "$EXTENSION_PARENT is not a directory"
        local uid mode
        uid=$(/usr/bin/stat -c '%u' "$EXTENSION_PARENT")
        mode=$(/usr/bin/stat -c '%a' "$EXTENSION_PARENT")
        [[ "$uid" == '0' && $((8#$mode & 22)) -eq 0 ]] || die unsafe_extension_parent "$EXTENSION_PARENT is not a private root-owned directory"
    else
        /usr/bin/install -d -o root -g root -m 0755 "$EXTENSION_PARENT"
        EXTENSIONS_PARENT_CREATED=1
    fi
}

read_state_field() {
    local field=$1
    /usr/bin/python3 - "$STATE_JSON" "$field" <<'PY'
import json
import sys

path, field = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    value = json.load(stream)
if value.get("schema") != "zeus-developer-sysext-qualification-v1" or value.get("schema_version") != 1:
    raise SystemExit("unsupported evidence schema")
mapping = {
    "pre_boot_id": value.get("boot", {}).get("pre_boot_id", ""),
    "base_id": value.get("guest", {}).get("id", ""),
    "base_version_id": value.get("guest", {}).get("version_id", ""),
    "base_sysext_level": value.get("guest", {}).get("sysext_level", ""),
    "base_architecture": value.get("guest", {}).get("architecture", ""),
    "sentinel_hash": value.get("sentinel", {}).get("base_sha256", ""),
    "sentinel_mode": value.get("sentinel", {}).get("base_mode", ""),
    "sentinel_uid": value.get("sentinel", {}).get("base_uid", ""),
    "sentinel_gid": value.get("sentinel", {}).get("base_gid", ""),
    "sentinel_context": value.get("sentinel", {}).get("base_selinux_context", ""),
    "base_list_hash": value.get("extension", {}).get("preexisting_list_sha256", ""),
    "unit_created": "1" if value.get("boot", {}).get("activation_source") == "conditional_oneshot" else "0",
    "mode": value.get("mode", ""),
    "result": value.get("result", ""),
    "safe_desktop_output_sha256": value.get("safe_desktop", {}).get("output_sha256") or "",
    "safe_desktop_helper_sha256": value.get("safe_desktop", {}).get("helper_sha256") or "",
    "safe_desktop_helper_context": value.get("safe_desktop", {}).get("helper_selinux_context") or "",
    "safe_desktop_checked": "1" if value.get("safe_desktop", {}).get("claim") == "dry_run_contract" else "0",
}
if field not in mapping:
    raise SystemExit("unknown state field")
print(mapping[field])
PY
}

load_prepare_state() {
    PRE_BOOT_ID=$(read_state_field pre_boot_id) || die invalid_state 'prepare evidence could not be parsed'
    [[ -n "$PRE_BOOT_ID" ]] || die invalid_state 'prepare evidence has no pre-reboot boot ID'
    [[ "$(read_state_field mode)" == 'prepare' ]] || die invalid_state 'prepare evidence mode is not prepare'
    [[ "$(read_state_field result)" == 'pre_reboot_ready' ]] || die invalid_state 'prepare evidence is not an unverified pre-reboot result'
    BASE_ID=$(read_state_field base_id)
    BASE_VERSION_ID=$(read_state_field base_version_id)
    BASE_SYSEXT_LEVEL=$(read_state_field base_sysext_level)
    BASE_ARCHITECTURE=$(read_state_field base_architecture)
    BASE_SENTINEL_HASH=$(read_state_field sentinel_hash)
    BASE_SENTINEL_MODE=$(read_state_field sentinel_mode)
    BASE_SENTINEL_UID=$(read_state_field sentinel_uid)
    BASE_SENTINEL_GID=$(read_state_field sentinel_gid)
    BASE_SENTINEL_CONTEXT=$(read_state_field sentinel_context)
    BASE_LIST_HASH=$(read_state_field base_list_hash)
    SAFE_DESKTOP_OUTPUT_SHA256=$(read_state_field safe_desktop_output_sha256)
    SAFE_DESKTOP_HELPER_SHA256=$(read_state_field safe_desktop_helper_sha256)
    SAFE_DESKTOP_HELPER_CONTEXT=$(read_state_field safe_desktop_helper_context)
    SAFE_DESKTOP_CHECKED=$(read_state_field safe_desktop_checked)
}

capture_guest_identity() {
    [[ -r /usr/lib/os-release ]] || die missing_identity '/usr/lib/os-release is unavailable'
    BASE_ID=$(read_os_value /usr/lib/os-release ID) || die malformed_identity 'could not parse ID from /usr/lib/os-release'
    BASE_VERSION_ID=$(read_os_value /usr/lib/os-release VERSION_ID) || die malformed_identity 'could not parse VERSION_ID from /usr/lib/os-release'
    BASE_SYSEXT_LEVEL=$(read_os_value /usr/lib/os-release SYSEXT_LEVEL) || true
    BASE_ARCHITECTURE=$(uname -m)
    case "$BASE_ARCHITECTURE" in
        x86_64) BASE_ARCHITECTURE='x86-64' ;;
        aarch64) BASE_ARCHITECTURE='arm64' ;;
        armv7l) BASE_ARCHITECTURE='arm' ;;
        *) die unsupported_architecture "unsupported kernel architecture: $BASE_ARCHITECTURE" ;;
    esac
    [[ "$BASE_ID" == 'fedora' ]] || die wrong_guest "expected ID=fedora, got $BASE_ID"
    [[ "$BASE_VERSION_ID" == '44' ]] || die wrong_guest "expected VERSION_ID=44, got $BASE_VERSION_ID"
    [[ "$BASE_SYSEXT_LEVEL" =~ ^[a-z0-9._-]+$ ]] || die missing_identity 'SYSEXT_LEVEL must be present and contain only lowercase letters, numbers, dots, underscores, or hyphens'
}

capture_systemd_version() {
    run_checked 'systemd-sysext version' "$SYSTEMD_SYSEXT" --version
    SYSTEMD_VERSION=$(trim_newlines "$(<"$LAST_OUT_PATH")")
    [[ "$SYSTEMD_VERSION" =~ systemd[[:space:]]+259([.[:space:]]|$) ]] || die wrong_systemd "expected systemd 259, got ${SYSTEMD_VERSION%%$'\n'*}"
}

check_selinux() {
    run_checked 'SELinux enforcing state' "$GETENFORCE"
    SELINUX_MODE=$(trim_newlines "$(<"$LAST_OUT_PATH")")
    [[ "$SELINUX_MODE" == 'Enforcing' ]] || die selinux_not_enforcing "getenforce returned $SELINUX_MODE"
    [[ -r /sys/fs/selinux/enforce ]] || die selinux_unavailable '/sys/fs/selinux/enforce is unavailable'
    [[ "$(< /sys/fs/selinux/enforce)" == '1' ]] || die selinux_not_enforcing 'the SELinux enforce switch is not 1'
}

capture_boot_id() {
    [[ -r /proc/sys/kernel/random/boot_id ]] || die missing_boot_id 'kernel boot ID is unavailable'
    BOOT_ID=$(< /proc/sys/kernel/random/boot_id)
    [[ "$BOOT_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || die malformed_boot_id 'kernel boot ID is malformed'
}

capture_baseline_extension_list() {
    run_checked 'baseline systemd-sysext list' "$SYSTEMD_SYSEXT" list
    BASE_LIST_HASH=$(sha256_file "$LAST_OUT_PATH")
}

capture_boot_timing() {
    local label=$1
    run_checked "${label} boot timing" /usr/bin/systemd-analyze time
    /usr/bin/python3 - "$label" "$LAST_OUT_PATH" "${RUN_TMP}/boot-timings.tsv" <<'PY'
import hashlib
import pathlib
import re
import sys

label, output_path, record_path = sys.argv[1:]
raw = pathlib.Path(output_path).read_bytes()
text = raw.decode("utf-8", "replace")
summary = " ".join(text.split())[:512]
if not summary:
    raise SystemExit("systemd-analyze returned no timing summary")
if not re.search(r"Startup finished in", summary, re.I):
    raise SystemExit("systemd-analyze timing summary was not recognized")
with pathlib.Path(record_path).open("a", encoding="utf-8") as stream:
    stream.write("\t".join((label, hashlib.sha256(raw).hexdigest(), summary)) + "\n")
PY
}

check_safe_desktop_recovery() {
    begin_case safe_desktop_recovery
    run_checked 'safe-desktop recovery dry run' "$ZEUS_CLI" desktop safe --dry-run
    SAFE_DESKTOP_OUTPUT_SHA256=$(sha256_file "$LAST_OUT_PATH")
    grep -Fqi 'terminal remains available' "$LAST_OUT_PATH" || die safe_desktop_failed 'safe-desktop dry run did not confirm terminal availability'
    grep -Fqi 'credentials and personal data remain untouched' "$LAST_OUT_PATH" || die safe_desktop_failed 'safe-desktop dry run did not confirm data preservation'
    SAFE_DESKTOP_CHECKED=1
    pass_case 'safe-desktop recovery remained reachable and preserved terminal, credentials, and personal data'
}

install_conditional_boot_unit_if_needed() {
    local enabled_state
    enabled_state=$(/usr/bin/systemctl is-enabled systemd-sysext.service 2>/dev/null || true)
    enabled_state=$(trim_newlines "$enabled_state")
    [[ "$enabled_state" != 'masked' ]] || die sysext_service_masked 'systemd-sysext.service is masked'
    if [[ "$enabled_state" == 'enabled' || "$enabled_state" == 'static' || "$enabled_state" == 'alias' || "$enabled_state" == 'generated' ]]; then
        BOOT_ACTIVATION_SOURCE='systemd-sysext.service'
        return
    fi

    [[ ! -e "$UNIT_PATH" && ! -L "$UNIT_PATH" ]] || die unit_collision "refusing to overwrite $UNIT_PATH"
    # Mark the path before the first write so an interrupted install or unit
    # generation is still removed by the EXIT cleanup trap.
    CUSTOM_UNIT_CREATED=1
    BOOT_ACTIVATION_SOURCE='conditional_oneshot'
    /usr/bin/install -o root -g root -m 0644 /dev/null "$UNIT_PATH"
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Zeus disposable Developer Mode sysext qualification
ConditionPathExists=${EXTENSION_ROOT}/usr/share/zeus/developer-mode-qualification-sentinel
After=local-fs.target systemd-sysext.service
Wants=systemd-sysext.service
Before=graphical.target

[Service]
Type=oneshot
ExecStart=${SYSTEMD_SYSEXT} refresh
ExecStart=/usr/bin/test -f ${SENTINEL}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    /usr/bin/chown root:root "$UNIT_PATH"
    /usr/bin/chmod 0644 "$UNIT_PATH"
    run_checked 'reload systemd for conditional sysext unit' /usr/bin/systemctl daemon-reload
    run_checked 'enable conditional sysext unit' /usr/bin/systemctl enable "$UNIT_NAME"
}

assert_overlay_mount() {
    run_checked 'observe merged usr overlay' /usr/bin/findmnt --noheadings --output FSTYPE --target /usr
    local fstype
    fstype=$(trim_newlines "$(<"$LAST_OUT_PATH")")
    [[ "$fstype" == 'overlay' ]] || die merge_failed "merged /usr filesystem type was $fstype, expected overlay"
}

refresh_and_assert_override() {
    run_checked 'systemd-sysext refresh compatible fixture' "$SYSTEMD_SYSEXT" refresh
    SYSEXT_MAY_BE_MERGED=1
    assert_override_sentinel
    assert_overlay_mount
    run_checked 'systemd-sysext status after compatible merge' "$SYSTEMD_SYSEXT" --json=short status
    run_checked 'systemd-sysext list after compatible merge' "$SYSTEMD_SYSEXT" list
    grep -Fq "$EXTENSION_NAME" "$LAST_OUT_PATH" || die merge_failed 'compatible extension was not listed after refresh'
}

unmerge_and_assert_baseline() {
    run_checked 'systemd-sysext unmerge fixture' "$SYSTEMD_SYSEXT" unmerge
    SYSEXT_MAY_BE_MERGED=0
    assert_baseline_sentinel
}

scan_avc_denials() {
    local label=$1
    local since_ns=$2
    local relevant=$3
    run_checked "$label journal scan" /usr/bin/journalctl -b 0 --no-pager --output=json
    local journal_path=$LAST_OUT_PATH
    local result count digest
    result=$(/usr/bin/python3 - "$journal_path" "$since_ns" "$relevant" <<'PY'
import hashlib
import json
import re
import sys

path, since_ns, relevant = sys.argv[1:]
since = int(since_ns)
relevant = relevant == "1"
hits = []
pattern = re.compile(r"(?:avc|type=avc).*denied|denied.*(?:avc|type=avc)", re.I)
with open(path, encoding="utf-8", errors="replace") as stream:
    for line in stream:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        monotonic = int(item.get("__MONOTONIC_TIMESTAMP", "0") or 0) * 1000
        fields = " ".join(str(item.get(key, "")) for key in (
            "MESSAGE", "MESSAGE_ID", "_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER", "_COMM"
        ))
        if not pattern.search(fields):
            continue
        if monotonic >= since and (not relevant or "systemd-sysext" in fields.lower() or "zeus-developer-sysext" in fields.lower()):
            hits.append(line.strip().encode("utf-8", "replace"))
digest = hashlib.sha256(b"\n".join(hits)).hexdigest()
print(f"{len(hits)}\t{digest}")
PY
)
    IFS=$'\t' read -r count digest <<< "$result"
    [[ "$count" =~ ^[0-9]+$ ]] || die malformed_avc_scan "$label AVC scan was malformed"
    if [[ "$label" == *boot* ]]; then
        AVC_BOOT_COUNT=$count
    else
        AVC_OPERATION_COUNT=$count
    fi
    [[ "$count" == '0' ]] || die selinux_avc "${label} found $count AVC denial(s); digest $digest"
}

sample_idle() {
    local label=$1
    local output="${RUN_TMP}/idle-${label}.json"
    local started ended
    started=$(mono_ns)
    /usr/bin/python3 - "$label" "$IDLE_SETTLE_SECONDS" "$IDLE_SECONDS" "$IDLE_INTERVAL_SECONDS" "$BOOT_ID" > "$output" <<'PY'
import json
import statistics
import sys
import time

label, settle_text, seconds_text, interval_text, boot_id = sys.argv[1:]
settle = int(settle_text)
seconds = int(seconds_text)
interval = int(interval_text)

def counters():
    with open("/proc/stat", encoding="utf-8") as stream:
        line = next(row for row in stream if row.startswith("cpu "))
    fields = list(map(int, line.split()[1:9]))
    return sum(fields), fields[3] + fields[4]

def memory_used_mib():
    values = {}
    with open("/proc/meminfo", encoding="utf-8") as stream:
        for line in stream:
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
    return round((values["MemTotal"] - values["MemAvailable"]) / 1024, 1)

time.sleep(settle)
measurement_start = time.monotonic_ns()
previous = counters()
samples = []
sample_count = max(1, (seconds + interval - 1) // interval)
for index in range(sample_count):
    if index or seconds:
        time.sleep(interval)
    current = counters()
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    cpu = round(100 * (1 - idle / total), 3) if total > 0 else 0.0
    samples.append({
        "sample": index + 1,
        "monotonic_ns": time.monotonic_ns(),
        "cpu_percent": cpu,
        "memory_used_mib": memory_used_mib(),
    })
    previous = current
measurement_end = time.monotonic_ns()
print(json.dumps({
    "label": label,
    "boot_id": boot_id,
    "clock": "CLOCK_MONOTONIC",
    "measurement_started_monotonic_ns": measurement_start,
    "measurement_ended_monotonic_ns": measurement_end,
    "settle_seconds": settle,
    "requested_seconds": seconds,
    "sample_interval_seconds": interval,
    "samples": samples,
    "median_cpu_percent": statistics.median(item["cpu_percent"] for item in samples),
    "median_memory_used_mib": statistics.median(item["memory_used_mib"] for item in samples),
    "memory_definition": "MemTotal minus MemAvailable; includes non-reclaimable guest memory",
}, sort_keys=True))
PY
    ended=$(mono_ns)
    printf '%s\t%s\t%s\t%s\n' "idle_${label}" "$started" "$ended" "$(utc_now)" >> "${RUN_TMP}/phases.tsv"
    [[ -s "$output" ]] || die metrics_failed "idle sample $label produced no JSON"
}

runtime_qualification() {
    begin_case runtime_merge_override
    refresh_and_assert_override
    pass_case 'compatible extension merged and replaced the dedicated sentinel'

    begin_case runtime_unmerge_restoration
    unmerge_and_assert_baseline
    pass_case 'unmerge restored sentinel bytes, mode, owner, group, and SELinux context'

    begin_case runtime_remerge
    refresh_and_assert_override
    pass_case 'the same extension merged again after an exact unmerge'

    begin_case incompatible_version_rejected
    unmerge_and_assert_baseline
    remove_owned_extension "$EXTENSION_NAME"
    write_extension "$BAD_VERSION_NAME" '0' '' 0
    run_rejection_check 'refresh incompatible VERSION_ID fixture' "$SYSTEMD_SYSEXT" refresh
    SYSEXT_MAY_BE_MERGED=0
    assert_baseline_sentinel
    remove_owned_extension "$BAD_VERSION_NAME"
    run_checked 'restore base after incompatible VERSION_ID fixture' "$SYSTEMD_SYSEXT" refresh
    SYSEXT_MAY_BE_MERGED=0
    assert_baseline_sentinel
    pass_case 'extension without matching VERSION_ID stayed inactive'

    begin_case incompatible_sysext_level_rejected
    write_extension "$BAD_LEVEL_NAME" "$BASE_VERSION_ID" "${BASE_SYSEXT_LEVEL}-wrong" 1
    run_rejection_check 'refresh incompatible SYSEXT_LEVEL fixture' "$SYSTEMD_SYSEXT" refresh
    SYSEXT_MAY_BE_MERGED=0
    assert_baseline_sentinel
    remove_owned_extension "$BAD_LEVEL_NAME"
    run_checked 'restore base after incompatible SYSEXT_LEVEL fixture' "$SYSTEMD_SYSEXT" refresh
    SYSEXT_MAY_BE_MERGED=0
    assert_baseline_sentinel
    pass_case 'extension with incompatible SYSEXT_LEVEL stayed inactive'

    write_extension "$EXTENSION_NAME" "$BASE_VERSION_ID" "$BASE_SYSEXT_LEVEL" 1
    refresh_and_assert_override
}

prepare() {
    OPERATION_START_NS=$(mono_ns)
    begin_phase prepare
    capture_guest_identity
    capture_systemd_version
    check_selinux
    capture_boot_id
    PRE_BOOT_ID=$BOOT_ID
    snapshot_base_sentinel
    capture_baseline_extension_list
    capture_boot_timing disabled
    check_safe_desktop_recovery
    ensure_extension_parent
    [[ ! -e "$EXTENSION_ROOT" && ! -L "$EXTENSION_ROOT" ]] || die extension_collision "refusing to overwrite $EXTENSION_ROOT"

    begin_case protected_scope
    pass_case 'only the fixed extension, qualification state, evidence, and optional unit are writable'

    begin_case selinux_enforcing
    pass_case 'SELinux is enforcing for the fixture'

    write_extension "$EXTENSION_NAME" "$BASE_VERSION_ID" "$BASE_SYSEXT_LEVEL" 1
    install_conditional_boot_unit_if_needed
    sample_idle baseline
    runtime_qualification
    sample_idle merged
    scan_avc_denials operation "$OPERATION_START_NS" 0

    begin_case selinux_no_avc
    pass_case 'no AVC denial was recorded during fixture operations'

    begin_case metrics_recorded
    pass_case 'monotonic timing and baseline/merged idle samples were recorded'

    begin_case reboot_boundary
    record_case reboot_boundary pending "$(mono_ns)" "$(mono_ns)" 'prepare never claims reboot success; run verify after an operator-controlled reboot'

    RESULT='pre_reboot_ready'
    KEEP_FIXTURE=1
    end_phase
}

verify_boot_activation() {
    begin_phase verify_boot
    capture_guest_identity
    capture_systemd_version
    check_selinux
    capture_boot_id
    CURRENT_BOOT_ID=$BOOT_ID
    [[ "$CURRENT_BOOT_ID" != "$PRE_BOOT_ID" ]] || die reboot_not_observed 'boot ID did not change; verify must run after the operator-controlled reboot'
    REBOOT_OBSERVED=1
    if [[ "$BASE_ID" != 'fedora' || "$BASE_VERSION_ID" != '44' || "$BASE_SYSEXT_LEVEL" != "$(read_state_field base_sysext_level)" ]]; then
        begin_case incompatible_base_after_reboot
        record_case incompatible_base_after_reboot incompatible "$VERIFY_START_NS" "$(mono_ns)" 'base identity changed; the stale extension must remain inactive and requires a rebuild'
        die incompatible_base 'the booted guest base identity differs from the prepared extension'
    fi

    run_checked 'systemd-sysext status after reboot' "$SYSTEMD_SYSEXT" --json=short status
    [[ -d "$EXTENSION_ROOT" && -f "$EXTENSION_ROOT/usr/share/zeus/developer-mode-qualification-sentinel" ]] || die boot_activation_failed 'prepared extension is missing after reboot'
    assert_override_sentinel
    SYSEXT_MAY_BE_MERGED=1
    case "$BOOT_ACTIVATION_SOURCE" in
        systemd-sysext.service)
    run_checked 'systemd-sysext service active after reboot' /usr/bin/systemctl is-active systemd-sysext.service
            ;;
        conditional_oneshot)
            run_checked 'conditional sysext service active after reboot' /usr/bin/systemctl is-active "$UNIT_NAME"
            ;;
        *)
            die boot_activation_failed 'no recorded systemd-sysext boot activation source'
            ;;
    esac
    capture_boot_timing active
    # The activation happened during boot, before this verify invocation.  A
    # boot scan therefore starts at monotonic zero and filters to sysext units.
    scan_avc_denials boot 0 1
    begin_case boot_activation
    pass_case "boot ID changed and $BOOT_ACTIVATION_SOURCE merged the prepared extension"
    end_phase
}

cleanup_extension_and_unit() {
    begin_phase cleanup
    if ((SYSEXT_MAY_BE_MERGED)); then
        if ! record_command 'cleanup systemd-sysext unmerge' must-succeed "$SYSTEMD_SYSEXT" unmerge; then
            ERROR_CODE=${ERROR_CODE:-cleanup_unmerge_failed}
            ERROR_MESSAGE=${ERROR_MESSAGE:-'systemd-sysext unmerge failed; extension was retained for manual recovery'}
            return 1
        fi
        SYSEXT_MAY_BE_MERGED=0
    fi
    if [[ -d "$EXTENSION_ROOT" || -L "$EXTENSION_ROOT" ]]; then
        if [[ -L "$EXTENSION_ROOT" ]]; then
            ERROR_CODE=${ERROR_CODE:-unsafe_extension}
            ERROR_MESSAGE=${ERROR_MESSAGE:-'refusing to remove a replaced extension symlink'}
            return 1
        fi
        /usr/bin/rm -rf -- "$EXTENSION_ROOT"
        EXTENSION_PRESENT=0
    fi
    if [[ -e "${EXTENSION_PARENT}/${BAD_VERSION_NAME}" || -e "${EXTENSION_PARENT}/${BAD_LEVEL_NAME}" ]]; then
        ERROR_CODE=${ERROR_CODE:-cleanup_bad_extension}
        ERROR_MESSAGE=${ERROR_MESSAGE:-'an incompatible extension fixture remained after cleanup'}
        return 1
    fi
    if ! record_command 'cleanup systemd-sysext refresh' must-succeed "$SYSTEMD_SYSEXT" refresh; then
        ERROR_CODE=${ERROR_CODE:-cleanup_refresh_failed}
        ERROR_MESSAGE=${ERROR_MESSAGE:-'systemd-sysext refresh failed after extension removal'}
        return 1
    fi
    SYSEXT_MAY_BE_MERGED=0
    if [[ "$CUSTOM_UNIT_CREATED" == '1' || "$BOOT_ACTIVATION_SOURCE" == 'conditional_oneshot' ]]; then
        if ! record_command 'disable conditional sysext unit' must-succeed /usr/bin/systemctl disable --now "$UNIT_NAME"; then
            ERROR_CODE=${ERROR_CODE:-cleanup_unit_disable_failed}
            ERROR_MESSAGE=${ERROR_MESSAGE:-'conditional unit could not be disabled'}
            return 1
        fi
        if [[ -L "/etc/systemd/system/multi-user.target.wants/${UNIT_NAME}" ]]; then
            /usr/bin/rm -f -- "/etc/systemd/system/multi-user.target.wants/${UNIT_NAME}"
        fi
        [[ ! -e "$UNIT_PATH" && ! -L "$UNIT_PATH" ]] || /usr/bin/rm -f -- "$UNIT_PATH"
        if ! record_command 'reload systemd after conditional unit cleanup' must-succeed /usr/bin/systemctl daemon-reload; then
            ERROR_CODE=${ERROR_CODE:-cleanup_unit_reload_failed}
            ERROR_MESSAGE=${ERROR_MESSAGE:-'systemd daemon reload failed after unit cleanup'}
            return 1
        fi
    fi
    if ((EXTENSIONS_PARENT_CREATED)); then
        if [[ -z "$(/usr/bin/find "$EXTENSION_PARENT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
            /usr/bin/rmdir "$EXTENSION_PARENT" || true
        fi
    fi
    end_phase
    CLEANUP_DONE=1
}

verify_cleanup() {
    begin_case clean_restore
    cleanup_extension_and_unit || return 1
    assert_baseline_sentinel
    run_checked 'final systemd-sysext list' "$SYSTEMD_SYSEXT" list
    local final_list_hash
    final_list_hash=$(sha256_file "$LAST_OUT_PATH")
    [[ "$final_list_hash" == "$BASE_LIST_HASH" ]] || die restoration_failed 'pre-existing systemd-sysext list changed during qualification'
    pass_case 'extension removed and the exact base sentinel and extension list were restored'

    # This sample is intentionally taken only after the final unmerge and
    # refresh.  It is the disabled-mode idle observation for the same boot;
    # the separate prepare boot-timing sample records the pre-apply disabled
    # startup boundary without asking this guest-side helper to reboot.
    sample_idle post_boot_disabled

    begin_case selinux_no_avc
    scan_avc_denials operation "$VERIFY_START_NS" 0
    pass_case 'no AVC denial was recorded during post-reboot verification and cleanup'

    begin_case metrics_recorded
    pass_case 'post-reboot monotonic timing and idle samples were recorded'
}

verify() {
    VERIFY_START_NS=$(mono_ns)
    OPERATION_START_NS=$VERIFY_START_NS
    load_prepare_state
    BOOT_ACTIVATION_SOURCE=$(read_state_field unit_created)
    if [[ "$BOOT_ACTIVATION_SOURCE" == '1' ]]; then
        BOOT_ACTIVATION_SOURCE='conditional_oneshot'
        CUSTOM_UNIT_CREATED=1
    else
        BOOT_ACTIVATION_SOURCE='systemd-sysext.service'
    fi
    # Restore the saved base values after capture_guest_identity overwrites them
    # in verify_boot_activation; these values describe the extension we must
    # restore and are compared separately below.
    local prepared_id prepared_version prepared_level prepared_arch
    prepared_id=$BASE_ID
    prepared_version=$BASE_VERSION_ID
    prepared_level=$BASE_SYSEXT_LEVEL
    prepared_arch=$BASE_ARCHITECTURE
    verify_boot_activation
    # verify_boot_activation re-read the current identity.  The comparisons
    # below are deliberately exact and are performed before any cleanup.
    [[ "$BASE_ID" == "$prepared_id" && "$BASE_VERSION_ID" == "$prepared_version" && "$BASE_SYSEXT_LEVEL" == "$prepared_level" && "$BASE_ARCHITECTURE" == "$prepared_arch" ]] || die incompatible_base 'prepared and booted base identities differ'
    sample_idle post_boot_merged
    verify_cleanup
    RESULT='qualified'
}

write_evidence() {
    local destination=$1
    local source="${RUN_TMP}/evidence.json"
    /usr/bin/python3 - "$source" "$destination" "$MODE" "$RESULT" "$ERROR_CODE" "$ERROR_MESSAGE" "$PRE_BOOT_ID" "$CURRENT_BOOT_ID" "$REBOOT_OBSERVED" "$BOOT_ACTIVATION_SOURCE" "$BASE_ID" "$BASE_VERSION_ID" "$BASE_SYSEXT_LEVEL" "$BASE_ARCHITECTURE" "$SYSTEMD_VERSION" "$SELINUX_MODE" "$SENTINEL" "$BASE_SENTINEL_HASH" "$BASE_SENTINEL_MODE" "$BASE_SENTINEL_UID" "$BASE_SENTINEL_GID" "$BASE_SENTINEL_CONTEXT" "$BASE_LIST_HASH" "$AVC_OPERATION_COUNT" "$AVC_BOOT_COUNT" "$EXTENSION_NAME" "$OVERRIDE_CONTENT" "$STATE_DIR" "$EXTENSION_ROOT" "$UNIT_PATH" "$PROTECTED_PREFIXES" "$RUN_TMP" "$SAFE_DESKTOP_OUTPUT_SHA256" "$SAFE_DESKTOP_HELPER_SHA256" "$SAFE_DESKTOP_HELPER_CONTEXT" "$SAFE_DESKTOP_CHECKED" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import tempfile

(source, destination, mode, result, error_code, error_message, pre_boot_id,
 post_boot_id, reboot_observed, activation_source, base_id, version_id,
 sysext_level, architecture, systemd_version, selinux_mode, sentinel,
 sentinel_hash, sentinel_mode, sentinel_uid, sentinel_gid, sentinel_context,
 base_list_hash, avc_operation_count, avc_boot_count, extension_name,
 override_content, state_dir, extension_root, unit_path, protected_prefixes,
 run_tmp, safe_desktop_output_sha256, safe_desktop_helper_sha256, safe_desktop_helper_context, safe_desktop_checked) = sys.argv[1:]
run = pathlib.Path(run_tmp)

def rows(name):
    path = run / name
    if not path.exists():
        return []
    values = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        values.append(raw.split("\t"))
    return values

cases = []
for row in rows("cases.tsv"):
    if len(row) != 5:
        continue
    case_id, status, started, ended, message = row
    item = {
        "id": case_id,
        "status": status,
        "started_monotonic_ns": int(started),
        "ended_monotonic_ns": int(ended),
        "duration_seconds": round((int(ended) - int(started)) / 1_000_000_000, 6),
        "message": message,
    }
    cases.append(item)

commands = []
for row in rows("commands.tsv"):
    if len(row) != 8:
        continue
    label, expected, rc, started, ended, stdout_sha256, stderr_sha256, argv_json = row
    try:
        argv = json.loads(argv_json)
    except json.JSONDecodeError:
        argv = []
    commands.append({
        "label": label,
        "expected": expected,
        "returncode": int(rc),
        "started_monotonic_ns": int(started),
        "ended_monotonic_ns": int(ended),
        "duration_seconds": round((int(ended) - int(started)) / 1_000_000_000, 6),
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "argv": argv,
    })

phases = []
for row in rows("phases.tsv"):
    if len(row) != 4:
        continue
    phase, started, ended, ended_at = row
    phases.append({
        "name": phase,
        "started_monotonic_ns": int(started),
        "ended_monotonic_ns": int(ended),
        "duration_seconds": round((int(ended) - int(started)) / 1_000_000_000, 6),
        "ended_at_utc": ended_at,
    })

boot_timings = []
for row in rows("boot-timings.tsv"):
    if len(row) != 3:
        continue
    label, output_sha256, summary = row
    boot_timings.append({
        "label": label,
        "output_sha256": output_sha256,
        "summary": summary[:512],
        "clock": "CLOCK_MONOTONIC",
    })

idle = {}
for path in sorted(run.glob("idle-*.json")):
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    idle[item.get("label", path.stem)] = item

safe_desktop = {
    "command": ["zeus", "desktop", "safe", "--dry-run"],
    "helper": "/usr/libexec/zeus-desktop-safe",
    "helper_sha256": safe_desktop_helper_sha256 or None,
    "helper_selinux_context": safe_desktop_helper_context or None,
    "output_sha256": safe_desktop_output_sha256 or None,
    "mutation_performed": False,
    "terminal_available": safe_desktop_checked == "1",
    "credentials_untouched": safe_desktop_checked == "1",
    "personal_data_untouched": safe_desktop_checked == "1",
    "claim": "dry_run_contract" if safe_desktop_checked == "1" else "not_observed",
}

errors = []
if error_code:
    errors.append({"code": error_code, "message": error_message})

payload = {
    "schema": "zeus-developer-sysext-qualification-v1",
    "schema_version": 1,
    "fixture_version": 1,
    "mode": mode,
    "result": result,
    "qualification_passed": result == "qualified",
    "errors": errors,
    "guest": {
        "id": base_id,
        "version_id": version_id,
        "sysext_level": sysext_level,
        "architecture": architecture,
        "systemd_version": systemd_version.splitlines()[0] if systemd_version else "",
        "selinux": selinux_mode,
    },
    "boot": {
        "pre_boot_id": pre_boot_id,
        "post_boot_id": post_boot_id,
        "observed_boot_change": bool(int(reboot_observed)),
        "reboot_required": mode == "prepare" and result == "pre_reboot_ready",
        "activation_source": activation_source,
        "claim": "observed_after_boot_id_change" if bool(int(reboot_observed)) and result == "qualified" else "not_observed",
    },
    "sentinel": {
        "path": sentinel,
        "base_sha256": sentinel_hash,
        "base_mode": sentinel_mode,
        "base_uid": int(sentinel_uid) if sentinel_uid.isdigit() else None,
        "base_gid": int(sentinel_gid) if sentinel_gid.isdigit() else None,
        "base_selinux_context": sentinel_context,
        "override_sha256": hashlib.sha256((override_content + "\n").encode()).hexdigest(),
        "override_content_label": "fixed fixture string; raw content is not copied into evidence",
        "restoration": "exact bytes, mode, owner, group, and SELinux context",
    },
    "extension": {
        "name": extension_name,
        "directory": extension_root,
        "format": "directory",
        "release_metadata": f"{extension_root}/usr/lib/extension-release.d/extension-release.{extension_name}",
        "preexisting_list_sha256": base_list_hash,
        "mutable_data_touched": False,
        "cleanup_required": mode == "prepare" and result == "pre_reboot_ready",
    },
    "selinux": {
        "enforcing": selinux_mode == "Enforcing",
        "avc_denials_during_operations": int(avc_operation_count),
        "avc_denials_during_boot_activation": int(avc_boot_count),
        "avc_scan_note": "journal entries were filtered by monotonic range and sysext unit fields; raw journal text is intentionally excluded from evidence",
    },
    "safe_desktop": safe_desktop,
    "metrics": {
        "clock": "CLOCK_MONOTONIC",
        "phases": phases,
        "boot_timings": boot_timings,
        "idle": idle,
        "idle_memory_definition": "MemTotal minus MemAvailable; includes non-reclaimable guest memory",
        "cpu_definition": "guest aggregate busy percentage from /proc/stat deltas",
        "disabled_mode": {
            "boot_timing_labels": [item["label"] for item in boot_timings if item["label"] == "disabled"],
            "idle_labels": [item["label"] for item in idle if item in {"baseline", "post_boot_disabled"}],
            "boot_claim": "observed_before_apply" if any(item["label"] == "disabled" for item in boot_timings) else "not_observed",
            "idle_claim": "observed_after_cleanup" if "post_boot_disabled" in idle else "not_observed",
        },
    },
    "cases": cases,
    "commands": commands,
    "scope": {
        "allowed_mutation_paths": [state_dir, extension_root, unit_path],
        "protected_write_prefixes": protected_prefixes.splitlines(),
        "owner_data_touched": False,
        "updater_touched": False,
        "bootloader_touched": False,
        "reboot_requested_by_script": False,
        "proxmox_operated_by_script": False,
    },
}
text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
pathlib.Path(source).write_text(text, encoding="utf-8")
if destination != "-":
    dest = pathlib.Path(destination)
    if dest.is_symlink():
        raise SystemExit("refusing to write evidence through a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".sysext-evidence-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, dest)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
print(text, end="")
PY
}

cleanup_on_exit() {
    local original_rc=$1
    ((FINISHED)) && return
    FINISHED=1
    local cleanup_rc=0
    if ((KEEP_FIXTURE == 0 && CLEANUP_DONE == 0)) && [[ -n "$RUN_TMP" ]]; then
        cleanup_extension_and_unit || cleanup_rc=$?
        if ((cleanup_rc != 0)) && [[ -z "$ERROR_CODE" ]]; then
            ERROR_CODE='cleanup_failed'
            ERROR_MESSAGE='fixture cleanup did not complete'
        fi
    fi
    if [[ -n "$RUN_TMP" ]]; then
        local evidence_rc=0
        if [[ "$MODE" == 'prepare' && "$RESULT" == 'pre_reboot_ready' && "$OUTPUT" != "$STATE_JSON" ]]; then
            write_evidence "$STATE_JSON" || evidence_rc=$?
            if ((evidence_rc == 0)); then
                write_evidence "$OUTPUT" || evidence_rc=$?
            fi
        else
            write_evidence "$OUTPUT" || evidence_rc=$?
        fi
        if ((evidence_rc != 0)); then
            if [[ -z "$ERROR_CODE" ]]; then
                ERROR_CODE='evidence_write_failed'
                ERROR_MESSAGE='structured evidence could not be written'
            fi
            # A successful prepare must not leave an active extension when its
            # durable state cannot be recorded.
            if ((KEEP_FIXTURE)); then
                KEEP_FIXTURE=0
                cleanup_extension_and_unit || true
            fi
            original_rc=1
        fi
    fi
    if [[ "$RESULT" == 'qualified' && -n "$ERROR_CODE" ]]; then
        original_rc=1
    fi
    if [[ -n "$RUN_TMP" ]]; then
        /usr/bin/rm -rf -- "$RUN_TMP"
    fi
    trap - EXIT
    exit "$original_rc"
}

main() {
    parse_args "$@"
    if ((DRY_RUN)); then
        dry_run
        return 0
    fi
    require_root_and_tools
    ensure_state
    acquire_lock
    RUN_TMP=$(/usr/bin/mktemp -d /run/zeus-developer-sysext.XXXXXX)
    /usr/bin/chmod 0700 "$RUN_TMP"
    : > "${RUN_TMP}/commands.tsv"
    : > "${RUN_TMP}/cases.tsv"
    : > "${RUN_TMP}/phases.tsv"
    trap 'rc=$?; cleanup_on_exit "$rc"' EXIT

    if [[ "$MODE" == 'prepare' ]]; then
        prepare
    else
        verify
    fi
    return 0
}

main "$@"
