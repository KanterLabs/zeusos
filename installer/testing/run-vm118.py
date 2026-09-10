#!/usr/bin/python3 -I
"""Qualification harness for the disposable VM118; never packaged for owners.

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
        raise SystemExit('The fixed VM118 qualification requires root.')
    marker = Path('/etc/zeus-dualboot-fixture.json')
    st = marker.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
        raise SystemExit('Invalid fixture marker ownership.')
    value = json.loads(marker.read_text())
    if value != {'vmid': 118, 'purpose': 'zeus-dualboot-rehearsal'}:
        raise SystemExit('This is not the authorized fixture.')
    dmi = Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()
    if dmi != '29f40b00-bc18-4ff3-82da-1d9006d7db4c':
        raise SystemExit('VM118 machine identity mismatch.')


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
    # Trial permission is confined to this process after exact VM118 proof.
    # Use the same service graph as the installed privileged helper.
    from zeus_installer import backend as module
    module.QUALIFIED = True
    return module.make_service()


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
    raise SystemExit('Usage: run-vm118.py prepare|retry_prepare|status|install|files')


if __name__ == '__main__':
    try:
        result = main()
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get('ok', True) else 1)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': getattr(error, 'code', type(error).__name__), 'message': str(error)}))
        raise
