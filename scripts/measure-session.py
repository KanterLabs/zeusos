#!/usr/bin/env python3
"""Comparable idle samples; this reports VM activity, never battery runtime."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument('--label', required=True)
parser.add_argument('--seconds', type=int, default=60)
parser.add_argument('--settle', type=int, default=20)
args = parser.parse_args()
assert 20 <= args.seconds <= 300 and 0 <= args.settle <= 60

def counters():
    rows = {row.split()[0]: row.split()[1:] for row in Path('/proc/stat').read_text().splitlines()}
    cpu = list(map(int, rows['cpu'][:8]))
    return sum(cpu), cpu[3] + cpu[4], int(rows['ctxt'][0]), int(rows['processes'][0])

time.sleep(args.settle)
start = time.time()
previous = initial = counters()
samples = []
for _ in range(args.seconds // 5):
    time.sleep(5)
    current = counters()
    total = current[0] - previous[0]
    info = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    samples.append({'cpu_percent': round(100 * (1 - (current[1] - previous[1]) / total), 3) if total else 0,
                    'memory_used_mib': round((info['MemTotal'] - info['MemAvailable']) / 1024, 1)})
    previous = current
end = time.time()
log = subprocess.run(['journalctl', '--user', '-u', 'zeus-temp-clean.service', '--since', '@'+str(int(start)), '--until', '@'+str(int(end)), '-o', 'json', '--no-pager'], capture_output=True, text=True)
activations = 0
if log.returncode == 0:
    for row in log.stdout.splitlines():
        event=json.loads(row)
        if str(event.get('MESSAGE','')).startswith('Starting '): activations += 1
print(json.dumps({'label': args.label, 'seconds':round(end-start,2), 'settle_seconds':args.settle,
                  'samples':samples,'median_cpu_percent':statistics.median(s['cpu_percent'] for s in samples),
                  'median_memory_used_mib':statistics.median(s['memory_used_mib'] for s in samples),
                  'context_switches_per_second':round((current[2]-initial[2])/(end-start),1),
                  'processes_started':current[3]-initial[3], 'temp_cleanup_activations':activations if log.returncode==0 else None,
                  'limits':'VM idle activity; no physical battery, energy or hardware-wakeup measurement'},indent=2))
