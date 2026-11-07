"""Entry point: ``simple-cuts [files or folders...]``."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

if sys.platform.startswith("linux"):
    # Qt 6.10+ defaults to its native PipeWire audio backend, which crashes on systems
    # without PipeWire's client config files. PulseAudio (served by pipewire-pulse on
    # modern distros) is the robust choice. Override with QT_AUDIO_BACKEND if needed.
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from .mainwindow import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Simple Cuts")
    app.setOrganizationName("simple-cuts")
    app.setStyle("Fusion")

    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        QMessageBox.critical(None, "ffmpeg not found",
                             f"Simple Cuts needs {' and '.join(missing)} on your PATH.\n\n"
                             "On Debian/Ubuntu:  sudo apt install ffmpeg")
        return 1

    win = MainWindow()
    win.show()
    args = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        win.add_paths(args)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
