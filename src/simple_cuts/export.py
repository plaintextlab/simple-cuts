"""Builds and runs the ffmpeg commands that cut clips."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .media import Clip, MediaError, MediaInfo, find_tool, keyframe_times, snap_to_keyframe


class Mode(str, Enum):
    LOSSLESS = "lossless"   # stream copy, starts on the keyframe at/before the in-point
    ACCURATE = "accurate"   # re-encode, frame-exact


# Containers we keep as-is. Anything else is written as .mp4 / .mkv.
KEEP_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".mts", ".m2ts", ".ts", ".mxf"}


@dataclass
class ExportSettings:
    output_dir: Path
    mode: Mode = Mode.LOSSLESS
    remove_audio: bool = True
    join_segments: bool = False
    use_gpu: bool = True


@dataclass
class Job:
    """One ffmpeg invocation that produces one output file."""
    src: Path
    info: MediaInfo
    start: float          # where the output actually starts (snapped in lossless mode)
    end: float
    out: Path

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@lru_cache(maxsize=None)
def encoder_available(encoder: str) -> bool:
    """True if ffmpeg can actually open this encoder (e.g. NVENC needs a working driver)."""
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
           "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
           "-c:v", encoder, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def output_extension(src: Path, mode: Mode) -> str:
    ext = src.suffix.lower()
    if ext in KEEP_EXTENSIONS:
        return ext
    return ".mkv" if mode is Mode.LOSSLESS else ".mp4"


def unique_path(path: Path, reserved: set[Path]) -> Path:
    candidate, n = path, 1
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    reserved.add(candidate)
    return candidate


def video_codec_args(info: MediaInfo, use_gpu: bool, gpu_compatible: bool = False
                     ) -> list[str]:
    """High-quality intermediate encode; 10-bit sources stay 10-bit (HEVC).

    ``gpu_compatible`` makes the CPU encoder produce the same codec/pixel format
    NVENC would (4:2:0), so CPU-fallback parts can still be joined with GPU parts.
    """
    ten_bit = info.is_10bit
    if use_gpu:
        enc = "hevc_nvenc" if ten_bit else "h264_nvenc"
        if encoder_available(enc):
            args = ["-c:v", enc, "-preset", "p5", "-tune", "hq",
                    "-rc", "vbr", "-cq", "16", "-b:v", "0"]
            args += ["-pix_fmt", "p010le", "-profile:v", "main10"] if ten_bit \
                else ["-pix_fmt", "yuv420p"]
            return args
    keep_422 = "422" in info.pix_fmt and not gpu_compatible
    if ten_bit:
        pix = "yuv422p10le" if keep_422 else "yuv420p10le"
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "16", "-pix_fmt", pix]
    pix = "yuv422p" if keep_422 else "yuv420p"
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", pix]


def build_command(job: Job, settings: ExportSettings, cpu_fallback: bool = False
                  ) -> list[str]:
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-progress", "pipe:1", "-nostats"]
    if settings.mode is Mode.LOSSLESS:
        # A hair past the keyframe so float rounding can't make ffmpeg pick the previous one.
        cmd += ["-ss", f"{job.start + 0.001:.6f}", "-i", str(job.src),
                "-t", f"{job.duration:.6f}"]
    else:
        cmd += ["-ss", f"{job.start:.6f}", "-i", str(job.src), "-t", f"{job.duration:.6f}"]

    cmd += ["-map", "0:v:0"]
    if not settings.remove_audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-map_metadata", "0", "-dn", "-sn"]

    if settings.mode is Mode.LOSSLESS:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        cmd += video_codec_args(job.info, settings.use_gpu and not cpu_fallback,
                                gpu_compatible=settings.use_gpu)
        if not settings.remove_audio:
            cmd += ["-c:a", "copy"]
    if settings.remove_audio:
        cmd += ["-an"]
    if job.out.suffix in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(job.out))
    return cmd


def plan_jobs(clips: list[Clip], settings: ExportSettings
              ) -> list[tuple[Clip, list[Job], Path | None]]:
    """For each clip: its jobs, plus the joined output path (or None)."""
    reserved: set[Path] = set()
    plan = []
    for clip in clips:
        if not clip.segments:
            continue
        ext = output_extension(clip.path, settings.mode)
        stem = clip.path.stem
        join = settings.join_segments and len(clip.segments) > 1
        if settings.mode is Mode.LOSSLESS and clip.keyframes is None:
            clip.keyframes = keyframe_times(clip.path, clip.info.start_time)
        tmp_dir = Path(tempfile.mkdtemp(prefix=".simplecuts-", dir=settings.output_dir)) \
            if join else None
        jobs = []
        for i, seg in enumerate(clip.segments, 1):
            start = seg.start
            if settings.mode is Mode.LOSSLESS:
                start = snap_to_keyframe(clip.keyframes, seg.start)
            if tmp_dir is not None:
                out = tmp_dir / f"part{i:02d}{ext}"
            else:
                out = unique_path(settings.output_dir / f"{stem}_seg{i:02d}{ext}", reserved)
            jobs.append(Job(clip.path, clip.info, start, seg.end, out))
        joined = None
        if join:
            joined = unique_path(settings.output_dir / f"{stem}_cut{ext}", reserved)
        plan.append((clip, jobs, joined))
    return plan


ProgressFn = Callable[[float, str], None]   # (fraction 0..1, status text)


class Exporter:
    """Runs a plan sequentially. Safe to call ``cancel()`` from another thread."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self, cmd: list[str], on_time: Callable[[float], None]) -> None:
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, bufsize=1)
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                on_time(max(0, int(value)) / 1e6)
        stderr = self._proc.stderr.read() if self._proc.stderr else ""
        code = self._proc.wait()
        self._proc = None
        if self._cancelled:
            raise CancelledError()
        if code != 0:
            raise MediaError(stderr.strip().splitlines()[-1] if stderr.strip()
                             else f"ffmpeg exited with code {code}")

    def run(self, clips: list[Clip], settings: ExportSettings,
            progress: ProgressFn | None = None) -> list[Path]:
        progress = progress or (lambda f, s: None)
        self._cancelled = False
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        plan = plan_jobs(clips, settings)
        total = sum(job.duration for _, jobs, _ in plan for job in jobs) or 1.0
        done = 0.0
        outputs: list[Path] = []
        n_jobs = sum(len(jobs) for _, jobs, _ in plan)
        k = 0
        gpu_failed = False
        try:
            for clip, jobs, joined in plan:
                for job in jobs:
                    k += 1
                    label = f"[{k}/{n_jobs}] {clip.path.name}"
                    progress(done / total, label)
                    on_time = (lambda t, d=done, j=job, lb=label:
                               progress((d + min(t, j.duration)) / total, lb))
                    try:
                        self._run(build_command(job, settings, cpu_fallback=gpu_failed),
                                  on_time)
                    except MediaError:
                        if not (settings.mode is Mode.ACCURATE and settings.use_gpu
                                and not gpu_failed):
                            raise
                        # NVENC can fail when the GPU is busy/out of memory (e.g. an
                        # editor is open). Redo this and later jobs on the CPU.
                        gpu_failed = True
                        progress(done / total, f"{label} (GPU busy, using CPU)")
                        self._run(build_command(job, settings, cpu_fallback=True), on_time)
                    done += job.duration
                    if joined is None:
                        outputs.append(job.out)
                if joined is not None:
                    progress(done / total, f"Joining {clip.path.name}")
                    self._concat([j.out for j in jobs], joined)
                    outputs.append(joined)
        except BaseException:
            for _, jobs, joined in plan:   # don't leave half-written files behind
                for job in jobs:
                    if job.out not in outputs:
                        _remove(job.out)
                if joined is not None and joined not in outputs:
                    _remove(joined)
            raise
        finally:
            for _, jobs, joined in plan:
                if joined is not None:
                    for job in jobs:
                        _remove(job.out)
                        _rmdir(job.out.parent)
        progress(1.0, "Done")
        return outputs

    def _concat(self, parts: list[Path], out: Path) -> None:
        list_file = parts[0].parent / "list.txt"
        list_file.write_text("".join(f"file '{p.name}'\n" for p in parts))
        try:
            self._run([find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error",
                       "-progress", "pipe:1", "-nostats",
                       "-f", "concat", "-safe", "0", "-i", str(list_file),
                       "-map", "0", "-c", "copy", str(out)], lambda t: None)
        finally:
            _remove(list_file)


class CancelledError(Exception):
    pass


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _rmdir(p: Path) -> None:
    if p.name.startswith(".simplecuts-"):
        try:
            os.rmdir(p)
        except OSError:
            pass
