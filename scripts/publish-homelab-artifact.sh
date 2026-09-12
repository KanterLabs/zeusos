#!/usr/bin/env bash
# Publish one immutable Zeus OS archive to the public homelab artifact origin.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ $# -eq 1 ]] || {
  echo 'Usage: scripts/publish-homelab-artifact.sh /path/to/zeusos-VERSION-git-COMMIT.oci' >&2
  exit 2
}

archive=$1
[[ -f "$archive" && ! -L "$archive" ]] || {
  echo 'Artifact must be a regular file, not a symlink.' >&2
  exit 1
}

name=${archive##*/}
if [[ ! "$name" =~ ^zeusos-([0-9A-Za-z][0-9A-Za-z.-]*)-(git-[0-9a-f]{12})\.oci$ ]]; then
  echo 'Artifact name is not a supported Zeus OS iteration archive.' >&2
  exit 1
fi
version=${BASH_REMATCH[1]}

size=$(stat -c %s -- "$archive")
[[ "$size" =~ ^[1-9][0-9]*$ && "$size" -le 8589934592 ]] || {
  echo 'Artifact size is outside the supported range.' >&2
  exit 1
}
digest=$(sha256sum -- "$archive" | awk '{print $1}')
[[ "$digest" =~ ^[0-9a-f]{64}$ ]] || exit 1

edge=(ssh -F /dev/null -i /home/shane/.ssh/homelab_mesh -o IdentitiesOnly=yes debian@10.0.0.101)
url="https://updates.shanekanterman.dev/zeusos/preview/v$version/$name"

# The root-owned remote helper validates the same fixed name/size/hash tuple,
# streams into a no-clobber partial file, and atomically publishes it.
"${edge[@]}" sudo /usr/local/sbin/zeus-publish-artifact \
  "$version" "$name" "$size" "$digest" < "$archive"

headers=$(mktemp)
trap 'rm -f -- "$headers"' EXIT
curl --fail --silent --show-error --location --head --output /dev/null \
  --dump-header "$headers" "$url"
published_size=$(awk -F ': *' 'tolower($1) == "content-length" {gsub("\\r", "", $2); value=$2} END {print value}' "$headers")
[[ "$published_size" == "$size" ]] || {
  echo "Published Content-Length mismatch: expected $size, got ${published_size:-missing}." >&2
  exit 1
}
if ! awk -F ': *' 'tolower($1) == "cf-cache-status" && toupper($2) ~ /^DYNAMIC\r?$|^BYPASS\r?$/ {found=1} END {exit !found}' "$headers"; then
  echo 'Cloudflare unexpectedly cached the archive; the >512 MiB stream must bypass cache.' >&2
  exit 1
fi

printf '%s\n' "$url"
