"""Privileged updater transactions against populated, unprivileged fixtures."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'desktop/rootfs/usr/lib/zeus'))
import zeus_update as update


def release(sequence=200, commit='b' * 40):
    name = f'zeusos-0.1.0-preview.2-git-{commit[:12]}.oci'
    return {'schema_version': 1, 'product': 'zeusos', 'channel': 'preview',
            'architecture': 'amd64', 'version': '0.1.0-preview.2',
            'build_id': f'git-{commit[:12]}', 'source_commit': commit,
            'sequence': sequence, 'published_at': '2026-09-09T10:00:00Z',
            'notes': 'Preview update', 'archive': {'name': name,
            'url': f'https://github.com/KanterLabs/zeusos/releases/download/v0.1.0-preview.2/{name}',
            'size': 7, 'sha256': hashlib.sha256(b'archive').hexdigest(),
            'manifest_digest': 'sha256:' + 'd' * 64}}


class UpdaterTransactions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root, self.share = self.base / 'state', self.base / 'share'
        self.share.mkdir()
        self.current = release(100, 'a' * 40)
        self.install_identity(self.current)
        self.cache = self.base / 'cache.json'
        self.manifest = release()
        self.status = {'booted': {'image': {'imageDigest': 'sha256:' + 'c' * 64}},
                       'staged': None, 'rollback': {'image': {'imageDigest': 'sha256:' + 'e' * 64}},
                       'rollbackQueued': False}
        self.commands = []
        self.active = 'inactive'
        self.fail_start = self.fail_stage = False
        self.downloads = 0
        self.installer = update.Installer(self.root, self.share, runner=self.runner,
            fetch=lambda: copy.deepcopy(self.manifest), download=self.download,
            inspect=self.inspect, this_boot='boot-one', storage_check=False)
        # Populated owner state lives outside the additive updater directory.
        self.owner = self.base / 'home'
        self.owner.mkdir()
        for name in ('Downloads', 'Documents', 'Temp', 'preferences.json'):
            (self.owner / name).write_bytes(b'existing owner content')
        self.owner_before = {p.name: p.read_bytes() for p in self.owner.iterdir()}

    def tearDown(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.owner.iterdir()}, self.owner_before)

    def install_identity(self, manifest):
        for file, key in [('version', 'version'), ('build-id', 'build_id'),
                          ('source-commit', 'source_commit'), ('update-sequence', 'sequence')]:
            (self.share / file).write_text(str(manifest[key]))

    def runner(self, args, **kwargs):
        self.commands.append(args)
        code, stdout = 0, ''
        if args[:2] == ['/usr/bin/systemctl', 'show']:
            stdout = self.active
        elif args[:2] == ['/usr/bin/systemctl', 'start']:
            code = int(self.fail_start)
        elif args[:2] == ['/usr/bin/bootc', 'status']:
            stdout = json.dumps({'status': self.status})
        elif args[:2] == ['/usr/bin/bootc', 'switch']:
            code = int(self.fail_stage)
            if not code:
                self.status['staged'] = {'image': {'imageDigest': self.manifest['archive']['manifest_digest']}}
        else:
            self.fail(f'Unexpected command {args}')
        self.assertEqual(kwargs['env']['HOME'], '/root')
        return SimpleNamespace(returncode=code, stdout=stdout, stderr='')

    def download(self, manifest, path, progress=None):
        self.downloads += 1
        Path(path).write_bytes(b'archive')
        if progress:
            progress(7, 7)

    def inspect(self, path):
        return {**{key: self.manifest[key] for key in ('architecture', 'version',
                    'source_commit', 'build_id', 'sequence')},
                'manifest_digest': self.manifest['archive']['manifest_digest']}

    def request(self):
        return self.installer.request(self.manifest['build_id'], self.manifest['archive']['sha256'])

    def job(self):
        return update.read_json(self.root / 'status.json')

    def assert_error(self, code, action):
        with self.assertRaises(update.trusted.UpdateError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_request_is_exact_and_only_queues_then_worker_stages_without_reboot(self):
        result = self.request()
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(self.downloads, 0)
        self.assertIsNone(self.status['staged'])
        rollback = copy.deepcopy(self.status['rollback'])
        self.assertTrue(self.installer.run())
        self.assertEqual(self.job()['phase'], 'ready')
        switches = [c for c in self.commands if c[1] == 'switch']
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0][2:5], ['--transport', 'oci-archive', '--retain'])
        self.assertEqual(self.status['rollback'], rollback)
        self.assertNotIn('--apply', sum(self.commands, []))
        self.assertNotIn('reboot', sum(self.commands, []))
        self.assertEqual(self.request()['state'], 'ready')
        self.assertEqual(self.downloads, 1)

    def test_forged_selection_and_changed_feed_never_start_service(self):
        for build, checksum, error in [('../../evil', 'a' * 64, 'invalid_selection'),
                ('git-' + 'a' * 12, self.manifest['archive']['sha256'], 'feed_changed'),
                (self.manifest['build_id'], 'a' * 64, 'feed_changed')]:
            self.assert_error(error, lambda: self.installer.request(build, checksum))
        self.assertFalse(any(c[1] == 'start' for c in self.commands))

    def test_old_equal_conflicting_and_trust_watermark_are_rejected(self):
        self.manifest = release(99)
        self.assert_error('not_newer', self.request)
        self.manifest = release(100)
        self.assert_error('conflicting_build', self.request)
        self.manifest = release()
        self.request()
        self.manifest = release(199, 'f' * 40)
        self.assert_error('stale_feed', self.request)

    def test_different_stage_and_queued_rollback_are_preserved(self):
        self.status['staged'] = {'image': {'imageDigest': 'sha256:' + 'f' * 64}}
        preserved = copy.deepcopy(self.status)
        self.assert_error('staged_update_exists', self.request)
        self.assertEqual(self.status, preserved)
        self.status['staged'] = None
        self.status['rollbackQueued'] = True
        self.assert_error('rollback_queued', self.request)

    def test_concurrent_lock_or_running_service_rejects_second_request(self):
        with self.installer.lock():
            self.assert_error('busy', self.request)
        self.active = 'active'
        self.assert_error('busy', self.request)
        self.assertFalse(any(c[1] == 'start' for c in self.commands))

    def test_unidentified_pending_deployment_is_preserved(self):
        self.status['staged'] = {'image': {'imageDigest': None}}
        preserved = copy.deepcopy(self.status)
        self.assert_error('invalid_staged', self.request)
        self.assertEqual(self.status, preserved)

    def test_failed_service_start_remains_retryable(self):
        self.fail_start = True
        self.assert_error('service_start_failed', self.request)
        self.assertEqual(self.job()['phase'], 'error')
        self.fail_start = False
        self.assertEqual(self.request()['state'], 'queued')

    def test_feed_changed_after_authorization_aborts_without_download(self):
        self.request()
        self.manifest = release(201, 'f' * 40)
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'feed_changed')
        self.assertEqual(self.downloads, 0)

    def test_interrupted_previous_boot_is_not_automatically_resumed(self):
        self.request()
        self.installer.this_boot = 'boot-two'
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'interrupted')
        self.assertEqual(self.downloads, 0)

    def test_low_space_does_not_download_or_stage(self):
        self.request()
        with patch.object(update.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'disk_space')
        self.assertEqual(self.downloads, 0)

    def test_bad_image_identity_fails_before_bootc_switch(self):
        self.request()
        self.installer.inspect = lambda path: {**self.inspect(path), 'sequence': 199}
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'wrong_image')
        self.assertFalse(any(c[1] == 'switch' for c in self.commands))

    def test_failure_can_retry_cached_archive_and_owner_state_remains_populated(self):
        self.request()
        self.fail_stage = True
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'staging_failed')
        self.fail_stage = False
        self.request()
        self.assertTrue(self.installer.run())
        self.assertEqual(self.downloads, 1)

    def test_download_failure_never_stages_and_retry_replaces_only_partial(self):
        self.request()
        def fail(manifest, path, **kwargs):
            Path(path).write_bytes(b'partial')
            raise update.trusted.UpdateError('network_error', 'Disconnected')
        self.installer.download = fail
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'network_error')
        self.assertIsNone(self.status['staged'])
        self.installer.download = self.download
        self.request()
        self.assertTrue(self.installer.run())
        self.assertFalse(list((self.root / 'downloads').glob('*.part')))

    def test_reconcile_crashed_worker_reports_interruption_or_exact_ready_stage(self):
        self.request()
        self.installer.reconcile()
        self.assertEqual(self.job()['phase'], 'interrupted')
        self.request()
        self.status['staged'] = {'image': {'imageDigest': self.manifest['archive']['manifest_digest']}}
        self.installer.reconcile()
        self.assertEqual(self.job()['phase'], 'ready')

    def test_symlink_download_cache_cannot_replace_other_file(self):
        self.request()
        destination = self.root / 'downloads' / self.manifest['archive']['name']
        destination.symlink_to(self.owner / 'Documents')
        self.assertFalse(self.installer.run())
        self.assertEqual(self.job()['error'], 'unsafe_storage')

    def test_newly_installed_job_does_not_hide_later_available_update(self):
        self.request()
        self.installer.run()
        self.install_identity(self.manifest)
        update.atomic_json(self.cache, {'schema_version': 1, 'manifest': self.manifest,
                          'message': 'A signed update is available.'})
        result = update.local_status(self.root, self.share, self.cache, 'boot-two')
        self.assertEqual(result['state'], 'up_to_date')
        self.assertEqual(result['message'], 'The selected update is installed.')
        self.manifest = release(300, 'f' * 40)
        result = update.check_updates(self.root, self.share, self.cache, fetch=lambda: self.manifest)
        self.assertEqual(result['state'], 'available')
        self.assertEqual(update.local_status(self.root, self.share, self.cache, 'boot-two')['state'], 'available')

    def test_display_cache_never_authorizes_root_selection(self):
        forged = release(300, 'f' * 40)
        update.atomic_json(self.cache, {'schema_version': 1, 'manifest': forged})
        self.assertEqual(update.local_status(self.root, self.share, self.cache)['state'], 'available')
        self.assert_error('feed_changed', lambda: self.installer.request(forged['build_id'], forged['archive']['sha256']))

    def test_unknown_mutable_schema_is_preserved_and_rejected(self):
        self.installer.prepare()
        path = self.root / 'trust.json'
        original = b'{"schema_version":2,"future_setting":"preserve"}'
        path.write_bytes(original)
        self.assert_error('invalid_state', self.request)
        self.assertEqual(path.read_bytes(), original)

    def test_public_output_is_text_unless_json_requested(self):
        value = update.public_result(update.current_image(self.share), message='Local status')
        for args, is_json in [(['status'], False), (['status', '--json'], True)]:
            with patch.object(update, 'local_status', return_value=value), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(update.client_main(args), 0)
            if is_json:
                self.assertEqual(json.loads(output.getvalue())['message'], 'Local status')
            else:
                self.assertTrue(output.getvalue().startswith('Zeus OS'))


if __name__ == '__main__':
    unittest.main()
