#!/usr/bin/env bash
set -euo pipefail

# Package the Fedora launcher independently of the Zeus OS image build.  The
# only mutable path is the task-specific output directory; source files remain
# untouched and no credentials are copied into the archive.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

version=0.1.0-preview.2
output_dir=${ZEUS_INSTALLER_OUTPUT_DIR:-"$repo_root/out/installer"}
mkdir -p "$output_dir"

# Keep the product version shown by the launcher stable while giving each
# source checkout a distinct RPM release. A clean commit is reproducible;
# local edits are explicitly marked so a developer cannot mistake a local
# package for the committed release. ZEUS_INSTALLER_BUILD_ID is useful for
# release automation and tests, but is restricted to RPM-safe characters.
git_commit=unknown
git_count=0
git_dirty=false
if git_commit_value=$(git rev-parse --short=12 HEAD 2>/dev/null) &&
    git_count_value=$(git rev-list --count HEAD 2>/dev/null); then
    git_commit=$git_commit_value
    git_count=$git_count_value
    if [[ -n "$(git status --porcelain --untracked-files=all 2>/dev/null)" ]]; then
        git_dirty=true
    fi
fi
build_id=${ZEUS_INSTALLER_BUILD_ID:-}
if [[ -z "$build_id" ]]; then
    if [[ "$git_commit" != "unknown" ]]; then
        build_id="git${git_count}.g${git_commit}"
        if [[ "$git_dirty" == true ]]; then
            build_id+=".dirty"
        fi
    else
        build_id="local"
        git_dirty=true
    fi
fi
if [[ ! "$build_id" =~ ^[A-Za-z0-9][A-Za-z0-9._]*$ ]]; then
    echo "ZEUS_INSTALLER_BUILD_ID must contain only letters, numbers, dots and underscores" >&2
    exit 2
fi
rpm_release="0.preview.2.${build_id}"

required_files=(
    LICENSE
    installer/zeus-installer
    installer/zeus-installer-helper
    installer/README.md
    installer/zeus_installer/__init__.py
    installer/zeus_installer/__main__.py
    installer/zeus_installer/gui.py
    installer/zeus_installer/artifacts.py
    installer/zeus_installer/backend.py
    installer/zeus_installer/bootmenu.py
    installer/zeus_installer/efi_update.py
    installer/zeus_installer/executor.py
    installer/zeus_installer/preflight.py
    installer/zeus_installer/removal.py
    installer/zeus_installer/storage.py
    installer/zeus_installer/data/update-allowed-signers
    installer/zeus_installer/vendor/update_manifest.py
    installer/packaging/org.zeus.Installer.desktop
    installer/packaging/zeus-installer.spec
)
for path in "${required_files[@]}"; do
    [[ -f "$path" ]] || { echo "Missing installer package input: $path" >&2; exit 1; }
done

# Parse every launcher module without writing interpreter caches into the
# source checkout.  The package script should leave its inputs read-only even
# when it is run repeatedly during release validation.
python3 - "$repo_root"/installer/zeus_installer/*.py <<'PY'
from pathlib import Path
import sys

for filename in sys.argv[1:]:
    source_path = Path(filename)
    compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")
PY

tmp_parent=${TMPDIR:-"$output_dir"}
mkdir -p "$tmp_parent"
staging_root=$(mktemp -d "$tmp_parent/.zeus-installer-package.XXXXXX")
rpm_root=""
cleanup() {
    rm -rf "$staging_root"
    if [[ -n "$rpm_root" ]]; then
        rm -rf "$rpm_root"
    fi
}
trap cleanup EXIT
package_name="zeus-installer-$version"
package_root="$staging_root/$package_name"
mkdir -p "$package_root/installer/packaging" "$package_root/installer/zeus_installer"

cp LICENSE "$package_root/LICENSE"
cp -a installer/zeus-installer installer/zeus-installer-helper installer/README.md "$package_root/installer/"
cp -a installer/packaging/. "$package_root/installer/packaging/"
cp -a installer/zeus_installer/. "$package_root/installer/zeus_installer/"
chmod 0755 "$package_root/installer/zeus-installer"
chmod 0755 "$package_root/installer/zeus-installer-helper"
# Never publish interpreter caches created by local validation runs.
find "$package_root/installer/zeus_installer" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$package_root/installer/zeus_installer" -type f -name '*.pyc' -delete
find "$package_root/installer/zeus_installer" -type f -name '*.py' -exec chmod 0644 {} \;
cat > "$package_root/BUILD-INFO" <<EOF
product_version=$version
build_id=$build_id
rpm_release=$rpm_release
git_commit=$git_commit
source_dirty=$git_dirty
EOF

tarball="$output_dir/$package_name.tar.gz"
tar -C "$staging_root" --sort=name --mtime='UTC 1970-01-01' \
    --owner=0 --group=0 --numeric-owner -czf "$tarball" "$package_name"
sha256sum "$tarball" > "$tarball.sha256"
echo "Created $tarball"

rpmbuild_bin=${RPMBUILD_BIN:-}
rpm_skip_reason=""
if [[ -z "$rpmbuild_bin" ]]; then
    rpmbuild_bin=$(command -v rpmbuild || true)
fi
if [[ -n "$rpmbuild_bin" ]] && ! command -v "$rpmbuild_bin" >/dev/null 2>&1 && [[ ! -x "$rpmbuild_bin" ]]; then
    rpm_skip_reason="Configured RPMBUILD_BIN is unavailable"
    rpmbuild_bin=""
fi
if [[ -n "$rpmbuild_bin" ]]; then
    rpm_root=$(mktemp -d "$tmp_parent/.zeus-installer-rpm.XXXXXX")
    mkdir -p "$rpm_root"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
    # The spec deliberately reads source files from the repository source
    # directory, so no network source fetch or OS image build can occur.
    "$rpmbuild_bin" -bb installer/packaging/zeus-installer.spec \
        --define "_topdir $rpm_root" \
        --define "zeus_build_id $build_id" \
        --define "zeus_git_commit $git_commit" \
        --define "zeus_source_dirty $git_dirty" \
        --define "zeus_rpm_release $rpm_release" \
        --define "_sourcedir $repo_root" \
        --define "_rpmdir $output_dir/rpm" \
        --define "_srcrpmdir $output_dir/rpm" \
        --define "_builddir $rpm_root/BUILD" \
        --define "_buildrootdir $rpm_root/BUILDROOT" \
        --define "_specdir $repo_root/installer/packaging" \
        --define "_build_id_links none"
    find "$output_dir/rpm" -type f -name '*.rpm' -print -exec sha256sum {} \;
else
    if [[ -n "$rpm_skip_reason" ]]; then
        echo "$rpm_skip_reason; skipped RPM creation (tarball is ready)." >&2
    else
        echo "rpmbuild not available; skipped RPM creation (tarball is ready)."
    fi
fi
