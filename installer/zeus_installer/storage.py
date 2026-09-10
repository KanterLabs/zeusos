"""Exact-sector storage planning; execution belongs to the journaled installer.

No command in this module is executed. Existing partitions are never recreated:
the sole proposed Fedora change is its final partition's end, after a verified
filesystem shrink and before a mandatory reboot.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

GIB = 1024 ** 3
MIB = 1024 ** 2
EFI = 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'
LINUX = '0FC63DAF-8483-4772-8E79-3D69D8477DE4'


class StorageError(ValueError):
    pass


def integer(value, name):
    if type(value) is not int or value <= 0:
        raise StorageError(f'{name} must be a positive integer')
    return value


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def layout(table, allocation_gib=128):
    """Validate an sfdisk JSON table and reserve a tail allocation.

    Input is the value of sfdisk's ``partitiontable`` key. This only computes
    geometry, never establishes whether the filesystem can actually shrink.
    """
    if table.get('label') != 'gpt' or table.get('unit') != 'sectors':
        raise StorageError('Only GPT with sector geometry is supported')
    try:
        uuid.UUID(table['id'])
    except (KeyError, ValueError, AttributeError):
        raise StorageError('Missing GPT disk identity') from None
    device = table.get('device', '')
    if not re.fullmatch(r'/dev/(?:nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+)', device):
        raise StorageError('Unsupported disk path')
    sector = integer(table.get('sectorsize'), 'sector size')
    if sector not in (512, 4096):
        raise StorageError('Unsupported logical sector size')
    first = integer(table.get('firstlba'), 'first usable sector')
    last = integer(table.get('lastlba'), 'last usable sector')
    parts = table.get('partitions')
    if not isinstance(parts, list) or len(parts) != 3:
        raise StorageError('Expected precisely ESP, Fedora boot and Fedora root')
    suffix = 'p' if device[-1].isdigit() else ''
    previous_end = first
    for index, part in enumerate(parts, 1):
        start = integer(part.get('start'), 'partition start')
        size = integer(part.get('size'), 'partition size')
        if part.get('node') != f'{device}{suffix}{index}' or start < previous_end or start + size > last + 1:
            raise StorageError('Unexpected, overlapping or out-of-bounds partition geometry')
        try:
            uuid.UUID(part['uuid'])
        except (KeyError, ValueError, AttributeError):
            raise StorageError('Missing partition identity') from None
        expected_type = EFI if index == 1 else LINUX
        if str(part.get('type', '')).upper() != expected_type:
            raise StorageError('Unexpected partition type')
        previous_end = start + size
    allocation = integer(allocation_gib, 'allocation') * GIB
    if allocation < 64 * GIB:
        raise StorageError('Allocate at least 64 GiB for Zeus')
    alignment = MIB // sector
    # Use the old partition end rather than optimistic disk free-space estimates.
    old_end = parts[2]['start'] + parts[2]['size']
    new_end = ((old_end - allocation // sector) // alignment) * alignment
    if new_end - parts[2]['start'] < 64 * GIB // sector:
        raise StorageError('Allocation leaves too little Fedora capacity')
    esp_size = GIB // sector
    boot_size = 2 * GIB // sector
    root_start = new_end + esp_size + boot_size
    root_size = old_end - root_start
    if root_size < 56 * GIB // sector:
        raise StorageError('Zeus root is below the image minimum')
    additions = []
    for number, start, size, kind, name in (
        (4, new_end, esp_size, EFI, 'Zeus EFI'),
        (5, new_end + esp_size, boot_size, LINUX, 'Zeus boot'),
        (6, root_start, root_size, LINUX, 'Zeus root'),
    ):
        additions.append({'number': number, 'node': f'{device}{suffix}{number}',
                          'start': start, 'size': size, 'type': kind, 'name': name})
    return {'schema_version': 1, 'disk': device, 'disk_guid': table['id'],
            'table_fingerprint': canonical_hash(table), 'sector_size': sector,
            'fedora_partition': parts[2]['node'], 'fedora_start': parts[2]['start'],
            'fedora_original_size': parts[2]['size'],
            'fedora_new_size': new_end - parts[2]['start'],
            'fedora_filesystem_limit_bytes': (new_end - parts[2]['start']) * sector,
            'partitions': additions}


def shrink_partition_command(proposal, actual_filesystem_bytes):
    """Generate the GPT end change only after checking the actual Btrfs bound."""
    integer(actual_filesystem_bytes, 'actual filesystem size')
    if actual_filesystem_bytes > proposal['fedora_filesystem_limit_bytes']:
        raise StorageError('Filesystem still exceeds the proposed partition boundary')
    return ['/usr/sbin/sfdisk', '--no-reread', '--no-tell-kernel', '--lock',
            '--wipe', 'never', '--wipe-partitions', 'never', '-N', '3', proposal['disk']]


def shrink_partition_input(proposal, actual_filesystem_bytes):
    """Set only partition 3's size; sfdisk -N retains unspecified fields."""
    shrink_partition_command(proposal, actual_filesystem_bytes)
    integer(proposal.get('fedora_new_size'), 'new Fedora partition size')
    return f'size={proposal["fedora_new_size"]}\n'


def verify_after_reboot(original, current, proposal, old_boot_id, boot_id, kernel_partition_bytes):
    if not old_boot_id or old_boot_id == boot_id:
        raise StorageError('Reboot into Fedora is required before allocating Zeus space')
    expected = json.loads(json.dumps(original))
    expected['partitions'][2]['size'] = proposal['fedora_new_size']
    if current != expected:
        raise StorageError('Partition table differs from the recorded end-only change')
    if kernel_partition_bytes != proposal['fedora_filesystem_limit_bytes']:
        raise StorageError('Kernel has not adopted the new Fedora partition size')


def append_input(proposal, partition_guids):
    """Build sfdisk input with recorded unique IDs for subsequent ownership checks."""
    if len(partition_guids) != 3 or len(set(partition_guids)) != 3:
        raise StorageError('Three distinct partition identities are required')
    lines = []
    for part, guid in zip(proposal['partitions'], partition_guids):
        try:
            guid = str(uuid.UUID(guid))
        except (ValueError, AttributeError):
            raise StorageError('Invalid partition identity') from None
        lines.append(f'start={part["start"]}, size={part["size"]}, type={part["type"]}, uuid={guid}, name="{part["name"]}"')
    return '\n'.join(lines) + '\n'
