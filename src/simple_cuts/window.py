"""Main window (GTK4, no libadwaita so the system GTK theme is used as-is)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .export import CancelledError, ExportSettings, Exporter, Mode  # noqa: E402
from .media import Clip, MediaError, format_time, keyframe_times, probe  # noqa: E402
from .player import Player  # noqa: E402
from .timeline import Timeline  # noqa: E402

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".mts", ".m2ts", ".ts", ".mxf", ".avi",
              ".webm", ".3gp", ".insv", ".lrv"}

HELP_TEXT = ("Space play/pause · J/K/L back/pause/forward · ←/→ frame · Shift+←/→ 1 s · "
             "I in · O out · Esc clear in · Del delete segment · PgUp/PgDn clip · Ctrl+E export all · Ctrl+Shift+E export current")

MODES = [(Mode.LOSSLESS, "Lossless (fast, starts on keyframe)"),
         (Mode.ACCURATE, "Frame-accurate (re-encode)")]

CSS = b"""
.video-area { background-color: black; }
.timecode { font-family: monospace; font-feature-settings: "tnum"; }
.dim { opacity: 0.65; }
.seg-header { font-weight: bold; }
button.compact { padding-left: 6px; padding-right: 6px; min-width: 0; }
"""

_KF_SLOTS = threading.Semaphore(2)    # at most 2 keyframe scans at once


def _settings_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "simple-cuts" / "settings.json"


def load_prefs() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(prefs, indent=2))
    except OSError:
        pass


def install_css() -> None:
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(CSS, -1)          # GTK >= 4.12 signature
    except TypeError:
        provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _button(label: str, cb, tooltip: str | None = None) -> Gtk.Button:
    b = Gtk.Button(label=label)
    b.set_focus_on_click(False)
    b.connect("clicked", lambda _b: cb())
    if tooltip:
        b.set_tooltip_text(tooltip)
    return b


def _icon_button(icon: str, cb, tooltip: str) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon)
    b.set_focus_on_click(False)
    b.set_tooltip_text(tooltip)
    b.connect("clicked", lambda _b: cb())
    return b


def _label(text: str = "", xalign: float = 0.0, css: str | None = None) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=xalign)
    if css:
        lab.add_css_class(css)
    return lab


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="Simple Cuts")
        self.set_default_size(1280, 820)
        install_css()
        self.prefs = load_prefs()
        self.clips: list[Clip] = []
        self.current: Clip | None = None
        self.exported: set[Path] = set()
        self._rate = 1.0
        self._output_custom = False
        self._exporter: Exporter | None = None
        self._export_clips: list[Clip] = []
        self._status_timer = 0
        self._allow_close = False
        self._refreshing = False

        self._build_ui()
        self.player = Player(self.picture)
        self.player.connect("position-changed", lambda _p, t: self._on_position(t))
        self.player.connect("state-changed", lambda _p, playing: self._on_play_state(playing))
        self.player.connect("error", lambda _p, msg: self._on_player_error(msg))
        self.player.set_muted(self.mute_check.get_active())

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("close-request", self._on_close_request)
        self._refresh_all()

    # ---- construction ----------------------------------------------------
    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.pack_start(_button("Open files…", self.open_files, "Ctrl+O"))
        header.pack_start(_button("Open folder…", self.open_folder, "Open every video in a "
                                  "folder (e.g. a camera card)"))
        header.pack_start(_button("Remove clip", self.remove_clip))
        self.set_titlebar(header)

        # Left: clips
        self.clip_list = Gtk.ListBox()
        self.clip_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.clip_list.connect("row-selected", self._on_clip_row)
        self.clip_list.set_placeholder(_label("Drop clips here\nor use Open files…", 0.5, "dim"))
        left = self._titled("Clips", self._scrolled(self.clip_list))
        left.set_size_request(190, -1)

        # Centre: video + timeline + transport
        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        if hasattr(self.picture, "set_content_fit"):
            self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        else:
            self.picture.set_keep_aspect_ratio(True)
        self.picture.set_vexpand(True)
        self.picture.set_hexpand(True)
        # A ScrolledWindow without scrollbars stops the picture's natural size (the full
        # video resolution) from inflating the window; it just fills the space it gets.
        video_box = Gtk.ScrolledWindow()
        video_box.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        video_box.add_css_class("video-area")
        video_box.set_size_request(320, 180)
        video_box.set_vexpand(True)
        video_box.set_child(self.picture)

        self.timeline = Timeline()
        self.timeline.connect("seek-requested", lambda _t, t: self.seek(t))
        self.timeline.connect("segments-edited", lambda _t: self._refresh_segments())
        self.timeline.connect("segment-clicked", lambda _t, i: self._select_segment_row(i))

        self.play_btn = _icon_button("media-playback-start-symbolic", self.toggle_play,
                                     "Play / pause (Space)")
        self.time_label = _label("--:--:--:-- / --:--:--:--", css="timecode")
        self.mute_check = Gtk.CheckButton(label="Mute preview")
        self.mute_check.set_active(bool(self.prefs.get("mute", False)))
        self.mute_check.set_focus_on_click(False)
        self.mute_check.connect("toggled", lambda c: self.player.set_muted(c.get_active()))

        nav = Gtk.Box(spacing=4)
        for w in (_icon_button("media-skip-backward-symbolic", lambda: self.seek(0),
                               "Start (Home)"),
                  _icon_button("media-seek-backward-symbolic", lambda: self.nudge(-1),
                               "Back 1 s (Shift+←)"),
                  _icon_button("go-previous-symbolic", lambda: self.step(-1),
                               "Previous frame (←)"),
                  self.play_btn,
                  _icon_button("go-next-symbolic", lambda: self.step(1), "Next frame (→)"),
                  _icon_button("media-seek-forward-symbolic", lambda: self.nudge(1),
                               "Forward 1 s (Shift+→)"),
                  _icon_button("media-skip-forward-symbolic",
                               lambda: self.seek(self.timeline.duration), "End (End)")):
            nav.append(w)
        for b in _children(nav):
            b.add_css_class("compact")
        marks = Gtk.Box(spacing=6)
        marks.append(_button("Mark In [I]", self.mark_in))
        marks.append(_button("Mark Out [O]", self.mark_out))
        # A FlowBox wraps the controls onto a second line when the window is narrow
        # (tiling window managers) instead of clipping them.
        transport = Gtk.FlowBox()
        transport.set_selection_mode(Gtk.SelectionMode.NONE)
        transport.set_max_children_per_line(4)
        transport.set_column_spacing(12)
        transport.set_row_spacing(4)
        for w in (nav, self.time_label, marks, self.mute_check):
            w.set_valign(Gtk.Align.CENTER)
            transport.append(w)
        for child in _children(transport):
            child.set_focusable(False)

        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        centre.append(video_box)
        centre.append(self.timeline)
        centre.append(transport)
        hint = _label(HELP_TEXT, css="dim")
        hint.set_wrap(True)
        hint.add_css_class("caption")
        centre.append(hint)

        # Right: segments
        self.seg_list = Gtk.ListBox()
        self.seg_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.seg_list.connect("row-selected", self._on_segment_row)
        self.seg_list.connect("row-activated", self._on_segment_activated)
        self.seg_list.set_placeholder(_label("Press I and O\nto mark the parts to keep",
                                             0.5, "dim"))
        seg_header = self._seg_row_box("#", "In", "Out", "Length")
        seg_header.add_css_class("seg-header")
        seg_header.set_margin_start(6)
        seg_header.set_margin_end(6)
        seg_buttons = Gtk.Box(spacing=6, homogeneous=True)
        seg_buttons.append(_button("Delete segment", self.delete_segment, "Delete"))
        seg_buttons.append(_button("Clear all", self.clear_segments))
        right_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_inner.append(seg_header)
        right_inner.append(self._scrolled(self.seg_list))
        right_inner.append(seg_buttons)
        right = self._titled("Segments to keep", right_inner)
        right.set_size_request(250, -1)

        inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_start_child(centre)
        inner.set_end_child(right)
        inner.set_resize_end_child(False)
        inner.set_shrink_end_child(False)
        outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_start_child(left)
        outer.set_end_child(inner)
        outer.set_resize_start_child(False)
        outer.set_shrink_start_child(False)
        outer.set_shrink_end_child(False)
        outer.set_vexpand(True)

        # Bottom: export
        self.out_entry = Gtk.Entry()
        self.out_entry.set_placeholder_text("Output folder")
        self.out_entry.set_hexpand(True)
        self.out_entry.connect("changed", self._on_out_changed)
        self.mode_drop = Gtk.DropDown.new_from_strings([label for _, label in MODES])
        self.mode_drop.set_selected(1 if self.prefs.get("mode") == "accurate" else 0)
        self.mode_drop.connect("notify::selected", lambda *_: self._on_mode_changed())
        self.audio_check = Gtk.CheckButton(label="Remove audio")
        self.audio_check.set_active(bool(self.prefs.get("remove_audio", True)))
        self.join_check = Gtk.CheckButton(label="Join segments per clip")
        self.join_check.set_active(bool(self.prefs.get("join", False)))
        self.gpu_check = Gtk.CheckButton(label="Use GPU (NVENC)")
        self.gpu_check.set_active(bool(self.prefs.get("gpu", True)))
        for c in (self.audio_check, self.join_check, self.gpu_check):
            c.set_focus_on_click(False)

        row1 = Gtk.Box(spacing=8)
        row1.append(_label("Output:"))
        row1.append(self.out_entry)
        row1.append(_button("Browse…", self.choose_output))
        options = Gtk.FlowBox()
        options.set_selection_mode(Gtk.SelectionMode.NONE)
        options.set_max_children_per_line(4)
        options.set_column_spacing(8)
        options.set_hexpand(True)
        for w in (self.mode_drop, self.audio_check, self.join_check, self.gpu_check):
            options.append(w)
        for child in _children(options):
            child.set_focusable(False)

        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress_label = _label("")
        self.cancel_btn = _button("Cancel", self.cancel_export)
        self.cancel_btn.set_sensitive(False)
        self.export_current_btn = _button("Export current", self.export_current,
                                          "Export only the clip in the timeline (Ctrl+Shift+E)")
        self.export_btn = _button("Export all", self.export, "Export every clip (Ctrl+E)")
        self.export_btn.add_css_class("suggested-action")
        row2 = Gtk.Box(spacing=8)
        row2.append(self.progress)
        row2.append(self.progress_label)
        row2.append(self.cancel_btn)
        row2.append(self.export_current_btn)
        row2.append(self.export_btn)
        self.progress_label.set_ellipsize(3)
        self.progress_label.set_max_width_chars(28)

        export_frame = Gtk.Frame()
        eb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(eb, f"set_margin_{side}")(8)
        eb.append(row1)
        eb.append(options)
        eb.append(row2)
        export_frame.set_child(eb)

        self.status_label = _label("", css="dim")
        self.status_label.set_hexpand(True)
        self.status_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.info_label = _label("", 1.0, "dim")
        status = Gtk.Box(spacing=8)
        status.append(self.status_label)
        status.append(self.info_label)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(8)
        root.append(outer)
        root.append(export_frame)
        root.append(status)
        self.set_child(root)
        self._on_mode_changed()

    def _titled(self, title: str, child: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head = _label(title)
        head.add_css_class("heading")
        head.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        box.append(head)
        box.append(child)
        box.set_margin_start(4)
        box.set_margin_end(4)
        return box

    @staticmethod
    def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        sw.set_child(child)
        sw.add_css_class("frame")
        return sw

    @staticmethod
    def _seg_row_box(*cols: str) -> Gtk.Box:
        box = Gtk.Box(spacing=6)
        widths = (3, 8, 8, 9)
        for text, w in zip(cols, widths):
            lab = _label(text, 0.5, "timecode")
            lab.set_width_chars(w)
            box.append(lab)
        return box

    # ---- status ----------------------------------------------------------
    def flash(self, text: str, seconds: int = 5) -> None:
        self.status_label.set_text(text)
        if self._status_timer:
            GLib.source_remove(self._status_timer)
        self._status_timer = GLib.timeout_add_seconds(seconds, self._clear_status)

    def _clear_status(self):
        self.status_label.set_text("")
        self._status_timer = 0
        return False

    def alert(self, title: str, detail: str = "") -> None:
        if hasattr(Gtk, "AlertDialog"):
            dlg = Gtk.AlertDialog()
            dlg.set_message(title)
            dlg.set_detail(detail)
            dlg.show(self)
        else:  # GTK < 4.10
            dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                    buttons=Gtk.ButtonsType.OK, text=title,
                                    secondary_text=detail or None)
            dlg.connect("response", lambda d, _r: d.destroy())
            dlg.present()

    def confirm(self, title: str, detail: str, ok_label: str, on_yes) -> None:
        if hasattr(Gtk, "AlertDialog"):
            dlg = Gtk.AlertDialog()
            dlg.set_message(title)
            dlg.set_detail(detail)
            dlg.set_buttons(["Cancel", ok_label])
            dlg.set_cancel_button(0)
            dlg.set_default_button(0)

            def done(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        on_yes()
                except GLib.Error:
                    pass
            dlg.choose(self, None, done)
        else:
            dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                    buttons=Gtk.ButtonsType.OK_CANCEL, text=title,
                                    secondary_text=detail)

            def resp(d, r):
                d.destroy()
                if r == Gtk.ResponseType.OK:
                    on_yes()
            dlg.connect("response", resp)
            dlg.present()

    # ---- opening ---------------------------------------------------------
    def _start_folder(self) -> Gio.File:
        last = self.prefs.get("last_dir")
        return Gio.File.new_for_path(last if last and Path(last).is_dir() else str(Path.home()))

    def open_files(self) -> None:
        filt = Gtk.FileFilter()
        filt.set_name("Video files")
        filt.add_mime_type("video/*")
        for ext in VIDEO_EXTS:
            filt.add_suffix(ext[1:]) if hasattr(filt, "add_suffix") else \
                filt.add_pattern(f"*{ext}")
        all_files = Gtk.FileFilter()
        all_files.set_name("All files")
        all_files.add_pattern("*")
        if hasattr(Gtk, "FileDialog"):
            dlg = Gtk.FileDialog(title="Open clips", modal=True)
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filt)
            filters.append(all_files)
            dlg.set_filters(filters)
            dlg.set_default_filter(filt)
            dlg.set_initial_folder(self._start_folder())

            def done(d, res):
                try:
                    files = d.open_multiple_finish(res)
                except GLib.Error:
                    return
                self.add_paths([Path(files.get_item(i).get_path())
                                for i in range(files.get_n_items()) if files.get_item(i).get_path()])
            dlg.open_multiple(self, None, done)
        else:
            self._legacy_chooser(Gtk.FileChooserAction.OPEN, "Open clips", True, [filt, all_files])

    def open_folder(self) -> None:
        self._pick_folder("Open folder of clips", lambda p: self.add_paths([p]))

    def _pick_folder(self, title: str, on_pick, initial: Path | None = None) -> None:
        if hasattr(Gtk, "FileDialog"):
            dlg = Gtk.FileDialog(title=title, modal=True)
            dlg.set_initial_folder(Gio.File.new_for_path(str(initial)) if initial and
                                   initial.is_dir() else self._start_folder())

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                if f and f.get_path():
                    on_pick(Path(f.get_path()))
            dlg.select_folder(self, None, done)
        else:
            self._legacy_chooser(Gtk.FileChooserAction.SELECT_FOLDER, title, False, [],
                                 on_pick)

    def _legacy_chooser(self, action, title, multiple, filters, on_pick=None) -> None:
        dlg = Gtk.FileChooserNative.new(title, self, action, None, None)
        dlg.set_select_multiple(multiple)
        for f in filters:
            dlg.add_filter(f)
        try:
            dlg.set_current_folder(self._start_folder())
        except GLib.Error:
            pass

        def resp(d, r):
            if r == Gtk.ResponseType.ACCEPT:
                files = d.get_files()
                paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())]
                if on_pick and paths:
                    on_pick(paths[0])
                elif paths:
                    self.add_paths(paths)
            d.destroy()
        dlg.connect("response", resp)
        dlg.show()
        self._legacy_dialog = dlg   # keep a reference

    def _on_drop(self, _target, value, _x, _y) -> bool:
        paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        self.add_paths(paths)
        return True

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
        errors, added = [], 0
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
            threading.Thread(target=self._scan_keyframes, args=(clip,), daemon=True).start()
        if files:
            self.prefs["last_dir"] = str(files[0].parent)
        if added and not self._output_custom and not self.out_entry.get_text():
            self._set_output_text(str(files[0].parent / "cuts"))
        first_new = len(self.clips) - added
        self._refresh_clip_list()
        if added and self.current is None:
            self.clip_list.select_row(self.clip_list.get_row_at_index(first_new))
        if errors:
            self.alert("Some files could not be opened", "\n".join(errors[:20]))
        elif paths and not files:
            self.flash("No video files found")

    def _scan_keyframes(self, clip: Clip) -> None:
        with _KF_SLOTS:
            try:
                result: object = keyframe_times(clip.path, clip.info.start_time)
            except Exception as exc:  # noqa: BLE001 - reported in UI
                result = str(exc)
        GLib.idle_add(self._on_keyframes, clip, result)

    def _on_keyframes(self, clip: Clip, result) -> bool:
        if isinstance(result, list):
            clip.keyframes = result
        else:
            self.flash(f"Keyframe scan failed for {clip.path.name}: {result}")
        if clip is self.current:
            self.timeline.queue_draw()
            self._update_info()
        return False

    def remove_clip(self) -> None:
        if not self.current:
            return
        idx = self.clips.index(self.current)
        self.player.unload()
        self.clips.pop(idx)
        self.current = None
        self._refresh_clip_list()
        if self.clips:
            self.clip_list.select_row(self.clip_list.get_row_at_index(min(idx, len(self.clips) - 1)))
        else:
            self._load_clip(None)

    def _on_clip_row(self, _lb, row) -> None:
        if self._refreshing:
            return
        clip = self.clips[row.get_index()] if row is not None and \
            row.get_index() < len(self.clips) else None
        if clip is not self.current:
            self._load_clip(clip)

    def _load_clip(self, clip: Clip | None) -> None:
        self.current = clip
        self._rate = 1.0
        self.timeline.set_clip(clip)
        if clip:
            self.player.load(str(clip.path), clip.info.duration)
        else:
            self.player.unload()
        self._refresh_segments()
        self._update_info()
        self._update_time(0.0)

    def _move_clip(self, delta: int) -> None:
        if not self.clips:
            return
        cur = self.clips.index(self.current) if self.current in self.clips else 0
        row = self.clip_list.get_row_at_index(max(0, min(len(self.clips) - 1, cur + delta)))
        self.clip_list.select_row(row)

    # ---- transport -------------------------------------------------------
    def toggle_play(self) -> None:
        if not self.current:
            return
        if self.player.playing:
            self.player.pause()
        else:
            self._rate = 1.0
            self.player.play(1.0)

    def pause(self) -> None:
        if self.current:
            self._rate = 1.0
            self.player.pause()

    def play_faster(self) -> None:
        if not self.current:
            return
        self._rate = min(8.0, self._rate * 2) if self.player.playing else 1.0
        self.player.play(self._rate)
        self.flash(f"Speed {self._rate:g}×", 2)

    def seek(self, t: float) -> None:
        if not self.current:
            return
        t = max(0.0, min(t, self.current.info.duration))
        self.player.seek(t)
        self.timeline.set_position(t)
        self._update_time(t)

    def nudge(self, seconds: float) -> None:
        if self.current:
            self.seek(self.player.position() + seconds)

    def step(self, frames: int) -> None:
        if not self.current:
            return
        if self.player.playing:
            self.player.pause()
        fd = self.current.info.frame_duration
        frame = round(self.player.position() / fd) + frames
        self.seek(max(0, frame) * fd + fd * 0.25)

    def _on_position(self, t: float) -> None:
        if not self.timeline.dragging:
            self.timeline.set_position(t)
            self._update_time(t)

    def _on_player_error(self, msg: str) -> None:
        if "decoder" in msg.lower() or "plug-in" in msg.lower():
            self.flash("Can't preview this codec: install the GStreamer libav plugin "
                       "(run simple-cuts.sh). Marking and export still work.", 15)
        else:
            self.flash(f"Playback error: {msg}", 10)

    def _on_play_state(self, playing: bool) -> None:
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing
                                    else "media-playback-start-symbolic")

    def _update_time(self, t: float) -> None:
        if not self.current:
            self.time_label.set_text("--:--:--:-- / --:--:--:--")
            return
        fps = self.current.info.fps
        self.time_label.set_text(f"{format_time(t, fps)} / "
                                 f"{format_time(self.current.info.duration, fps)}")

    # ---- marking ---------------------------------------------------------
    def mark_in(self) -> None:
        if not self.current:
            return
        t = self.player.position()
        self.timeline.pending_in = t
        self.timeline.queue_draw()
        self.flash(f"In point at {format_time(t, self.current.info.fps)} — press O at the end")

    def mark_out(self) -> None:
        clip = self.current
        if not clip:
            return
        out = self.player.position()
        start = self.timeline.pending_in
        if start is None:
            start = max((s.end for s in clip.segments if s.end <= out), default=0.0)
        if out - start < clip.info.frame_duration:
            self.flash("Out point must be after the in point")
            return
        seg = clip.add_segment(start, out)
        self.timeline.pending_in = None
        self.timeline.selected = clip.segments.index(seg)
        self._refresh_segments()
        self._refresh_clip_list()

    def clear_in(self) -> None:
        self.timeline.pending_in = None
        self.timeline.queue_draw()

    def delete_segment(self) -> None:
        clip, idx = self.current, self.timeline.selected
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
        self._refreshing = True
        self.seg_list.select_row(self.seg_list.get_row_at_index(idx))
        self._refreshing = False

    def _on_segment_row(self, _lb, row) -> None:
        if self._refreshing or row is None:
            return
        self.timeline.selected = row.get_index()
        self.timeline.queue_draw()

    def _on_segment_activated(self, _lb, row) -> None:
        if self.current and row is not None and row.get_index() < len(self.current.segments):
            self.seek(self.current.segments[row.get_index()].start)

    # ---- refresh ---------------------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_clip_list()
        self._refresh_segments()
        self._update_info()

    def _refresh_clip_list(self) -> None:
        self._refreshing = True
        self.clip_list.remove_all() if hasattr(self.clip_list, "remove_all") else \
            _remove_children(self.clip_list)
        cur_row = None
        for c in self.clips:
            n = len(c.segments)
            mark = "✓ " if c.path in self.exported else ""
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{side}")(4)
            name = _label(f"{mark}{c.path.name}")
            name.set_ellipsize(2)  # middle
            box.append(name)
            box.append(_label(f"{format_time(c.info.duration)[:8]}  ·  "
                              f"{n} segment{'s' if n != 1 else ''}", css="dim"))
            row = Gtk.ListBoxRow()
            row.set_child(box)
            row.set_tooltip_text(str(c.path))
            self.clip_list.append(row)
            if c is self.current:
                cur_row = row
        if cur_row is not None:
            self.clip_list.select_row(cur_row)
        self._refreshing = False
        self._update_export_button()

    def _refresh_segments(self) -> None:
        self._refreshing = True
        clip = self.current
        self.seg_list.remove_all() if hasattr(self.seg_list, "remove_all") else \
            _remove_children(self.seg_list)
        fps = clip.info.fps if clip else None
        short = clip is not None and clip.info.duration < 3600

        def tc(t: float) -> str:     # drop the hours for clips under an hour
            text = format_time(t, fps)
            return text[3:] if short else text
        for i, s in enumerate(clip.segments if clip else []):
            row = Gtk.ListBoxRow()
            row.set_child(self._seg_row_box(str(i + 1), tc(s.start), tc(s.end),
                                            format_time(s.duration)[3:]))
            self.seg_list.append(row)
        if clip and 0 <= self.timeline.selected < len(clip.segments):
            self.seg_list.select_row(self.seg_list.get_row_at_index(self.timeline.selected))
        self._refreshing = False
        self.timeline.queue_draw()
        self._update_export_button()

    def _update_export_button(self) -> None:
        total = sum(len(c.segments) for c in self.clips)
        self.export_btn.set_label(f"Export all ({total})" if total else "Export all")
        self.export_btn.set_sensitive(total > 0 and self._exporter is None)
        n_cur = len(self.current.segments) if self.current else 0
        self.export_current_btn.set_label(f"Export current ({n_cur})" if n_cur
                                          else "Export current")
        self.export_current_btn.set_sensitive(n_cur > 0 and self._exporter is None)

    def _update_info(self) -> None:
        c = self.current
        if not c:
            self.info_label.set_text("")
            return
        i = c.info
        if c.keyframes is None:
            kf = " · scanning keyframes…"
        elif len(c.keyframes) > 1:
            kf = f" · keyframe every ~{i.duration / len(c.keyframes):.2f}s"
        else:
            kf = ""
        fps = f"{i.fps:.3f}".rstrip("0").rstrip(".")
        audio = "audio" if i.has_audio else "no audio"
        self.info_label.set_text(f"{i.video_codec} {i.width}×{i.height} {fps} fps "
                                 f"{i.pix_fmt} · {audio}{kf}")

    def _mode(self) -> Mode:
        return MODES[self.mode_drop.get_selected()][0]

    def _on_mode_changed(self) -> None:
        lossless = self._mode() is Mode.LOSSLESS
        self.timeline.show_snap = lossless
        self.gpu_check.set_sensitive(not lossless)
        self.timeline.queue_draw()

    # ---- export ----------------------------------------------------------
    def _set_output_text(self, text: str) -> None:
        self._setting_output = True
        self.out_entry.set_text(text)
        self._setting_output = False

    def _on_out_changed(self, _e) -> None:
        if not getattr(self, "_setting_output", False):
            self._output_custom = True

    def choose_output(self) -> None:
        cur = Path(self.out_entry.get_text()).expanduser() if self.out_entry.get_text() else None

        def picked(p: Path):
            self._set_output_text(str(p))
            self._output_custom = True
        self._pick_folder("Output folder", picked, cur if cur and cur.is_dir() else
                          (cur.parent if cur else None))

    def _save_prefs(self) -> None:
        self.prefs.update(remove_audio=self.audio_check.get_active(),
                          join=self.join_check.get_active(),
                          gpu=self.gpu_check.get_active(),
                          mute=self.mute_check.get_active(),
                          mode=self._mode().value)
        save_prefs(self.prefs)

    def export(self) -> None:
        """Export every clip that has segments."""
        self._start_export([c for c in self.clips if c.segments])

    def export_current(self) -> None:
        """Export only the clip shown in the timeline, with all its segments."""
        if self.current is None:
            self.alert("No clip selected", "Open a clip and mark segments with I and O first.")
            return
        self._start_export([self.current] if self.current.segments else [])

    def _start_export(self, clips: list[Clip]) -> None:
        if self._exporter is not None:
            return
        if not clips:
            self.alert("Nothing to export", "Mark at least one segment with I and O first.")
            return
        out_text = self.out_entry.get_text().strip()
        if not out_text:
            self.alert("Choose an output folder first")
            return
        settings = ExportSettings(output_dir=Path(out_text).expanduser(), mode=self._mode(),
                                  remove_audio=self.audio_check.get_active(),
                                  join_segments=self.join_check.get_active(),
                                  use_gpu=self.gpu_check.get_active())
        self._save_prefs()
        if self.player.playing:
            self.player.pause()
        self._export_clips = clips
        self._exporter = Exporter()
        self.cancel_btn.set_sensitive(True)
        self.progress.set_fraction(0)
        self._update_export_button()
        exporter = self._exporter

        def work():
            try:
                outs = exporter.run(clips, settings, lambda f, s: GLib.idle_add(
                    self._on_export_progress, f, s))
                GLib.idle_add(self._on_export_done, outs, None, settings.output_dir)
            except CancelledError:
                GLib.idle_add(self._on_export_done, None, "cancelled", settings.output_dir)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_export_done, None, str(exc), settings.output_dir)
        threading.Thread(target=work, daemon=True).start()

    def cancel_export(self) -> None:
        if self._exporter:
            self._exporter.cancel()
            self.progress_label.set_text("Cancelling…")

    def _on_export_progress(self, frac: float, text: str) -> bool:
        if self._exporter is not None:
            self.progress.set_fraction(max(0.0, min(1.0, frac)))
            self.progress_label.set_text(text)
        return False

    def _on_export_done(self, outputs, error, out_dir: Path) -> bool:
        self._exporter = None
        self.cancel_btn.set_sensitive(False)
        if error is None:
            self.exported |= {c.path for c in self._export_clips}
            self.progress.set_fraction(1.0)
            self.progress_label.set_text(f"Exported {len(outputs)} file(s)")
            self.flash(f"Exported {len(outputs)} file(s) to {out_dir}", 10)
        elif error == "cancelled":
            self.progress.set_fraction(0)
            self.progress_label.set_text("Cancelled")
        else:
            self.progress.set_fraction(0)
            self.progress_label.set_text("Export failed")
            self.alert("Export failed", error)
        self._refresh_clip_list()
        return False

    # ---- keyboard --------------------------------------------------------
    def _on_key(self, _ctl, keyval, _code, state) -> bool:
        focus = self.get_focus()
        if focus is not None and focus.get_ancestor(Gtk.Popover) is not None:
            return False   # navigating a drop-down / popover
        if isinstance(focus, (Gtk.Text, Gtk.Editable)) and not (
                state & Gdk.ModifierType.CONTROL_MASK):
            return False   # typing in the output path
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        k = Gdk.keyval_to_lower(keyval)
        K = Gdk
        if ctrl:
            export = self.export_current if shift else self.export
            actions = {K.KEY_o: self.open_files, K.KEY_e: export, K.KEY_q: self.close,
                       K.KEY_Left: lambda: self.nudge(-10), K.KEY_Right: lambda: self.nudge(10)}
        else:
            actions = {
                K.KEY_space: self.toggle_play, K.KEY_k: self.pause, K.KEY_l: self.play_faster,
                K.KEY_j: lambda: self.nudge(-1),
                K.KEY_Left: (lambda: self.nudge(-1)) if shift else (lambda: self.step(-1)),
                K.KEY_Right: (lambda: self.nudge(1)) if shift else (lambda: self.step(1)),
                K.KEY_Home: lambda: self.seek(0),
                K.KEY_End: lambda: self.seek(self.timeline.duration),
                K.KEY_i: self.mark_in, K.KEY_o: self.mark_out, K.KEY_Escape: self.clear_in,
                K.KEY_Delete: self.delete_segment, K.KEY_BackSpace: self.delete_segment,
                K.KEY_Page_Up: lambda: self._move_clip(-1),
                K.KEY_Page_Down: lambda: self._move_clip(1),
                K.KEY_m: lambda: self.mute_check.set_active(not self.mute_check.get_active()),
            }
        fn = actions.get(k)
        if fn is None:
            return False
        fn()
        return True

    # ---- closing ---------------------------------------------------------
    def _on_close_request(self, _w) -> bool:
        if self._allow_close:
            return False
        if self._exporter is not None:
            self.confirm("Export running", "An export is still running. Cancel it and quit?",
                         "Quit", self._force_close)
            return True
        pending = [c for c in self.clips if c.segments and c.path not in self.exported]
        if pending:
            self.confirm("Quit?", f"{len(pending)} clip(s) have marked segments that were "
                         "not exported.", "Quit anyway", self._force_close)
            return True
        self._shutdown()
        return False

    def _force_close(self) -> None:
        if self._exporter is not None:
            self._exporter.cancel()
        self._allow_close = True
        self._shutdown()
        self.close()

    def _shutdown(self) -> None:
        self._save_prefs()
        self.player.shutdown()


def _children(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _remove_children(listbox: Gtk.ListBox) -> None:
    child = listbox.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt
