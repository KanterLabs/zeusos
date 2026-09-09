#!/usr/bin/env python3
"""Validate the installed standalone package without login or model requests."""
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import time
import tomllib


def validate():
    lock = json.loads(Path('/usr/share/zeus/codex.lock.json').read_text())
    package = Path('/usr/lib/codex')
    command = Path('/usr/bin/codex')
    if not command.is_symlink() or command.resolve() != package / 'bin/codex':
        raise ValueError('codex command does not resolve to the image package')
    verified = []
    for entry in lock['directories'] + lock['files']:
        path = package / entry['path']
        metadata = path.lstat()
        is_file = 'sha256' in entry
        expected_type = stat.S_ISREG if is_file else stat.S_ISDIR
        if not expected_type(metadata.st_mode):
            raise ValueError(f'Unexpected file type: {entry["path"]}')
        if (metadata.st_uid, metadata.st_gid) != (0, 0):
            raise ValueError(f'Package is not root-owned: {entry["path"]}')
        if stat.S_IMODE(metadata.st_mode) != entry['mode']:
            raise ValueError(f'Unexpected permissions: {entry["path"]}')
        if is_file:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if metadata.st_size != entry['size'] or digest != entry['sha256']:
                raise ValueError(f'Package content differs: {entry["path"]}')
            verified.append(entry['path'])
    metadata = json.loads((package / 'codex-package.json').read_text())
    if (metadata['version'], metadata['target'], metadata['entrypoint']) != (
        lock['version'], lock['target'], 'bin/codex'
    ):
        raise ValueError('Upstream package metadata differs from lock')
    receipt = json.loads((package / 'zeus-package-receipt.json').read_text())
    if any(receipt.get(key) != lock[key] for key in ('version', 'release_tag', 'target')):
        raise ValueError('Installed receipt differs from the locked release')
    if any(receipt['source'].get(key) != lock[key] for key in ('url', 'size', 'sha256')):
        raise ValueError('Installed receipt differs from the locked archive')
    defaults = tomllib.loads(Path('/etc/codex/config.toml').read_text())
    if defaults.get('check_for_update_on_startup') is not False:
        raise ValueError('Codex startup update checks must default off')
    started = time.monotonic()
    version = subprocess.run([str(command), '--version'], check=True,
                             capture_output=True, text=True, timeout=20).stdout.strip()
    version_seconds = time.monotonic() - started
    if version != 'codex-cli ' + lock['version']:
        raise ValueError('CLI version differs from the locked release')
    for args in (['--help'], ['sandbox', '--help'], ['login', '--help']):
        result = subprocess.run([str(command), *args], check=True,
                                capture_output=True, text=True, timeout=20)
        if 'Usage:' not in result.stdout:
            raise ValueError(f'Missing help output for {args}')
    return {
        'ok': True, 'version': version, 'target': lock['target'],
        'archive_sha256': lock['sha256'], 'verified_files': verified,
        'installed_package_bytes': sum(entry['size'] for entry in lock['files']),
        'version_wall_seconds': round(version_seconds, 4),
        'offline_help': ['main', 'sandbox', 'login'],
        'startup_update_checks_default': False,
        'authentication_performed': False, 'model_request_performed': False,
    }


if __name__ == '__main__':
    print(json.dumps(validate(), indent=2))
