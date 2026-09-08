#!/usr/bin/env python3
"""Five guest CPU/memory samples after an operator-controlled idle settle."""
import argparse
import json
from pathlib import Path
import statistics
import time

parser = argparse.ArgumentParser()
parser.add_argument('--settle', type=int, default=60)
parser.add_argument('--label', required=True)
args = parser.parse_args()
if not 0 <= args.settle <= 300:
    parser.error('Settle time must be between 0 and 300 seconds')

def cpu():
    fields = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return sum(fields), fields[3] + fields[4]

time.sleep(args.settle)
samples = []
previous = cpu()
for _ in range(5):
    time.sleep(4)
    current = cpu()
    total = current[0] - previous[0]
    busy = 100 * (1 - (current[1] - previous[1]) / total) if total else 0
    info = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    samples.append({'cpu_percent': round(busy, 3), 'memory_used_mib': round((info['MemTotal'] - info['MemAvailable']) / 1024, 1)})
    previous = current
print(json.dumps({
    'label': args.label,
    'settle_seconds': args.settle,
    'interval_seconds': 4,
    'samples': samples,
    'median_cpu_percent': statistics.median(x['cpu_percent'] for x in samples),
    'median_memory_used_mib': statistics.median(x['memory_used_mib'] for x in samples),
    'memory_definition': 'MemTotal minus MemAvailable; includes non-reclaimable guest memory',
}, indent=2))
