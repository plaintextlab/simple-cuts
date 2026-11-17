"""Thin wrappers around ffprobe for reading clip information."""

from __future__ import annotations

import bisect
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


class MediaError(Exception):
    """Raised when ffprobe/ffmpeg cannot read or write a file."""


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path and sys.platform == "darwin":
        # Apps started from Finder get a minimal PATH without Homebrew/MacPorts.
        extra = os.pathsep.join(("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"))
        path = shutil.which(name, path=extra)
    if not path:
        raise MediaError(f"'{name}' was not found on PATH. Install ffmpeg first.")
    return path


@dataclass
class MediaInfo:
    path: Path
    duration: float
    fps: float
    width: int
    height: int
    video_codec: str
    pix_fmt: str
    has_audio: bool
    format_name: str = ""
    start_time: float = 0.0

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1.0 / 30.0

    @property
    def is_10bit(self) -> bool:
        return any(tag in self.pix_fmt for tag in ("10", "12", "p016"))


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Clip:
    info: MediaInfo
    segments: list[Segment] = field(default_factory=list)
    keyframes: list[float] | None = None  # filled in lazily (background)

    @property
    def path(self) -> Path:
        return self.info.path

    def add_segment(self, start: float, end: float) -> Segment:
        start, end = sorted((max(0.0, start), min(self.info.duration, end)))
        seg = Segment(start, end)
        self.segments.append(seg)
        self.segments.sort(key=lambda s: s.start)
        return seg

    def snapped_start(self, t: float) -> float:
        """Last keyframe at or before ``t`` (where a stream copy really starts)."""
        return snap_to_keyframe(self.keyframes, t)


def snap_to_keyframe(keyframes: list[float] | None, t: float) -> float:
    if not keyframes:
        return t
    i = bisect.bisect_right(keyframes, t + 1e-6)
    return keyframes[i - 1] if i > 0 else keyframes[0]


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    cmd = [
        find_tool("ffprobe"), "-v", "error",
        "-show_entries",
        "format=duration,format_name,start_time:"
        "stream=index,codec_type,codec_name,width,height,pix_fmt,"
        "avg_frame_rate,r_frame_rate,duration",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"Cannot read {path.name}: {proc.stderr.strip() or 'ffprobe failed'}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("codec_name") not in ("mjpeg", "png")), None)
    if video is None:
        raise MediaError(f"{path.name} has no video stream")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return MediaInfo(
        path=path,
        duration=duration,
        fps=fps or 30.0,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_codec=video.get("codec_name", "?"),
        pix_fmt=video.get("pix_fmt", ""),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        format_name=fmt.get("format_name", ""),
        start_time=float(fmt.get("start_time") or 0.0),
    )


def keyframe_times(path: str | Path, start_time: float = 0.0) -> list[float]:
    """Keyframe times relative to the file start, read from packet flags (no decoding).

    Times are relative to the container ``start_time`` because that is the zero
    point used by both ffmpeg's ``-ss`` and the preview player.
    """
    cmd = [
        find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(proc.stderr.strip() or "ffprobe failed")
    times = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            times.append(float(parts[0]))
    return sorted(max(0.0, round(t - start_time, 6)) for t in times)


def format_time(seconds: float, fps: float | None = None) -> str:
    """HH:MM:SS.mmm, or HH:MM:SS:FF when fps is given."""
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if fps:
        frame = int(round((seconds - int(seconds)) * fps))
        if frame >= round(fps):
            frame = int(round(fps)) - 1
        return f"{h:02d}:{m:02d}:{s:02d}:{frame:02d}"
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
