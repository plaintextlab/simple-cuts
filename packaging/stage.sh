#!/bin/sh
# Install Simple Cuts into a staging tree. Used by every package format.
#   packaging/stage.sh DESTDIR [PREFIX=/usr]
set -eu
DEST=${1?usage: stage.sh DESTDIR [PREFIX]}   # may be "" (Flatpak installs into /app)
PREFIX=${2:-/usr}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ID=io.github.simplecuts.SimpleCuts

lib="$DEST$PREFIX/lib/simple-cuts/simple_cuts"
mkdir -p "$lib/data"
for f in "$ROOT"/src/simple_cuts/*.py; do install -m644 "$f" "$lib/"; done
install -m644 "$ROOT"/src/simple_cuts/data/* "$lib/data/"

install -Dm644 "$ROOT/src/simple_cuts/data/$ID.desktop" "$DEST$PREFIX/share/applications/$ID.desktop"
install -Dm644 "$ROOT/src/simple_cuts/data/$ID.svg" \
    "$DEST$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg"
install -Dm644 "$ROOT/src/simple_cuts/data/$ID.metainfo.xml" \
    "$DEST$PREFIX/share/metainfo/$ID.metainfo.xml"
install -Dm644 "$ROOT/LICENSE" "$DEST$PREFIX/share/licenses/simple-cuts/LICENSE"

mkdir -p "$DEST$PREFIX/bin"
cat > "$DEST$PREFIX/bin/simple-cuts" <<EOF
#!/bin/sh
PYTHONPATH="$PREFIX/lib/simple-cuts\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m simple_cuts "\$@"
EOF
chmod 755 "$DEST$PREFIX/bin/simple-cuts"
