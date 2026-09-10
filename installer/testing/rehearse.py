#!/usr/bin/python3
"""Run storage experiments ONLY in the owned QEMU Fedora fixture (VM 117).

This is not the downloadable installer. It deliberately refuses physical
machines and requires a separately verified pre-experiment backup receipt.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zeus_installer import storage

STATE = Path('/var/lib/zeus-dualboot-rehearsal')


def run(args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-2000:]}')
    return result.stdout


def save(value):
    STATE.mkdir(mode=0o700, exist_ok=True)
    temp = STATE / 'journal.tmp'
    with temp.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(STATE / 'journal.json')
    fd = os.open(STATE, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def guard():
    if os.geteuid() != 0:
        raise RuntimeError('Root is required in the disposable fixture')
    if Path('/sys/class/dmi/id/sys_vendor').read_text().strip() not in ('QEMU', 'Red Hat'):
        raise RuntimeError('This experiment refuses physical machines')
    marker = Path('/etc/zeus-dualboot-fixture.json')
    metadata = marker.lstat()
    if marker.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError('Unsafe fixture ownership')
    config = json.loads(marker.read_text())
    if config.get('vmid') != 117 or config.get('purpose') != 'zeus-dualboot-rehearsal':
        raise RuntimeError('Not the disposable Zeus fixture')
    receipt = json.loads(Path('/etc/zeus-dualboot-backup.json').read_text())
    if receipt.get('vmid') != 117 or receipt.get('verified') is not True or not receipt.get('backup_target'):
        raise RuntimeError('Verified fixture backup receipt required')


def current_table(disk):
    return json.loads(run(['/usr/sbin/sfdisk', '--json', disk]))['partitiontable']


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('shrink', 'allocate', 'status'))
    parser.add_argument('--disk', default='/dev/sda')
    parser.add_argument('--allocation-gib', type=int, default=128)
    args = parser.parse_args()
    guard()
    journal = STATE / 'journal.json'
    if args.action == 'status':
        print(journal.read_text() if journal.exists() else '{"phase":"not_started"}')
        return
    if args.action == 'shrink':
        if journal.exists():
            raise RuntimeError('An experiment already exists; inspect it instead of repeating shrink')
        original = current_table(args.disk)
        proposal = storage.layout(original, args.allocation_gib)
        mounted = json.loads(run(['/usr/bin/findmnt', '--json', '--target', '/', '--output', 'SOURCE,FSTYPE']))['filesystems'][0]
        if mounted['fstype'] != 'btrfs' or mounted['source'].split('[')[0] != proposal['fedora_partition']:
            raise RuntimeError('Fedora root does not match the proposed Btrfs partition')
        record = {'original': original, 'proposal': proposal, 'boot_id': boot_id(),
                  'phase': 'before_filesystem_shrink', 'new_guids': [str(uuid.uuid4()) for _ in range(3)]}
        save(record)
        # Leave 16 MiB between the filesystem boundary and the new partition end.
        limit = proposal['fedora_filesystem_limit_bytes'] - 16 * storage.MIB
        run(['/usr/sbin/btrfs', 'filesystem', 'resize', str(limit), '/'])
        run(['/usr/sbin/btrfs', 'filesystem', 'sync', '/'])
        superblock = run(['/usr/sbin/btrfs', 'inspect-internal', 'dump-super', proposal['fedora_partition']])
        values = [line.split()[-1] for line in superblock.splitlines() if line.strip().startswith('dev_item.total_bytes')]
        if len(values) != 1:
            raise RuntimeError('Cannot verify actual filesystem device size')
        actual = int(values[0])
        record.update(phase='before_partition_end_change', actual_filesystem_bytes=actual)
        save(record)
        run(storage.shrink_partition_command(proposal, actual),
            storage.shrink_partition_input(proposal, actual))
        record['phase'] = 'reboot_required'
        save(record)
        print('Reboot Fedora, then run allocate. No new partitions have been created.')
        return
    record = json.loads(journal.read_text())
    if record['phase'] != 'reboot_required':
        raise RuntimeError('Expected the reboot-required phase; refuse ambiguous replay')
    proposal = record['proposal']
    actual_kernel_size = int(run(['/usr/sbin/blockdev', '--getsize64', proposal['fedora_partition']]))
    storage.verify_after_reboot(record['original'], current_table(proposal['disk']), proposal,
                                record['boot_id'], boot_id(), actual_kernel_size)
    record['phase'] = 'before_partition_append'
    save(record)
    run(['/usr/sbin/sfdisk', '--append', '--no-reread', '--no-tell-kernel', proposal['disk']],
        storage.append_input(proposal, record['new_guids']))
    run(['/usr/sbin/partx', '--add', '--nr', '4:6', proposal['disk']])
    run(['/usr/bin/udevadm', 'settle'])
    actual = current_table(proposal['disk'])
    if actual['partitions'][:3] != record['original']['partitions'][:2] + [dict(record['original']['partitions'][2], size=proposal['fedora_new_size'])]:
        raise RuntimeError('Existing partition geometry changed unexpectedly')
    for part, guid in zip(proposal['partitions'], record['new_guids']):
        entry = actual['partitions'][part['number']-1]
        if (entry['uuid'].lower(), entry['start'], entry['size']) != (guid.lower(), part['start'], part['size']):
            raise RuntimeError('New partition identity or bounds mismatch')
    record['phase'] = 'allocated_unformatted'
    save(record)
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
