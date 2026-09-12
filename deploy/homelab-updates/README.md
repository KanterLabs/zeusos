# Public homelab update origin

Zeus OS keeps its small, detached-signed channel manifest on GitHub and serves
the large immutable OCI archive from `updates.shanekanterman.dev`. Cloudflare
Tunnel supplies the public TLS edge without exposing a router port or origin IP.
The loopback Caddy origin sends `Cache-Control: no-store, no-transform`: standard
Cloudflare plans cannot cache files larger than 512 MiB, but the tunnel can
stream the response dynamically.

The updater accepts only exact GitHub release URLs or this exact hostname and
path shape. It still enforces the signed byte count, SHA-256, OCI manifest
digest, build identity, and sequence. Redirects from the homelab origin are
restricted to the same hostname.

## Edge layout

- VM: Proxmox 108 `homelab-edge` (`10.0.0.101`)
- Dedicated disk: `scsi-0QEMU_QEMU_HARDDISK_drive-scsi1`, 64 GiB
- Filesystem label and mount: `zeus-updates` at `/srv/zeusos`
- Private origin: Caddy on TLS loopback `127.0.0.1:19101`
- Tunnel: `b0ba744b-0ca9-4098-8172-136f72f76799`
- Public route: `updates.shanekanterman.dev`

The disk has `backup=0`: every published object is append-only and must also be
retained in the Proxmox release store. The VM itself has a verified backup from
2026-09-12 before this service was added.

## Install and validate

Before first formatting, re-check that the by-id device is exactly 64 GiB,
blank, unmounted, and has serial `drive-scsi1`. Then format only that exact
device as ext4 with label `zeus-updates`. Install the mount unit, Caddy service
and config, publisher helper, and full cloudflared config from this directory.

Validate both configurations before restart:

```sh
caddy validate --config /etc/zeusos-updates/Caddyfile --adapter caddyfile
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
cloudflared tunnel --config /etc/cloudflared/config.yml ingress rule \
  https://updates.shanekanterman.dev/zeusos/preview/v0.1.0-preview.2/zeusos-0.1.0-preview.2-git-0123456789ab.oci
```

Run `configure-cloudflare-dns.py` without arguments to inspect the exact record,
then rerun with `--apply` only when it reports the route absent. Finally verify
unknown hosts and paths return 404, archive `HEAD` reports the exact byte count,
`CF-Cache-Status` is `DYNAMIC` or `BYPASS`, and a complete file larger than 2
GiB downloads with the published SHA-256.

## Publish

`scripts/publish-homelab-artifact.sh` streams one validated archive to the
root-owned append-only helper and verifies the public response. Generate the
signed feed with `make-update-manifest.py --artifact-origin homelab` only after
publication and full-download verification.

Older Zeus OS builds accept only GitHub release URLs. Ship one final GitHub
bootstrap build containing the new fixed-host policy before moving the signed
feed to the homelab URL.

## Rollback

Restore the previous `/etc/cloudflared/config.yml`, validate it, and restart the
tunnel service. Point the signed feed back to the retained GitHub bootstrap
asset and signature. The dedicated origin service can then be disabled without
touching stored archives; never reformat the artifact disk as a rollback step.
