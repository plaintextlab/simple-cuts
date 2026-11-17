#!/bin/sh
# Publish a GitHub release for the version in src/simple_cuts/__init__.py.
# Installed copies (simple-cuts.sh --install) pick it up through the in-app updater.
#
#   packaging/release.sh             build, tag, push and create the release
#   packaging/release.sh --no-flatpak  skip the (slow) Flatpak bundle
#   packaging/release.sh --mac       on a Mac, after the release exists: build the .dmg
#                                    and add it to that release (once per architecture)
#
# Needs: a clean git tree on main, the GitHub CLI (gh) logged in.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/simple_cuts/__init__.py)
TAG="v$V"

if [ "${1:-}" = "--mac" ]; then
    [ "$(uname)" = Darwin ] || { echo "--mac must run on macOS" >&2; exit 1; }
    git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git fetch -q origin tag "$TAG" ||
        { echo "release $TAG doesn't exist yet: run packaging/release.sh on Linux first" >&2; exit 1; }
    [ "$(git rev-parse HEAD)" = "$(git rev-parse "$TAG^{commit}")" ] ||
        { echo "check out $TAG first (git checkout $TAG)" >&2; exit 1; }
    echo "==> Building the macOS app for $TAG"
    packaging/macos/build-app.sh
    gh release upload "$TAG" --clobber "dist/simple-cuts-$V-macos-$(uname -m).dmg"
    echo "==> Added the .dmg to $(gh release view "$TAG" --json url -q .url)"
    exit 0
fi

[ -z "$(git status --porcelain)" ] || { echo "commit your changes first" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release from main" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag $TAG already exists: bump __version__ first" >&2; exit 1
fi
grep -q "^VERSION=\"$V\"" simple-cuts.sh || { echo "simple-cuts.sh VERSION != $V" >&2; exit 1; }

echo "==> Testing"
if command -v uv >/dev/null 2>&1; then uv run --frozen pytest -q; else python3 -m pytest -q; fi

echo "==> Building $V"
rm -f dist/simple-cuts*
packaging/build-run.sh
packaging/deb/build-deb.sh
command -v rpmbuild >/dev/null 2>&1 && packaging/rpm/build-rpm.sh
if [ "${1:-}" != "--no-flatpak" ] && command -v flatpak-builder >/dev/null 2>&1; then
    packaging/flatpak/build-flatpak.sh
fi

echo "==> Publishing $TAG"
git tag -a "$TAG" -m "Simple Cuts $V"
git push origin main "$TAG"
gh release create "$TAG" --title "Simple Cuts $V" --generate-notes dist/simple-cuts*
echo "==> Released: $(gh release view "$TAG" --json url -q .url)"
