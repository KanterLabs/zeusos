"""Read-only inspector filters by kernel peer identity and fails closed."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SRC=Path(__file__).resolve().parents[1]/'desktop/rootfs/usr/libexec/zeus-temp-inspector'
loader=importlib.machinery.SourceFileLoader('temp_inspector_test',str(SRC))
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec);loader.exec_module(module)

class Inspector(unittest.TestCase):
    def test_uid_filter_and_mapped_file_without_open_descriptor(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);proc=root/'proc';proc.mkdir();data=root/'mapped.txt';data.write_bytes(b'unchanged')
            value=data.stat()
            for pid,uid in [('100',1234),('101',4321)]:
                p=proc/pid;p.mkdir();(p/'fd').mkdir()
                (p/'status').write_text(f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
                (p/'maps').write_text(f'1-2 r--p 0 {os.major(value.st_dev):x}:{os.minor(value.st_dev):x} {value.st_ino} /ignored-path\n' if uid==1234 else '')
            result=module.inspect_uid(1234,proc)
            self.assertTrue(result['reliable'])
            self.assertIn((value.st_dev,value.st_ino),result['active'])
            self.assertEqual(module.inspect_uid(4321,proc)['active'],[])
            self.assertEqual(data.read_bytes(),b'unchanged')

    def test_incomplete_process_metadata_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'100').mkdir();(root/'100/status').write_text('malformed')
            self.assertFalse(module.inspect_uid(1234,root)['reliable'])
