#!/usr/bin/env python3
"""Create or verify the one exact DNS route used by Zeus OS updates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


API = "https://api.cloudflare.com/client/v4"
ZONE = "shanekanterman.dev"
NAME = "updates.shanekanterman.dev"
TARGET = "b0ba744b-0ca9-4098-8172-136f72f76799.cfargotunnel.com"
TOKEN_FILE = Path("/etc/homelab-edge/credentials/cloudflare.ini")


def request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Cloudflare API returned HTTP {error.code}") from error
    if not result.get("success"):
        raise RuntimeError("Cloudflare API rejected the request")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="create the record when absent")
    args = parser.parse_args()

    try:
        credential_lines = TOKEN_FILE.read_text().splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot read {TOKEN_FILE}") from error
    values = {}
    for line in credential_lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    token = values.get("dns_cloudflare_api_token", "")
    if not credential_lines:
        raise RuntimeError(f"cannot read {TOKEN_FILE}")
    if not token:
        raise RuntimeError("DNS API token is missing")

    zones = request(token, "GET", f"/zones?{urllib.parse.urlencode({'name': ZONE, 'status': 'active'})}")["result"]
    if len(zones) != 1:
        raise RuntimeError("expected exactly one active DNS zone")
    zone_id = zones[0]["id"]
    query = urllib.parse.urlencode({"type": "CNAME", "name": NAME})
    records = request(token, "GET", f"/zones/{zone_id}/dns_records?{query}")["result"]
    if len(records) > 1:
        raise RuntimeError("multiple update-host records require operator inspection")

    expected = {"type": "CNAME", "name": NAME, "content": TARGET, "proxied": True, "ttl": 1}
    if records:
        record = records[0]
        matches = all(record.get(key) == value for key, value in expected.items())
        if not matches:
            raise RuntimeError("an existing update-host record differs; refusing to replace it")
        print("Cloudflare DNS route already matches")
        return 0
    if not args.apply:
        print("Cloudflare DNS route is absent; rerun with --apply", file=sys.stderr)
        return 3

    request(token, "POST", f"/zones/{zone_id}/dns_records", expected)
    print("Created proxied Cloudflare DNS route for updates.shanekanterman.dev")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
