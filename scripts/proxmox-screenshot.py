#!/usr/bin/env python3
"""Capture an existing VM's display through its private host QMP socket."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='pve')
parser.add_argument('--vmid', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.vmid < 100 or args.host.startswith('-'):
    parser.error('Invalid host or VM ID')
remote_path = f'/var/tmp/zeusos-vm-{args.vmid}.png'
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
call('screendump',{{'filename':'{remote_path}','format':'png'}})
'''
subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host, 'python3 -'], input=script, text=True, check=True)
args.output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(['scp', f'{args.host}:{remote_path}', str(args.output)], check=True)
print(args.output)
