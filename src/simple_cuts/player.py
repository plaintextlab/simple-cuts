"""GStreamer preview player that renders into a Gtk.Picture.

Uses ``gtk4paintablesink`` when installed (zero-copy), otherwise an ``appsink`` that
hands scaled RGBA frames to GTK as textures — that works with only gst-plugins-base.
Seeks are always ACCURATE so frame stepping lands on the requested frame.
"""

from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gst, Gtk  # noqa: E402

Gst.init(None)

PREVIEW_MAX_W, PREVIEW_MAX_H = 1920, 1080


class Player(GObject.Object):
    __gsignals__ = {
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),   # playing?
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, picture: Gtk.Picture):
        super().__init__()
        self.picture = picture
        self.playbin = Gst.ElementFactory.make("playbin", None)
        if self.playbin is None:
            raise RuntimeError("GStreamer 'playbin' is missing (install gst-plugins-base)")
        self._appsink = None
        self._frame = None          # latest sample waiting for the main loop
        self._frame_pending = False
        self._setup_video_sink()

        self.playbin.set_property("volume", 1.0)
        # Run on the system clock: PulseAudio-on-PipeWire's sink clock can take ~1.5 s to
        # start ticking, which freezes the picture after pressing play. The audio sink
        # simply follows the system clock instead.
        self.playbin.use_clock(Gst.SystemClock.obtain())
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

        self.duration = 0.0
        self.playing = False
        self.rate = 1.0
        self._seek_busy = False      # a seek is in flight (wait for ASYNC_DONE)
        self._seek_target: float | None = None
        self._last_pos = 0.0
        self._tick = GLib.timeout_add(40, self._poll_position)

    # ---- sink setup ------------------------------------------------------
    def _setup_video_sink(self) -> None:
        paintable_sink = Gst.ElementFactory.make("gtk4paintablesink", None)
        if paintable_sink is not None:
            paintable = paintable_sink.get_property("paintable")
            self.picture.set_paintable(paintable)
            glsink = Gst.ElementFactory.make("glsinkbin", None)
            if glsink is not None and paintable.props.gl_context is not None:
                glsink.set_property("sink", paintable_sink)
                self.playbin.set_property("video-sink", glsink)
            else:
                self.playbin.set_property("video-sink", paintable_sink)
            return

        caps = (f"video/x-raw,format=RGBA,pixel-aspect-ratio=1/1,"
                f"width=[1,{PREVIEW_MAX_W}],height=[1,{PREVIEW_MAX_H}]")
        bin_desc = (f"videoconvert n-threads=0 ! videoscale n-threads=0 ! "
                    f"capsfilter caps=\"{caps}\" ! "
                    f"appsink name=sink emit-signals=true max-buffers=2 drop=true sync=true")
        vbin = Gst.parse_bin_from_description(bin_desc, True)
        self._appsink = vbin.get_by_name("sink")
        self._appsink.connect("new-sample", self._on_sample)
        self._appsink.connect("new-preroll", self._on_preroll)
        self.playbin.set_property("video-sink", vbin)

    def _on_preroll(self, sink):
        return self._queue_sample(sink.emit("pull-preroll"))

    def _on_sample(self, sink):
        return self._queue_sample(sink.emit("pull-sample"))

    def _queue_sample(self, sample):
        # Runs on a streaming thread: just stash the newest frame for the main loop.
        if sample is not None:
            self._frame = sample
            if not self._frame_pending:
                self._frame_pending = True
                GLib.idle_add(self._show_frame, priority=GLib.PRIORITY_HIGH_IDLE)
        return Gst.FlowReturn.OK

    def _show_frame(self):
        self._frame_pending = False
        sample, self._frame = self._frame, None
        if sample is None:
            return False
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            data = GLib.Bytes.new(bytes(info.data))   # copy out before unmapping
            size = info.size
        finally:
            buf.unmap(info)
        stride = size // h if h else w * 4
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, data, stride)
        self.picture.set_paintable(tex)
        return False

    # ---- control ---------------------------------------------------------
    def load(self, path: str, duration: float) -> None:
        self.playbin.set_state(Gst.State.NULL)
        self.playing = False
        self.rate = 1.0
        self.duration = duration
        self._seek_busy, self._seek_target, self._last_pos = False, None, 0.0
        if self._appsink is not None:
            self.picture.set_paintable(None)      # don't show the previous clip's frame
        self.playbin.set_property("uri", Gst.filename_to_uri(path))
        self.playbin.set_state(Gst.State.PAUSED)   # shows the first frame
        self.emit("state-changed", False)

    def unload(self) -> None:
        self.playbin.set_state(Gst.State.NULL)
        self.playing = False
        if self._appsink is not None:
            self.picture.set_paintable(None)
        self.emit("state-changed", False)

    def play(self, rate: float = 1.0) -> None:
        if rate != self.rate:
            self.rate = rate
            self._do_seek(self.position(), rate)
        self.playbin.set_state(Gst.State.PLAYING)
        self.playing = True
        self.emit("state-changed", True)

    def pause(self) -> None:
        self.playbin.set_state(Gst.State.PAUSED)
        self.playing = False
        if self.rate != 1.0:
            self.rate = 1.0
            self._do_seek(self.position(), 1.0)
        self.emit("state-changed", False)

    def set_muted(self, muted: bool) -> None:
        self.playbin.set_property("mute", muted)

    def position(self) -> float:
        if self._seek_target is not None:
            return self._seek_target
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        if ok and pos >= 0:
            self._last_pos = pos / Gst.SECOND
        return self._last_pos

    def seek(self, t: float) -> None:
        """Accurate seek; rapid calls (dragging) are coalesced to the latest target."""
        t = max(0.0, min(t, self.duration or t))
        self._seek_target = t
        self._last_pos = t
        if not self._seek_busy:
            self._do_seek(t, self.rate)

    def _do_seek(self, t: float, rate: float) -> None:
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        ns = int(t * Gst.SECOND)
        ok = self.playbin.seek(rate, Gst.Format.TIME, flags, Gst.SeekType.SET, ns,
                               Gst.SeekType.NONE, -1)
        if ok:
            self._seek_busy = True
            if self._seek_target == t:
                self._seek_target = None
                self._last_pos = t
        # If the pipeline isn't ready yet, the target stays pending and is retried.

    def _poll_position(self):
        if self.playing and not self._seek_busy and self._seek_target is None:
            self.emit("position-changed", self.position())
        return True

    def _on_message(self, _bus, msg) -> None:
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self._seek_busy = False
            if self._seek_target is not None:          # a newer target arrived meanwhile
                self._do_seek(self._seek_target, self.rate)
            else:
                self.emit("position-changed", self.position())
        elif t == Gst.MessageType.EOS:
            self.playbin.set_state(Gst.State.PAUSED)
            self.playing = False
            self.emit("state-changed", False)
        elif t == Gst.MessageType.WARNING:
            err, _debug = msg.parse_warning()
            if "decoder" in err.message.lower():       # missing codec plugin
                self.emit("error", err.message)
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self.emit("error", f"{err.message}")
            self.playing = False
            self.emit("state-changed", False)

    def shutdown(self) -> None:
        GLib.source_remove(self._tick)
        self.playbin.set_state(Gst.State.NULL)
