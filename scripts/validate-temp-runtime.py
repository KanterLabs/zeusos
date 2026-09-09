#!/usr/bin/env python3
"""Exercise deletion only inside a newly created disposable fixture home.

Requires the socket inspector; never touches the real owner's Temp/Downloads.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

parser=argparse.ArgumentParser()
parser.add_argument('--module-dir', default='/usr/lib/zeus')
args=parser.parse_args()
sys.path.insert(0,args.module_dir)
from zeus_temp import HomeManager, ActiveFileError

with tempfile.TemporaryDirectory(prefix='zeus-temp-acceptance-') as folder:
    home=Path(folder)
    (home/'Downloads').mkdir();(home/'Downloads/legacy.txt').write_bytes(b'permanent')
    manager=HomeManager(home, now=lambda: 1000, boot_id='runtime-a', proc_root='/proc')
    manager.setup()
    (home/'Temp/nested').mkdir();(home/'Temp/nested/expired.txt').write_bytes(b'expired')
    result=manager.sweep()
    assert result['ok'], result.get('errors')
    assert not (home/'Temp/nested').exists(), result
    (home/'Temp/new.txt').write_bytes(b'keep me')
    assert not manager.sweep()['swept']
    assert (home/'Temp/new.txt').exists()
    (home/'Documents').mkdir();(home/'Documents/new.txt').write_bytes(b'older permanent')
    kept=manager.keep(['new.txt'])
    assert kept['ok'], kept
    assert Path(kept['kept'][0]['destination']).read_bytes()==b'keep me'
    assert (home/'Documents/new.txt').read_bytes()==b'older permanent'
    assert not (home/'Temp/new.txt').exists()
    active=home/'Temp/active.txt';active.write_bytes(b'being used')
    with active.open('rb'):
        result=manager.sweep(force=True)
        assert active.exists(), result
        try:manager.keep(['active.txt'])
        except ActiveFileError:pass
        else:raise AssertionError('Keep accepted active file')
    assert manager.sweep(force=True)['ok']
    assert not active.exists()
    (home/'Temp/movie').write_bytes(b'placeholder');(home/'Temp/movie.part').write_bytes(b'partial')
    result=manager.sweep(force=True)
    assert (home/'Temp/movie').exists() and (home/'Temp/movie.part').exists(), result
    # Fixture-only removal to advance to the next independent case.
    (home/'Temp/movie').unlink();(home/'Temp/movie.part').unlink()
    outside=home/'permanent.txt';outside.write_bytes(b'outside')
    (home/'Temp/link').symlink_to(outside)
    manager.sweep(force=True)
    assert outside.read_bytes()==b'outside'
    assert (home/'Downloads/legacy.txt').read_bytes()==b'permanent'
    before=len(os.listdir('/proc/self/fd'))
    for _ in range(100): manager.status()
    assert len(os.listdir('/proc/self/fd')) <= before+1, 'File descriptor leak'
    print(json.dumps({'boot_once':True,'nested_cleanup':True,'legacy_preserved':True,'keep_conflict_safe':True,'active_fd_preserved':True,'partial_companion_preserved':True,'symlink_target_preserved':True,'status_fd_stable':True}))
