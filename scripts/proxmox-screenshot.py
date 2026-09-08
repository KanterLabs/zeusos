#!/usr/bin/env python3
"""Capture an existing VM's display through its private host QMP socket."""
import argparse
from pathlib import Path
import subprocess
import struct
import tempfile
import zlib

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='pve')
parser.add_argument('--vmid', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.vmid < 100 or args.host.startswith('-'):
    parser.error('Invalid host or VM ID')
remote_path = f'/var/tmp/zeusos-vm-{args.vmid}.ppm'
script = f'''
import json,socket
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.settimeout(10)
s.connect('/var/run/qemu-server/{args.vmid}.qmp')
f=s.makefile('rwb',buffering=0)
json.loads(f.readline())
def call(command,arguments=None):
    f.write((json.dumps({{'execute':command,'arguments':arguments or {{}}}})+'\\n').encode())
    while True:
        r=json.loads(f.readline())
        if 'error' in r:raise RuntimeError(r['error'])
        if 'return' in r:return r['return']
call('qmp_capabilities')
call('screendump',{{'filename':'{remote_path}'}})
'''
subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host, 'python3 -'], input=script, text=True, check=True)
args.output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as directory:
    ppm = Path(directory) / 'screen.ppm'
    subprocess.run(['scp', f'{args.host}:{remote_path}', str(ppm)], check=True)
    with ppm.open('rb') as stream:
        assert stream.readline().strip() == b'P6'
        width, height = map(int, stream.readline().split())
        assert stream.readline().strip() == b'255'
        pixels = stream.read()
    assert len(pixels) == width * height * 3
    def chunk(kind, payload):
        return struct.pack('!I', len(payload)) + kind + payload + struct.pack('!I', zlib.crc32(kind + payload))
    raw = b''.join(b'\0' + pixels[row * width * 3:(row + 1) * width * 3] for row in range(height))
    args.output.write_bytes(b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) +
        chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
print(args.output)
