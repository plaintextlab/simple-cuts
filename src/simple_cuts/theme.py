"""Make the app follow the desktop's GTK theme and dark/light preference.

GTK4 already honours ``~/.config/gtk-4.0/settings.ini``, XSettings and ``GTK_THEME``.
This module adds two things that plain GTK4 misses on many desktops:

* the freedesktop *color-scheme* preference (portal ``org.freedesktop.appearance``),
  followed live, so switching the desktop between dark and light switches the app;
* a fallback to ``~/.config/gtk-3.0/settings.ini`` when the theme was only configured
  for GTK3 (common with lxappearance / nwg-look on i3, sway, Openbox, ...);
* on macOS, the system Light/Dark appearance (GTK's macOS backend ignores it).
"""

from __future__ import annotations

import configparser
import os
import plistlib
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _read_ini(path: Path) -> dict[str, str]:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    return dict(cp["Settings"]) if cp.has_section("Settings") else {}


def _theme_exists(name: str) -> bool:
    dirs = [Path(GLib.get_user_data_dir()) / "themes", Path.home() / ".themes"]
    dirs += [Path(d) / "themes" for d in GLib.get_system_data_dirs()]
    return any((d / name / "gtk-4.0").is_dir() or (d / name / "gtk-3.0").is_dir()
               for d in dirs)


class ThemeFollower:
    def __init__(self) -> None:
        self.settings = Gtk.Settings.get_default()
        self._proxy: Gio.DBusProxy | None = None
        if self.settings is None:
            return
        if sys.platform == "darwin":
            self._follow_macos()
            return
        if "GTK_THEME" not in os.environ:
            self._apply_gtk3_fallback()
        self._connect_portal()

    # ---- macOS appearance --------------------------------------------------
    _MAC_PREFS = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"

    def _follow_macos(self) -> None:
        self._mac_mtime = None
        self._poll_macos()
        GLib.timeout_add_seconds(2, self._poll_macos)

    def _poll_macos(self) -> bool:
        """System Settings writes AppleInterfaceStyle=Dark to the global preferences
        (absent in light mode); re-read it whenever that file changes."""
        try:
            mtime = self._MAC_PREFS.stat().st_mtime
        except OSError:
            return True
        if mtime != self._mac_mtime:
            self._mac_mtime = mtime
            try:
                with open(self._MAC_PREFS, "rb") as f:
                    style = plistlib.load(f).get("AppleInterfaceStyle")
            except (OSError, plistlib.InvalidFileException, ValueError):
                return True
            self._set_dark(style == "Dark")
        return True

    def _apply_gtk3_fallback(self) -> None:
        gtk4 = _read_ini(_config_home() / "gtk-4.0" / "settings.ini")
        gtk3 = _read_ini(_config_home() / "gtk-3.0" / "settings.ini")
        theme = gtk3.get("gtk-theme-name")
        if "gtk-theme-name" not in gtk4 and theme and _theme_exists(theme):
            self.settings.set_property("gtk-theme-name", theme)
        dark = gtk3.get("gtk-application-prefer-dark-theme")
        if "gtk-application-prefer-dark-theme" not in gtk4 and dark is not None:
            self._set_dark(dark.strip().lower() in ("1", "true", "yes"))
        icons = gtk3.get("gtk-icon-theme-name")
        if "gtk-icon-theme-name" not in gtk4 and icons:
            self.settings.set_property("gtk-icon-theme-name", icons)

    def _set_dark(self, dark: bool) -> None:
        # Deprecated in GTK 4.20 in favour of the portal-driven color scheme, but it is
        # still what themes with a -dark variant (Adwaita, adw-gtk3, Arc, ...) react to.
        self.settings.set_property("gtk-application-prefer-dark-theme", dark)

    # ---- freedesktop color-scheme portal ---------------------------------
    def _connect_portal(self) -> None:
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION
                | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                PORTAL_BUS, PORTAL_PATH, PORTAL_IFACE, None)
        except GLib.Error:
            return
        self._proxy.connect("g-signal", self._on_portal_signal)
        self._read_scheme()

    def _read_scheme(self) -> None:
        for method in ("ReadOne", "Read"):
            try:
                res = self._proxy.call_sync(
                    method, GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                    Gio.DBusCallFlags.NONE, 1000, None)
            except GLib.Error:
                continue
            value = res.unpack()[0]
            while isinstance(value, GLib.Variant):     # Read() nests the variant twice
                value = value.unpack()
            self._apply_scheme(value)
            return

    def _on_portal_signal(self, _proxy, _sender, signal, params) -> None:
        if signal != "SettingChanged":
            return
        namespace, key, value = params.unpack()
        if namespace == "org.freedesktop.appearance" and key == "color-scheme":
            self._apply_scheme(value)

    def _apply_scheme(self, value) -> None:
        # 0 = no preference (keep settings.ini), 1 = prefer dark, 2 = prefer light
        if value == 1:
            self._set_dark(True)
        elif value == 2:
            self._set_dark(False)
