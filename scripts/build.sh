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
track=$(python3 -c 'import json; print(json.load(open("image/release-track.json"))["version"])')
[[ "$version" == "$track" ]] || { echo 'Version differs from the active release track.' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo 'Commit tracked build changes first.' >&2; exit 1; }
[[ -z "$(git ls-files --others --exclude-standard desktop image zeus scripts)" ]] || { echo 'Commit build inputs first.' >&2; exit 1; }
revision=$(git rev-parse HEAD)
build_id="git-${revision:0:12}"
update_sequence=$(git show -s --format=%ct "$revision")
[[ "$update_sequence" =~ ^[1-9][0-9]+$ ]] || { echo 'Invalid payload timestamp.' >&2; exit 1; }
mkdir -p out
podman image exists "$base" || podman pull --platform linux/amd64 "$base"
podman build --pull=never --build-arg "BASE_IMAGE=$base" --build-arg "VERSION=$version" \
  --build-arg "SOURCE_COMMIT=$revision" --build-arg "BUILD_ID=$build_id" \
  --build-arg "UPDATE_SEQUENCE=$update_sequence" -f image/Containerfile \
  -t "localhost/zeusos:$version" -t "localhost/zeusos:$version-$build_id" .
podman image inspect "localhost/zeusos:$version" > out/image-inspect.json
podman run --rm "localhost/zeusos:$version" cat /usr/share/zeus/packages.lock > out/packages.lock
printf '%s\n' "$revision" > out/source-commit
python3 - "$version" "$revision" "$build_id" "$update_sequence" <<'PYINFO'
import json, sys
from pathlib import Path
info = dict(zip(('version', 'source_commit', 'build_id'), sys.argv[1:4]))
info['update_sequence'] = int(sys.argv[4])
Path('out/build-info.json').write_text(json.dumps(info, indent=2) + '\n')
PYINFO
