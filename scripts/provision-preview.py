#!/usr/bin/env python3
"""Provision the empty review VM through a private NoCloud seed.

Only for the initial installation. Never use this to update an existing user.
The generic image contains no credentials. The owner-requested preview password default is applied locally;
only a password hash and public SSH key enter the hypervisor seed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='pve')
parser.add_argument('--vmid', type=int, default=115, choices=[115])
parser.add_argument('--public-key', type=Path, required=True)
parser.add_argument('--credentials', type=Path, required=True)
parser.add_argument('--fresh-install', action='store_true', required=True)
args = parser.parse_args()
if args.host.startswith('-'):
    parser.error('Invalid SSH host')
public_key = args.public_key.read_text().strip()
if not public_key.startswith('ssh-ed25519 ') or '\n' in public_key:
    parser.error('Expected one Ed25519 public key')
status = subprocess.check_output(['ssh', '-o', 'BatchMode=yes', args.host, f'qm status {args.vmid}'], text=True)
if status.strip() != 'status: stopped':
    parser.error('Initial provisioning requires the review VM to be stopped')
args.credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
# Owner-requested default for the isolated Zeus preview account.
password = 'root'
fd = os.open(args.credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump({'vmid': args.vmid, 'username': 'shane', 'password': password}, stream, indent=2)
    stream.write('\n')
password_hash = subprocess.check_output(['openssl', 'passwd', '-6', '-stdin'], input=password+'\n', text=True).strip()
config = {
    'hostname': 'zeusos-preview', 'manage_etc_hosts': True,
    'disable_root': True, 'ssh_pwauth': False,
    'users': [{'name': 'shane', 'gecos': 'Shane', 'groups': ['wheel'],
               'shell': '/bin/bash', 'lock_passwd': False, 'passwd': password_hash,
               'sudo': ['ALL=(ALL) ALL'], 'ssh_authorized_keys': [public_key]}],
    'package_update': False, 'package_upgrade': False,
    'runcmd': [['touch', '/etc/cloud/cloud-init.disabled']],
}
seed = '#cloud-config\n' + json.dumps(config, indent=2) + '\n'
remote = '/mnt/pve/sata-ssd/snippets/zeusos-preview-user.yaml'
subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host,
                f'umask 077; mkdir -p /mnt/pve/sata-ssd/snippets; test ! -e {remote} && cat > {remote}'],
               input=seed, text=True, check=True)
subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host,
                f'qm set {args.vmid} --ide2 local-lvm:cloudinit --cicustom user=sata-ssd:snippets/zeusos-preview-user.yaml --ipconfig0 ip=dhcp'], check=True)
print('Private first-boot seed attached. Login details stored in the requested local credentials file.')
