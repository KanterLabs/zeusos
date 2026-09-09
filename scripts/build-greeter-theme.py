#!/usr/bin/env python3
"""Rebuild the upstream theme resource with a CSS-only greeter overlay.

All original resources are retained; high-contrast CSS and authentication code
are unchanged. Run only while assembling an image, never against a live shell.
"""
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from gi.repository import Gio

resource_path = Path('/usr/share/gnome-shell/gnome-shell-theme.gresource')
resource = Gio.Resource.load(str(resource_path))
overlay = Path('/usr/share/zeus/greeter.css').read_text()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    manifest = ET.Element('gresources')
    group = ET.SubElement(manifest, 'gresource', prefix='/')
    names = []
    def extract(prefix):
        for child in resource.enumerate_children(prefix, Gio.ResourceLookupFlags.NONE):
            name = prefix + child
            if child.endswith('/'):
                extract(name)
            else:
                path = root / name.lstrip('/')
                path.parent.mkdir(parents=True, exist_ok=True)
                data = resource.lookup_data(name, Gio.ResourceLookupFlags.NONE).get_data()
                if name.endswith(('gnome-shell-light.css', 'gnome-shell-dark.css')):
                    data += ('\n' + overlay).encode()
                path.write_bytes(data)
                ET.SubElement(group, 'file').text = name.lstrip('/')
                names.append(name)
    extract('/')
    assert any(name.endswith('gnome-shell-dark.css') for name in names)
    xml = root / 'theme.xml'
    ET.ElementTree(manifest).write(xml, encoding='utf-8', xml_declaration=True)
    target = resource_path.with_suffix('.gresource.zeus')
    subprocess.run(['glib-compile-resources', str(xml), '--sourcedir', str(root), '--target', str(target)], check=True)
    rebuilt = Gio.Resource.load(str(target))
    for name in names:
        assert rebuilt.lookup_data(name, Gio.ResourceLookupFlags.NONE).get_size() > 0
    target.replace(resource_path)
print('Preserved upstream theme resources and added Zeus greeter CSS.')
