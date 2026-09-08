#!/usr/bin/env python3
"""One-time owner setup on the fresh Zeus review VM, never an upgrade path.

Passwords travel through stdin to QGA, not command arguments or the image.
An existing owner account is a hard stop; this cannot reset an existing user.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import string
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='pve')
parser.add_argument('--vmid', type=int, default=115, choices=[115])
parser.add_argument('--public-key', type=Path, required=True)
parser.add_argument('--credentials', type=Path, required=True)
args = parser.parse_args()
if args.host.startswith('-'):
    parser.error('Invalid SSH host')
public_key = args.public_key.read_text().strip()
if not public_key.startswith('ssh-ed25519 ') or '\n' in public_key:
    parser.error('Expected one Ed25519 public key')
args.credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
password = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
credentials = {'vmid': args.vmid, 'username': 'shane', 'password': password}
fd = os.open(args.credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump(credentials, stream, indent=2)
    stream.write('\n')

script = '''import os,pwd,pathlib,subprocess,json
assert pathlib.Path('/usr/share/zeus/version').is_file(), 'Not a Zeus image'
try:
    pwd.getpwnam('shane')
except KeyError:
    pass
else:
    raise SystemExit('Owner already exists; refusing to change credentials or data')
def run(*args,**kwargs):return subprocess.run(args,check=True,**kwargs)
run('useradd','--create-home','--groups','wheel','--shell','/bin/bash','shane')
run('chpasswd',input='shane:'+PASSWORD+'\\n',text=True)
user=pwd.getpwnam('shane')
home=pathlib.Path(user.pw_dir)
ssh=home/'.ssh';ssh.mkdir(mode=0o700,exist_ok=True)
key=ssh/'authorized_keys';key.write_text(PUBLIC_KEY+'\\n');key.chmod(0o600)
os.chown(ssh,user.pw_uid,user.pw_gid);os.chown(key,user.pw_uid,user.pw_gid)
run('restorecon','-RF',str(home))
run('hostnamectl','set-hostname','zeusos-preview')
print(json.dumps({'owner':'shane','created':True,'automatic_login':False}))
'''
script = 'PASSWORD=' + repr(password) + '\nPUBLIC_KEY=' + repr(public_key) + '\n' + script
result = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', args.host,
     f'qm guest exec {args.vmid} --pass-stdin -- /usr/bin/python3 -'],
    input=script, text=True, capture_output=True, check=True,
)
report = json.loads(result.stdout)
if report.get('exitcode') != 0:
    raise SystemExit('Guest setup failed; credentials retained locally for diagnosis. Existing accounts are never reset.')
print('Owner created. Credentials are stored in the requested private file; no password was embedded in the image.')
