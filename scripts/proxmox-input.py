#!/usr/bin/env python3
"""Send console keys through private QMP. Sensitive text is stdin-only."""
import argparse
import json
import shlex
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='pve')
parser.add_argument('--vmid', type=int, default=115)
parser.add_argument('--keys', action='append', default=[])
parser.add_argument('--text-stdin', action='store_true')
args = parser.parse_args()
if args.vmid < 100 or args.host.startswith('-'):
    parser.error('Invalid host or VM ID')
steps = [combo.split('-') for combo in args.keys]
if args.text_stdin:
    basic = {' ': 'spc', '\n': 'ret', '\t': 'tab', '-': 'minus', '=': 'equal',
             '.': 'dot', ',': 'comma', '/': 'slash', ';': 'semicolon',
             "'": 'apostrophe', '[': 'bracket_left', ']': 'bracket_right',
             '\\': 'backslash', '`': 'grave_accent'}
    shifted = {'_': 'minus', '+': 'equal', ':': 'semicolon', '"': 'apostrophe',
               '?': 'slash', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
               '^': '6', '&': '7', '*': '8', '(': '9', ')': '0', '<': 'comma',
               '>': 'dot', '{': 'bracket_left', '}': 'bracket_right', '|': 'backslash'}
    for char in sys.stdin.read():
        if char.isascii() and char.isalnum():
            steps.append((['shift'] if char.isupper() else []) + [char.lower()])
        elif char in basic:
            steps.append([basic[char]])
        elif char in shifted:
            steps.append(['shift', shifted[char]])
        else:
            parser.error('Console text must use the supported ASCII keyboard characters')
remote = '''import json,socket,sys,time
request=json.load(sys.stdin)
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(10)
s.connect('/var/run/qemu-server/'+str(request['vmid'])+'.qmp')
f=s.makefile('rwb',buffering=0);json.loads(f.readline())
def call(command,arguments=None):
 f.write((json.dumps({'execute':command,'arguments':arguments or {}})+'\\n').encode())
 while True:
  r=json.loads(f.readline())
  if 'error' in r:raise RuntimeError(r['error'])
  if 'return' in r:return r['return']
call('qmp_capabilities')
for step in request['steps']:
 call('send-key',{'keys':[{'type':'qcode','data':key} for key in step],'hold-time':40})
 time.sleep(.07)
'''
subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host, 'python3 -c ' + shlex.quote(remote)],
               input=json.dumps({'vmid': args.vmid, 'steps': steps}), text=True, check=True)
