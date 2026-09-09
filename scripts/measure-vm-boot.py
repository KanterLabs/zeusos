#!/usr/bin/env python3
"""Measure a dedicated test VM cold start to GDM, including firmware.

Requires an explicitly stopped VM. Never stops or resets a running guest.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
parser=argparse.ArgumentParser()
parser.add_argument('--vmid',type=int,required=True)
parser.add_argument('--host',default='pve')
parser.add_argument('--guest',required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
assert args.vmid >=100 and not args.host.startswith('-') and not args.guest.startswith('-')

def utc_rfc3339(epoch_ns):
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds)) + f'.{nanoseconds:09d}Z'

status=subprocess.check_output(['ssh',args.host,f'qm status {args.vmid}'],text=True)
if status.strip()!='status: stopped':raise SystemExit('Refusing: cold-start measurement requires a stopped VM')
start=time.monotonic()
measurement_started_at_utc=utc_rfc3339(time.time_ns())
subprocess.run(['ssh',args.host,f'qm start {args.vmid}'],check=True,stdout=subprocess.DEVNULL)
probe=shlex.join(['python3','-c',"import subprocess; sessions=subprocess.check_output(['loginctl','list-sessions','--no-legend'],text=True).splitlines();\nfor row in sessions:\n p=dict(x.split('=',1) for x in subprocess.check_output(['loginctl','show-session',row.split()[0],'-p','Class','-p','State'],text=True).splitlines());\n if p.get('Class')=='greeter' and p.get('State')=='active':print('greeter-active')"])
# GDM's greeter session active is a consistent programmatic milestone, not a
# claim of exact compositor presentation. Capture a console image separately.
connected=None
while time.monotonic()-start<120:
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=1','-o','ConnectionAttempts=1',args.guest,probe],capture_output=True,text=True)
    if r.returncode==0:
        if connected is None:connected=time.monotonic()-start
        if 'greeter-active' in r.stdout:break
    time.sleep(.5)
else:raise SystemExit('GDM did not become ready in120seconds')
ready=time.monotonic()-start
r=subprocess.run(['ssh',args.guest,'systemd-analyze time'],capture_output=True,text=True,check=True)
measurement_ended_at_utc=utc_rfc3339(time.time_ns())
result={'measurement_started_at_utc':measurement_started_at_utc,
        'measurement_ended_at_utc':measurement_ended_at_utc,
        'cold_start_to_active_greeter_s':round(ready,3),'first_ssh_s':round(connected,3),
        'systemd_analyze':r.stdout.strip(),'measurement':'Host qm start command to active GDM greeter; SSH polling interval0.5s; includes firmware and probe overhead'}
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
