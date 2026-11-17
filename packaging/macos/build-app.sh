#!/bin/sh
# Build a self-contained "Simple Cuts.app" (and a .dmg) for macOS.
#
#   packaging/macos/build-app.sh          → dist/Simple Cuts.app, dist/simple-cuts-VERSION-macos-ARCH.dmg
#   packaging/macos/build-app.sh --no-dmg → only the .app
#
# The app bundles Python, PyGObject, GTK 4, GStreamer and ffmpeg from conda-forge, so
# the Mac it runs on needs nothing installed (no Homebrew). It is built for the
# architecture of the Mac you build on (arm64 or x86_64).
#
# Needs: macOS with the Xcode command line tools (iconutil, hdiutil, codesign) and
# micromamba (taken from $MICROMAMBA or PATH, otherwise downloaded into build/macos).
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/simple_cuts/__init__.py)
ID=io.github.simplecuts.SimpleCuts
ARCH=$(uname -m)
case "$ARCH" in arm64) PLATFORM=osx-arm64 ;; x86_64) PLATFORM=osx-64 ;;
    *) echo "unsupported architecture $ARCH" >&2; exit 1 ;; esac

BUILD="$ROOT/build/macos"
ENV="$BUILD/env"
APP="$ROOT/dist/Simple Cuts.app"
mkdir -p "$BUILD" "$ROOT/dist"

# ---- micromamba ----------------------------------------------------------------
MM=${MICROMAMBA:-$(command -v micromamba || true)}
if [ -z "$MM" ]; then
    MM="$BUILD/micromamba/bin/micromamba"
    if [ ! -x "$MM" ]; then
        echo "==> Downloading micromamba ($PLATFORM) from micro.mamba.pm"
        mkdir -p "$BUILD/micromamba"
        curl -fsSL "https://micro.mamba.pm/api/micromamba/$PLATFORM/latest" \
            | tar -xj -C "$BUILD/micromamba" bin/micromamba
    fi
fi
export MAMBA_ROOT_PREFIX="$BUILD/mamba-root"

# ---- runtime environment ---------------------------------------------------------
if [ ! -x "$ENV/bin/python3" ] || [ "${REBUILD_ENV:-0}" = 1 ]; then
    echo "==> Creating the runtime (conda-forge)"
    rm -rf "$ENV"
    "$MM" create -y -q -p "$ENV" --platform "$PLATFORM" -c conda-forge --override-channels \
        "python=3.12" pygobject pycairo "gtk4>=4.12" librsvg adwaita-icon-theme \
        gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav \
        "ffmpeg=*=gpl*"
fi

# ---- assemble the bundle ---------------------------------------------------------
echo "==> Assembling $APP"
rm -rf "$APP"
C="$APP/Contents"
RT="$C/Resources/runtime"
mkdir -p "$C/MacOS" "$C/Resources/app"
cp -R "$ENV" "$RT"

# Trim what an end user never needs.
rm -rf "$RT/conda-meta" "$RT/include" "$RT/man" "$RT/share/man" "$RT/share/doc" \
       "$RT/share/info" "$RT/share/gtk-doc" "$RT/share/locale" "$RT/share/gir-1.0" \
       "$RT/lib/pkgconfig" "$RT/share/pkgconfig" "$RT/lib/cmake" "$RT/ssl/man"
PYLIB=$(ls -d "$RT"/lib/python3.*)
rm -rf "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" \
       "$PYLIB/ensurepip" "$PYLIB/lib2to3" "$PYLIB/site-packages/pip"
find "$RT" -name '*.a' -delete
rm -f "$RT/lib/gstreamer-1.0/libgstcurl.dylib"   # needs libcurl; unused
find "$RT" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The app itself.
mkdir -p "$C/Resources/app/simple_cuts/data"
cp src/simple_cuts/*.py "$C/Resources/app/simple_cuts/"
cp src/simple_cuts/data/* "$C/Resources/app/simple_cuts/data/"
cp LICENSE "$C/Resources/"

# Python runs from Contents/MacOS so macOS treats the process as this bundle (name and
# icon in the Dock and menu bar). Its @rpath (@loader_path/../lib) resolves via the
# lib symlink.
cp "$RT/bin/python3" "$C/MacOS/python3"
ln -s Resources/runtime/lib "$C/lib"

# Launcher: point every GTK/GStreamer/Python lookup at the bundle, wherever it was moved.
cat > "$C/MacOS/simple-cuts" <<EOF
#!/bin/sh
C=\$(cd "\$(dirname "\$0")/.." && pwd -P)
RT="\$C/Resources/runtime"
CACHE="\$HOME/Library/Caches/$ID"
mkdir -p "\$CACHE"

export PATH="\$RT/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONHOME="\$RT" PYTHONPATH="\$C/Resources/app" PYTHONNOUSERSITE=1
export DYLD_FALLBACK_LIBRARY_PATH="\$RT/lib"
export GI_TYPELIB_PATH="\$RT/lib/girepository-1.0"
export XDG_DATA_DIRS="\$RT/share"
export GSETTINGS_SCHEMA_DIR="\$RT/share/glib-2.0/schemas"
export FONTCONFIG_PATH="\$RT/etc/fonts" FONTCONFIG_FILE="\$RT/etc/fonts/fonts.conf"
export GST_PLUGIN_SYSTEM_PATH="\$RT/lib/gstreamer-1.0" GST_PLUGIN_PATH=
export GST_PLUGIN_SCANNER="\$RT/libexec/gstreamer-1.0/gst-plugin-scanner"
export GST_REGISTRY="\$CACHE/gst-registry-$V.bin"
export SSL_CERT_FILE="\$RT/ssl/cacert.pem"
export GTK_IM_MODULE_FILE=/dev/null

# gdk-pixbuf's loader cache holds absolute paths: regenerate it when the app moves.
loaders=\$(ls -d "\$RT"/lib/gdk-pixbuf-2.0/*/loaders 2>/dev/null | head -n1)
if [ -n "\$loaders" ]; then
    export GDK_PIXBUF_MODULEDIR="\$loaders" GDK_PIXBUF_MODULE_FILE="\$CACHE/loaders-$V.cache"
    if ! grep -qF "\$loaders" "\$GDK_PIXBUF_MODULE_FILE" 2>/dev/null; then
        "\$RT/bin/gdk-pixbuf-query-loaders" > "\$GDK_PIXBUF_MODULE_FILE" 2>/dev/null || true
    fi
fi

# Finder passes -psn_… on old macOS versions; drop it.
case "\${1:-}" in -psn_*) shift ;; esac
exec "\$C/MacOS/python3" -m simple_cuts "\$@"
EOF
chmod 755 "$C/MacOS/simple-cuts"

# fontconfig's config names the build-time prefix: make it relative to the bundle.
if [ -f "$RT/etc/fonts/fonts.conf" ]; then
    sed -i '' "s|$ENV|$RT|g" "$RT/etc/fonts/fonts.conf"
    sed -i '' "s|<cachedir>$RT[^<]*</cachedir>|<cachedir prefix=\"xdg\">fontconfig</cachedir>|" \
        "$RT/etc/fonts/fonts.conf"
fi

# ---- icon ----------------------------------------------------------------------
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
    "$ENV/bin/rsvg-convert" -w $s -h $s "src/simple_cuts/data/$ID.svg" -o "$ICONSET/icon_${s}x${s}.png"
    d=$((s * 2))
    "$ENV/bin/rsvg-convert" -w $d -h $d "src/simple_cuts/data/$ID.svg" -o "$ICONSET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$ICONSET" -o "$C/Resources/AppIcon.icns"

# ---- Info.plist ------------------------------------------------------------------
cat > "$C/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Simple Cuts</string>
    <key>CFBundleDisplayName</key><string>Simple Cuts</string>
    <key>CFBundleIdentifier</key><string>$ID</string>
    <key>CFBundleVersion</key><string>$V</string>
    <key>CFBundleShortVersionString</key><string>$V</string>
    <key>CFBundleExecutable</key><string>simple-cuts</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key><string>MIT License</string>
</dict>
</plist>
EOF

# Ad-hoc signature (required to run on Apple Silicon after the files were copied).
codesign --force --deep --sign - "$APP" 2>/dev/null || codesign --force --deep --sign - "$APP"
echo "==> Built $APP ($(du -sh "$APP" | cut -f1))"

# ---- disk image ------------------------------------------------------------------
if [ "${1:-}" != "--no-dmg" ]; then
    DMG="$ROOT/dist/simple-cuts-$V-macos-$ARCH.dmg"
    STAGE="$BUILD/dmg"
    rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
    cp -R "$APP" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    hdiutil create -quiet -volname "Simple Cuts $V" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
    rm -rf "$STAGE"
    echo "==> Built $DMG ($(du -h "$DMG" | cut -f1))"
fi
