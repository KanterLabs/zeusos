#!/usr/bin/python3 -I
"""Qualification harness for the disposable VM117; never packaged for owners.

Run the real backend and executor against the fixed, backed-up fixture. This
explicit trial does not enable the installed application's production gate.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, '/usr/lib/zeus-installer')
from zeus_installer import preflight
from zeus_installer.backend import InstallerBackend
from zeus_installer.executor import DualBootExecutor
from zeus_installer import efi_update
from zeus_installer import storage


def fixture():
    if os.geteuid() != 0:
        raise SystemExit('The fixed VM117 qualification requires root.')
    marker = Path('/etc/zeus-dualboot-fixture.json')
    st = marker.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
        raise SystemExit('Invalid fixture marker ownership.')
    value = json.loads(marker.read_text())
    if value != {'vmid': 117, 'purpose': 'zeus-dualboot-rehearsal'}:
        raise SystemExit('This is not the authorized fixture.')
    dmi = Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()
    if dmi != '08325676-16c6-4890-a203-631a88e6a926':
        raise SystemExit('VM117 machine identity mismatch.')


def check_files():
    manifest = json.loads(Path('/var/lib/zeus-fixture/baseline-files.json').read_text())
    failed = []
    for name, expected in manifest.items():
        digest = hashlib.sha256()
        with Path(name).open('rb') as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected['sha256']:
            failed.append(name)
    return {'ok': not failed, 'verified_files': len(manifest), 'changed_files': failed}


def service():
    trial = {
        'scope': 'VM117 qualification trial; no physical qualification asserted',
        'vmid': 117,
        'build_id': 'git-f080c2d9bc53',
        'manifest_digest': 'sha256:8797860dc4c27c7e8e3f0034bfcf71f9876401df809509c6b49588752c9c1c18',
        'bootc_version': '1.16.10',
        'bootupd_version': '0.2.35',
    }
    executor = DualBootExecutor(
        qualified=True, qualification_receipt=trial,
        efi_update=efi_update.run_update, inventory_provider=preflight.collect,
    )
    return InstallerBackend(maintenance_executor=executor, inventory_provider=preflight.collect)


def recover_partition_end(backend):
    """One explicit repair of the recorded unsupported-option fixture failure."""
    with backend.journal.lock():
        record = backend.journal.load()
        state = record['executor_state']
        last = record['commands'][-1]
        if (record['phase'] != 'error' or state['status'] != 'in_progress'
                or state['action'] != 'partition_end_change'
                or '--part-size' not in last['argv'] or last['returncode'] != 1):
            raise RuntimeError('Not the inspected unsupported-option failure.')
        if not check_files()['ok']:
            raise RuntimeError('A baseline file changed; refuse fixture recovery.')
        proposal = state['proposal']
        current = json.loads(subprocess.check_output(
            ['/usr/sbin/sfdisk', '--json', proposal['disk']], text=True))['partitiontable']
        if current != state['original_table']:
            raise RuntimeError('GPT is not exactly the unchanged original table.')
        executor = backend.maintenance_executor
        actual = executor._btrfs_superblock_size(backend.command_runner, proposal['fedora_partition'])
        if actual != state['filesystem_limit_bytes']:
            raise RuntimeError('Filesystem is not at the inspected shrunk boundary.')
        command = storage.shrink_partition_command(proposal, actual)
        script = storage.shrink_partition_input(proposal, actual)
        subprocess.run([*command[:-1], '--no-act', command[-1]], input=script,
                       text=True, capture_output=True, check=True)
        with executor._write_inhibitor():
            subprocess.run(command, input=script, text=True, capture_output=True, check=True)
            current = json.loads(subprocess.check_output(
                ['/usr/sbin/sfdisk', '--json', proposal['disk']], text=True))['partitiontable']
            if current != state['expected_table']:
                raise RuntimeError('Unexpected GPT after the explicit end-only repair.')
            state.update(phase='reboot_required', status='complete', action=None, reboot_required=True)
            state['fixture_recovery'] = {
                'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'reason': 'Inspected unsupported sfdisk option; original GPT and all file hashes proved unchanged.',
                'commands': [command],
            }
            record['executor_state'] = state
            backend.journal.write(record)
            result = executor._reboot_result(state)
            backend.journal.transition(record, 'reboot_required', executor_result=result,
                                       message='Fixture-only inspected command repair completed; reboot Fedora.')
        return {'ok': True, 'phase': 'reboot_required', 'fixture_recovery': state['fixture_recovery']}


def main():
    fixture()
    action = sys.argv[1] if len(sys.argv) == 2 else ''
    if action == 'files':
        return check_files()
    backend = service()
    if action == 'prepare':
        plan = preflight.plan(preflight.collect(), allocation_gib=128)
        if not plan['supported']:
            return {'ok': False, 'blockers': plan['blockers']}
        return backend.prepare(plan)
    if action == 'retry_prepare':
        return backend.retry_prepare()
    if action == 'status':
        return backend.status()
    if action == 'install':
        return backend.install()
    if action == 'recover_partition_end':
        return recover_partition_end(backend)
    raise SystemExit('Usage: run-vm117.py prepare|retry_prepare|status|install|files|recover_partition_end')


if __name__ == '__main__':
    try:
        result = main()
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get('ok', True) else 1)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': getattr(error, 'code', type(error).__name__), 'message': str(error)}))
        raise
