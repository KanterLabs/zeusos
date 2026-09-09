#!/usr/bin/env python3
"""Offline, unprivileged sandbox checks using only disposable test files."""
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def run():
    if os.geteuid() == 0:
        raise SystemExit('Run as a normal desktop user, not root')
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # A sibling of the workspace under the user's home is outside the profile's
    # writable roots. /tmp would be an invalid negative-write test.
    with tempfile.TemporaryDirectory(prefix='.zeus-codex-check-', dir=Path.home()) as name:
        root = Path(name)
        workspace = root / 'project'
        workspace.mkdir()
        outside = root / 'outside.txt'
        outside.write_text('preserve this fixture\n')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(2)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2):
                connection, _ = listener.accept()
                connection.close()
            probe = '''import errno,json,pathlib,socket,sys
workspace,outside,port=sys.argv[1:]
pathlib.Path(workspace,'allowed.txt').write_text('workspace write passed\\n')
try:
    pathlib.Path(outside).write_text('unexpected write')
except OSError as e:
    assert e.errno in (errno.EACCES,errno.EPERM,errno.EROFS), e
else:
    raise RuntimeError('Sandbox allowed a write outside the workspace')
try:
    socket.create_connection(('127.0.0.1',int(port)),timeout=2).close()
except OSError:
    pass
else:
    raise RuntimeError('Sandbox could connect to the host listener')
print(json.dumps({'workspace_write':True,'outside_write_denied':True,'host_network_denied':True}))
'''
            command = ['codex', 'sandbox', '-P', ':workspace', '-C', str(workspace),
                       '--', '/usr/bin/python3', '-c', probe, str(workspace),
                       str(outside), str(address[1])]
            result = subprocess.run(command, check=True, capture_output=True,
                                    text=True, timeout=30)
            checks = json.loads(result.stdout)
        if outside.read_text() != 'preserve this fixture\n':
            raise RuntimeError('Outside fixture changed')
        if (workspace / 'allowed.txt').read_text() != 'workspace write passed\n':
            raise RuntimeError('Allowed workspace write did not persist')
        readonly_probe = '''import errno,pathlib,sys
try:
    pathlib.Path(sys.argv[1],'readonly-denied.txt').write_text('unexpected')
except OSError as e:
    assert e.errno in (errno.EACCES,errno.EPERM,errno.EROFS), e
else:
    raise RuntimeError('Read-only profile allowed a workspace write')
print('read-only denied workspace write')
'''
        result = subprocess.run(
            ['codex', 'sandbox', '-P', ':read-only', '-C', str(workspace), '--',
             '/usr/bin/python3', '-c', readonly_probe, str(workspace)],
            check=True, capture_output=True, text=True, timeout=30)
        if result.stdout.strip() != 'read-only denied workspace write':
            raise RuntimeError('Read-only probe did not finish')
        checks['read_only_write_denied'] = True
    return {'ok': True, 'started_at_utc': started,
            'completed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'uid': os.geteuid(), 'checks': checks, 'fixture_removed': not root.exists(),
            'authentication_performed': False, 'model_request_performed': False}


if __name__ == '__main__':
    try:
        print(json.dumps(run(), indent=2))
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        raise SystemExit(error.returncode)
