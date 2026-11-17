#!/bin/sh
# Simple Cuts — all-in-one launcher.
#
#   ./simple-cuts.sh [files or folders...]   check dependencies, install missing ones, run
#   ./simple-cuts.sh --check                 only report what is missing
#   ./simple-cuts.sh --install-deps          only install dependencies
#   ./simple-cuts.sh --install               install for this user: app menu entry, updates
#   ./simple-cuts.sh --uninstall             remove that install (keeps your settings)
#   ./simple-cuts.sh --help
#
# Works from a git checkout (runs ./src) or as a single self-contained file built by
# packaging/build-run.sh (the app is embedded after the __PAYLOAD__ marker below).
#
# Supported package managers: apt (Debian, Ubuntu, Mint, Pop!_OS, ...), dnf/yum (Fedora,
# RHEL, Alma, Rocky), zypper (openSUSE), pacman (Arch, Manjaro, EndeavourOS, ...),
# xbps (Void), apk (Alpine), emerge (Gentoo). Immutable systems (Silverblue, SteamOS, ...)
# are pointed to the Flatpak or a distrobox instead.
set -eu

APP="simple-cuts"
VERSION="0.3.2"
# Set by --install (and by the in-app updater) in the installed copy of this script.
INSTALLED_APP_DIR=""

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

SELF=$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")
HERE=$(dirname "$SELF")

# ---------------------------------------------------------------- locate the app
find_app_dir() {
    if [ -n "$INSTALLED_APP_DIR" ] && [ -f "$INSTALLED_APP_DIR/simple_cuts/app.py" ]; then
        echo "$INSTALLED_APP_DIR"; return                 # per-user install (--install)
    fi
    if [ -f "$HERE/src/simple_cuts/app.py" ]; then        # git checkout / package
        echo "$HERE/src"; return
    fi
    if [ -f "$HERE/../lib/simple-cuts/simple_cuts/app.py" ]; then   # installed layout
        echo "$HERE/../lib/simple-cuts"; return
    fi
    # Self-contained script: unpack the embedded payload once per version. Done in
    # Python (always installed by now) so minimal systems without awk/tar/base64 work.
    "$(find_python)" - "$SELF" "${XDG_CACHE_HOME:-$HOME/.cache}/$APP" "$VERSION" <<'PY' \
        || die "cannot find the Simple Cuts sources next to $SELF"
import base64, hashlib, io, os, sys, tarfile, tempfile
script, cache, version = sys.argv[1:4]
data = open(script, "rb").read()
marker = b"\n__PAY" + b"LOAD__\n"          # split so this line is not itself a marker
i = data.rfind(marker)
if i < 0:
    sys.exit(1)
payload = data[i + len(marker):]
dest = os.path.join(cache, f"{version}-{hashlib.sha256(payload).hexdigest()[:12]}")
if not os.path.isfile(os.path.join(dest, "simple_cuts", "app.py")):
    os.makedirs(cache, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=cache)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload)), mode="r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(tmp, filter="data")
        else:
            tar.extractall(tmp)
    try:
        os.rename(tmp, dest)
    except OSError:                          # another launcher won the race
        pass
print(dest)
PY
}

# ---------------------------------------------------------------- python
find_python() {
    # Prefer the distro's Python: that is where the system PyGObject lives
    # (a pyenv/conda python3 earlier on PATH would not see it).
    for p in /usr/bin/python3 /usr/local/bin/python3 python3; do
        if command -v "$p" >/dev/null 2>&1; then echo "$p"; return; fi
    done
    echo ""
}

# Prints missing components, one per line: "hard:<what>" or "soft:<what>".
check_deps() {
    for t in ffmpeg ffprobe; do
        command -v "$t" >/dev/null 2>&1 || echo "hard:$t"
    done
    py=$(find_python)
    if [ -z "$py" ]; then echo "hard:python3"; return; fi
    "$py" - <<'PY' 2>/dev/null || echo "hard:python3-gobject (PyGObject)"
import sys
try:
    import gi
except ImportError:
    print("hard:python3-gobject (PyGObject)"); sys.exit(0)
for ns, ver, what in (("Gtk", "4.0", "GTK 4"), ("Gst", "1.0", "GStreamer"),
                      ("GstApp", "1.0", "GStreamer base plugins (GstApp)"),
                      ("GstVideo", "1.0", "GStreamer base plugins (GstVideo)")):
    try:
        gi.require_version(ns, ver)
        __import__("gi.repository." + ns)
    except (ValueError, ImportError):
        print(f"hard:{what}")
        if ns == "Gst":
            sys.exit(0)
try:
    from gi.repository import Gst
    Gst.init(None)
except Exception:
    sys.exit(0)
find = Gst.ElementFactory.find
for el in ("playbin", "appsink", "videoconvert", "videoscale", "audioconvert"):
    if find(el) is None:
        print(f"hard:GStreamer element {el} (gst-plugins-base)")
if find("qtdemux") is None:
    print("soft:GStreamer good plugins (MP4/MOV demuxer)")
if find("avdec_h264") is None or find("avdec_h265") is None:
    print("soft:GStreamer libav plugin (H.264/H.265/ProRes preview)")
if find("pulsesink") is None and find("pipewiresink") is None and find("alsasink") is None:
    print("soft:GStreamer audio output")
PY
}

# ---------------------------------------------------------------- installing
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    elif command -v doas >/dev/null 2>&1; then doas "$@"
    elif command -v pkexec >/dev/null 2>&1; then pkexec "$@"
    else
        say "Enter the root password to install packages"
        # shellcheck disable=SC2145
        su -c "$*"
    fi
}

is_immutable() {
    [ -e /run/ostree-booted ] && return 0
    command -v steamos-readonly >/dev/null 2>&1 && return 0
    [ -f /etc/os-release ] && grep -qiE '^ID=(steamos|bazzite|nixos)' /etc/os-release && return 0
    return 1
}

install_deps() {
    if is_immutable; then
        die "this system has a read-only /usr (Silverblue/Kinoite/SteamOS/NixOS...).
       Use the Flatpak (see packaging/flatpak) or run this script inside a distrobox:
         distrobox create -n simple-cuts -i fedora:latest && distrobox enter simple-cuts -- $SELF"
    fi
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing dependencies with apt"
        as_root apt-get update
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
            gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
            gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
            gstreamer1.0-libav gstreamer1.0-pulseaudio ffmpeg
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        pm=$(command -v dnf >/dev/null 2>&1 && echo dnf || echo yum)
        say "Installing dependencies with $pm"
        base="python3 python3-gobject gtk4 gstreamer1 gstreamer1-plugins-base
              gstreamer1-plugins-good gstreamer1-plugins-bad-free"
        # shellcheck disable=SC2086
        as_root "$pm" install -y $base
        # Stock Fedora/RHEL only ship a patent-free ffmpeg that cannot decode H.264/HEVC
        # camera footage. RPM Fusion (the standard community repo) has the full builds.
        if ! rpm -q rpmfusion-free-release >/dev/null 2>&1 && \
           [ "${SIMPLE_CUTS_NO_RPMFUSION:-}" != 1 ]; then
            say "Enabling RPM Fusion (free) for H.264/H.265 camera codecs" \
                "(set SIMPLE_CUTS_NO_RPMFUSION=1 to skip)"
            if [ -f /etc/fedora-release ]; then
                as_root "$pm" install -y \
                    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" || true
            else    # RHEL, Alma, Rocky, CentOS Stream: RPM Fusion needs EPEL
                as_root "$pm" install -y epel-release || true
                as_root "$pm" install -y \
                    "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm" || true
            fi
        fi
        if rpm -q rpmfusion-free-release >/dev/null 2>&1; then
            if rpm -q ffmpeg-free >/dev/null 2>&1; then
                as_root "$pm" swap -y ffmpeg-free ffmpeg --allowerasing
            fi
            as_root "$pm" install -y --allowerasing ffmpeg gstreamer1-libav
        else
            as_root "$pm" install -y ffmpeg-free gstreamer1-plugin-libav gstreamer1-plugin-openh264 || true
            command -v ffmpeg >/dev/null 2>&1 || as_root "$pm" install -y ffmpeg-free
            warn "using Fedora's codec-limited ffmpeg-free: H.264/H.265 camera footage may not
         preview or re-encode. Enable RPM Fusion (https://rpmfusion.org) for full support."
        fi
    elif command -v zypper >/dev/null 2>&1; then
        say "Installing dependencies with zypper"
        as_root zypper --non-interactive install --no-recommends \
            python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0 \
            typelib-1_0-Gst-1_0 typelib-1_0-GstApp-1_0 typelib-1_0-GstVideo-1_0 \
            gstreamer gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad \
            ffmpeg
        # openSUSE's own ffmpeg cannot decode H.264/HEVC camera footage; Packman (the
        # standard community multimedia repo) has the full builds.
        if ! zypper repos 2>/dev/null | grep -qi packman && \
           [ "${SIMPLE_CUTS_NO_PACKMAN:-}" != 1 ]; then
            . /etc/os-release
            case "${ID:-}" in
                opensuse-tumbleweed|opensuse-slowroll) dist=openSUSE_Tumbleweed ;;
                *) dist="openSUSE_Leap_${VERSION_ID:-}" ;;
            esac
            say "Adding the Packman repository for H.264/H.265 camera codecs" \
                "(set SIMPLE_CUTS_NO_PACKMAN=1 to skip)"
            as_root zypper --non-interactive addrepo -cfp 90 \
                "https://ftp.gwdg.de/pub/linux/misc/packman/suse/$dist/" packman || true
        fi
        if zypper repos 2>/dev/null | grep -qi packman; then
            as_root zypper --non-interactive --gpg-auto-import-keys refresh packman
            as_root zypper --non-interactive dist-upgrade --from packman --allow-vendor-change
            as_root zypper --non-interactive install --allow-vendor-change gstreamer-plugins-libav
        else
            as_root zypper --non-interactive install gstreamer-plugins-libav || true
            warn "without Packman, H.264/H.265 camera footage may not preview or re-encode"
        fi
    elif command -v pacman >/dev/null 2>&1; then
        say "Installing dependencies with pacman"
        # -Syu: Arch does not support partial upgrades (and a fresh system has no sync db)
        as_root pacman -Syu --needed --noconfirm python python-gobject gtk4 gstreamer \
            gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav ffmpeg
    elif command -v xbps-install >/dev/null 2>&1; then
        say "Installing dependencies with xbps"
        as_root xbps-install -Sy python3 python3-gobject gtk4 gstreamer1 gst-plugins-base1 \
            gst-plugins-good1 gst-plugins-bad1 gst-libav ffmpeg
    elif command -v apk >/dev/null 2>&1; then
        say "Installing dependencies with apk"
        as_root apk add python3 py3-gobject3 gtk4.0 gstreamer gst-plugins-base \
            gst-plugins-good gst-plugins-bad gst-libav ffmpeg
    elif command -v emerge >/dev/null 2>&1; then
        say "Installing dependencies with emerge (needs USE=introspection)"
        as_root emerge --noreplace dev-python/pygobject gui-libs/gtk:4 \
            media-libs/gst-plugins-base media-libs/gst-plugins-good \
            media-libs/gst-plugins-bad media-plugins/gst-plugins-libav media-video/ffmpeg
    else
        die "unknown package manager. Install these yourself, then re-run:
       Python 3, PyGObject, GTK 4, GStreamer (base, good, bad, libav) and ffmpeg.
       Or use the Flatpak in packaging/flatpak."
    fi
}

ensure_deps() {
    missing=$(check_deps)
    [ -z "$missing" ] && return 0
    say "Missing: $(echo "$missing" | sed 's/^[a-z]*://' | paste -sd, - | sed 's/,/, /g')"
    install_deps
    missing=$(check_deps)
    hard=$(echo "$missing" | grep '^hard:' | sed 's/^hard://' || true)
    soft=$(echo "$missing" | grep '^soft:' | sed 's/^soft://' || true)
    [ -n "$hard" ] && die "still missing after install: $(echo "$hard" | paste -sd, -)"
    [ -n "$soft" ] && warn "optional parts still missing (preview may not play some codecs): $(echo "$soft" | paste -sd, -)"
    return 0
}

# ---------------------------------------------------------------- per-user install
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="$DATA_DIR/$APP/app"
BIN="$HOME/.local/bin/$APP"
DESKTOP="$DATA_DIR/applications/io.github.simplecuts.SimpleCuts.desktop"
ICON="$DATA_DIR/icons/hicolor/scalable/apps/io.github.simplecuts.SimpleCuts.svg"
MARKER="Simple Cuts — all-in-one launcher"

write_desktop() {
    mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
    cp "$INSTALL_DIR/simple_cuts/data/io.github.simplecuts.SimpleCuts.svg" "$ICON"
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Simple Cuts
GenericName=Video Cutter
Comment=Trim camera footage and strip audio before editing
Exec="$BIN" %F
Icon=io.github.simplecuts.SimpleCuts
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;
MimeType=video/mp4;video/quicktime;video/x-matroska;video/mp2t;video/x-msvideo;video/webm;
Keywords=video;cut;trim;footage;camera;
StartupWMClass=io.github.simplecuts.SimpleCuts
EOF
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null
    return 0
}

install_user() {
    src=$(find_app_dir)
    if [ "$(readlink -f "$src")" != "$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo -)" ]; then
        rm -rf "$INSTALL_DIR.new"
        mkdir -p "$INSTALL_DIR.new"
        cp -R "$src/simple_cuts" "$INSTALL_DIR.new/"
        find "$INSTALL_DIR.new" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
        rm -rf "$INSTALL_DIR.old"
        if [ -d "$INSTALL_DIR" ]; then mv "$INSTALL_DIR" "$INSTALL_DIR.old"; fi
        mv "$INSTALL_DIR.new" "$INSTALL_DIR"
        rm -rf "$INSTALL_DIR.old"
    fi
    # The installed launcher is this script minus any embedded payload, pointed at INSTALL_DIR.
    mkdir -p "$(dirname "$BIN")"
    sed -e '/^__PAYLOAD__$/,$d' \
        -e "s|^INSTALLED_APP_DIR=\".*\"\$|INSTALLED_APP_DIR=\"$INSTALL_DIR\"|" \
        "$SELF" > "$BIN.tmp"
    chmod 755 "$BIN.tmp"
    mv "$BIN.tmp" "$BIN"
    write_desktop
    say "Installed Simple Cuts $VERSION — it is in your application menu"
    say "App: $INSTALL_DIR   Launcher: $BIN"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) say "Tip: add ~/.local/bin to your PATH to run 'simple-cuts' from a terminal" ;;
    esac
}

uninstall_user() {
    rm -rf "$INSTALL_DIR" "$INSTALL_DIR.new" "$INSTALL_DIR.old" \
        "${XDG_CACHE_HOME:-$HOME/.cache}/$APP"
    rmdir "$DATA_DIR/$APP" 2>/dev/null || true
    if [ -f "$BIN" ] && grep -q "$MARKER" "$BIN"; then rm -f "$BIN"; fi
    rm -f "$DESKTOP" "$ICON"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null
    say "Removed Simple Cuts (settings in ${XDG_CONFIG_HOME:-$HOME/.config}/$APP were kept)"
}

# ---------------------------------------------------------------- main
case "${1:-}" in
    -h|--help)
        sed -n '2,17p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --check)
        missing=$(check_deps)
        if [ -z "$missing" ]; then say "All dependencies are installed"; exit 0; fi
        echo "$missing" | sed 's/^hard:/missing:  /; s/^soft:/optional: /'; exit 1 ;;
    --install-deps)
        ensure_deps; say "Dependencies are installed"; exit 0 ;;
    --install|--desktop)
        ensure_deps; install_user; exit 0 ;;
    --uninstall)
        uninstall_user; exit 0 ;;
    --version)
        echo "$APP $VERSION"; exit 0 ;;
esac

ensure_deps
APPDIR=$(find_app_dir)
PY=$(find_python)
PYTHONPATH="$APPDIR${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m simple_cuts "$@"
