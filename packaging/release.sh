#!/bin/sh
# Release the version in src/simple_cuts/__init__.py: tag it and push the tag.
# GitHub Actions (.github/workflows/release.yml) then builds every package (.sh, .deb,
# .rpm, Flatpak, macOS .dmg) and publishes the GitHub release that installed copies
# update from. Works from Linux or macOS.
#
#   packaging/release.sh
#
# Needs: a clean git tree on main, pushed to origin.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/simple_cuts/__init__.py)
TAG="v$V"

[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit your changes first" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release from main" >&2; exit 1; }
git fetch -q origin main --tags
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag $TAG already exists: bump __version__ first" >&2; exit 1
fi
grep -q "^VERSION=\"$V\"" simple-cuts.sh || { echo "simple-cuts.sh VERSION != $V" >&2; exit 1; }

echo "==> Testing"
if command -v uv >/dev/null 2>&1; then
    uv run --frozen pytest -q
elif python3 -c 'import pytest' 2>/dev/null; then
    PYTHONPATH=src python3 -m pytest -q
else
    echo "pytest isn't installed here; GitHub Actions runs the tests before publishing"
fi

echo "==> Tagging and pushing $TAG"
git tag -a "$TAG" -m "Simple Cuts $V"
git push origin main "$TAG"
URL=$(git remote get-url origin | sed 's|^git@github.com:|https://github.com/|; s|\.git$||')
echo "==> GitHub Actions is building and publishing the release:"
echo "    $URL/actions/workflows/release.yml"
echo "    $URL/releases/tag/$TAG (the Flatpak and .dmg follow a few minutes later)"
