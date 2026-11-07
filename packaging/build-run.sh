#!/bin/sh
# Build the single-file, self-contained launcher: dist/simple-cuts-VERSION.sh
# It embeds the app; on first run it installs missing dependencies and starts.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/simple-cuts.sh")
OUT="$ROOT/dist/simple-cuts-$VERSION.sh"
mkdir -p "$ROOT/dist"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/simple_cuts/data"
cp "$ROOT"/src/simple_cuts/*.py "$tmp/simple_cuts/"
cp "$ROOT"/src/simple_cuts/data/* "$tmp/simple_cuts/data/"

{
    cat "$ROOT/simple-cuts.sh"
    printf '\nexit 0\n__PAYLOAD__\n'
    tar -C "$tmp" --owner=0 --group=0 --sort=name -czf - simple_cuts | base64
} > "$OUT"
chmod 755 "$OUT"
echo "$OUT"
