#!/usr/bin/env python3
"""Validate shipped dconf values against the image's actual installed schemas."""
import configparser
from pathlib import Path
from gi.repository import Gio, GLib

config = configparser.ConfigParser(interpolation=None, strict=True)
config.read([str(path) for path in sorted(Path('/etc/dconf/db/local.d').iterdir()) if path.is_file()])
assert config.sections(), 'No desktop defaults loaded'
source = Gio.SettingsSchemaSource.get_default()
count = 0
for section in config.sections():
    schema_id = section.replace('/', '.')
    if '/custom-keybindings/' in section:
        schema_id = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
    schema = source.lookup(schema_id, True)
    assert schema, f'Missing installed schema: {schema_id}'
    for key, raw in config[section].items():
        assert schema.has_key(key), f'Unknown setting {schema_id}:{key}'
        definition = schema.get_key(key)
        value = GLib.Variant.parse(definition.get_value_type(), raw, None, None)
        assert definition.range_check(value), f'Invalid value {schema_id}:{key}'
        if schema_id == 'org.gnome.shell' and key == 'favorite-apps':
            for desktop_id in value.unpack():
                assert Gio.DesktopAppInfo.new(desktop_id), f'Missing favorite launcher: {desktop_id}'
        count += 1
print(f'Validated {count} desktop settings against installed GNOME schemas.')

