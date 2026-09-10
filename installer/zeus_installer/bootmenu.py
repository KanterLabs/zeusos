"""Render a Fedora GRUB entry pointing only at the isolated Zeus ESP.

The installer must record and verify the ESP identity before using this output.
This module never regenerates GRUB or changes firmware/default boot selection.
"""
import re

ENTRY_ID = 'zeusos-dualboot'
SCRIPT_PATH = '/etc/grub.d/42_zeus_dualboot'


def render(esp_uuid):
    # FAT volume IDs from blkid. Do not interpolate arbitrary paths or labels
    # into shell or GRUB source.
    if not isinstance(esp_uuid, str) or not re.fullmatch(r'[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}', esp_uuid):
        raise ValueError('Expected the verified Zeus FAT filesystem UUID')
    return f'''#!/bin/sh
# Managed by Zeus dual-boot installer. Fedora retains boot ownership.
cat <<'ZEUS_GRUB_ENTRY'
menuentry 'Zeus OS' --id '{ENTRY_ID}' {{
    insmod part_gpt
    insmod fat
    insmod chain
    search --no-floppy --fs-uuid --set=zeus_esp {esp_uuid.upper()}
    chainloader ($zeus_esp)/EFI/fedora/shimx64.efi
}}
ZEUS_GRUB_ENTRY
'''


def visible_menu_config(existing):
    """Modify only menu presentation; retain every default selection setting."""
    lines = existing.splitlines()
    keys = {'GRUB_TIMEOUT_STYLE': 'menu', 'GRUB_TIMEOUT': '5'}
    seen = set()
    result = []
    for line in lines:
        match = re.match(r'^\s*(GRUB_TIMEOUT_STYLE|GRUB_TIMEOUT)\s*=', line)
        if match:
            key = match.group(1)
            if key not in seen:
                result.append(f'{key}={keys[key]}')
                seen.add(key)
        else:
            result.append(line)
    result.extend(f'{key}={value}' for key, value in keys.items() if key not in seen)
    return '\n'.join(result) + '\n'
