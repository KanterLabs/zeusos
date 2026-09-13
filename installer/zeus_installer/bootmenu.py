"""Render Fedora's managed GRUB entry and the installed boot contract.

The installer must record and verify the ESP identity before using this output.
This module never regenerates GRUB or changes firmware/default boot selection.
The boot chooser marker is deliberately a small, immutable description of the
pre-existing Fedora boot path.  It is built from already validated inventory;
it does not probe firmware or infer a boot entry from a client supplied path.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping, Sequence

ENTRY_ID = 'zeusos-dualboot'
SCRIPT_PATH = '/etc/grub.d/42_zeus_dualboot'
BOOT_CHOOSER_SCHEMA_VERSION = 1
BOOT_CHOOSER_PATH = '/etc/zeus/boot-chooser.json'
BOOT_CHOOSER_MAX_BYTES = 4 * 1024
FEDORA_EFI_PATH = r'\EFI\fedora\shimx64.efi'

_BOOT_ID_RE = re.compile(r'\A[0-9A-Fa-f]{4}\Z')
_UUID_RE = re.compile(
    r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z'
)
_SAFE_LABEL_RE = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}\Z')
_GPT_GUID_RE = re.compile(
    r'GPT,([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?:,|\))',
    re.IGNORECASE,
)

# These names are intentionally flat.  They are consumed by the installed
# runtime without needing to understand preflight's nested lsblk/efibootmgr
# representations, and the exact key set keeps the marker bounded and
# rejectable when a journal or target file is malformed.
_BOOT_CHOOSER_KEYS = frozenset({
    'schema_version',
    'fedora_boot_entry',
    'fedora_boot_path',
    'fedora_esp_partuuid',
    'fedora_boot_partuuid',
    'grub_entry_id',
    'grub_timeout_style',
    'grub_timeout',
    'default_preserved',
})


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _normalized_uuid(value: Any, *, field: str) -> str:
    text = _text(value).strip()
    if _UUID_RE.fullmatch(text) is None:
        raise ValueError(f'{field} must be a GPT PARTUUID')
    try:
        return str(uuid.UUID(text))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f'{field} must be a GPT PARTUUID') from error


def normalize_efi_path(value: Any) -> str:
    """Return Fedora's one accepted EFI path or reject it.

    efibootmgr and fixtures differ in separator and case presentation.  Only
    the exact Fedora shim path is accepted after separator/case normalization;
    arbitrary paths, traversal, shell syntax, and control characters never
    reach the marker or the target writer.
    """

    text = _text(value).strip()
    if not text or len(text) > 256 or any(ord(character) < 0x20 or ord(character) == 0x7f for character in text):
        raise ValueError('The Fedora EFI path is invalid')
    normalized = text.replace('/', '\\')
    if normalized.casefold() != FEDORA_EFI_PATH.casefold():
        raise ValueError('The Fedora EFI path is not the supported shim path')
    return FEDORA_EFI_PATH


def _normalized_label(value: Any) -> str:
    text = _text(value).strip()
    if not text or len(text) > 256 or any(ord(character) < 0x20 or ord(character) == 0x7f for character in text):
        raise ValueError('The Fedora EFI entry label is invalid')
    # ``description`` from efibootmgr includes the hardware path after the
    # human label (for example ``Fedora HD(...)/File(...)``).  Persist only
    # the label while retaining its exact display spelling.
    text = re.split(r'\s+(?:HD\(|File\()', text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if not text or 'fedora' not in text.casefold():
        raise ValueError('The Fedora EFI entry label is invalid')
    if _SAFE_LABEL_RE.fullmatch(text) is None:
        raise ValueError('The Fedora EFI entry label is invalid')
    return text


def build_boot_chooser_marker(
    *,
    fedora_boot_entry: Any | None = None,
    fedora_boot_entry_id: Any | None = None,
    fedora_efi_entry_label: Any | None = None,
    fedora_efi_entry_path: Any | None = None,
    fedora_boot_path: Any | None = None,
    fedora_esp_partuuid: Any = None,
    fedora_boot_partuuid: Any = None,
    grub_timeout: Any | None = None,
    timeout_seconds: Any | None = None,
) -> dict[str, Any]:
    """Build and validate the bounded schema-1 installed boot marker.

    This pure helper accepts only already selected values.  Selection and
    cross-checking against inventory belongs to :func:`derive_boot_chooser_marker`.
    """

    if fedora_boot_entry is not None and fedora_boot_entry_id is not None:
        raise ValueError('The Fedora EFI entry ID was supplied twice')
    raw_entry = fedora_boot_entry if fedora_boot_entry is not None else fedora_boot_entry_id
    entry_id = _text(raw_entry).strip()
    if entry_id.casefold().startswith('boot'):
        entry_id = entry_id[4:]
    if _BOOT_ID_RE.fullmatch(entry_id) is None:
        raise ValueError('The Fedora EFI entry ID is invalid')
    if grub_timeout is not None and timeout_seconds is not None:
        raise ValueError('The GRUB timeout was supplied twice')
    raw_timeout = grub_timeout if grub_timeout is not None else timeout_seconds
    if raw_timeout is None:
        raw_timeout = 5
    path = fedora_boot_path if fedora_boot_path is not None else fedora_efi_entry_path
    if path is None:
        path = FEDORA_EFI_PATH
    if fedora_efi_entry_label is not None:
        # The label is required during inventory derivation, but is not a
        # separate runtime field: the runtime revalidates the firmware entry
        # description and normalized path together.
        _normalized_label(fedora_efi_entry_label)
    if type(raw_timeout) is not int or raw_timeout != 5:
        raise ValueError('The GRUB timeout must be five seconds')
    marker = {
        'schema_version': BOOT_CHOOSER_SCHEMA_VERSION,
        'fedora_boot_entry': f'Boot{entry_id.upper()}',
        'fedora_boot_path': normalize_efi_path(path),
        'fedora_esp_partuuid': _normalized_uuid(fedora_esp_partuuid, field='The Fedora ESP PARTUUID'),
        'fedora_boot_partuuid': _normalized_uuid(fedora_boot_partuuid, field='The Fedora /boot PARTUUID'),
        'grub_entry_id': ENTRY_ID,
        'grub_timeout_style': 'menu',
        'grub_timeout': raw_timeout,
        'default_preserved': True,
    }
    if marker['fedora_esp_partuuid'] == marker['fedora_boot_partuuid']:
        raise ValueError('The Fedora ESP and /boot PARTUUIDs must be distinct')
    validate_boot_chooser_marker(marker)
    return marker


def _marker_json_bytes(marker: Mapping[str, Any]) -> bytes:
    validate_boot_chooser_marker(marker)
    try:
        encoded = (json.dumps(
            dict(marker),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ) + '\n').encode('ascii')
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError('The boot chooser marker is not JSON-safe') from error
    if len(encoded) > BOOT_CHOOSER_MAX_BYTES:
        raise ValueError('The boot chooser marker is too large')
    return encoded


def boot_chooser_marker_bytes(marker: Mapping[str, Any]) -> bytes:
    """Serialize a validated marker to bounded deterministic UTF-8 bytes."""

    return _marker_json_bytes(marker)


def validate_marker(marker: Any) -> bool:
    """Compatibility spelling for callers that use the short marker name."""

    return validate_boot_chooser_marker(marker)


def validate_boot_chooser_marker(marker: Any) -> bool:
    """Return whether *marker* is exactly the supported bounded schema."""

    if not isinstance(marker, Mapping) or set(marker) != _BOOT_CHOOSER_KEYS:
        return False
    if marker.get('schema_version') != BOOT_CHOOSER_SCHEMA_VERSION:
        return False
    entry_id = marker.get('fedora_boot_entry')
    path = marker.get('fedora_boot_path')
    esp = marker.get('fedora_esp_partuuid')
    boot = marker.get('fedora_boot_partuuid')
    if (
        not isinstance(entry_id, str)
        or not re.fullmatch(r'Boot[0-9A-F]{4}', entry_id)
        or entry_id[4:] != entry_id[4:].upper()
        or path != FEDORA_EFI_PATH
        or not isinstance(esp, str)
        or _UUID_RE.fullmatch(esp) is None
        or not isinstance(boot, str)
        or _UUID_RE.fullmatch(boot) is None
        or esp == boot
        or marker.get('grub_entry_id') != ENTRY_ID
        or marker.get('grub_timeout_style') != 'menu'
        or type(marker.get('grub_timeout')) is not int
        or marker.get('grub_timeout') != 5
        or marker.get('default_preserved') is not True
    ):
        return False
    try:
        # UUID case is presentation-only.  The builder canonicalizes it, while
        # validation accepts a hand-preserved runtime record from older
        # installers that used uppercase GPT identifiers.
        uuid.UUID(esp)
        uuid.UUID(boot)
    except (AttributeError, TypeError, ValueError):
        return False
    try:
        return len(_marker_json_bytes_unchecked(marker)) <= BOOT_CHOOSER_MAX_BYTES
    except (TypeError, ValueError, UnicodeError):
        return False


def _marker_json_bytes_unchecked(marker: Mapping[str, Any]) -> bytes:
    """Serialize without recursively invoking the public validator."""

    return (json.dumps(
        dict(marker),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    ) + '\n').encode('ascii')


def _mapping_value(owner: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in owner:
            return owner.get(name)
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _device_path(device: Mapping[str, Any]) -> str | None:
    value = _mapping_value(device, ('path', 'device_path', 'devpath', 'node', 'name', 'kname', 'device'))
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith('/') else '/dev/' + value


def _device_partuuid(device: Mapping[str, Any]) -> str | None:
    value = _mapping_value(device, ('partuuid', 'PARTUUID', 'partition_uuid', 'partition_guid'))
    return value if isinstance(value, str) and value.strip() else None


def _flatten_maps(value: Any, *, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    pending = [value]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, Mapping):
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            if any(key in candidate for key in keys):
                result.append(candidate)
            for key in ('blockdevices', 'block_devices', 'devices', 'children', 'partitions', 'filesystems', 'mounts'):
                nested = candidate.get(key)
                if nested is not None:
                    pending.append(nested)
        elif isinstance(candidate, (list, tuple)):
            pending.extend(candidate)
    return result


def _extract_table(value: Any) -> Mapping[str, Any] | None:
    pending = [value]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, Mapping):
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            if candidate.get('label') == 'gpt' and isinstance(candidate.get('partitions'), list):
                return candidate
            for key in ('partition_table', 'sfdisk_table', 'partitiontable', 'sfdisk', 'table', 'target', 'inventory'):
                nested = candidate.get(key)
                if nested is not None:
                    pending.append(nested)
        elif isinstance(candidate, (list, tuple)):
            pending.extend(candidate)
    return None


def _inventory_and_table(source: Mapping[str, Any], table: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    inventory = source.get('inventory') if isinstance(source.get('inventory'), Mapping) else source
    selected_table = table if isinstance(table, Mapping) else _extract_table(source)
    if not isinstance(inventory, Mapping):
        raise ValueError('The Fedora preflight inventory is unavailable')
    return inventory, selected_table


def _efi_inventory(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ('efi', 'efibootmgr', 'bootloader'):
        candidate = inventory.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    raise ValueError('The Fedora EFI inventory is unavailable')


def _entry_label(entry: Mapping[str, Any]) -> str:
    value = _mapping_value(entry, ('label', 'description', 'name'))
    if not isinstance(value, str):
        return ''
    return value.strip()


def _entry_path(entry: Mapping[str, Any], description: str) -> str | None:
    value = _mapping_value(entry, ('path', 'efi_path', 'loader_path'))
    if isinstance(value, str) and value.strip():
        return value.strip()
    match = re.search(r'File\(([^)]*)\)', description, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _entry_gpt_guid(description: str) -> str | None:
    matches = _GPT_GUID_RE.findall(description)
    if len(matches) > 1:
        raise ValueError('The Fedora EFI entry has ambiguous GPT identities')
    if not matches:
        if 'gpt,' in description.casefold():
            raise ValueError('The Fedora EFI entry GPT identity is malformed')
        return None
    return _normalized_uuid(matches[0], field='The Fedora EFI GPT identity')


def _select_fedora_entry(efi: Mapping[str, Any], esp_partuuid: str | None = None) -> tuple[str, str, str]:
    entries = _mapping_value(efi, ('entries', 'boot_entries'))
    if not isinstance(entries, list) or not entries:
        raise ValueError('The Fedora EFI entry inventory is unavailable')
    candidates: list[tuple[str, str, str, str | None]] = []
    mismatched_path = False
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        description = _text(_mapping_value(entry, ('description', 'raw', 'label', 'name'))).strip()
        raw_label = _entry_label(entry)
        raw_path = _entry_path(entry, description)
        fedora_hint = 'fedora' in ' '.join((raw_label, description, raw_path or '')).casefold()
        if not fedora_hint:
            continue
        identifier = _mapping_value(entry, ('id', 'number', 'boot_id', 'boot_number'))
        if entry.get('active') is False:
            raise ValueError('The Fedora EFI entry is inactive')
        label = raw_label or description
        if not isinstance(identifier, str) or _BOOT_ID_RE.fullmatch(identifier.strip()) is None:
            raise ValueError('The Fedora EFI entry ID is missing or invalid')
        if raw_path is None:
            raise ValueError('The Fedora EFI entry path is missing')
        try:
            normalized_path = normalize_efi_path(raw_path)
        except ValueError:
            mismatched_path = True
            normalized_path = raw_path
        candidates.append((identifier.strip().upper(), label, normalized_path, _entry_gpt_guid(description)))
    if mismatched_path:
        raise ValueError('The Fedora EFI entry path is not the supported shim path')
    if len(candidates) != 1:
        raise ValueError('The Fedora EFI entry is missing or ambiguous')
    identifier, label, path, entry_guid = candidates[0]
    label = _normalized_label(label)
    path = normalize_efi_path(path)
    if esp_partuuid is not None and entry_guid is not None and entry_guid != esp_partuuid:
        raise ValueError('The Fedora EFI entry GPT identity does not match the ESP')
    order = _mapping_value(efi, ('boot_order', 'order'))
    order_values = [str(item).strip().upper().removeprefix('BOOT') for item in _as_list(order)]
    if isinstance(order, str):
        order_values = [item.upper() for item in re.findall(r'[0-9A-Fa-f]{4}', order)]
    if not order_values or identifier not in order_values:
        raise ValueError('The Fedora EFI entry is not present in BootOrder')
    return identifier, label, path


def _inventory_mounts(inventory: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidate = inventory.get('mounts')
    result = _flatten_maps(candidate, keys=('target', 'mountpoint', 'source', 'device'))
    if result:
        return result
    return _flatten_maps(inventory.get('findmnt'), keys=('target', 'mountpoint', 'source', 'device'))


def _inventory_devices(inventory: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidate = inventory.get('block_devices')
    result = _flatten_maps(candidate, keys=('path', 'name', 'kname', 'type', 'partuuid', 'partn'))
    if result:
        # preflight's flattened lsblk records retain the original parent's
        # ``children`` key.  The same partition therefore appears once as a
        # direct record and once beneath its parent, with the nested copy
        # carrying parent context.  Collapse only that structural duplicate;
        # two direct records remain distinct and are rejected as ambiguous by
        # the identity checks below.
        unique: list[Mapping[str, Any]] = []
        for item in result:
            path = _device_path(item)
            partuuid = _device_partuuid(item)
            duplicate_index = next(
                (
                    index
                    for index, prior in enumerate(unique)
                    if path is not None
                    and path == _device_path(prior)
                    and partuuid is not None
                    and partuuid == _device_partuuid(prior)
                    and bool(item.get('parent_path')) != bool(prior.get('parent_path'))
                ),
                None,
            )
            if duplicate_index is None:
                unique.append(item)
            elif item.get('parent_path'):
                # Prefer the direct preflight record, which carries the full
                # partition attributes but no synthetic parent annotation.
                continue
            else:
                unique[duplicate_index] = item
        return unique
    return _flatten_maps(
        inventory.get('devices', inventory.get('lsblk')),
        keys=('path', 'name', 'kname', 'type', 'partuuid', 'partn'),
    )


def _mount_for(inventory: Mapping[str, Any], target: str) -> Mapping[str, Any]:
    return _mount_for_targets(inventory, (target,), target)


def _mount_for_targets(
    inventory: Mapping[str, Any], targets: Sequence[str], role: str
) -> Mapping[str, Any]:
    wanted = set(targets)
    matches = [item for item in _inventory_mounts(inventory) if _mapping_value(item, ('target', 'mountpoint')) in wanted]
    if len(matches) != 1:
        raise ValueError(f'The Fedora {role} mount identity is missing or ambiguous')
    return matches[0]


def _table_partition_for_mount(
    mount: Mapping[str, Any],
    table: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    role: str,
) -> str:
    partitions = table.get('partitions')
    if not isinstance(partitions, list) or not partitions:
        raise ValueError('The Fedora GPT partition inventory is unavailable')
    source = _mapping_value(mount, ('source', 'SOURCE', 'device', 'path'))
    source_text = source.strip() if isinstance(source, str) else ''
    source_text = source_text.split('[', 1)[0]
    mount_guid = _mapping_value(mount, ('partuuid', 'PARTUUID', 'partition_uuid', 'partition_guid'))
    if isinstance(mount_guid, str) and mount_guid.startswith('PARTUUID='):
        mount_guid = mount_guid[9:]
    normalized_mount_guid = _normalized_uuid(mount_guid, field=f'The Fedora {role} PARTUUID') if mount_guid else None
    source_guid = None
    if source_text.startswith('PARTUUID='):
        source_guid = source_text[9:]
    elif source_text.startswith('/dev/disk/by-partuuid/'):
        source_guid = source_text.rsplit('/', 1)[-1]
    if source_guid is not None:
        normalized_source_guid = _normalized_uuid(source_guid, field=f'The Fedora {role} PARTUUID')
        if normalized_mount_guid is not None and normalized_mount_guid != normalized_source_guid:
            raise ValueError(f'The Fedora {role} mount identities disagree')
        normalized_mount_guid = normalized_source_guid

    device_matches: list[Mapping[str, Any]] = []
    if source_text and source_text.startswith('/') and not source_text.startswith('/dev/disk/'):
        device_matches = [item for item in _inventory_devices(inventory) if _device_path(item) == source_text]
    if normalized_mount_guid is not None:
        by_guid = [
            item for item in _inventory_devices(inventory)
            if isinstance(_device_partuuid(item), str)
            and _normalized_uuid(_device_partuuid(item), field=f'The Fedora {role} PARTUUID') == normalized_mount_guid
        ]
        if device_matches and by_guid and {id(item) for item in device_matches} != {id(item) for item in by_guid}:
            raise ValueError(f'The Fedora {role} mount source does not match its PARTUUID')
        if not device_matches:
            device_matches = by_guid
    if len(device_matches) > 1:
        # Even duplicate records carrying the same GUID are ambiguous here:
        # the marker must be tied to one inventory observation, not whichever
        # duplicate a later consumer happens to select.
        raise ValueError(f'The Fedora {role} device identity is ambiguous')
    device_guid = None
    if device_matches:
        device_guid = _normalized_uuid(_device_partuuid(device_matches[0]), field=f'The Fedora {role} PARTUUID')
        if normalized_mount_guid is not None and device_guid != normalized_mount_guid:
            raise ValueError(f'The Fedora {role} device identity does not match its mount')
        normalized_mount_guid = device_guid

    table_matches = []
    for part in partitions:
        if not isinstance(part, Mapping):
            continue
        part_path = _device_path(part)
        part_guid = _mapping_value(part, ('uuid', 'partuuid', 'partition_uuid', 'partition_guid'))
        if source_text and source_text.startswith('/') and part_path == source_text:
            table_matches.append(part)
        elif normalized_mount_guid is not None and isinstance(part_guid, str):
            try:
                if _normalized_uuid(part_guid, field=f'The Fedora {role} PARTUUID') == normalized_mount_guid:
                    table_matches.append(part)
            except ValueError:
                raise
    if len(table_matches) != 1:
        raise ValueError(f'The Fedora {role} GPT partition is missing or ambiguous')
    part_guid = _mapping_value(table_matches[0], ('uuid', 'partuuid', 'partition_uuid', 'partition_guid'))
    normalized_part_guid = _normalized_uuid(part_guid, field=f'The Fedora {role} PARTUUID')
    expected_number = 1 if role == 'ESP' else 2
    part_number = _mapping_value(table_matches[0], ('number', 'partn', 'partition_number'))
    if part_number is None:
        part_path = _device_path(table_matches[0]) or ''
        number_match = re.search(r'(?:p|)([0-9]+)\Z', part_path)
        part_number = int(number_match.group(1)) if number_match else None
    if part_number != expected_number:
        raise ValueError(f'The Fedora {role} GPT partition is not in the validated slot')
    fstype = _mapping_value(mount, ('fstype', 'FSTYPE', 'filesystem', 'fs_type'))
    if isinstance(fstype, str) and fstype.strip().lower() not in (
        {'vfat', 'fat', 'fat32'} if role == 'ESP' else {'ext4', 'xfs'}
    ):
        raise ValueError(f'The Fedora {role} mount filesystem type is invalid')
    if normalized_mount_guid is not None and normalized_part_guid != normalized_mount_guid:
        raise ValueError(f'The Fedora {role} GPT identity does not match its mount')
    if device_guid is not None and normalized_part_guid != device_guid:
        raise ValueError(f'The Fedora {role} GPT identity does not match its device')
    return normalized_part_guid


def derive_boot_chooser_marker(source: Mapping[str, Any], table: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derive a marker from a validated plan/preflight inventory.

    Fedora's EFI entry, ESP mount, /boot mount, block-device IDs, and GPT
    table must agree.  Any missing or duplicate observation is rejected so a
    caller cannot silently choose a similarly named entry or partition.
    """

    if not isinstance(source, Mapping):
        raise ValueError('The Fedora preflight plan is invalid')
    inventory, selected_table = _inventory_and_table(source, table)
    if selected_table is None:
        raise ValueError('The Fedora GPT inventory is unavailable')
    partitions = selected_table.get('partitions')
    if not isinstance(partitions, list):
        raise ValueError('The Fedora GPT partition inventory is unavailable')
    table_guids = [
        _normalized_uuid(_mapping_value(part, ('uuid', 'partuuid', 'partition_uuid', 'partition_guid')), field='The Fedora GPT PARTUUID')
        for part in partitions
        if isinstance(part, Mapping)
    ]
    if len(table_guids) != len(partitions) or len(set(table_guids)) != len(table_guids):
        raise ValueError('The Fedora GPT partition identities are missing or duplicated')
    esp_mount = _mount_for_targets(inventory, ('/boot/efi', '/efi'), 'ESP')
    boot_mount = _mount_for(inventory, '/boot')
    esp_partuuid = _table_partition_for_mount(esp_mount, selected_table, inventory, role='ESP')
    boot_partuuid = _table_partition_for_mount(boot_mount, selected_table, inventory, role='/boot')
    entry_id, entry_label, entry_path = _select_fedora_entry(_efi_inventory(inventory), esp_partuuid)
    return build_boot_chooser_marker(
        fedora_boot_entry_id=entry_id,
        fedora_efi_entry_label=entry_label,
        fedora_efi_entry_path=entry_path,
        fedora_esp_partuuid=esp_partuuid,
        fedora_boot_partuuid=boot_partuuid,
    )


def build_marker(
    *,
    fedora_boot_entry: Any,
    fedora_boot_partuuid: Any,
    fedora_esp_partuuid: Any,
    grub_timeout: Any,
    fedora_boot_path: Any = FEDORA_EFI_PATH,
) -> dict[str, Any]:
    """Compatibility spelling matching the installed runtime helper."""

    return build_boot_chooser_marker(
        fedora_boot_entry=fedora_boot_entry,
        fedora_boot_path=fedora_boot_path,
        fedora_esp_partuuid=fedora_esp_partuuid,
        fedora_boot_partuuid=fedora_boot_partuuid,
        grub_timeout=grub_timeout,
    )


def render(esp_uuid):
    # FAT volume IDs from blkid. Do not interpolate arbitrary paths or labels
    # into shell or GRUB source.
    if not isinstance(esp_uuid, str) or not re.fullmatch(r'[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}', esp_uuid):
        raise ValueError('Expected the verified Zeus FAT filesystem UUID')
    return f'''#!/bin/sh
# Managed by Zeus dual-boot installer. Fedora retains boot ownership.
cat <<'ZEUS_GRUB_ENTRY'
menuentry 'Zeus OS' --id '{ENTRY_ID}' {{
    insmod part_gpt
    insmod fat
    insmod chain
    search --no-floppy --fs-uuid --set=zeus_esp {esp_uuid.upper()}
    chainloader ($zeus_esp)/EFI/fedora/shimx64.efi
}}
ZEUS_GRUB_ENTRY
'''


def visible_menu_config(existing):
    """Modify only menu presentation; retain every default selection setting."""
    lines = existing.splitlines()
    keys = {'GRUB_TIMEOUT_STYLE': 'menu', 'GRUB_TIMEOUT': '5'}
    seen = set()
    result = []
    for line in lines:
        match = re.match(r'^\s*(GRUB_TIMEOUT_STYLE|GRUB_TIMEOUT)\s*=', line)
        if match:
            key = match.group(1)
            if key not in seen:
                result.append(f'{key}={keys[key]}')
                seen.add(key)
        else:
            result.append(line)
    result.extend(f'{key}={value}' for key, value in keys.items() if key not in seen)
    return '\n'.join(result) + '\n'
