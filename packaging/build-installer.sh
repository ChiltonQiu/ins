#!/usr/bin/env bash
#
# Build the Windows installer, on Linux.
#
#     ./packaging/build-installer.sh            # version from pyproject.toml
#     ./packaging/build-installer.sh 0.2.2
#
# Needs makensis:  sudo pacman -S nsis   /   sudo apt install nsis
#
# The staged tree comes out of the source distribution rather than out of the
# working copy, so the installer and the tarball cannot contain different
# things — and so that a dirty checkout cannot ship .venv, runtime/ or
# somebody's .env inside a file that goes to other people.

set -euo pipefail
cd "$(dirname "$0")/.."

command -v makensis >/dev/null || {
    echo "makensis is not installed."
    echo "  Arch:   sudo pacman -S nsis"
    echo "  Debian: sudo apt install nsis"
    exit 1
} >&2

VERSION="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)}"
[ -n "$VERSION" ] || { echo "Could not determine the version." >&2; exit 1; }

STAGE="dist/stage"
TARBALL="dist/renewal-$VERSION.tar.gz"

if [ ! -f "$TARBALL" ]; then
    echo "==> building $TARBALL"
    python -m build --outdir dist >/dev/null
fi

echo "==> staging $TARBALL"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar xzf "$TARBALL" -C "$STAGE" --strip-components=1

# Belt and braces: none of these should be in a source distribution, and all
# of them would be a disclosure rather than a bug if one ever were.
rm -rf "$STAGE/.venv" "$STAGE/runtime" "$STAGE/blobs" "$STAGE/.env" \
       "$STAGE"/*.egg-info

for required in install.cmd scripts/bootstrap.ps1 scripts/install.ps1 scripts/start.ps1; do
    [ -e "$STAGE/$required" ] || {
        echo "staged tree is missing $required" >&2
        exit 1
    }
done

echo "==> makensis"
makensis -DVERSION="$VERSION" packaging/renewal.nsi

echo
echo "built dist/Renewal-$VERSION-setup.exe"
ls -lh "dist/Renewal-$VERSION-setup.exe"
