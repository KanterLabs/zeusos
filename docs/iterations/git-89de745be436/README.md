# Homelab update qualification

Build `git-89de745be436` qualified public, signed OCI delivery through the
Cloudflare Tunnel origin on 2026-09-12. The archive is larger than GitHub's
2 GiB release-asset limit and completed the native Zeus updater's download,
verification, staging, reboot, and final status checks on VM 115.

The signed manifest remains in `updates/candidates/` while the public latest
pointer serves compatibility build `git-4b8e9fae3ae6`. Once the owner laptop
reports that bridge build, the candidate can be promoted without rebuilding or
re-signing it.

See `homelab-update-qualification.json` for the recorded validation evidence.
