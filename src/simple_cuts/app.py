"""Entry point: ``simple-cuts [files or folders...]``."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

APP_ID = "io.github.simplecuts.SimpleCuts"


def _missing_deps() -> list[str]:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        missing.append(f"PyGObject / GTK4 / GStreamer ({exc})")
    return missing


def _flatpak_host_themes() -> None:
    """Inside Flatpak, let GTK find the host's themes (mounted under /run/host)."""
    import os
    if os.path.exists("/.flatpak-info") and os.path.isdir("/run/host/usr/share/themes"):
        dirs = os.environ.get("XDG_DATA_DIRS", "/app/share:/usr/share")
        if "/run/host/usr/share" not in dirs.split(":"):
            os.environ["XDG_DATA_DIRS"] = f"{dirs}:/run/host/usr/share"


def main() -> int:
    _flatpak_host_themes()     # must run before GLib caches the data dirs
    missing = _missing_deps()
    if missing:
        print("Simple Cuts is missing: " + ", ".join(missing), file=sys.stderr)
        print("Run ./simple-cuts.sh — it installs everything for your distro.", file=sys.stderr)
        return 1

    from gi.repository import Gio, GLib, Gtk

    from .theme import ThemeFollower
    from .window import MainWindow

    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Simple Cuts")

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.HANDLES_OPEN
                             | Gio.ApplicationFlags.NON_UNIQUE)
            self.window: MainWindow | None = None
            self.theme: ThemeFollower | None = None

        def _ensure_window(self) -> MainWindow:
            if self.window is None:
                self.theme = ThemeFollower()
                self.window = MainWindow(self)
            return self.window

        def do_activate(self):
            self._ensure_window().present()

        def do_open(self, files, _n, _hint):
            win = self._ensure_window()
            win.present()
            win.add_paths([Path(f.get_path()) for f in files if f.get_path()])

    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
