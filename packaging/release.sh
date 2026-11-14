#!/bin/sh
# Publish a GitHub release for the version in src/simple_cuts/__init__.py.
# Installed copies (simple-cuts.sh --install) pick it up through the in-app updater.
#
#   packaging/release.sh             build, tag, push and create the release
#   packaging/release.sh --no-flatpak  skip the (slow) Flatpak bundle
#
# Needs: a clean git tree on main, the GitHub CLI (gh) logged in.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/simple_cuts/__init__.py)
TAG="v$V"

[ -z "$(git status --porcelain)" ] || { echo "commit your changes first" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release from main" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag $TAG already exists: bump __version__ first" >&2; exit 1
fi
grep -q "^VERSION=\"$V\"" simple-cuts.sh || { echo "simple-cuts.sh VERSION != $V" >&2; exit 1; }

echo "==> Testing"
if command -v uv >/dev/null 2>&1; then uv run pytest -q; else python3 -m pytest -q; fi

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
