#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ ${EUID} -eq 0 ]] || { echo 'Run on the dedicated builder as root.' >&2; exit 1; }
builder=$(python3 -c 'import json; print(json.load(open("image/inputs.json"))["builder"])')
version=$(python3 -c 'import json; print(json.load(open("image/inputs.json"))["version"])')
mkdir -p out/disk
podman run --rm --privileged --security-opt label=disable \
  -v "$PWD/image/disk.toml:/config.toml:ro" \
  -v "$PWD/out/disk:/output" \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  "$builder" build --type qcow2 --rootfs ext4 "localhost/zeusos:$version"
find out/disk -name '*.qcow2' -exec sha256sum {} \; > out/SHA256SUMS
