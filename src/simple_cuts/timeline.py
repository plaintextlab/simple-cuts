"""Timeline widget: scrub, see keyframes, see/adjust kept segments."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .media import Clip, format_time

EDGE_GRAB_PX = 6
MARGIN = 10


class Timeline(QWidget):
    seekRequested = Signal(float)        # seconds
    segmentsEdited = Signal()            # a segment edge was dragged
    segmentClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.clip: Clip | None = None
        self.position = 0.0
        self.pending_in: float | None = None
        self.selected = -1
        self.show_snap = True            # draw where lossless cuts really start
        self._drag: tuple[str, int] | None = None   # ("seek", -1) | ("start"/"end", idx)

    # ---- state -----------------------------------------------------------
    def set_clip(self, clip: Clip | None) -> None:
        self.clip, self.position, self.pending_in, self.selected = clip, 0.0, None, -1
        self.update()

    def set_position(self, t: float) -> None:
        self.position = t
        self.update()

    @property
    def duration(self) -> float:
        return self.clip.info.duration if self.clip else 0.0

    # ---- geometry --------------------------------------------------------
    def _bar(self) -> QRectF:
        return QRectF(MARGIN, 22, self.width() - 2 * MARGIN, self.height() - 34)

    def _x(self, t: float) -> float:
        bar = self._bar()
        return bar.left() + (t / self.duration) * bar.width() if self.duration else bar.left()

    def _t(self, x: float) -> float:
        bar = self._bar()
        if not self.duration or bar.width() <= 0:
            return 0.0
        return min(max((x - bar.left()) / bar.width(), 0.0), 1.0) * self.duration

    def _edge_at(self, x: float) -> tuple[str, int] | None:
        if not self.clip:
            return None
        for i, seg in enumerate(self.clip.segments):
            if abs(self._x(seg.start) - x) <= EDGE_GRAB_PX:
                return ("start", i)
            if abs(self._x(seg.end) - x) <= EDGE_GRAB_PX:
                return ("end", i)
        return None

    def _segment_at(self, x: float) -> int:
        t = self._t(x)
        for i, seg in enumerate(self.clip.segments if self.clip else []):
            if seg.start <= t <= seg.end:
                return i
        return -1

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        bar = self._bar()
        p.fillRect(self.rect(), pal.window())
        p.fillRect(bar, QColor(40, 40, 46))

        if not self.clip:
            p.setPen(pal.color(pal.ColorRole.PlaceholderText))
            p.drawText(bar, Qt.AlignmentFlag.AlignCenter, "Open a clip to begin")
            return

        # Ruler
        p.setPen(pal.color(pal.ColorRole.WindowText))
        font = p.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        p.setFont(font)
        step = _nice_step(self.duration, bar.width())
        t = 0.0
        while t <= self.duration + 1e-6:
            x = self._x(t)
            p.drawLine(QPointF(x, bar.top() - 4), QPointF(x, bar.top()))
            p.drawText(QPointF(x + 2, bar.top() - 6), format_time(t)[3:8] if self.duration < 3600
                       else format_time(t)[:8])
            t += step

        # Keyframe ticks (only when they're far enough apart to be meaningful)
        kfs = self.clip.keyframes or []
        if kfs and bar.width() / max(1, len(kfs)) >= 3:
            p.setPen(QPen(QColor(120, 120, 135), 1))
            for k in kfs:
                x = self._x(k)
                p.drawLine(QPointF(x, bar.bottom() - 6), QPointF(x, bar.bottom()))

        # Segments
        for i, seg in enumerate(self.clip.segments):
            x0, x1 = self._x(seg.start), self._x(seg.end)
            if self.show_snap and kfs:
                xs = self._x(self.clip.snapped_start(seg.start))
                if x0 - xs >= 1:
                    p.fillRect(QRectF(xs, bar.top(), x0 - xs, bar.height()),
                               QBrush(QColor(80, 170, 120, 110), Qt.BrushStyle.BDiagPattern))
            color = QColor(70, 190, 130) if i != self.selected else QColor(110, 230, 170)
            p.fillRect(QRectF(x0, bar.top(), max(2.0, x1 - x0), bar.height()),
                       QColor(color.red(), color.green(), color.blue(), 150))
            p.setPen(QPen(color, 2))
            p.drawLine(QPointF(x0, bar.top()), QPointF(x0, bar.bottom()))
            p.drawLine(QPointF(x1, bar.top()), QPointF(x1, bar.bottom()))
            p.setPen(Qt.GlobalColor.white)
            if x1 - x0 > 18:
                p.drawText(QRectF(x0, bar.top(), x1 - x0, bar.height()),
                           Qt.AlignmentFlag.AlignCenter, str(i + 1))

        # Pending in-point
        if self.pending_in is not None:
            x = self._x(self.pending_in)
            p.setPen(QPen(QColor(255, 200, 60), 2))
            p.drawLine(QPointF(x, bar.top()), QPointF(x, bar.bottom()))
            p.fillRect(QRectF(x, bar.top(), max(0.0, self._x(self.position) - x), bar.height()),
                       QColor(255, 200, 60, 50))

        # Playhead
        x = self._x(self.position)
        p.setPen(QPen(QColor(235, 70, 70), 2))
        p.drawLine(QPointF(x, bar.top() - 4), QPointF(x, bar.bottom() + 4))

    # ---- mouse -----------------------------------------------------------
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if not self.clip or e.button() != Qt.MouseButton.LeftButton:
            return
        x = e.position().x()
        edge = self._edge_at(x)
        if edge:
            self._drag = edge
            self.selected = edge[1]
            self.segmentClicked.emit(edge[1])
        else:
            self._drag = ("seek", -1)
            idx = self._segment_at(x)
            if idx >= 0:
                self.selected = idx
                self.segmentClicked.emit(idx)
            self.seekRequested.emit(self._t(x))
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        x = e.position().x()
        if self._drag is None:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._edge_at(x)
                           else Qt.CursorShape.ArrowCursor)
            return
        kind, idx = self._drag
        t = self._t(x)
        if kind == "seek":
            self.seekRequested.emit(t)
            return
        seg = self.clip.segments[idx]
        min_len = self.clip.info.frame_duration
        if kind == "start":
            seg.start = min(t, seg.end - min_len)
        else:
            seg.end = max(t, seg.start + min_len)
        self.seekRequested.emit(t)
        self.segmentsEdited.emit()
        self.update()

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        if self._drag and self._drag[0] in ("start", "end") and self.clip:
            sel = self.clip.segments[self._drag[1]]
            self.clip.segments.sort(key=lambda s: s.start)
            self.selected = self.clip.segments.index(sel)
            self.segmentsEdited.emit()
        self._drag = None
        self.update()


def _nice_step(duration: float, width: float) -> float:
    if duration <= 0 or width <= 0:
        return 1.0
    target = duration / max(1.0, width / 90.0)
    for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if s >= target:
            return float(s)
    return 3600.0
