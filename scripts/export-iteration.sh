#!/usr/bin/env bash
# Export an immutable build artifact without creating a new product version.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ ${EUID} -eq 0 ]] || { echo 'Run with sudo on the dedicated builder.' >&2; exit 1; }
version=$(python3 -c 'import json; print(json.load(open("out/build-info.json"))["version"])')
build_id=$(python3 -c 'import json; print(json.load(open("out/build-info.json"))["build_id"])')
[[ "$version" =~ ^[0-9A-Za-z.-]+$ && "$build_id" =~ ^git-[0-9a-f]{12}$ ]] || exit 1
artifact_dir="out/iterations/$build_id"
mkdir -p "$artifact_dir"
archive="zeusos-${version}-${build_id}.oci"
[[ ! -e "$artifact_dir/$archive" ]] || { echo 'Build archive already exists; verify or use a new source commit.' >&2; exit 1; }
podman save --format oci-archive -o "$artifact_dir/$archive" "localhost/zeusos:$version-$build_id"
cp out/build-info.json out/packages.lock "$artifact_dir/"
(cd "$artifact_dir" && sha256sum "$archive" > SHA256SUMS)
printf '%s\n' "$artifact_dir"
