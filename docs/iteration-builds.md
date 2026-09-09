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
