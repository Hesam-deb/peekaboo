"""Switching the animation (a GNOME Shell extension) on and off."""
from __future__ import annotations

import subprocess
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

UUID = "peekaboo@hesam-deb.github.io"
OLD_UUIDS = {"faceid-lock@irface"}
PATHS = (Path("/usr/share/gnome-shell/extensions") / UUID,
         Path(__file__).resolve().parents[2] / "extension")


def _settings() -> Gio.Settings | None:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup("org.gnome.shell", True) is None:
        return None
    return Gio.Settings.new("org.gnome.shell")


def available() -> bool:
    return _settings() is not None


def enabled() -> bool:
    s = _settings()
    return s is not None and UUID in s.get_strv("enabled-extensions") \
        and UUID not in s.get_strv("disabled-extensions")


def set_enabled(on: bool) -> None:
    s = _settings()
    if s is None:
        raise RuntimeError("the animation needs GNOME Shell")
    for key, present in (("enabled-extensions", on), ("disabled-extensions", not on)):
        uuids = s.get_strv(key)
        wanted = [u for u in uuids if u != UUID and u not in OLD_UUIDS] + [UUID] * present
        if sorted(wanted) != sorted(uuids):
            s.set_strv(key, wanted)
    Gio.Settings.sync()


def loaded() -> bool:
    """GNOME Shell only looks for new extensions when the session starts."""
    try:
        info = Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
            "org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions",
            "GetExtensionInfo", GLib.Variant("(s)", (UUID,)), None,
            Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        return bool(info.unpack()[0])
    except GLib.Error:
        return False


def open_studio() -> None:
    for path in PATHS:
        if (path / "studio-app.js").exists():
            subprocess.Popen(["gjs", "-m", str(path / "studio-app.js")],
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return
    raise RuntimeError("the animation is not installed")
