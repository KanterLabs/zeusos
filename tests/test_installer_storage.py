import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from zeus_installer import storage


def table():
    sectors = int(931.5 * storage.GIB / 512)
    start = 2048 + (600 * storage.MIB + storage.GIB) // 512
    return {'label': 'gpt', 'id': 'a21c680f-f699-4bdf-95eb-e9ca012f8170',
            'device': '/dev/nvme0n1', 'unit': 'sectors', 'firstlba': 34,
            'lastlba': sectors - 34, 'sectorsize': 512,
            'partitions': [
                {'node': '/dev/nvme0n1p1', 'start': 2048, 'size': 600*storage.MIB//512,
                 'type': storage.EFI, 'uuid': '9f143da6-ff85-45c3-ac68-51be50c23622'},
                {'node': '/dev/nvme0n1p2', 'start': 2048+600*storage.MIB//512,
                 'size': storage.GIB//512, 'type': storage.LINUX,
                 'uuid': '2d0c8330-ad37-443c-91f0-a49843f0bcdf'},
                {'node': '/dev/nvme0n1p3', 'start': start, 'size': sectors-34-start,
                 'type': storage.LINUX, 'uuid': 'b1c6131b-68dc-4c19-9745-5778c358a541'}]}


class StorageTests(unittest.TestCase):
    def test_laptop_geometry_has_no_overlap(self):
        original = table()
        result = storage.layout(original)
        self.assertEqual(result['fedora_start'], original['partitions'][2]['start'])
        self.assertEqual(result['partitions'][0]['start'], result['fedora_start'] + result['fedora_new_size'])
        self.assertEqual(result['partitions'][-1]['start'] + result['partitions'][-1]['size'],
                         original['partitions'][-1]['start'] + original['partitions'][-1]['size'])
        self.assertEqual(original, table())

    def test_reject_unknown_or_overlapping_geometry(self):
        for change in ('type', 'overlap', 'missing_identity', 'four_partitions', 'allocation'):
            with self.subTest(change=change), self.assertRaises(storage.StorageError):
                value = table()
                if change == 'type': value['partitions'][2]['type'] = storage.EFI
                if change == 'overlap': value['partitions'][2]['start'] = 100
                if change == 'missing_identity': del value['id']
                if change == 'four_partitions': value['partitions'].append(value['partitions'][2])
                storage.layout(value, 900 if change == 'allocation' else 128)

    def test_shrink_requires_real_filesystem_boundary(self):
        plan = storage.layout(table())
        with self.assertRaises(storage.StorageError):
            storage.shrink_partition_command(plan, plan['fedora_filesystem_limit_bytes'] + 1)
        command = storage.shrink_partition_command(plan, plan['fedora_filesystem_limit_bytes'])
        self.assertIn('--no-tell-kernel', command)
        self.assertEqual(command[-3:], ['-N', '3', plan['disk']])
        self.assertEqual(storage.shrink_partition_input(plan, plan['fedora_filesystem_limit_bytes']),
                         f'size={plan["fedora_new_size"]}\n')

    @unittest.skipUnless(Path('/usr/sbin/sfdisk').exists(), 'util-linux sfdisk is required')
    def test_real_sfdisk_changes_only_end_and_preserves_partition_data(self):
        """Use the real binary against a sparse file, with no root or loop device."""
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'disk.img'
            with image.open('xb') as stream:
                stream.truncate(256 * storage.GIB)
            subprocess.run(['/usr/sbin/sfdisk', str(image)], input=(
                'label: gpt\n'
                'size=4M,type=U,name="Fedora EFI"\n'
                'size=4M,type=L,name="Fedora boot"\n'
                'type=L,name="Fedora root",attrs="LegacyBIOSBootable"\n'
            ), text=True, capture_output=True, check=True)
            def inspect():
                return json.loads(subprocess.check_output(
                    ['/usr/sbin/sfdisk', '--json', str(image)], text=True))['partitiontable']
            original = inspect()
            mapped = copy.deepcopy(original)
            mapped['device'] = '/dev/vda'
            for n, row in enumerate(mapped['partitions'], 1):
                row['node'] = f'/dev/vda{n}'
            plan = storage.layout(mapped, 128)
            sentinels = {}
            with image.open('r+b') as stream:
                for part in original['partitions']:
                    offset = part['start'] * 512 + 65536
                    sentinels[offset] = os.urandom(4096)
                    stream.seek(offset)
                    stream.write(sentinels[offset])
            command = storage.shrink_partition_command(plan, plan['fedora_filesystem_limit_bytes'])
            command[-1] = str(image)
            subprocess.run(command, input=storage.shrink_partition_input(
                plan, plan['fedora_filesystem_limit_bytes']), text=True, capture_output=True, check=True)
            expected = copy.deepcopy(original)
            expected['partitions'][2]['size'] = plan['fedora_new_size']
            self.assertEqual(inspect(), expected)
            with image.open('rb') as stream:
                for offset, data in sentinels.items():
                    stream.seek(offset)
                    self.assertEqual(stream.read(len(data)), data)

    def test_requires_reboot_and_exact_retained_table(self):
        original = table()
        plan = storage.layout(original)
        current = copy.deepcopy(original)
        current['partitions'][2]['size'] = plan['fedora_new_size']
        size = plan['fedora_filesystem_limit_bytes']
        storage.verify_after_reboot(original, current, plan, 'before', 'after', size)
        for boot, delta, changed in [('before', 0, False), ('after', 512, False), ('after', 0, True)]:
            with self.subTest(boot=boot, delta=delta, changed=changed), self.assertRaises(storage.StorageError):
                mutated = copy.deepcopy(current)
                if changed: mutated['partitions'][0]['size'] -= 1
                storage.verify_after_reboot(original, mutated, plan, 'before', boot, size+delta)

    def test_append_ids_are_distinct_and_valid(self):
        import uuid
        plan = storage.layout(table())
        ids = [str(uuid.uuid4()) for _ in range(3)]
        text = storage.append_input(plan, ids)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertNotIn('/dev/', text)
        with self.assertRaises(storage.StorageError):
            storage.append_input(plan, ids[:1]*3)


if __name__ == '__main__':
    unittest.main()
