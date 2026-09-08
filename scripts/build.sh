#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo on the dedicated Zeus builder VM.' >&2
  exit 1
fi
command -v podman >/dev/null
base=$(python3 -c 'import json; print(json.load(open("image/inputs.json"))["base"])')
version=$(python3 -c 'import json; print(json.load(open("image/inputs.json"))["version"])')
revision=$(git rev-parse HEAD)
mkdir -p out
podman build --pull=never --build-arg "BASE_IMAGE=$base" --build-arg "VERSION=$version" \
  --build-arg "SOURCE_COMMIT=$revision" -f image/Containerfile \
  -t "localhost/zeusos:$version" .
podman image inspect "localhost/zeusos:$version" > out/image-inspect.json
podman run --rm "localhost/zeusos:$version" cat /usr/share/zeus/packages.lock > out/packages.lock
printf '%s\n' "$revision" > out/source-commit

