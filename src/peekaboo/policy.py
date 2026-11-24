"""When a face is good enough, and when only the password will do."""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from pathlib import Path

from .config import LEVELS, USES, Config

DISPLAY_MANAGERS = {"gdm-password", "lightdm", "sddm"}
LOCKERS = {"kde", "swaylock", "hyprlock", "i3lock", "xscreensaver", "gnome-screensaver",
           "cinnamon-screensaver", "mate-screensaver", "xfce4-screensaver"}
SHELL = {"sudo", "sudo-i", "su", "su-l", "doas"}
SYSTEM = {"polkit-1"}


def use_of(service: str, at_greeter: bool) -> str | None:
    """Which use a PAM service belongs to; None is never allowed.

    Display managers unlock and log in through the same service. Only at the
    login screen does a greeter session own the seat.
    """
    if service in DISPLAY_MANAGERS:
        return "login" if at_greeter else "lock"
    if service in LOCKERS:
        return "lock"
    if service in SHELL:
        return "shell"
    if service in SYSTEM:
        return "system"
    return None


class Settings:
    """Per-user choices. Kept by the daemon so that programs running as the user
    cannot loosen them."""

    def __init__(self, directory: Path, cfg: Config):
        self.dir, self.cfg = directory, cfg

    def _file(self, uid: int) -> Path:
        return self.dir / f"{uid}.json"

    def _defaults(self) -> dict:
        return {"uses": list(USES), "gesture": list(self.cfg.gesture),
                "attention": self.cfg.require_attention, "level": "standard"}

    def get(self, uid: int) -> dict:
        settings = self._defaults()
        try:
            saved = json.loads(self._file(uid).read_text())
            settings.update(self.validate(saved))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            settings["uses"] = []        # damaged: off until chosen again
        return settings

    @staticmethod
    def validate(changes) -> dict:
        """Check a (partial) settings object. Raises ValueError."""
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        clean = {}
        for key, value in changes.items():
            if key in ("uses", "gesture"):
                if not isinstance(value, list) or not all(
                        isinstance(u, str) and u in USES for u in value):
                    raise ValueError(f"{key} must be among {', '.join(USES)}")
                clean[key] = [u for u in USES if u in value]
            elif key == "attention":
                if type(value) is not bool:
                    raise ValueError(f"{key} must be true or false")
                clean[key] = value
            elif key == "level":
                if not isinstance(value, str) or value not in LEVELS:
                    raise ValueError(f"level must be among {', '.join(LEVELS)}")
                clean[key] = value
            else:
                raise ValueError(f"unknown setting {key!r}")
        return clean

    @staticmethod
    def loosens(old: dict, new: dict) -> bool:
        """Whether a change makes anything easier to get past."""
        return bool(set(new["uses"]) - set(old["uses"])
                    or set(old["gesture"]) - set(new["gesture"])
                    or old["attention"] and not new["attention"]
                    or LEVELS[new["level"]] < LEVELS[old["level"]])

    def save(self, uid: int, settings: dict) -> None:
        path = self._file(uid)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


class History:
    """What Peekaboo was asked to do, for the activity page. No scores, no images."""
    KEEP = 100

    def __init__(self, directory: Path):
        self.dir = directory
        self._lock = threading.Lock()

    def _file(self, uid: int) -> Path:
        return self.dir / f"{uid}.history"

    def read(self, uid: int) -> list[dict]:
        entries = []
        try:
            for line in self._file(uid).read_text().splitlines():
                entries.append(json.loads(line))
        except (OSError, ValueError):
            pass
        return entries

    def add(self, uid: int, **entry) -> None:
        with self._lock:
            entries = [*self.read(uid), {"time": int(time.time()), **entry}][-self.KEEP:]
            path = self._file(uid)
            tmp = path.with_suffix(".history.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write("".join(json.dumps(e) + "\n" for e in entries))
                os.replace(tmp, path)
            except OSError:
                pass        # a full disk must not fail a login

    def clear(self, uid: int) -> None:
        with self._lock:
            self._file(uid).unlink(missing_ok=True)


class Policy:
    """Lockout and password-freshness state.

    Lives in /run, which is wiped at boot. That is what makes the first login
    after a restart need the password (and so unlocks the login keyring too).
    """

    def __init__(self, run_dir: Path, cfg: Config):
        self.path = run_dir / "policy.json"
        self.cfg = cfg
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)   # immune to clock changes

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}    # lost state means "password required"

    def _update(self, uid: int, **changes) -> None:
        with self._lock:
            state = self._read()
            state.setdefault(str(uid), {}).update(changes)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.path)

    def blocked(self, uid: int) -> tuple[str, str] | None:
        """(code, message) if the password is needed."""
        s = self._read().get(str(uid), {})
        if s.get("failures", 0) >= self.cfg.max_failures:
            return "locked", "Too many tries. Enter your password."
        at = s.get("password_at")
        if at is None:
            if self.cfg.password_after_boot:
                return "password", "Enter your password to enable Peekaboo after a restart."
        elif self._now() - at > self.cfg.password_max_age_hours * 3600:
            return "password", "Enter your password to keep using Peekaboo."
        return None

    def failures(self, uid: int) -> int:
        return self._read().get(str(uid), {}).get("failures", 0)

    def note_failure(self, uid: int) -> None:
        self._update(uid, failures=self.failures(uid) + 1)

    def note_success(self, uid: int) -> None:
        self._update(uid, failures=0)

    def note_password(self, uid: int) -> None:
        self._update(uid, failures=0, password_at=self._now())


def lid_closed() -> bool:
    for path in Path("/proc/acpi/button/lid").glob("*/state"):
        try:
            if "closed" in path.read_text():
                return True
        except OSError:
            pass
    return False


class Logind:
    """The few sd-login calls needed to tell who is at the keyboard."""

    def __init__(self):
        self.lib = ctypes.CDLL("libsystemd.so.0")
        self.libc = ctypes.CDLL(None)
        self.libc.free.argtypes = [ctypes.c_void_p]

    def _take(self, pointer: ctypes.c_void_p) -> str | None:
        if not pointer.value:
            return None
        text = ctypes.string_at(pointer.value).decode()
        self.libc.free(pointer)
        return text

    def peer_session(self, fd: int) -> str | None:
        out = ctypes.c_void_p()
        if self.lib.sd_peer_get_session(ctypes.c_int(fd), ctypes.byref(out)) < 0:
            return None
        return self._take(out)

    def is_remote(self, session: str) -> bool:
        return self.lib.sd_session_is_remote(session.encode()) != 0   # errors: assume remote

    def seat_active(self) -> tuple[int, str] | None:
        """(uid, session class) in front of the laptop, or None."""
        session, uid, cls = ctypes.c_void_p(), ctypes.c_uint32(), ctypes.c_void_p()
        if self.lib.sd_seat_get_active(b"seat0", ctypes.byref(session), ctypes.byref(uid)) < 0:
            return None
        name = self._take(session)
        if name and self.lib.sd_session_get_class(name.encode(), ctypes.byref(cls)) >= 0:
            return uid.value, self._take(cls) or ""
        return uid.value, ""

    def at_greeter(self) -> bool:
        active = self.seat_active()
        return active is not None and active[1] == "greeter"

    def refuse(self, fd: int, uid: int) -> str | None:
        """Why this request cannot come from someone at the laptop, if so."""
        session = self.peer_session(fd)
        if session and self.is_remote(session):
            return "remote session"
        active = self.seat_active()
        if active is None:
            return "nobody is at the local seat"
        if active[0] != uid and active[1] != "greeter":
            return "another user is at the local seat"
        return None
