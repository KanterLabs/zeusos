# Same-version preview iterations

The active product version is **0.1.0-preview.2**. Refinements and new features
stay on this version until Shane explicitly chooses a milestone release.
`image/release-track.json` records that rule. CI checks the image, Cargo and
Welcome versions against it; the builder also refuses a mismatched version.

Each committed build receives `git-<12-character-commit>` as its build ID.
The full revision and short build ID are embedded in the OS, exposed by
`zeus version` and Welcome, and recorded in `out/build-info.json`. Builds require
clean tracked source; artifact names and container tags include the build ID.

On the dedicated builder:

```sh
sudo ./scripts/build.sh
sudo ./scripts/export-iteration.sh
```

The immutable archive and checksum are under `out/iterations/git-…/`. Sign the
checksum outside the builder using the existing private release key and verify
it before deployment. Store each archive in its own root-owned update directory
on the guest; bootc continues to identify the exact image by digest even when
several builds share the same version.

Publish new archives as uniquely named assets of the existing
`v0.1.0-preview.2` prerelease. Do not overwrite a historical archive, its checksum,
or its signature. Keep the initial release notes as historical evidence and add
a latest-build reference and dated iteration notes. A Git commit/build is not a
new product release or version bump.

## Signed preview update publication

The owner-triggered Updates path consumes the latest public preview feed at
`updates/preview.json` and its detached signature `updates/preview.json.sig`.
Installed clients read the canonical metadata URL
`https://raw.githubusercontent.com/KanterLabs/zeusos/main/updates/preview.json`
and its `.sig` companion; this is public publication access, not access to the
private review environment.
The feed points to an immutable, uniquely named OCI release asset such as
`zeusos-0.1.0-preview.2-git-<12-character-commit>.oci`; publish each asset once
and never replace an existing archive, manifest, or signature. The public feed
and release assets carry only signed build metadata and image bytes. They do not
make the Proxmox host, review VM, SSH service, or guest publicly reachable.

Build and export the immutable candidate on the dedicated builder first. Create
a per-build manifest from a separate publication/signing environment with the
repository's [`make-update-manifest.py`](../scripts/make-update-manifest.py)
tool. Keep the update notes file concise (4 KiB or less) and keep the matching
build info beside the archive:

```sh
candidate_dir='out/iterations/git-<12-character-commit>'
python3 scripts/make-update-manifest.py \
  --archive "$candidate_dir/zeusos-<version>-git-<12-character-commit>.oci" \
  --build-info "$candidate_dir/build-info.json" \
  --sequence "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["update_sequence"])' "$candidate_dir/build-info.json")" \
  --notes-file "$candidate_dir/update-notes.md" \
  --output "$candidate_dir/update-git-<12-character-commit>.json" \
  --signing-key /secure/release/zeusos-preview
```

The generator's required flags are `--archive`, `--build-info`, `--sequence`,
`--notes-file`, `--output`, and `--signing-key`. `--sequence` is the payload
Git commit timestamp in seconds; it is embedded in the image as
`/usr/share/zeus/update-sequence` and lets the client order builds that retain
the product version. The generator refuses to overwrite an existing output and
writes `update-git-<12-character-commit>.json.sig` beside the per-build
manifest. Keep the private signing key outside the builder and outside the
image; the installed public verification material is rooted at
`/usr/share/zeus/update-allowed-signers`.

Verify the source commit, image identity, per-build signature, archive checksum,
and release-asset upload before publishing the latest pointer. After those
gates pass, copy the exact signed pair from the per-build directory to
`updates/preview.json` and `updates/preview.json.sig` in one repository commit.
Replacing that latest pointer across iterations is deliberate; the immutable
per-build manifest and release asset remain retained under their build ID.

For example, verify the exported archive and the per-build feed pair before the
copy:

```sh
(cd "$candidate_dir" && sha256sum -c SHA256SUMS)
ssh-keygen -Y verify \
  -f security/allowed_signers \
  -I zeusos-preview \
  -n zeusos-update \
  -s "$candidate_dir/update-git-<12-character-commit>.json.sig" \
  < "$candidate_dir/update-git-<12-character-commit>.json"

cp -- "$candidate_dir/update-git-<12-character-commit>.json" updates/preview.json
cp -- "$candidate_dir/update-git-<12-character-commit>.json.sig" updates/preview.json.sig
git add updates/preview.json updates/preview.json.sig
git commit -m "Publish signed Zeus OS preview update"
```

The release receipt used for manual bootstrap and bootc recovery has its own
checksum signature namespace, `zeusos-release` (for example,
`docs/releases/preview-2/SHA256SUMS.sig`). The updater feed signature uses
`zeusos-update` and must be verified separately:

```sh
ssh-keygen -Y verify \
  -f security/allowed_signers \
  -I zeusos-preview \
  -n zeusos-update \
  -s updates/preview.json.sig \
  < updates/preview.json
```

The distinct namespaces prevent a valid bootstrap artifact receipt from being
mistaken for authorization of feed metadata. Feed publication is an input to
native qualification; record that qualification separately before owner
handoff, and retain the manual bootc bootstrap and rollback procedure in the
[preview VM runbook](preview-vm.md#bootstrap-and-manual-update-path) until then.
