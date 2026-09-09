"""Owner-selected, signed image updates. No periodic work or implicit reboot.

The desktop cache is display-only. Every privileged request independently fetches
and verifies trusted release metadata and accepts only its exact build and hash.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import update_manifest as trusted

ROOT = Path('/var/lib/zeus/updater')
SHARE = Path('/usr/share/zeus')
SERVICE = 'zeus-update-install.service'
ADMIN = '/usr/libexec/zeus-update-admin'
BUSY = {'queued', 'downloading', 'verifying', 'staging'}
SCHEMA = 1
JSON_LIMIT = 128 * 1024
BUILD_RE = re.compile(r'git-[0-9a-f]{12}\Z')
SHA_RE = re.compile(r'[0-9a-f]{64}\Z')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def read_json(path, *, required=False):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(JSON_LIMIT + 1)
        if len(raw) > JSON_LIMIT:
            raise ValueError('oversized state')
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('schema_version') != SCHEMA:
            raise ValueError('unsupported state')
        return value
    except FileNotFoundError:
        if not required:
            return None
        raise trusted.UpdateError('missing_state', 'Update job state is unavailable.')
    except (OSError, ValueError, TypeError):
        raise trusted.UpdateError('invalid_state', 'Update state cannot be read safely.')


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def current_image(share=SHARE):
    def read(name, default):
        try:
            value = (Path(share) / name).read_text().strip()
            return value if len(value) <= 256 else default
        except OSError:
            return default
    sequence = read('update-sequence', '0')
    return {'version': read('version', 'unknown'),
            'build_id': read('build-id', 'unknown'),
            'source_commit': read('source-commit', 'unknown'),
            'sequence': int(sequence) if sequence.isdecimal() else 0}


def candidate_view(manifest):
    if not manifest:
        return None
    return {**{key: manifest[key] for key in
               ('version', 'build_id', 'source_commit', 'sequence', 'published_at', 'notes')},
            'archive_size': manifest['archive']['size'],
            'archive_sha256': manifest['archive']['sha256'],
            'image_digest': manifest['archive']['manifest_digest']}


def public_result(current, state='idle', manifest=None, message='', error=None,
                  progress=None, checked_at=None):
    return {'schema_version': SCHEMA, 'ok': error is None, 'state': state,
            'current': current, 'candidate': candidate_view(manifest),
            'progress': progress, 'message': message, 'error': error,
            'checked_at': checked_at}


def newer(manifest, current, watermark=None):
    """Never order Git IDs lexically or infer a new release from version alone."""
    sequence = manifest['sequence']
    if watermark:
        seen = watermark.get('sequence')
        if type(seen) is not int or not BUILD_RE.fullmatch(str(watermark.get('build_id', ''))):
            raise trusted.UpdateError('invalid_state', 'Update trust state is invalid.')
        if sequence < seen or (sequence == seen and manifest['build_id'] != watermark['build_id']):
            raise trusted.UpdateError('stale_feed', 'Older or conflicting update information was received.')
    if sequence == current['sequence'] and manifest['build_id'] != current['build_id']:
        raise trusted.UpdateError('conflicting_build', 'This build conflicts with the installed update sequence.')
    return sequence > current['sequence'] and manifest['build_id'] != current['build_id']


def default_cache():
    root = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return root / 'zeus/updater/check.json'


def local_status(root=ROOT, share=SHARE, cache=None, this_boot=None):
    current = current_image(share)
    installed = None
    try:
        job = read_json(Path(root) / 'status.json')
        if job:
            manifest = trusted.validate_manifest(job['manifest']) if job.get('manifest') else None
            phase = job.get('phase')
            if manifest and manifest['build_id'] == current['build_id']:
                installed = manifest
                phase = None
            if phase in BUSY | {'ready'}:
                if job.get('boot_id') != (this_boot or boot_id()):
                    return public_result(current, 'interrupted', manifest,
                                         'The previous update did not finish booting. Check for updates to retry.',
                                         'interrupted')
                return public_result(current, phase, manifest, job.get('message', ''),
                                     progress=job.get('progress'))
            if phase in {'error', 'interrupted'}:
                return public_result(current, phase, manifest, job.get('message', 'Update interrupted.'),
                                     job.get('error', 'update_failed'))
        remembered = read_json(cache or default_cache())
        if remembered:
            manifest = trusted.validate_manifest(remembered['manifest']) if remembered.get('manifest') else None
            state = 'available' if manifest and newer(manifest, current) else 'up_to_date'
            return public_result(current, state, manifest, remembered.get('message', ''),
                                 checked_at=remembered.get('checked_at'))
        if installed:
            return public_result(current, 'up_to_date', installed, 'The selected update is installed.')
        return public_result(current, message='Check for a newer signed Zeus OS build.')
    except (trusted.UpdateError, KeyError, TypeError):
        return public_result(current, 'error', message='Update information is unavailable. Check again.',
                             error='invalid_state')


def check_updates(root=ROOT, share=SHARE, cache=None, fetch=None):
    current = current_image(share)
    busy = local_status(root, share, cache)
    if busy['state'] in BUSY | {'ready'}:
        return busy
    try:
        manifest = (fetch or trusted.fetch_manifest)()
        watermark = read_json(Path(root) / 'trust.json')
        available = newer(manifest, current, watermark)
        message = 'A signed update is available.' if available else 'No newer build is published on this channel.'
        checked = utc_now()
        destination = Path(cache or default_cache())
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(destination, {'schema_version': SCHEMA, 'manifest': manifest,
                                  'checked_at': checked, 'message': message})
        # An install started in another window while this network check ran.
        after = local_status(root, share, cache)
        if after['state'] in BUSY | {'ready'}:
            return after
        return public_result(current, 'available' if available else 'up_to_date', manifest,
                             message, checked_at=checked)
    except trusted.UpdateError as error:
        return public_result(current, 'error', message=str(error), error=error.code)
    except OSError:
        return public_result(current, 'error', message='Update information could not be saved.', error='cache_error')


class Installer:
    """Root-only at the entry point; injected paths/runners are for fixture tests."""
    def __init__(self, root=ROOT, share=SHARE, *, runner=None, fetch=None,
                 download=None, inspect=None, this_boot=None, storage_check=True):
        self.root, self.share = Path(root), Path(share)
        self.runner = runner or subprocess.run
        self.fetch = fetch or trusted.fetch_manifest
        self.download = download or trusted.download_archive
        self.inspect = inspect or trusted.inspect_archive
        self.this_boot = this_boot or boot_id()
        self.storage_check = storage_check

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.storage_check:
            for path in [self.root, *self.root.parents]:
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise trusted.UpdateError('unsafe_storage', 'The update storage directory is not protected.')
            for name in ('status.json', 'trust.json', 'request.json'):
                path = self.root / name
                if path.exists() or path.is_symlink():
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                        raise trusted.UpdateError('unsafe_storage', 'Update state is not protected.')
        directory = self.root / 'downloads'
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (self.storage_check and
                (info.st_uid != 0 or info.st_mode & 0o077)):
            raise trusted.UpdateError('unsafe_storage', 'Downloaded updates must use private storage.')

    @contextmanager
    def lock(self, wait=False):
        self.prepare()
        fd = os.open(self.root / 'operation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (self.storage_check and
                    (info.st_uid != 0 or info.st_mode & 0o077)):
                raise trusted.UpdateError('unsafe_storage', 'The update lock is not protected.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise trusted.UpdateError('busy', 'Another update operation is already running.')
            yield
        finally:
            os.close(fd)

    def command(self, args, timeout=30):
        try:
            return self.runner(args, capture_output=True, text=True, timeout=timeout,
                               env={'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C.UTF-8', 'HOME': '/root'})
        except (OSError, subprocess.TimeoutExpired):
            raise trusted.UpdateError('command_failed', 'An update operation failed or timed out. The running OS remains selected until a verified update is staged.')

    def bootc_status(self):
        result = self.command(['/usr/bin/bootc', 'status', '--json'])
        try:
            if result.returncode or len(result.stdout) > JSON_LIMIT:
                raise ValueError()
            status = json.loads(result.stdout)['status']
            if not isinstance(status, dict) or not status.get('booted'):
                raise ValueError()
            if status.get('rollbackQueued'):
                raise trusted.UpdateError('rollback_queued', 'An OS rollback is already queued. Finish it before installing another update.')
            return status
        except (ValueError, KeyError, TypeError):
            raise trusted.UpdateError('bootc_unavailable', 'The installed OS update state could not be verified.')

    @staticmethod
    def staged_digest(status):
        staged = status.get('staged')
        if staged is None:
            return None
        try:
            digest = staged['image']['imageDigest']
            if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                raise ValueError()
            return digest
        except (KeyError, TypeError, ValueError):
            raise trusted.UpdateError('invalid_staged', 'The pending OS deployment could not be identified.')

    def check_stage(self, manifest):
        digest = self.staged_digest(self.bootc_status())
        if digest and digest != manifest['archive']['manifest_digest']:
            raise trusted.UpdateError('staged_update_exists', 'A different OS update is already staged. It has been preserved.')
        return digest is not None

    def state(self, phase, manifest, message, error=None, progress=None):
        value = {'schema_version': SCHEMA, 'phase': phase, 'manifest': manifest,
                 'message': message, 'error': error, 'progress': progress,
                 'boot_id': self.this_boot, 'updated_at': utc_now()}
        atomic_json(self.root / 'status.json', value, 0o644)
        return value

    def eligibility(self, manifest):
        current = current_image(self.share)
        if current['sequence'] <= 0 or not BUILD_RE.fullmatch(current['build_id']):
            raise trusted.UpdateError('unsupported_installation', 'This installation needs the first updater-capable image before in-OS updates can be installed.')
        watermark = read_json(self.root / 'trust.json')
        if not newer(manifest, current, watermark):
            raise trusted.UpdateError('not_newer', 'The selected build is not newer than the installed image.')

    @staticmethod
    def selection(manifest, build, digest):
        if not BUILD_RE.fullmatch(build) or not SHA_RE.fullmatch(digest):
            raise trusted.UpdateError('invalid_selection', 'The selected update identifier is invalid.')
        if manifest['build_id'] != build or manifest['archive']['sha256'] != digest:
            raise trusted.UpdateError('feed_changed', 'The published update changed. Check again before installing.')

    def request(self, build, digest):
        if not BUILD_RE.fullmatch(build) or not SHA_RE.fullmatch(digest):
            raise trusted.UpdateError('invalid_selection', 'The selected update identifier is invalid.')
        with self.lock():
            active = self.command(['/usr/bin/systemctl', 'show', SERVICE, '--property=ActiveState', '--value'])
            if active.returncode:
                raise trusted.UpdateError('service_unavailable', 'The update installer service is unavailable.')
            if active.stdout.strip() in {'active', 'activating', 'deactivating', 'reloading'}:
                raise trusted.UpdateError('busy', 'An update is already being installed.')
            manifest = self.fetch()
            self.selection(manifest, build, digest)
            self.eligibility(manifest)
            if self.check_stage(manifest):
                self.state('ready', manifest, 'The verified update is ready. Restart when convenient.')
                return local_status(self.root, self.share, this_boot=self.this_boot)
            atomic_json(self.root / 'request.json', {'schema_version': SCHEMA, 'build_id': build,
                        'archive_sha256': digest, 'boot_id': self.this_boot})
            atomic_json(self.root / 'trust.json', {'schema_version': SCHEMA,
                        'sequence': manifest['sequence'], 'build_id': build}, 0o644)
            self.state('queued', manifest, 'Preparing the selected update. Installation continues if this window closes.')
            started = self.command(['/usr/bin/systemctl', 'start', '--no-block', SERVICE])
            if started.returncode:
                self.state('error', manifest, 'The installer could not start. Try again.', 'service_start_failed')
                raise trusted.UpdateError('service_start_failed', 'The installer could not start. Try again.')
            return local_status(self.root, self.share, this_boot=self.this_boot)

    def private_file(self, path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (self.storage_check and
                (info.st_uid != 0 or info.st_mode & 0o077)):
            raise trusted.UpdateError('unsafe_storage', 'A downloaded update file is not protected.')

    def archive_matches(self, path, archive):
        self.private_file(path)
        if path.stat().st_size != archive['size']:
            return False
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(data)
        return digest.hexdigest() == archive['sha256']

    def run(self):
        manifest = None
        with self.lock(wait=True):
            try:
                request = read_json(self.root / 'request.json', required=True)
                if request.get('boot_id') != self.this_boot:
                    raise trusted.UpdateError('interrupted', 'The previous update request was interrupted by a restart. Check again to retry.')
                manifest = self.fetch()
                self.selection(manifest, request['build_id'], request['archive_sha256'])
                self.eligibility(manifest)
                if self.check_stage(manifest):
                    self.state('ready', manifest, 'The verified update is ready. Restart when convenient.')
                    return True
                archive = manifest['archive']
                # Archive, unpacked/staged image, and a reserve for normal OS use.
                if shutil.disk_usage(self.root).free < archive['size'] * 3 + 2 * 1024**3:
                    raise trusted.UpdateError('disk_space', 'There is not enough free space to download and stage this update safely.')
                destination = self.root / 'downloads' / archive['name']
                partial = destination.with_name(destination.name + '.part')
                if destination.exists() or destination.is_symlink():
                    if not self.archive_matches(destination, archive):
                        destination.unlink()
                if not destination.exists():
                    if partial.exists() or partial.is_symlink():
                        self.private_file(partial)
                        partial.unlink()
                    self.state('downloading', manifest, 'Downloading the signed update.',
                               progress={'bytes': 0, 'total': archive['size']})
                    last = [0.0]
                    def progress(done, total):
                        now = time.monotonic()
                        if now - last[0] >= 1 or done == total:
                            self.state('downloading', manifest, 'Downloading the signed update.',
                                       progress={'bytes': done, 'total': total})
                            last[0] = now
                    self.download(manifest, partial, progress=progress)
                    self.private_file(partial)
                    os.replace(partial, destination)
                self.state('verifying', manifest, 'Verifying the archive and OS identity.')
                actual = self.inspect(destination)
                for key in ('architecture', 'version', 'source_commit', 'build_id', 'sequence'):
                    if actual.get(key) != manifest[key]:
                        raise trusted.UpdateError('wrong_image', 'The downloaded image does not match the selected Zeus OS build.')
                if actual.get('manifest_digest') != archive['manifest_digest']:
                    raise trusted.UpdateError('wrong_image', 'The downloaded image manifest does not match the signed release.')
                # Protect a deployment staged by another administrator during download.
                if not self.check_stage(manifest):
                    self.state('staging', manifest, 'Installing the verified image for the next restart.')
                    result = self.command(['/usr/bin/bootc', 'switch', '--transport', 'oci-archive',
                                           '--retain', str(destination)], timeout=1800)
                    if result.returncode:
                        raise trusted.UpdateError('staging_failed', 'The OS update could not be staged. Your running OS and personal files remain available; retry after checking free space and connectivity.')
                if not self.check_stage(manifest):
                    raise trusted.UpdateError('staging_unconfirmed', 'The update command ended without a verified pending deployment.')
                self.state('ready', manifest, 'The verified update is ready. Restart when convenient.')
                return True
            except trusted.UpdateError as error:
                self.state('error', manifest, str(error), error.code)
                return False
            except (OSError, KeyError, ValueError, TypeError):
                self.state('error', manifest, 'Installation was interrupted. Check for updates to retry.', 'interrupted')
                return False

    def reconcile(self):
        """ExecStopPost reports interrupted jobs, including killed workers."""
        with self.lock():
            previous = read_json(self.root / 'status.json')
            if not previous or previous.get('phase') not in BUSY:
                return
            manifest = previous.get('manifest')
            try:
                manifest = trusted.validate_manifest(manifest)
                if self.check_stage(manifest):
                    self.state('ready', manifest, 'The verified update is ready. Restart when convenient.')
                    return
            except trusted.UpdateError:
                pass
            self.state('interrupted', manifest, 'Installation was interrupted. Check for updates to retry.', 'interrupted')


def admin_main(argv=None):
    if os.geteuid() != 0:
        print(json.dumps({'ok': False, 'error': 'authorization_required', 'message': 'Administrator authentication is required.'}))
        return 1
    parser = argparse.ArgumentParser(description='Privileged signed Zeus update installer')
    parser.add_argument('action', choices=['install', 'run', 'reconcile'])
    parser.add_argument('selection', nargs='*')
    args = parser.parse_args(argv)
    try:
        installer = Installer()
        if args.action == 'install' and len(args.selection) == 2:
            result = installer.request(*args.selection)
        elif args.action == 'run' and not args.selection:
            ok = installer.run()
            print(json.dumps({'ok': ok, 'action': 'install'}))
            return 0 if ok else 1
        elif args.action == 'reconcile' and not args.selection:
            installer.reconcile()
            result = {'ok': True, 'action': 'reconcile'}
        else:
            raise trusted.UpdateError('invalid_arguments', 'The updater accepts only an exact build and checksum selection.')
        print(json.dumps(result))
        return 0 if result.get('ok') else 1
    except trusted.UpdateError as error:
        print(json.dumps({'ok': False, 'state': 'error', 'error': error.code, 'message': str(error)}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'ok': False, 'state': 'error', 'error': 'operation_failed', 'message': 'The update operation could not finish safely.'}))
        return 1


def client_main(argv=None):
    parser = argparse.ArgumentParser(description='Check and install signed Zeus OS builds')
    parser.add_argument('action', choices=['status', 'check', 'install'])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    result = local_status() if args.action == 'status' else check_updates()
    if args.action == 'install' and result['ok'] and result['state'] == 'available':
        candidate = result['candidate']
        try:
            command = subprocess.run(['/usr/bin/pkexec', ADMIN, 'install', candidate['build_id'],
                                      candidate['archive_sha256']], capture_output=True, text=True)
            if command.returncode in (126, 127):
                result = public_result(current_image(), 'error', message='Installation was not authorized. You can try again.', error='not_authorized')
            else:
                result = json.loads(command.stdout)
        except (OSError, ValueError):
            result = public_result(current_image(), 'error', message='The update installer could not be started.', error='installer_unavailable')
    if args.json:
        print(json.dumps(result))
    else:
        current = result.get('current', current_image())
        print(f"Zeus OS {current['version']} ({current['build_id']})")
        print(result.get('message', ''))
        if result.get('candidate'):
            candidate = result['candidate']
            print(f"Build: {candidate['build_id']} | {candidate['archive_size'] / 1024**2:.1f} MiB")
            print(candidate.get('notes', ''))
        if result.get('state') == 'ready':
            print('Restart explicitly from the desktop when ready. On boot Temp cleanup also applies to update reboots.')
    return 0 if result.get('ok') else 1
