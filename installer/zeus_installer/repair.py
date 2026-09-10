"""Stage a signed offline update on a journal-owned existing Zeus installation.

No partitioning, formatting, home replacement or Fedora boot changes occur here.
The booted Zeus applies the archive through bootc, retaining its previous image.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from types import SimpleNamespace

from . import artifacts
from .backend import Journal, InstallError

MOUNT_ROOT = Path('/run/zeus-existing-update')
PACKAGE = Path(__file__).resolve().parent
FINDMNT = '/usr/bin/findmnt'


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise InstallError('update_command_failed', 'Could not verify or mount the existing Zeus installation.')
    return result.stdout


class _ExecutorCommandRunner:
    """Adapt the small repair command API to the executor inspection API."""

    def run(self, argv, **_kwargs):
        try:
            return SimpleNamespace(returncode=0, stdout=command(list(argv)), stderr='')
        except (InstallError, OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
            return SimpleNamespace(returncode=1, stdout='', stderr=str(error))


def protected(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise InstallError('unsafe_path', 'An existing Zeus update path is not protected.')


def _mkdir_secure(path, *, mode, strict_root=True):
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            current.mkdir(mode=mode)
        except FileExistsError:
            pass
        protected(current, directory=True)


def write(path, data, mode=0o600):
    _mkdir_secure(path.parent, mode=0o755, strict_root=True)
    if path.exists() or path.is_symlink():
        protected(path)
    fd, temporary = tempfile.mkstemp(prefix='.zeus-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_record(record):
    if not isinstance(record, Mapping) or record.get('phase') != 'installed':
        raise InstallError('not_installed', 'Complete the original Zeus installation before updating it.')
    if record.get('schema_version') != 1:
        raise InstallError('invalid_state', 'The recorded Zeus installation identity is incomplete.')
    state = record.get('executor_state')
    if not isinstance(state, Mapping):
        raise InstallError('invalid_state', 'The recorded Zeus installation identity is incomplete.')
    if (state.get('schema_version') != 1 or state.get('executor') != 'zeus-dualboot'
            or state.get('stage') != 2 or state.get('phase') != 'installed'
            or state.get('status') != 'complete'):
        raise InstallError('invalid_state', 'The recorded Zeus installation is not complete.')
    table = state.get('allocated_table')
    parts = table.get('partitions', []) if isinstance(table, Mapping) else []
    uuids = state.get('filesystem_uuids')
    from .executor import _valid_finalization_fat_uuid, _valid_finalization_table, _valid_finalization_uuid
    if (not isinstance(table, Mapping) or not _valid_finalization_table(table, 6)
            or table.get('device') != state.get('disk')
            or not isinstance(uuids, Mapping) or set(uuids) != {'root', 'boot', 'esp'}):
        raise InstallError('invalid_state', 'The recorded Zeus installation identity is incomplete.')
    for key, part in zip(('esp', 'boot', 'root'), parts[3:]):
        valid_uuid = _valid_finalization_fat_uuid if key == 'esp' else _valid_finalization_uuid
        if (not valid_uuid(uuids.get(key)) or not isinstance(part, Mapping)
                or not _valid_finalization_uuid(part.get('uuid'))):
            raise InstallError('invalid_state', 'The recorded Zeus filesystem identity is invalid.')
    return state


def _verify_stable_ids(record, state):
    """Repeat the executor's recorded Fedora/disk identity checks when present."""

    plan = record.get('plan')
    if plan is None:
        # A few early installed journals predate the retained plan.  The exact
        # six-partition table and filesystem UUID checks still bind those
        # journals; newer journals get the stronger stable-ID proof below.
        return
    if not isinstance(plan, Mapping):
        raise InstallError('invalid_state', 'The recorded Zeus target plan is invalid.')
    from .executor import DualBootExecutor
    try:
        DualBootExecutor(require_root=False)._verify_finalization_stable_ids(
            plan, state['allocated_table'], _ExecutorCommandRunner()
        )
    except InstallError as error:
        raise InstallError(error.code, str(error)) from error


def verify_target(state, record=None):
    observed = json.loads(command(['/usr/sbin/sfdisk', '--json', state['disk']]))['partitiontable']
    if observed != state['allocated_table']:
        raise InstallError('target_mismatch', 'The disk layout no longer matches the recorded Zeus installation.')
    parts = observed['partitions']
    for key, part in zip(('esp', 'boot', 'root'), parts[3:]):
        details = dict(line.split('=', 1) for line in command(
            ['/usr/sbin/blkid', '-p', '-o', 'export', part['node']]).splitlines() if '=' in line)
        if (details.get('UUID', '').lower() != state['filesystem_uuids'][key].lower()
                or details.get('PART_ENTRY_UUID', '').lower() != part['uuid'].lower()
                or details.get('TYPE') != ('vfat' if key == 'esp' else 'ext4')):
            raise InstallError('target_mismatch', 'An existing Zeus filesystem no longer matches its recorded identity.')
    if record is not None:
        _verify_stable_ids(record, state)
    return parts


def _verify_mounted_target(state, part):
    """Prove the mount resolved to the recorded root filesystem."""

    try:
        value = json.loads(command([
            FINDMNT, '--json', '--target', str(MOUNT_ROOT),
            '--output', 'SOURCE,FSTYPE,UUID,PARTUUID',
        ]))
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as error:
        raise InstallError('target_mismatch', 'The mounted Zeus filesystem identity could not be verified.') from error
    item = None
    if isinstance(value, Mapping):
        filesystems = value.get('filesystems')
        if isinstance(filesystems, list) and len(filesystems) == 1 and isinstance(filesystems[0], Mapping):
            item = filesystems[0]
        elif 'source' in value or 'SOURCE' in value:
            item = value
    if not isinstance(item, Mapping):
        raise InstallError('target_mismatch', 'The mounted Zeus filesystem identity could not be verified.')
    source = str(item.get('source', item.get('SOURCE', ''))).split('[', 1)[0]
    fstype = str(item.get('fstype', item.get('FSTYPE', ''))).lower()
    observed_uuid = item.get('uuid', item.get('UUID'))
    observed_partuuid = item.get('partuuid', item.get('PARTUUID'))
    if (source != part.get('node') or fstype != 'ext4'
            or not isinstance(observed_uuid, str)
            or observed_uuid.lower() != state['filesystem_uuids']['root'].lower()
            or not isinstance(observed_partuuid, str)
            or observed_partuuid.lower() != str(part.get('uuid', '')).lower()):
        raise InstallError('target_mismatch', 'The mounted Zeus filesystem identity changed before staging.')


def fetch_signed():
    trusted = artifacts._trusted()
    blobs = []
    for suffix, limit in (('', trusted.MAX_MANIFEST_BYTES), ('.sig', trusted.MAX_SIGNATURE_BYTES)):
        path = PACKAGE / 'data' / ('offline-preview.json' + suffix)
        protected(path)
        if path.stat().st_size > limit:
            raise InstallError('manifest_invalid', 'The packaged update metadata is invalid.')
        blobs.append(path.read_bytes())
    manifest = trusted.verify_manifest(*blobs, trusted.DEFAULT_SIGNERS)
    return manifest, blobs


def stage(progress=lambda value: None):
    from .executor import DualBootExecutor
    if Path('/run/ostree-booted').exists():
        raise InstallError('wrong_os', 'Run this update installer from Fedora. In Zeus, use the Updates app.')
    journal = Journal()
    with journal.lock(), DualBootExecutor()._write_inhibitor():
        progress({'stage': 'checking_target'})
        record = journal.load()
        state = validate_record(record)
        verify_target(state, record)
        progress({'stage': 'checking_release'})
        manifest, signed = fetch_signed()
        cache = journal.root / 'existing-update'
        _mkdir_secure(cache, mode=0o700, strict_root=True)
        archive = cache / manifest['archive']['name']
        if archive.exists():
            protected(archive)
            artifacts.verify_archive(archive, manifest)
        else:
            if shutil.disk_usage(cache).free < manifest['archive']['size'] + 1024**3:
                raise InstallError('disk_space', 'Fedora needs more free space to download this update.')
            progress({'stage': 'downloading'})
            artifacts.download_release(manifest, archive, progress=lambda done, total: progress({'bytes': done, 'total': total}))
        progress({'stage': 'verifying'})
        artifacts.verify_archive(archive, manifest)
        # Mounts never escape this helper process into Fedora's mount namespace.
        os.unshare(os.CLONE_NEWNS)
        command(['/usr/bin/mount', '--make-rprivate', '/'])
        parts = verify_target(state, record)
        _mkdir_secure(MOUNT_ROOT, mode=0o700, strict_root=True)
        if os.path.ismount(MOUNT_ROOT):
            raise InstallError('mounted_target', 'The Zeus update mount is already in use.')
        command(['/usr/bin/mount', '-o', 'rw,nosuid,nodev',
                 f"UUID={state['filesystem_uuids']['root']}", str(MOUNT_ROOT)])
        try:
            _verify_mounted_target(state, parts[5])
            _stage_mounted(state, manifest, signed, archive, progress)
        finally:
            command(['/usr/bin/umount', str(MOUNT_ROOT)])
    return {'ok': True, 'build_id': manifest['build_id'], 'message':
            'Update prepared. Boot Zeus OS from the boot menu. It will apply the update offline; then open Updates and restart once more when ready. Fedora and Zeus personal files are preserved.'}


def _stage_mounted(state, manifest, signed, archive, progress):
    progress({'stage': 'copying_update'})
    parent = MOUNT_ROOT / 'ostree/deploy/default/deploy'
    protected(parent, directory=True)
    deployments = [p for p in parent.iterdir() if re.fullmatch(r'[0-9a-f]{64}\.[0-9]+', p.name)]
    if not deployments:
        raise InstallError('missing_deployment', 'The existing Zeus system deployment is missing.')
    from .offline_apply import OfflineApplyError, OfflineUpdateRunner

    for deployment in deployments:
        protected(deployment, directory=True)
        share = deployment / 'usr/share/zeus'
        for name in ('version', 'source-commit', 'build-id', 'update-sequence'):
            protected(share / name)
        try:
            current = OfflineUpdateRunner(
                # The repair boundary has already checked the mounted
                # deployment and all four identity files with ``protected``.
                # Disable the standalone runner's host-root check because a
                # mounted target may carry its own root mapping.
                share=share, storage_check=False, runner=lambda *_args, **_kwargs: None,
                plymouth=False,
            )._current_identity()
            OfflineUpdateRunner._eligibility(manifest, current)
        except OfflineApplyError as error:
            if error.code == 'not_newer':
                raise InstallError('not_newer', 'The installed Zeus system is newer than this installer payload.') from error
            if error.code == 'conflicting_build':
                raise InstallError('conflicting_build', 'The installer payload conflicts with the installed Zeus build.') from error
            raise InstallError('invalid_state', 'The installed Zeus system identity is invalid.') from error
    destination = MOUNT_ROOT / 'ostree/deploy/default/var/lib/zeus/offline-update'
    _mkdir_secure(destination, mode=0o700, strict_root=True)
    if shutil.disk_usage(destination).free < manifest['archive']['size'] * 3 + 2 * 1024**3:
        raise InstallError('disk_space', 'Zeus needs more free space to retain the old build and stage this update.')
    target_archive = destination / 'payload.oci'
    if target_archive.exists() or target_archive.is_symlink():
        protected(target_archive)
    for metadata_path in (
        destination / 'preview.json',
        destination / 'preview.json.sig',
        destination / 'update-allowed-signers',
    ):
        if metadata_path.exists() or metadata_path.is_symlink():
            protected(metadata_path)
    signer_path = PACKAGE / 'data/update-allowed-signers'
    protected(signer_path)
    signer_bytes = signer_path.read_bytes()
    pending = destination / 'pending'
    if pending.exists() or pending.is_symlink():
        protected(pending)
        try:
            pending_value = pending.read_bytes()
        except OSError as error:
            raise InstallError('pending_conflict', 'The existing offline update marker could not be read safely.') from error
        if pending_value != (manifest['build_id'] + '\n').encode():
            raise InstallError('pending_conflict', 'A different offline update is already pending.')
        for path, expected in (
            (destination / 'preview.json', signed[0]),
            (destination / 'preview.json.sig', signed[1]),
            (destination / 'update-allowed-signers', signer_bytes),
        ):
            if not path.exists() or path.is_symlink():
                raise InstallError('pending_conflict', 'The existing offline update metadata is incomplete.')
            protected(path)
            try:
                actual = path.read_bytes()
            except OSError as error:
                raise InstallError('pending_conflict', 'The existing offline update metadata could not be read safely.') from error
            if actual != expected:
                raise InstallError('pending_conflict', 'A different offline update is already pending.')

    service = b'''[Unit]
Description=Apply prepared Zeus OS update offline
After=local-fs.target
Before=display-manager.service plymouth-quit.service
ConditionPathExists=/var/lib/zeus/offline-update/pending
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /etc/zeus/offline-apply.py
TimeoutStartSec=3600
[Install]
WantedBy=multi-user.target
'''

    # Check every fixed service path before copying the payload.  Helper
    # scripts are installer-owned and may be upgraded in place; an existing
    # service file is only replaceable when it is one of our known versions.
    legacy_service = service.replace(
        b'Before=display-manager.service plymouth-quit.service\n',
        b'Before=display-manager.service\n',
    ).replace(
        b'ConditionPathExists=/var/lib/zeus/offline-update/pending\n',
        b'ConditionPathExists=/var/lib/zeus/offline-update/preview.json\n',
    )
    for deployment in deployments:
        etc = deployment / 'etc'
        protected(etc, directory=True)
        for directory in (
            etc / 'zeus',
            etc / 'systemd',
            etc / 'systemd/system',
        ):
            if directory.exists() or directory.is_symlink():
                protected(directory, directory=True)
        for helper_path in (
            etc / 'zeus/offline-apply.py',
            etc / 'zeus/update_manifest.py',
        ):
            if helper_path.exists() or helper_path.is_symlink():
                protected(helper_path)
        service_path = etc / 'systemd/system/zeus-offline-update.service'
        if service_path.exists() or service_path.is_symlink():
            protected(service_path)
            try:
                service_value = service_path.read_bytes()
            except OSError as error:
                raise InstallError('service_conflict', 'The existing offline update service could not be read safely.') from error
            if service_value not in (service, legacy_service):
                raise InstallError('service_conflict', 'An unrelated offline update service already exists.')
        wants = etc / 'systemd/system/multi-user.target.wants'
        if wants.exists() or wants.is_symlink():
            protected(wants, directory=True)
        link = wants / 'zeus-offline-update.service'
        if link.is_symlink() and os.readlink(link) == '../zeus-offline-update.service':
            continue
        if link.exists() or link.is_symlink():
            raise InstallError('service_conflict', 'An unrelated offline update service already exists.')

    destination.chmod(0o700)
    # Status is public; payloads remain private. Legacy installs created this
    # shared parent as 0700, making the desktop updater unable to read status.
    for public_directory in (destination.parent, destination.parent / 'updater'):
        _mkdir_secure(public_directory, mode=0o755, strict_root=True)
        public_directory.chmod(0o755)

    partial = destination / 'payload.oci.part'
    if partial.exists() or partial.is_symlink():
        protected(partial)
        partial.unlink()
    with archive.open('rb') as source, partial.open('xb') as target:
        os.fchmod(target.fileno(), 0o600)
        shutil.copyfileobj(source, target, 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    artifacts.verify_archive(partial, manifest)
    os.replace(partial, target_archive)
    write(destination / 'preview.json', signed[0])
    write(destination / 'preview.json.sig', signed[1])
    write(destination / 'update-allowed-signers', signer_bytes)
    command(['/usr/bin/chcon', '-R', 'system_u:object_r:var_lib_t:s0', str(destination)])
    for deployment in deployments:
        etc = deployment / 'etc'
        protected(etc, directory=True)
        write(etc / 'zeus/offline-apply.py', (PACKAGE / 'offline_apply.py').read_bytes(), 0o644)
        write(etc / 'zeus/update_manifest.py', (PACKAGE / 'vendor/update_manifest.py').read_bytes(), 0o644)
        write(etc / 'systemd/system/zeus-offline-update.service', service, 0o644)
        command(['/usr/bin/chcon', '-R', 'system_u:object_r:etc_t:s0', str(etc / 'zeus')])
        command(['/usr/bin/chcon', 'system_u:object_r:systemd_unit_file_t:s0',
                 str(etc / 'systemd/system/zeus-offline-update.service')])
        wants = etc / 'systemd/system/multi-user.target.wants'
        _mkdir_secure(wants, mode=0o755, strict_root=True)
        link = wants / 'zeus-offline-update.service'
        if link.is_symlink() and os.readlink(link) == '../zeus-offline-update.service':
            continue
        if link.exists() or link.is_symlink():
            raise InstallError('service_conflict', 'An unrelated offline update service already exists.')
        link.symlink_to('../zeus-offline-update.service')
        command(['/usr/bin/chcon', '-h', 'system_u:object_r:systemd_unit_file_t:s0', str(link)])
    write(destination / 'pending', (manifest['build_id'] + '\n').encode())
    command(['/usr/bin/chcon', 'system_u:object_r:var_lib_t:s0', str(destination / 'pending')])
    command(['/usr/bin/sync', '-f', str(MOUNT_ROOT)])
