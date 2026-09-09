#!/usr/bin/env python3
"""Time a native Zeus window's first map (not compositor presentation latency)."""
import time

def utc_rfc3339(epoch_ns):
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds)) + f'.{nanoseconds:09d}Z'

START = time.monotonic()
MEASUREMENT_STARTED_AT_UTC = utc_rfc3339(time.time_ns())
import argparse
import json
import runpy
import sys
parser=argparse.ArgumentParser()
parser.add_argument('app',choices=['temp','welcome'])
args=parser.parse_args()
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk,GLib
original=Gtk.Window.present
reported=False

def mapped(window):
    global reported
    if reported:return
    reported=True
    print(json.dumps({'app':args.app,'first_map_ms':round((time.monotonic()-START)*1000,1),
                      'definition':'Python measurement process start to first Gtk map; excludes compositor presentation',
                      'measurement_started_at_utc':MEASUREMENT_STARTED_AT_UTC,
                      'measurement_ended_at_utc':utc_rfc3339(time.time_ns())}),flush=True)
    def stop():
        app=window.get_application()
        if app:app.quit()
        else:window.destroy()
        return False
    GLib.timeout_add(100,stop)

def present(window,*values):
    window.connect('map',mapped)
    return original(window,*values)
Gtk.Window.present=present
path='/usr/libexec/zeus-temp-window' if args.app=='temp' else '/usr/libexec/zeus-welcome'
sys.argv=[path]
runpy.run_path(path,run_name='__main__')
