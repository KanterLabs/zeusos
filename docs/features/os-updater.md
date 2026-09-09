# In-OS preview updater

This implementation adds owner-triggered update checks and installation to the
current preview. The product version remains 0.1.0-preview.2; signed sequence
numbers distinguish newer Git builds. Checks fetch small metadata only. There is
no periodic updater, automatic download, live package upgrade or automatic reboot.

## Implementation contract

The native Updates app runs `/usr/libexec/zeus-update status --json` for local state and
`/usr/libexec/zeus-update check --json` for a network check. Both return one JSON object.
Install requests use `pkexec --disable-internal-agent /usr/libexec/zeus-update-admin
install BUILD_ID SHA256`. The privileged helper independently verifies the
current signed feed and exact selection, then starts a root systemd oneshot
service. Closing the window does not interrupt that service. The root helper
accepts no arbitrary URL, file path or executable from the desktop.

The public JSON shape is:

```json
{
  "ok": true,
  "state": "available",
  "current": {"version": "0.1.0-preview.2", "build_id": "git-0123456789ab", "sequence": 1},
  "candidate": {
    "version": "0.1.0-preview.2", "build_id": "git-abcdef012345",
    "source_commit": "abcdef012345abcdef012345abcdef012345abcd",
    "sequence": 2, "published_at": "2026-09-09T00:00:00Z",
    "notes": "Release notes", "archive_size": 12345,
    "archive_sha256": "64 lowercase hexadecimal characters"
  },
  "progress": null,
  "message": "An update is available.",
  "error": null,
  "checked_at": "2026-09-09T00:00:00Z"
}
```

States are `idle`, `available`, `up_to_date`, `queued`, `downloading`, `verifying`,
`staging`, `ready`, `error`, and `interrupted`. `candidate` and `checked_at` may be
null. Progress, when present, is `{ "bytes": 100, "total": 1000 }`. Root job state
is published atomically at `/var/lib/zeus/updater/status.json`; the window watches
that directory and only refreshes after changes, with no idle polling timer.
The window must treat queued/downloading/verifying/staging/ready as authoritative
over a simultaneous network-check result. User cancellation of authentication is
an ordinary canceled request, not a successful installation.

Ready means the exact verified image is staged for the next reboot. The UI asks
the owner to save work and explains that any Temp policy set to On boot also
applies to update reboots, then uses the native GNOME reboot confirmation.
Local status recognizes a completed update from the immutable installed build
identity, even when the previous job record still says ready.

## Signed publication and privilege boundary

`updates/preview.json` and its SSH signature on the repository's main branch are
the preview feed. The verification key is installed under `/usr/share/zeus/`;
private signing material stays outside the builder and image. The feed binds
product, channel, architecture, source/build, sequence, artifact size/hash and OCI
manifest digest. The client verifies the signature before parsing release data.
The installer verifies it again, enforces a sequence floor and exact image
identity, downloads into a private root-owned directory, and checks the archive
before invoking `bootc switch --transport oci-archive --retain` without `--apply`.
It preserves a different existing staged deployment and keeps owner files and
preferences out of updater storage. Root job locking serializes install requests.

Sequence is the payload Git commit timestamp, recorded in the image as
`/usr/share/zeus/update-sequence`. Build identity disambiguates the same sequence;
a different build with an equal/older sequence is not a forward update. Trusted
feed metadata and root state use schema 1. OS rollback selects a retained image;
it never restores a profile/database snapshot or replaces mutable owner data.

The [metrics history](../metrics.md) records measured results and limitations.
Native installation through signed Updates and an explicit reboot were
verified on VM 115 for `git-17d205103f3a`. The booted manifest is
`sha256:4da2b1c5bdba3d5e0451e5f36389d3238bdf739a976951bca11b925f5e9d133d`,
and the post-reboot status reported `The selected update is installed.` The
[final iteration receipt](../iterations/git-17d205103f3a/README.md) records the
full evidence, including the retained `git-1a34bbfe8509` rollback slot. No
rollback cycle was run. See the [timestamped metrics history](../metrics.md)
for runtime observations; physical battery remains unmeasured on this VM.
