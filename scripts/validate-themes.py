#!/usr/bin/env python3
"""Parse both GTK styles with the image's real GTK libraries."""
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('theme', type=Path)
parser.add_argument('--gtk', choices=['3.0', '4.0'])
args = parser.parse_args()
if args.gtk is None:
    for version in ('3.0', '4.0'):
        subprocess.run([sys.executable, __file__, str(args.theme), '--gtk', version], check=True)
else:
    import gi
    gi.require_version('Gtk', args.gtk)
    from gi.repository import Gtk
    errors = []
    provider = Gtk.CssProvider()
    def failed(_provider, section, error):
        if 'warning' not in error.domain:
            errors.append(error.message)
    provider.connect('parsing-error', failed)
    provider.load_from_path(str(args.theme / f'gtk-{args.gtk}' / 'gtk.css'))
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'GTK {args.gtk} theme parsed without CSS errors.')
