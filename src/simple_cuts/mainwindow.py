"""Main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QObject, QRunnable, QSettings, Qt, QThread, QThreadPool, QTimer,
                            QUrl, Signal, Slot)
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .export import CancelledError, ExportSettings, Exporter, Mode
from .media import Clip, MediaError, format_time, keyframe_times, probe
from .timeline import Timeline

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".mts", ".m2ts", ".ts", ".mxf", ".avi",
              ".webm", ".3gp", ".insv", ".lrv"}

HELP_TEXT = (
    "Space play/pause · J/K/L back/pause/forward · ←/→ frame · Shift+←/→ 1 s · "
    "I in · O out · Esc clear in · Del delete segment · PgUp/PgDn clip · Ctrl+E export"
)


# ---------------------------------------------------------------- background
class _KeyframeSignals(QObject):
    done = Signal(object, object)      # clip, list[float] | str (error)


class KeyframeTask(QRunnable):
    def __init__(self, clip: Clip):
        super().__init__()
        self.clip = clip
        self.signals = _KeyframeSignals()

    def run(self) -> None:
        try:
            result: object = keyframe_times(self.clip.path, self.clip.info.start_time)
        except Exception as exc:  # noqa: BLE001 - reported in UI
            result = str(exc)
        self.signals.done.emit(self.clip, result)


class ExportWorker(QObject):
    progress = Signal(float, str)
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, clips: list[Clip], settings: ExportSettings):
        super().__init__()
        self.clips, self.settings = clips, settings
        self.exporter = Exporter()

    @Slot()
    def run(self) -> None:
        try:
            outs = self.exporter.run(self.clips, self.settings,
                                     lambda f, s: self.progress.emit(f, s))
            self.finished.emit(outs)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


# ---------------------------------------------------------------- window
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Simple Cuts")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self.settings = QSettings("simple-cuts", "simple-cuts")
        self.clips: list[Clip] = []
        self.current: Clip | None = None
        self.exported: set[Path] = set()
        self._rate = 1.0
        self._output_dir_custom = False
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        self._pending_seek: int | None = None
        self._kf_tasks: set[KeyframeTask] = set()

        self._build_player()
        self._build_ui()
        self._build_shortcuts()
        self._load_settings()
        self._refresh_all()

    # ---- construction ----------------------------------------------------
    def _build_player(self) -> None:
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setMinimumSize(480, 270)
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(lambda _s: self._update_play_button())
        self.player.errorOccurred.connect(
            lambda _e, msg: self.statusBar().showMessage(f"Playback error: {msg}", 8000))
        self.player.mediaStatusChanged.connect(self._on_media_status)

    def _build_ui(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for text, slot, key in (("Open files…", self.open_files, QKeySequence.StandardKey.Open),
                                ("Open folder…", self.open_folder, "Ctrl+Shift+O"),
                                ("Remove clip", self.remove_clip, None)):
            act = QAction(text, self)
            if key:
                act.setShortcut(key)
            act.triggered.connect(slot)
            tb.addAction(act)

        # Left: clips
        self.clip_list = QListWidget()
        self.clip_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clip_list.currentRowChanged.connect(self._on_clip_selected)
        self.clip_list.setMinimumWidth(210)
        left = _titled("Clips", self.clip_list)

        # Centre: video + timeline + transport
        self.timeline = Timeline()
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.segmentsEdited.connect(self._refresh_segments)
        self.timeline.segmentClicked.connect(self._select_segment_row)

        self.play_btn = _btn("Play", self.toggle_play)
        self.time_label = QLabel("00:00:00:00 / 00:00:00:00")
        self.time_label.setStyleSheet("font-family: monospace; font-size: 13px;")
        self.mute_box = QCheckBox("Mute preview")
        self.mute_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_box.toggled.connect(self.audio.setMuted)
        transport = QHBoxLayout()
        for w in (_btn("|◀", lambda: self.seek(0)),
                  _btn("◀ 1s", lambda: self.nudge(-1.0)),
                  _btn("◀ frame", lambda: self.step(-1)),
                  self.play_btn,
                  _btn("frame ▶", lambda: self.step(1)),
                  _btn("1s ▶", lambda: self.nudge(1.0)),
                  _btn("▶|", lambda: self.seek(self.timeline.duration))):
            transport.addWidget(w)
        transport.addSpacing(12)
        transport.addWidget(self.time_label)
        transport.addStretch()
        transport.addWidget(_btn("Mark In  [I]", self.mark_in))
        transport.addWidget(_btn("Mark Out  [O]", self.mark_out))
        transport.addSpacing(12)
        transport.addWidget(self.mute_box)

        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.video, 1)
        cl.addWidget(self.timeline)
        cl.addLayout(transport)
        hint = QLabel(HELP_TEXT)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        cl.addWidget(hint)

        # Right: segments
        self.seg_table = QTableWidget(0, 4)
        self.seg_table.setHorizontalHeaderLabels(["#", "In", "Out", "Length"])
        self.seg_table.verticalHeader().hide()
        self.seg_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.seg_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.seg_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.seg_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.seg_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        self.seg_table.itemSelectionChanged.connect(self._on_segment_row)
        self.seg_table.cellDoubleClicked.connect(self._on_segment_double)
        seg_buttons = QHBoxLayout()
        seg_buttons.addWidget(_btn("Delete segment", self.delete_segment))
        seg_buttons.addWidget(_btn("Clear all", self.clear_segments))
        right_w = QWidget()
        rl = QVBoxLayout(right_w)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.seg_table)
        rl.addLayout(seg_buttons)
        right_w.setMinimumWidth(330)
        right = _titled("Segments to keep", right_w)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(centre)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 800, 280])

        # Bottom: export
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Output folder")
        self.out_edit.textEdited.connect(lambda _t: setattr(self, "_output_dir_custom", True))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Lossless (fast, starts on keyframe)", Mode.LOSSLESS)
        self.mode_combo.addItem("Frame-accurate (re-encode)", Mode.ACCURATE)
        self.mode_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.remove_audio_box = QCheckBox("Remove audio")
        self.join_box = QCheckBox("Join segments per clip")
        self.gpu_box = QCheckBox("Use GPU (NVENC)")
        for box in (self.remove_audio_box, self.join_box, self.gpu_box):
            box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.export_btn = _btn("Export all", self.export)
        self.export_btn.setStyleSheet("font-weight: bold; padding: 4px 14px;")
        self.cancel_btn = _btn("Cancel", self.cancel_export)
        self.cancel_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel("")

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Output:"))
        row1.addWidget(self.out_edit, 1)
        row1.addWidget(_btn("Browse…", self.choose_output))
        row1.addSpacing(12)
        row1.addWidget(self.mode_combo)
        row1.addWidget(self.remove_audio_box)
        row1.addWidget(self.join_box)
        row1.addWidget(self.gpu_box)
        row2 = QHBoxLayout()
        row2.addWidget(self.progress, 1)
        row2.addWidget(self.progress_label)
        row2.addWidget(self.cancel_btn)
        row2.addWidget(self.export_btn)
        bottom = QFrame()
        bottom.setFrameShape(QFrame.Shape.StyledPanel)
        bl = QVBoxLayout(bottom)
        bl.addLayout(row1)
        bl.addLayout(row2)

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addWidget(split, 1)
        lay.addWidget(bottom)
        self.setCentralWidget(root)
        self.info_label = QLabel("")
        self.statusBar().addPermanentWidget(self.info_label)

    def _build_shortcuts(self) -> None:
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(fn)
        sc(Qt.Key.Key_Space, self.toggle_play)
        sc(Qt.Key.Key_K, self.pause)
        sc(Qt.Key.Key_L, self.play_faster)
        sc(Qt.Key.Key_J, lambda: self.nudge(-1.0))
        sc(Qt.Key.Key_Left, lambda: self.step(-1))
        sc(Qt.Key.Key_Right, lambda: self.step(1))
        sc("Shift+Left", lambda: self.nudge(-1.0))
        sc("Shift+Right", lambda: self.nudge(1.0))
        sc("Ctrl+Left", lambda: self.nudge(-10.0))
        sc("Ctrl+Right", lambda: self.nudge(10.0))
        sc(Qt.Key.Key_Home, lambda: self.seek(0))
        sc(Qt.Key.Key_End, lambda: self.seek(self.timeline.duration))
        sc(Qt.Key.Key_I, self.mark_in)
        sc(Qt.Key.Key_O, self.mark_out)
        sc(Qt.Key.Key_Escape, self.clear_in)
        sc(Qt.Key.Key_Delete, self.delete_segment)
        sc(Qt.Key.Key_Backspace, self.delete_segment)
        sc(Qt.Key.Key_PageUp, lambda: self._move_clip(-1))
        sc(Qt.Key.Key_PageDown, lambda: self._move_clip(1))
        sc(Qt.Key.Key_M, lambda: self.mute_box.toggle())
        sc("Ctrl+E", self.export)

    # ---- settings --------------------------------------------------------
    def _load_settings(self) -> None:
        s = self.settings
        self.remove_audio_box.setChecked(s.value("remove_audio", True, type=bool))
        self.join_box.setChecked(s.value("join", False, type=bool))
        self.gpu_box.setChecked(s.value("gpu", True, type=bool))
        self.mute_box.setChecked(s.value("mute", False, type=bool))
        self.mode_combo.setCurrentIndex(1 if s.value("mode", "lossless") == "accurate" else 0)
        self._on_mode_changed()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("remove_audio", self.remove_audio_box.isChecked())
        s.setValue("join", self.join_box.isChecked())
        s.setValue("gpu", self.gpu_box.isChecked())
        s.setValue("mute", self.mute_box.isChecked())
        s.setValue("mode", self._mode().value)
        s.setValue("last_dir", self.settings.value("last_dir", ""))

    # ---- opening clips ---------------------------------------------------
    def open_files(self) -> None:
        exts = " ".join(f"*{e} *{e.upper()}" for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Open clips", self.settings.value("last_dir", str(Path.home())),
            f"Video files ({exts});;All files (*)")
        self.add_paths([Path(f) for f in files])

    def open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Open folder of clips", self.settings.value("last_dir", str(Path.home())))
        if folder:
            self.add_paths([Path(folder)])

    def add_paths(self, paths: list[Path]) -> None:
        files: list[Path] = []
        for p in paths:
            if p.is_dir():
                files += sorted(f for f in p.rglob("*")
                                if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                                and not f.name.startswith("."))
            elif p.is_file():
                files.append(p)
        known = {c.path.resolve() for c in self.clips}
        errors = []
        added = 0
        for f in files:
            if f.resolve() in known:
                continue
            try:
                clip = Clip(probe(f))
            except MediaError as exc:
                errors.append(str(exc))
                continue
            known.add(f.resolve())
            self.clips.append(clip)
            added += 1
            task = KeyframeTask(clip)
            task.setAutoDelete(False)       # we keep it alive until its signal arrives
            self._kf_tasks.add(task)
            task.signals.done.connect(self._on_keyframes)
            QThreadPool.globalInstance().start(task)
        if files:
            self.settings.setValue("last_dir", str(files[0].parent))
        if added and not self._output_dir_custom and not self.out_edit.text():
            self.out_edit.setText(str(files[0].parent / "cuts"))
        self._refresh_clip_list()
        if added and self.current is None:
            self.clip_list.setCurrentRow(len(self.clips) - added)
        if errors:
            QMessageBox.warning(self, "Some files could not be opened", "\n".join(errors[:20]))

    @Slot(object, object)
    def _on_keyframes(self, clip: Clip, result) -> None:
        self._kf_tasks = {t for t in self._kf_tasks if t.clip is not clip}
        if isinstance(result, list):
            clip.keyframes = result
        else:
            self.statusBar().showMessage(f"Keyframe scan failed for {clip.path.name}: {result}",
                                         8000)
        if clip is self.current:
            self.timeline.update()
            self._update_info()

    def remove_clip(self) -> None:
        if not self.current:
            return
        idx = self.clips.index(self.current)
        self.player.stop()
        self.player.setSource(QUrl())
        self.clips.pop(idx)
        self.current = None
        self._refresh_clip_list()
        if self.clips:
            self.clip_list.setCurrentRow(min(idx, len(self.clips) - 1))
        else:
            self._load_clip(None)

    def _on_clip_selected(self, row: int) -> None:
        clip = self.clips[row] if 0 <= row < len(self.clips) else None
        if clip is not self.current:
            self._load_clip(clip)

    def _load_clip(self, clip: Clip | None) -> None:
        self.current = clip
        self._rate = 1.0
        self.player.setPlaybackRate(1.0)
        self.timeline.set_clip(clip)
        if clip:
            self.player.setSource(QUrl.fromLocalFile(str(clip.path)))
            self.player.pause()   # show first frame
        else:
            self.player.setSource(QUrl())
        self._refresh_segments()
        self._update_info()
        self._update_time()

    def _move_clip(self, delta: int) -> None:
        if self.clips:
            row = max(0, min(len(self.clips) - 1, self.clip_list.currentRow() + delta))
            self.clip_list.setCurrentRow(row)

    # ---- transport -------------------------------------------------------
    def toggle_play(self) -> None:
        if not self.current:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self._rate = 1.0
            self.player.setPlaybackRate(1.0)
            self.player.play()

    def pause(self) -> None:
        self.player.pause()
        self._rate = 1.0
        self.player.setPlaybackRate(1.0)

    def play_faster(self) -> None:
        if not self.current:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._rate = min(8.0, self._rate * 2)
        else:
            self._rate = 1.0
        self.player.setPlaybackRate(self._rate)
        self.player.play()
        self.statusBar().showMessage(f"Speed {self._rate:g}×", 1500)

    def seek(self, t: float) -> None:
        if not self.current:
            return
        t = max(0.0, min(t, self.current.info.duration))
        ms = int(round(t * 1000))
        self.timeline.set_position(t)
        self._update_time(t)
        # Coalesce rapid seeks (dragging) so the decoder isn't flooded.
        if self._pending_seek is None:
            QTimer.singleShot(0, self._flush_seek)
        self._pending_seek = ms

    def _flush_seek(self) -> None:
        if self._pending_seek is not None:
            self.player.setPosition(self._pending_seek)
            self._pending_seek = None

    def nudge(self, seconds: float) -> None:
        self.seek(self._pos() + seconds)

    def step(self, frames: int) -> None:
        if not self.current:
            return
        self.player.pause()
        fd = self.current.info.frame_duration
        # Snap to the frame grid, then move by whole frames.
        frame = round(self._pos() / fd) + frames
        self.seek(frame * fd + fd * 0.25)

    def _pos(self) -> float:
        if self._pending_seek is not None:
            return self._pending_seek / 1000.0
        return self.player.position() / 1000.0

    def _on_position(self, ms: int) -> None:
        if self._pending_seek is None and not self.timeline._drag:
            t = ms / 1000.0
            self.timeline.set_position(t)
            self._update_time(t)

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.player.pause()

    def _update_play_button(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("Pause" if playing else "Play")

    def _update_time(self, t: float | None = None) -> None:
        if not self.current:
            self.time_label.setText("--:--:--:-- / --:--:--:--")
            return
        fps = self.current.info.fps
        t = self._pos() if t is None else t
        self.time_label.setText(f"{format_time(t, fps)} / "
                                f"{format_time(self.current.info.duration, fps)}")

    # ---- marking ---------------------------------------------------------
    def mark_in(self) -> None:
        if not self.current:
            return
        self.timeline.pending_in = self._pos()
        self.timeline.update()
        self.statusBar().showMessage(
            f"In point at {format_time(self._pos(), self.current.info.fps)} — press O at the end",
            4000)

    def mark_out(self) -> None:
        clip = self.current
        if not clip:
            return
        out = self._pos()
        start = self.timeline.pending_in
        if start is None:
            # No in-point: start right after the previous segment (or at 0).
            start = max((s.end for s in clip.segments if s.end <= out), default=0.0)
        if out - start < clip.info.frame_duration:
            self.statusBar().showMessage("Out point must be after the in point", 4000)
            return
        seg = clip.add_segment(start, out)
        self.timeline.pending_in = None
        self.timeline.selected = clip.segments.index(seg)
        self._refresh_segments()
        self._refresh_clip_list()

    def clear_in(self) -> None:
        self.timeline.pending_in = None
        self.timeline.update()

    def delete_segment(self) -> None:
        clip = self.current
        idx = self.timeline.selected
        if clip and 0 <= idx < len(clip.segments):
            clip.segments.pop(idx)
            self.timeline.selected = min(idx, len(clip.segments) - 1)
            self._refresh_segments()
            self._refresh_clip_list()

    def clear_segments(self) -> None:
        if self.current and self.current.segments:
            self.current.segments.clear()
            self.timeline.selected = -1
            self._refresh_segments()
            self._refresh_clip_list()

    def _select_segment_row(self, idx: int) -> None:
        self.seg_table.blockSignals(True)
        self.seg_table.selectRow(idx)
        self.seg_table.blockSignals(False)

    def _on_segment_row(self) -> None:
        rows = self.seg_table.selectionModel().selectedRows()
        if rows:
            self.timeline.selected = rows[0].row()
            self.timeline.update()

    def _on_segment_double(self, row: int, col: int) -> None:
        if self.current and 0 <= row < len(self.current.segments):
            seg = self.current.segments[row]
            self.seek(seg.end if col == 2 else seg.start)

    # ---- refresh ---------------------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_clip_list()
        self._refresh_segments()
        self._update_info()

    def _refresh_clip_list(self) -> None:
        self.clip_list.blockSignals(True)
        cur = self.clips.index(self.current) if self.current in self.clips else -1
        self.clip_list.clear()
        for c in self.clips:
            n = len(c.segments)
            mark = "✓ " if c.path in self.exported else ""
            item = QListWidgetItem(f"{mark}{c.path.name}\n   {format_time(c.info.duration)[:8]}"
                                   f"  ·  {n} segment{'s' if n != 1 else ''}")
            item.setToolTip(str(c.path))
            self.clip_list.addItem(item)
        self.clip_list.setCurrentRow(cur)
        self.clip_list.blockSignals(False)
        total = sum(len(c.segments) for c in self.clips)
        self.export_btn.setEnabled(total > 0 and self._export_thread is None)
        self.export_btn.setText(f"Export all ({total})" if total else "Export all")

    def _refresh_segments(self) -> None:
        clip = self.current
        segs = clip.segments if clip else []
        fps = clip.info.fps if clip else None
        self.seg_table.blockSignals(True)
        self.seg_table.setRowCount(len(segs))
        for i, s in enumerate(segs):
            for col, text in enumerate((str(i + 1), format_time(s.start, fps),
                                        format_time(s.end, fps), format_time(s.duration)[3:])):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.seg_table.setItem(i, col, item)
        if 0 <= self.timeline.selected < len(segs):
            self.seg_table.selectRow(self.timeline.selected)
        self.seg_table.blockSignals(False)
        self.timeline.update()
        self._refresh_clip_list_counts()

    def _refresh_clip_list_counts(self) -> None:
        total = sum(len(c.segments) for c in self.clips)
        self.export_btn.setText(f"Export all ({total})" if total else "Export all")
        self.export_btn.setEnabled(total > 0 and self._export_thread is None)

    def _update_info(self) -> None:
        c = self.current
        if not c:
            self.info_label.setText("")
            return
        i = c.info
        kf = ""
        if c.keyframes is None:
            kf = " · scanning keyframes…"
        elif len(c.keyframes) > 1:
            gap = i.duration / len(c.keyframes)
            kf = f" · keyframe every ~{gap:.2f}s"
        audio = "audio" if i.has_audio else "no audio"
        fps = f"{i.fps:.3f}".rstrip("0").rstrip(".")
        self.info_label.setText(f"{i.video_codec} {i.width}×{i.height} {fps} fps "
                                f"{i.pix_fmt} · {audio}{kf}")

    def _mode(self) -> Mode:
        return Mode(self.mode_combo.currentData())   # Qt hands str-enums back as str

    def _on_mode_changed(self) -> None:
        lossless = self._mode() is Mode.LOSSLESS
        self.timeline.show_snap = lossless
        self.gpu_box.setEnabled(not lossless)
        self.timeline.update()

    # ---- export ----------------------------------------------------------
    def choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder",
                                                  self.out_edit.text() or str(Path.home()))
        if folder:
            self.out_edit.setText(folder)
            self._output_dir_custom = True

    def export(self) -> None:
        if self._export_thread is not None:
            return
        clips = [c for c in self.clips if c.segments]
        if not clips:
            QMessageBox.information(self, "Nothing to export",
                                    "Mark at least one segment with I and O first.")
            return
        out_text = self.out_edit.text().strip()
        if not out_text:
            self.choose_output()
            out_text = self.out_edit.text().strip()
            if not out_text:
                return
        out_dir = Path(out_text).expanduser()
        settings = ExportSettings(
            output_dir=out_dir,
            mode=self._mode(),
            remove_audio=self.remove_audio_box.isChecked(),
            join_segments=self.join_box.isChecked(),
            use_gpu=self.gpu_box.isChecked(),
        )
        self._save_settings()

        self.player.pause()
        self._export_clips = clips
        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(clips, settings)
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.cancelled.connect(self._on_export_cancelled)
        self.export_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self._export_thread.start()

    def cancel_export(self) -> None:
        if self._export_worker:
            self._export_worker.exporter.cancel()
            self.progress_label.setText("Cancelling…")

    def _on_export_progress(self, frac: float, text: str) -> None:
        self.progress.setValue(int(frac * 1000))
        self.progress_label.setText(text)

    def _end_export(self) -> None:
        if self._export_thread:
            self._export_thread.quit()
            self._export_thread.wait()
        self._export_thread = None
        self._export_worker = None
        self.cancel_btn.setEnabled(False)
        self._refresh_clip_list()

    def _on_export_finished(self, outputs: list) -> None:
        self.exported |= {c.path for c in self._export_clips}
        self._end_export()
        self.progress.setValue(1000)
        self.progress_label.setText(f"Exported {len(outputs)} file(s)")
        out_dir = Path(self.out_edit.text()).expanduser()
        self.statusBar().showMessage(f"Exported {len(outputs)} file(s) to {out_dir}", 10000)

    def _on_export_failed(self, msg: str) -> None:
        self._end_export()
        self.progress.setValue(0)
        self.progress_label.setText("Export failed")
        QMessageBox.critical(self, "Export failed", msg)

    def _on_export_cancelled(self) -> None:
        self._end_export()
        self.progress.setValue(0)
        self.progress_label.setText("Cancelled")

    # ---- window events ---------------------------------------------------
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        self.add_paths([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    def closeEvent(self, e) -> None:
        if self._export_thread is not None:
            if QMessageBox.question(self, "Export running",
                                    "An export is still running. Cancel it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.cancel_export()
            self._end_export()
        pending = [c for c in self.clips if c.segments and c.path not in self.exported]
        if pending and QMessageBox.question(
                self, "Quit?",
                f"{len(pending)} clip(s) have marked segments that were not exported. Quit anyway?"
        ) != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self._save_settings()
        self.player.stop()
        e.accept()


def _btn(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.clicked.connect(slot)
    return b


def _titled(title: str, widget: QWidget) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    label = QLabel(title)
    label.setStyleSheet("font-weight: bold; padding: 2px;")
    lay.addWidget(label)
    lay.addWidget(widget)
    return w
