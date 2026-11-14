# simple-cuts

Simply cut the unnecessary parts of the raw footages before importing to your video editor.

Open your camera clips, mark the parts you want to keep, and export them,
with the audio stripped (optional), ready to import into your real editor.
A native GTK 4 app that uses your desktop's GTK theme and follows dark/light mode.

## Install

```bash
./simple-cuts.sh --install
```

That installs Simple Cuts for your user (no root needed for the app itself) and adds it to
your application menu. It updates itself: about once a day it checks this repository's
[GitHub Releases](https://github.com/plaintextlab/simple-cuts/releases) and offers
**Update now** when there's a newer version (turn this off in the ⋮ menu, or use
**Check for Updates** there). `./simple-cuts.sh --uninstall` removes it again; your
settings are kept.

You can also download `simple-cuts-VERSION.sh` from a release and run
`sh simple-cuts-VERSION.sh --install`: that one file contains the whole app.

To just run it without installing: `./simple-cuts.sh`. The script checks what's missing, installs it with your distro's
package manager (it asks for your password via sudo/doas/pkexec/su), and starts the app.
Supported: Debian/Ubuntu/Mint/Pop!_OS (apt), Fedora/RHEL (dnf), openSUSE (zypper),
Arch/Manjaro/EndeavourOS (pacman), Void (xbps), Alpine (apk), Gentoo (emerge).

Fedora/RHEL and openSUSE ship a patent-limited ffmpeg that can't decode H.264/HEVC camera
footage. On those, the script enables the standard community codec repository
(**RPM Fusion** or **Packman**) and installs the full ffmpeg and GStreamer libav. Set
`SIMPLE_CUTS_NO_RPMFUSION=1` / `SIMPLE_CUTS_NO_PACKMAN=1` to skip that step.

Tested in fresh containers of Debian 13, Ubuntu, Fedora, Arch, Void, Alpine and openSUSE
Tumbleweed: dependency install, H.264 and 10-bit 4:2:2 HEVC preview decoding, lossless and
frame-accurate export, the single-file build, and the .deb/.rpm/PKGBUILD packages.

| Option | |
|---|---|
| `./simple-cuts.sh clip.mp4 /media/card` | open files/folders straight away |
| `./simple-cuts.sh --install` | install for this user, with menu entry and updates |
| `./simple-cuts.sh --uninstall` | remove that install (keeps settings) |
| `./simple-cuts.sh --check` | only report missing dependencies |
| `./simple-cuts.sh --install-deps` | only install dependencies |

`packaging/build-run.sh` produces `dist/simple-cuts-VERSION.sh`, a **single
self-contained file** with the app embedded: copy it to any machine and run it.

### Updates for other install types

Copies installed by a package manager, the Flatpak and the single-file script aren't
replaced by the app; they show the new version with a link to the release page, so you
update them the way you installed them. A git checkout never checks automatically.

### Packages

| Format | Build | Install |
|---|---|---|
| Flatpak (any distro) | `packaging/flatpak/build-flatpak.sh` | `flatpak install --user dist/simple-cuts-*.flatpak` |
| .deb (Debian, Ubuntu, …) | `packaging/deb/build-deb.sh` | `sudo apt install ./dist/simple-cuts_*_all.deb` |
| .rpm (Fedora, RHEL) | `packaging/rpm/build-rpm.sh` | `sudo dnf install ./dist/simple-cuts-*.noarch.rpm` |
| Arch (PKGBUILD) | `cd packaging/arch && makepkg -si` | (installs) |

The Flatpak bundles its own ffmpeg (with x264, x265 and NVENC) on the GNOME runtime, so it
also covers immutable systems (Silverblue, Kinoite, SteamOS, Bazzite). It reads your
host's GTK themes and GTK settings read-only, so it looks like the rest of your desktop.

There is no AppImage. An AppImage has to either bundle GTK, which then ignores your
system theme, or use the host's GTK and Python, which breaks on distros that don't ship
them. The launcher script and the Flatpak each handle one of those cases properly.

## Features

- Open files, a whole folder (recursively, e.g. a camera card), or drag & drop
- Preview with frame stepping, 1 s / 10 s jumps and J/K/L shuttle
- Mark any number of segments per clip with **I** / **O**; drag segment edges on the timeline
- Two export modes:
  - **Lossless** (default): stream copy, no quality loss, takes seconds. Each segment
    starts on the keyframe at or before your in-point (shown hatched on the timeline)
    and may run a few frames past the out-point. It never cuts footage short.
  - **Frame-accurate**: re-encodes at near-lossless quality (CRF/CQ 16) so cuts land on
    the exact frame. Uses the NVIDIA GPU (NVENC) when available, otherwise the CPU
    (x264/x265), and falls back to the CPU automatically if the GPU is busy or out of
    memory (e.g. while DaVinci Resolve is open). 10-bit sources stay 10-bit (HEVC).
- Remove audio (on by default), optionally join a clip's segments into one file
- **Export current** exports just the clip in the timeline with all its segments;
  **Export all** exports every clip that has segments. Both show progress and can be cancelled
- Keeps the original container (`.MP4` → `.mp4`, `.MOV` → `.mov`, …)
- Never overwrites: outputs are named `C0001_seg01.mp4` (or `C0001_cut.mp4` when
  joined) and get a `_1`, `_2`… suffix if the name already exists

### Theming

Plain GTK 4 without libadwaita, so any GTK theme applies (Adwaita, adw-gtk3, Arc,
Breeze, …). On top of what GTK already reads (`~/.config/gtk-4.0/settings.ini`,
XSettings, `GTK_THEME`), the app
- follows the freedesktop dark/light preference live (GNOME, KDE, and anything that runs
  `xdg-desktop-portal`),
- falls back to `~/.config/gtk-3.0/settings.ini` when your theme was only set for GTK 3
  (lxappearance / nwg-look on i3, sway, Openbox, …).

## Keyboard shortcuts

| Key | Action |
|---|---|
| Space | Play / pause |
| J / K / L | Back 1 s / pause / play (press L again for 2×, 4×, 8×) |
| ← / → | Previous / next frame |
| Shift + ← / → | Back / forward 1 s |
| Ctrl + ← / → | Back / forward 10 s |
| Home / End | Start / end of clip |
| I | Set in-point |
| O | Set out-point (adds the segment; without an in-point it starts after the previous segment) |
| Esc | Clear pending in-point |
| Delete | Delete selected segment |
| PgUp / PgDn | Previous / next clip |
| M | Mute preview |
| Ctrl + O | Open files |
| Ctrl + E | Export all clips |
| Ctrl + Shift + E | Export the current clip |

## Development

Runtime dependencies are system packages: Python ≥ 3.9, PyGObject, GTK ≥ 4.6, GStreamer
(base, good; bad and libav recommended) and ffmpeg. Run from the checkout with
`./simple-cuts.sh`. The engine tests need only ffmpeg and pytest:

```bash
uv run pytest
```

| File | Role |
|---|---|
| `media.py` | ffprobe: clip info, keyframes |
| `export.py` | ffmpeg commands and export runner (no GUI; fully tested) |
| `player.py` | GStreamer preview with accurate seeking |
| `timeline.py` | timeline widget (cairo) |
| `theme.py` | dark/light + GTK theme following |
| `window.py`, `app.py` | GTK window and application |
| `updater.py` | GitHub Releases check and per-user self-update (fully tested) |
| `simple-cuts.sh` | all-in-one launcher, dependency installer, per-user installer |
| `packaging/` | Flatpak, deb, rpm, PKGBUILD, single-file builder, `release.sh` |

### Releasing

Bump `__version__` in `src/simple_cuts/__init__.py` (a test checks that the launcher,
`pyproject.toml`, PKGBUILD, spec and metainfo agree), commit, then run
`packaging/release.sh`. It runs the tests, builds every package, tags `vX.Y.Z`, pushes,
and creates the GitHub release that installed copies update from.

Windows / macOS support is planned; the code avoids Linux-specific APIs where it can.
