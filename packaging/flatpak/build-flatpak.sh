#!/bin/sh
# Build dist/simple-cuts-VERSION.flatpak — runs on any distro with Flatpak.
#   packaging/flatpak/build-flatpak.sh            build the bundle
#   packaging/flatpak/build-flatpak.sh --install  build and install it for this user
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
ID=io.github.simplecuts.SimpleCuts
VERSION=$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/simple-cuts.sh")

command -v flatpak-builder >/dev/null 2>&1 || {
    echo "flatpak-builder is required (e.g. sudo apt install flatpak-builder)" >&2; exit 1; }

flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
cd "$HERE"
flatpak-builder --user --install-deps-from=flathub --force-clean --ccache \
    ${FLATPAK_BUILDER_ARGS:-} \
    --repo=repo build "$ID.yml"
mkdir -p "$ROOT/dist"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    repo "$ROOT/dist/simple-cuts-$VERSION.flatpak" "$ID"
echo "$ROOT/dist/simple-cuts-$VERSION.flatpak"

if [ "${1:-}" = "--install" ]; then
    flatpak install --user -y --reinstall "$ROOT/dist/simple-cuts-$VERSION.flatpak"
    echo "Run with: flatpak run $ID"
fi
