"""Timeline widget: scrub, see keyframes, see/adjust kept segments (GTK4 + cairo)."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GObject, Gtk  # noqa: E402

from .media import Clip, format_time  # noqa: E402

EDGE_GRAB_PX = 6
MARGIN = 10
TOP = 22
BOTTOM_PAD = 8

KEEP = (0.27, 0.75, 0.51)        # segment green
KEEP_SEL = (0.43, 0.90, 0.67)
PENDING = (1.0, 0.78, 0.24)      # pending in-point
PLAYHEAD = (0.92, 0.27, 0.27)


class Timeline(Gtk.DrawingArea):
    __gsignals__ = {
        "seek-requested": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "segments-edited": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "segment-clicked": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        super().__init__()
        self.set_content_height(80)
        self.set_hexpand(True)
        self.add_css_class("timeline")
        self.clip: Clip | None = None
        self.position = 0.0
        self.pending_in: float | None = None
        self.selected = -1
        self.show_snap = True
        self._drag: tuple[str, int] | None = None
        self._drag_x0 = 0.0
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    # ---- state -----------------------------------------------------------
    def set_clip(self, clip: Clip | None) -> None:
        self.clip, self.position, self.pending_in, self.selected = clip, 0.0, None, -1
        self.queue_draw()

    def set_position(self, t: float) -> None:
        self.position = t
        self.queue_draw()

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    @property
    def duration(self) -> float:
        return self.clip.info.duration if self.clip else 0.0

    # ---- geometry --------------------------------------------------------
    def _bar(self) -> tuple[float, float, float, float]:
        w, h = self.get_width(), self.get_height()
        return MARGIN, TOP, max(1.0, w - 2 * MARGIN), max(1.0, h - TOP - BOTTOM_PAD)

    def _x(self, t: float) -> float:
        x, _, w, _ = self._bar()
        return x + (t / self.duration) * w if self.duration else x

    def _t(self, px: float) -> float:
        x, _, w, _ = self._bar()
        if not self.duration:
            return 0.0
        return min(max((px - x) / w, 0.0), 1.0) * self.duration

    def _edge_at(self, px: float) -> tuple[str, int] | None:
        for i, seg in enumerate(self.clip.segments if self.clip else []):
            if abs(self._x(seg.start) - px) <= EDGE_GRAB_PX:
                return ("start", i)
            if abs(self._x(seg.end) - px) <= EDGE_GRAB_PX:
                return ("end", i)
        return None

    def _segment_at(self, px: float) -> int:
        t = self._t(px)
        for i, seg in enumerate(self.clip.segments if self.clip else []):
            if seg.start <= t <= seg.end:
                return i
        return -1

    # ---- drawing ---------------------------------------------------------
    def _draw(self, _area, cr, width, height) -> None:
        fg = self.get_color()                    # theme foreground (light or dark)
        fr, fgc, fb = fg.red, fg.green, fg.blue
        bx, by, bw, bh = self._bar()

        # bar background: a translucent wash of the theme foreground colour
        cr.set_source_rgba(fr, fgc, fb, 0.10)
        _rounded(cr, bx, by, bw, bh, 4)
        cr.fill()

        layout = self.create_pango_layout("")
        if not self.clip:
            layout.set_text("Open a clip to begin", -1)
            _, logical = layout.get_pixel_extents()
            cr.set_source_rgba(fr, fgc, fb, 0.55)
            cr.move_to(bx + (bw - logical.width) / 2, by + (bh - logical.height) / 2)
            _show_layout(cr, layout)
            return

        # ruler
        attrs_small = _small_font(self)
        layout.set_font_description(attrs_small)
        step = _nice_step(self.duration, bw)
        cr.set_line_width(1)
        t = 0.0
        while t <= self.duration + 1e-6:
            x = math.floor(self._x(t)) + 0.5
            cr.set_source_rgba(fr, fgc, fb, 0.55)
            cr.move_to(x, by - 4)
            cr.line_to(x, by)
            cr.stroke()
            label = format_time(t)[3:8] if self.duration < 3600 else format_time(t)[:8]
            layout.set_text(label, -1)
            cr.set_source_rgba(fr, fgc, fb, 0.75)
            cr.move_to(x + 2, 3)
            _show_layout(cr, layout)
            t += step

        # keyframe ticks
        kfs = self.clip.keyframes or []
        if kfs and bw / max(1, len(kfs)) >= 3:
            cr.set_source_rgba(fr, fgc, fb, 0.35)
            for k in kfs:
                x = math.floor(self._x(k)) + 0.5
                cr.move_to(x, by + bh - 6)
                cr.line_to(x, by + bh)
            cr.stroke()

        # segments
        for i, seg in enumerate(self.clip.segments):
            x0, x1 = self._x(seg.start), self._x(seg.end)
            color = KEEP_SEL if i == self.selected else KEEP
            if self.show_snap and kfs:
                xs = self._x(self.clip.snapped_start(seg.start))
                if x0 - xs >= 1:
                    _hatch(cr, xs, by, x0 - xs, bh, (*KEEP, 0.55))
            cr.set_source_rgba(*color, 0.55)
            cr.rectangle(x0, by, max(2.0, x1 - x0), bh)
            cr.fill()
            cr.set_source_rgba(*color, 1.0)
            cr.set_line_width(2)
            for x in (x0, x1):
                cr.move_to(x, by)
                cr.line_to(x, by + bh)
            cr.stroke()
            if x1 - x0 > 18:
                layout.set_text(str(i + 1), -1)
                _, lg = layout.get_pixel_extents()
                cr.set_source_rgba(fr, fgc, fb, 0.95)
                cr.move_to(x0 + (x1 - x0 - lg.width) / 2, by + (bh - lg.height) / 2)
                _show_layout(cr, layout)

        # pending in-point
        if self.pending_in is not None:
            x = self._x(self.pending_in)
            xp = self._x(self.position)
            cr.set_source_rgba(*PENDING, 0.22)
            cr.rectangle(x, by, max(0.0, xp - x), bh)
            cr.fill()
            cr.set_source_rgba(*PENDING, 1.0)
            cr.set_line_width(2)
            cr.move_to(x, by)
            cr.line_to(x, by + bh)
            cr.stroke()

        # playhead
        x = self._x(self.position)
        cr.set_source_rgba(*PLAYHEAD, 1.0)
        cr.set_line_width(2)
        cr.move_to(x, by - 5)
        cr.line_to(x, by + bh + 4)
        cr.stroke()
        cr.move_to(x - 5, by - 7)
        cr.line_to(x + 5, by - 7)
        cr.line_to(x, by - 1)
        cr.close_path()
        cr.fill()

    # ---- input -----------------------------------------------------------
    def _on_motion(self, _ctl, x, _y) -> None:
        if self._drag is None:
            self.set_cursor(Gdk.Cursor.new_from_name("ew-resize" if self.clip and
                                                     self._edge_at(x) else "default", None))

    def _on_drag_begin(self, _g, x, _y) -> None:
        if not self.clip:
            return
        self._drag_x0 = x
        edge = self._edge_at(x)
        if edge:
            self._drag = edge
            self.selected = edge[1]
            self.emit("segment-clicked", edge[1])
        else:
            self._drag = ("seek", -1)
            idx = self._segment_at(x)
            if idx >= 0:
                self.selected = idx
                self.emit("segment-clicked", idx)
            self.emit("seek-requested", self._t(x))
        self.queue_draw()

    def _on_drag_update(self, _g, dx, _dy) -> None:
        if not self._drag or not self.clip:
            return
        kind, idx = self._drag
        t = self._t(self._drag_x0 + dx)
        if kind == "seek":
            self.emit("seek-requested", t)
            return
        seg = self.clip.segments[idx]
        min_len = self.clip.info.frame_duration
        if kind == "start":
            seg.start = min(t, seg.end - min_len)
        else:
            seg.end = max(t, seg.start + min_len)
        self.emit("seek-requested", t)
        self.emit("segments-edited")
        self.queue_draw()

    def _on_drag_end(self, _g, _dx, _dy) -> None:
        if self._drag and self._drag[0] in ("start", "end") and self.clip:
            sel = self.clip.segments[self._drag[1]]
            self.clip.segments.sort(key=lambda s: s.start)
            self.selected = self.clip.segments.index(sel)
            self._drag = None
            self.emit("segments-edited")
        self._drag = None
        self.queue_draw()


def _show_layout(cr, layout) -> None:
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import PangoCairo
    PangoCairo.show_layout(cr, layout)


def _small_font(widget):
    desc = widget.get_pango_context().get_font_description().copy()
    size = desc.get_size()
    if size > 0:
        desc.set_size(int(size * 0.85))
    return desc


def _rounded(cr, x, y, w, h, r) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _hatch(cr, x, y, w, h, rgba) -> None:
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    cr.set_source_rgba(*rgba)
    cr.set_line_width(1)
    i = x - h
    while i < x + w:
        cr.move_to(i, y + h)
        cr.line_to(i + h, y)
        i += 6
    cr.stroke()
    cr.restore()


def _nice_step(duration: float, width: float) -> float:
    if duration <= 0 or width <= 0:
        return 1.0
    target = duration / max(1.0, width / 90.0)
    for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if s >= target:
            return float(s)
    return 3600.0
