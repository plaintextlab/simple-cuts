#!/bin/sh
# Build dist/simple-cuts-VERSION-1.noarch.rpm (Fedora, RHEL/Alma/Rocky with EPEL)
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VERSION=$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/simple-cuts.sh")
top=$(mktemp -d)
trap 'rm -rf "$top"' EXIT
mkdir -p "$top/SOURCES"
# The spec installs straight from a copy of the source tree.
cp -r "$ROOT/src" "$ROOT/packaging" "$ROOT/LICENSE" "$top/SOURCES/"
rpmbuild --quiet -bb \
    --define "_topdir $top" \
    --define "app_version $VERSION" \
    --define "dist %{nil}" \
    "$ROOT/packaging/rpm/simple-cuts.spec"
mkdir -p "$ROOT/dist"
cp "$top"/RPMS/noarch/*.rpm "$ROOT/dist/"
ls "$ROOT"/dist/simple-cuts-"$VERSION"-*.noarch.rpm
