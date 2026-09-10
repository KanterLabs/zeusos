"""Existing-install updates must prove ownership before mounting or writing."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from zeus_installer import repair
from zeus_installer.backend import InstallError


def record():
    parts = [
        {'node': f'/dev/sda{i}', 'uuid': f'00000000-0000-0000-0000-{i:012d}',
         'start': i * 100, 'size': 100,
         'type': 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B' if i == 4
                 else '0FC63DAF-8483-4772-8E79-3D69D8477DE4'}
        for i in range(1, 7)
    ]
    state = {
        'schema_version': 1, 'executor': 'zeus-dualboot', 'stage': 2,
        'status': 'complete', 'phase': 'installed', 'disk': '/dev/sda',
        'allocated_table': {
            'device': '/dev/sda', 'label': 'gpt', 'id': '00000000-0000-0000-0000-000000000001',
            'unit': 'sectors', 'sectorsize': 512, 'firstlba': 34, 'lastlba': 10000,
            'partitions': parts,
        },
        'filesystem_uuids': {
            'esp': '1234-ABCD', 'boot': '11111111-2222-3333-4444-555555555555',
            'root': '66666666-2222-3333-4444-555555555555',
        },
    }
    return {'schema_version': 1, 'phase': 'installed', 'executor_state': state}


class ExistingUpdateTests(unittest.TestCase):
    def _mounted_fixture(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        root = Path(temporary.name)
        deployment = root / 'ostree/deploy/default/deploy' / ('a' * 64 + '.0')
        share = deployment / 'usr/share/zeus'
        share.mkdir(parents=True)
        (share / 'version').write_text('0.1.0-preview.2')
        (share / 'source-commit').write_text('a' * 40)
        (share / 'build-id').write_text('git-aaaaaaaaaaaa')
        (share / 'update-sequence').write_text('11')
        (deployment / 'etc').mkdir(parents=True)
        archive = root / 'source.oci'
        archive.write_bytes(b'new-payload')
        candidate = {
            'build_id': 'git-bbbbbbbbbbbb', 'sequence': 12,
            'archive': {'size': archive.stat().st_size},
        }
        signed = [b'{"build_id":"git-bbbbbbbbbbbb"}', b'signature']
        destination = root / 'ostree/deploy/default/var/lib/zeus/offline-update'
        destination.mkdir(parents=True)
        return temporary, root, deployment, destination, archive, candidate, signed

    def test_incomplete_install_cannot_be_reinstalled(self):
        for phase in ('installing', 'error', 'interrupted', 'prepared'):
            value = record()
            value['phase'] = phase
            with self.assertRaises(InstallError):
                repair.validate_record(value)

    def test_changed_partition_table_rejected_before_mount(self):
        state = repair.validate_record(record())
        changed = copy.deepcopy(state['allocated_table'])
        changed['partitions'][5]['uuid'] = 'different-target'
        with patch.object(repair, 'command', return_value=json.dumps({'partitiontable': changed})) as run:
            with self.assertRaisesRegex(InstallError, 'disk layout'):
                repair.verify_target(state)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], '/usr/sbin/sfdisk')

    def test_reformatted_zeus_partition_rejected(self):
        state = repair.validate_record(record())
        with patch.object(repair, 'command', side_effect=[json.dumps({'partitiontable': state['allocated_table']}),
                                                       'UUID=DEAD-BEEF\nTYPE=vfat\n']):
            with self.assertRaisesRegex(InstallError, 'filesystem'):
                repair.verify_target(state)

    def test_valid_filesystem_ownership_is_read_only(self):
        state = repair.validate_record(record())
        outputs = [json.dumps({'partitiontable': state['allocated_table']})]
        for key, part in zip(('esp', 'boot', 'root'), state['allocated_table']['partitions'][3:]):
            outputs.append(f"UUID={state['filesystem_uuids'][key]}\nPART_ENTRY_UUID={part['uuid']}\n"
                           f"TYPE={'vfat' if key == 'esp' else 'ext4'}\n")
        with patch.object(repair, 'command', side_effect=outputs) as run:
            self.assertEqual(len(repair.verify_target(state)), 6)
        self.assertEqual({call.args[0][0] for call in run.call_args_list},
                         {'/usr/sbin/sfdisk', '/usr/sbin/blkid'})

    def test_pending_candidate_mismatch_fails_before_any_write(self):
        temporary, root, deployment, destination, archive, candidate, signed = self._mounted_fixture()
        self.addCleanup(temporary.cleanup)
        signer = (repair.PACKAGE / 'data/update-allowed-signers').read_bytes()
        (destination / 'preview.json').write_bytes(b'other-candidate')
        (destination / 'preview.json.sig').write_bytes(signed[1])
        (destination / 'update-allowed-signers').write_bytes(signer)
        (destination / 'pending').write_bytes((candidate['build_id'] + '\n').encode())
        before = {path.name: path.read_bytes() for path in destination.iterdir()}
        with patch.object(repair, 'MOUNT_ROOT', root), \
             patch.object(repair, 'protected', return_value=None), \
             patch.object(repair.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**13)), \
             patch.object(repair.artifacts, 'verify_archive') as verify:
            with self.assertRaisesRegex(InstallError, 'different offline update'):
                repair._stage_mounted(record()['executor_state'], candidate, signed, archive, lambda _value: None)
        self.assertEqual(before, {path.name: path.read_bytes() for path in destination.iterdir()})
        verify.assert_not_called()

    def test_same_candidate_pending_resume_replaces_payload_safely(self):
        temporary, root, deployment, destination, archive, candidate, signed = self._mounted_fixture()
        self.addCleanup(temporary.cleanup)
        signer = (repair.PACKAGE / 'data/update-allowed-signers').read_bytes()
        destination.parent.chmod(0o700)
        destination.chmod(0o755)
        (destination / 'payload.oci').write_bytes(b'old-payload')
        (destination / 'preview.json').write_bytes(signed[0])
        (destination / 'preview.json.sig').write_bytes(signed[1])
        (destination / 'update-allowed-signers').write_bytes(signer)
        (destination / 'pending').write_bytes((candidate['build_id'] + '\n').encode())
        with patch.object(repair, 'MOUNT_ROOT', root), \
             patch.object(repair, 'protected', return_value=None), \
             patch.object(repair.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**13)), \
             patch.object(repair, 'command', return_value=''), \
             patch.object(repair.artifacts, 'verify_archive'):
            repair._stage_mounted(record()['executor_state'], candidate, signed, archive, lambda _value: None)
        self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((destination.parent / 'updater').stat().st_mode & 0o777, 0o755)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((destination / 'payload.oci').stat().st_mode & 0o777, 0o600)
        self.assertEqual((destination / 'payload.oci').read_bytes(), archive.read_bytes())
        self.assertEqual((destination / 'preview.json').read_bytes(), signed[0])
        self.assertEqual((destination / 'pending').read_text(), candidate['build_id'] + '\n')
        service = deployment / 'etc/systemd/system/zeus-offline-update.service'
        self.assertIn(b'plymouth-quit.service', service.read_bytes())
        self.assertTrue((deployment / 'etc/systemd/system/multi-user.target.wants/zeus-offline-update.service').is_symlink())


if __name__ == '__main__':
    unittest.main()
