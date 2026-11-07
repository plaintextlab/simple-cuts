# simple-cuts

Simply cut the unnecessary parts of the raw footages before importing to your video editor.

Open your camera clips, mark the parts you want to keep, and export them —
with the audio stripped (optional) — ready to import into your real editor.

## Features

- Open files, a whole folder (recursively, e.g. a camera card), or drag & drop
- Preview with frame stepping, 1 s / 10 s jumps and J/K/L shuttle
- Mark any number of segments per clip with **I** / **O**; drag segment edges on the timeline
- Two export modes:
  - **Lossless** (default) – stream copy, no quality loss, takes seconds. Each segment
    starts on the keyframe at or before your in-point (shown hatched on the timeline)
    and may run a few frames past the out-point. Never cuts footage short.
  - **Frame-accurate** – re-encodes at near-lossless quality (CRF/CQ 16) so cuts land on
    the exact frame. Uses the NVIDIA GPU (NVENC) when available, otherwise CPU
    (x264/x265), and falls back to CPU automatically if the GPU is busy.
    10-bit sources stay 10-bit (HEVC).
- Remove audio (on by default), optionally join a clip's segments into one file
- Batch export of all clips with progress and cancel
- Keeps the original container (`.MP4` → `.mp4`, `.MOV` → `.mov`, …)
- Never overwrites: outputs are named `C0001_seg01.mp4` (or `C0001_cut.mp4` when
  joined) and get a `_1`, `_2`… suffix if the name already exists

## Requirements (Linux)

- Python 3.11+
- ffmpeg / ffprobe on your `PATH`
- [uv](https://docs.astral.sh/uv/) (installs the Python dependencies)
- The Qt X11 cursor library

On Debian / Ubuntu:

```bash
sudo apt install ffmpeg libxcb-cursor0
```

## Run

```bash
uv run simple-cuts
```

You can also pass files or folders: `uv run simple-cuts /media/card/PRIVATE/M4ROOT/CLIP`.

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
| Ctrl + E | Export all |

## Development

```bash
uv run pytest
```

Layout: `media.py` (ffprobe), `export.py` (ffmpeg commands, no Qt — fully tested),
`timeline.py` (timeline widget), `mainwindow.py` (UI), `app.py` (entry point).

### Notes

- Qt 6.10+ defaults to a native PipeWire audio backend that crashes on some systems;
  the app sets `QT_AUDIO_BACKEND=pulseaudio` on Linux (works with pipewire-pulse).
  Set the variable yourself to override.
- Windows / macOS support is planned; the code avoids Linux-specific APIs.
