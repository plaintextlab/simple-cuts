#!/bin/sh
# Build dist/simple-cuts_VERSION_all.deb (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS, ...)
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VERSION=$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/simple-cuts.sh")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

"$ROOT/packaging/stage.sh" "$tmp" /usr
# Debian keeps licenses under /usr/share/doc
mkdir -p "$tmp/usr/share/doc/simple-cuts"
mv "$tmp/usr/share/licenses/simple-cuts/LICENSE" "$tmp/usr/share/doc/simple-cuts/copyright"
rm -rf "$tmp/usr/share/licenses"

size=$(du -sk "$tmp/usr" | cut -f1)
mkdir -p "$tmp/DEBIAN"
cat > "$tmp/DEBIAN/control" <<EOF
Package: simple-cuts
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Installed-Size: $size
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-4.0, gir1.2-gstreamer-1.0,
 gir1.2-gst-plugins-base-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, ffmpeg
Recommends: gstreamer1.0-libav, gstreamer1.0-plugins-bad, gstreamer1.0-pulseaudio
Maintainer: Bitangsha Ray <plaintextlab@gmail.com>
Description: Simple video cutter for camera footage
 Open camera clips, mark the parts to keep and export them (optionally
 without audio) before importing into a video editor. Lossless stream-copy
 or frame-accurate re-encoding (NVIDIA NVENC or CPU).
EOF
cat > "$tmp/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
EOF
chmod 755 "$tmp/DEBIAN/postinst"

mkdir -p "$ROOT/dist"
out="$ROOT/dist/simple-cuts_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$tmp" "$out" >/dev/null
echo "$out"
