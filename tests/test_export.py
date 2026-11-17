import json
import subprocess
import sys
from pathlib import Path

import pytest

from simple_cuts.export import (ExportSettings, Exporter, Mode, encoder_available,
                                plan_jobs)
from simple_cuts.media import Clip, format_time, keyframe_times, probe, snap_to_keyframe

FPS = 30


def make_clip(path: Path, *, ten_bit: bool = False, seconds: int = 10) -> Path:
    """Synthetic camera-like clip: 30 fps, keyframe every 1 s, stereo audio."""
    codec = (["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
              "-x265-params", f"keyint={FPS}:min-keyint={FPS}:scenecut=0:log-level=error"]
             if ten_bit else
             ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", str(FPS),
              "-keyint_min", str(FPS), "-sc_threshold", "0"])
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s=640x360:r={FPS}:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
         *codec, "-c:a", "aac", "-shortest", str(path)],
        check=True)
    return path


def streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_packets", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,nb_read_packets:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def video_frames(path: Path) -> int:
    v = [s for s in streams(path)["streams"] if s["codec_type"] == "video"][0]
    return int(v["nb_read_packets"])


def has_audio(path: Path) -> bool:
    return any(s["codec_type"] == "audio" for s in streams(path)["streams"])


@pytest.fixture(scope="session")
def sample(tmp_path_factory) -> Path:
    return make_clip(tmp_path_factory.mktemp("src") / "C0001.MP4")


@pytest.fixture
def clip(sample) -> Clip:
    info = probe(sample)
    return Clip(info, keyframes=keyframe_times(sample, info.start_time))


def run(clip, tmp_path, **kw):
    settings = ExportSettings(output_dir=tmp_path / "out", **kw)
    return Exporter().run([clip], settings)


def test_probe(sample):
    info = probe(sample)
    assert info.fps == pytest.approx(30)
    assert (info.width, info.height) == (640, 360)
    assert info.duration == pytest.approx(10, abs=0.1)
    assert info.has_audio and info.video_codec == "h264" and not info.is_10bit


def test_keyframes(clip):
    assert clip.keyframes == pytest.approx([float(i) for i in range(10)], abs=0.05)
    assert snap_to_keyframe(clip.keyframes, 2.5) == pytest.approx(2.0, abs=0.05)
    assert snap_to_keyframe(clip.keyframes, 3.0) == pytest.approx(3.0, abs=0.05)


def test_lossless_snaps_to_keyframe_and_strips_audio(clip, tmp_path):
    clip.add_segment(2.5, 5.0)
    [out] = run(clip, tmp_path)
    assert out.name == "C0001_seg01.mp4"
    assert not has_audio(out)
    # starts at keyframe 2.0 -> at least 3.0 s == 90 frames. Stream copy may run a
    # few frames past the out-point (B-frame packets) but never cuts footage short.
    assert 90 <= video_frames(out) <= 96
    assert streams(out)["streams"][0]["codec_name"] == "h264"


def test_keep_audio(clip, tmp_path):
    clip.add_segment(1.0, 3.0)
    [out] = run(clip, tmp_path, remove_audio=False)
    assert has_audio(out)


@pytest.mark.parametrize("gpu", [False, True])
def test_accurate_is_frame_exact(clip, tmp_path, gpu):
    gpu_enc = "h264_videotoolbox" if sys.platform == "darwin" else "h264_nvenc"
    if gpu and not encoder_available(gpu_enc):
        pytest.skip(f"{gpu_enc} not available")
    clip.add_segment(2.5, 5.0)
    [out] = run(clip, tmp_path, mode=Mode.ACCURATE, use_gpu=gpu)
    assert abs(video_frames(out) - 75) <= 1
    assert not has_audio(out)


def test_ten_bit_stays_ten_bit(tmp_path):
    src = make_clip(tmp_path / "C0002.MOV", ten_bit=True, seconds=4)
    info = probe(src)
    assert info.is_10bit
    clip = Clip(info, keyframes=keyframe_times(src, info.start_time))
    clip.add_segment(0.5, 2.0)
    [out] = run(clip, tmp_path, mode=Mode.ACCURATE)
    assert out.suffix == ".mov"
    assert "10" in streams(out)["streams"][0]["pix_fmt"]


def test_multiple_segments_and_join(clip, tmp_path):
    clip.add_segment(6.0, 8.0)
    clip.add_segment(1.0, 2.0)
    outs = run(clip, tmp_path)
    assert [o.name for o in outs] == ["C0001_seg01.mp4", "C0001_seg02.mp4"]

    [joined] = run(clip, tmp_path / "j", join_segments=True)
    assert joined.name == "C0001_cut.mp4"
    assert 90 <= video_frames(joined) <= 100
    # temp parts were cleaned up
    assert sorted(p.name for p in joined.parent.iterdir()) == ["C0001_cut.mp4"]


def test_never_overwrites(clip, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "C0001_seg01.mp4").write_text("precious")
    clip.add_segment(1.0, 2.0)
    [out] = run(clip, tmp_path)
    assert out.name == "C0001_seg01_1.mp4"
    assert (out_dir / "C0001_seg01.mp4").read_text() == "precious"


def test_plan_skips_clips_without_segments(clip, tmp_path):
    assert plan_jobs([clip], ExportSettings(output_dir=tmp_path)) == []


def test_format_time():
    assert format_time(3725.5) == "01:02:05.500"
    assert format_time(1.5, fps=30) == "00:00:01:15"


def test_gpu_failure_falls_back_to_cpu(clip, tmp_path, monkeypatch):
    import simple_cuts.export as ex
    real = ex.video_codec_args

    def fake(info, use_gpu, gpu_compatible=False):
        if use_gpu:   # simulate NVENC failing (busy / out of GPU memory)
            return ["-c:v", "h264_nvenc", "-b:v", "-5"]
        return real(info, False, gpu_compatible)

    monkeypatch.setattr(ex, "video_codec_args", fake)
    clip.add_segment(1.0, 2.0)
    clip.add_segment(4.0, 5.0)
    progress = []
    settings = ExportSettings(output_dir=tmp_path / "out", mode=Mode.ACCURATE,
                              use_gpu=True, join_segments=True)
    [out] = Exporter().run([clip], settings, lambda f, s: progress.append(s))
    assert abs(video_frames(out) - 60) <= 1
    assert any("using CPU" in s for s in progress)


def test_ten_bit_never_silently_downconverted(monkeypatch):
    import simple_cuts.export as ex
    from simple_cuts.media import MediaInfo
    info = MediaInfo(Path("x.mov"), 10, 25, 3840, 2160, "hevc", "yuv422p10le", False)
    # an 8-bit-only libx265 build must not be chosen for 10-bit footage
    monkeypatch.setattr(ex, "encoder_available", lambda e: e in ("libx265", "libx264"))
    monkeypatch.setattr(ex, "encoder_pix_fmts", lambda e: frozenset(
        {"libx265": {"yuv420p", "yuv422p"}, "libx264": {"yuv420p", "yuv422p10le"}}[e]))
    args = ex.video_codec_args(info, use_gpu=False)
    assert args[1] == "libx264" and args[-1] == "yuv422p10le"


def test_lossless_join_never_repeats_footage(clip, tmp_path):
    """Sparse keyframes make snapped segments overlap; joining must merge them."""
    clip.keyframes = [0.0, 5.0]                 # pretend keyframes are 5 s apart
    clip.add_segment(1.0, 2.0)                  # snaps to 0.0-2.0
    clip.add_segment(3.0, 4.0)                  # snaps to 0.0-4.0  (overlaps)
    clip.add_segment(6.0, 7.0)                  # snaps to 5.0-7.0
    from simple_cuts.export import lossless_extra, lossless_ranges
    assert lossless_ranges(clip, merge=True) == [(0.0, 4.0), (5.0, 7.0)]
    assert lossless_extra(clip) == pytest.approx(3.0)
    [out] = run(clip, tmp_path, join_segments=True)
    # 4 s + 2 s = 180 frames (+ a few B-frame tail frames), not 0-2 + 0-4 + 5-7 = 240
    assert 180 <= video_frames(out) <= 190


def test_separate_lossless_files_are_not_merged(clip, tmp_path):
    clip.keyframes = [0.0, 5.0]
    clip.add_segment(1.0, 2.0)
    clip.add_segment(3.0, 4.0)
    outs = run(clip, tmp_path)
    assert len(outs) == 2
