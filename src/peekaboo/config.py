"""Configuration and filesystem locations."""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# PEEKABOO_DEV=<dir> runs the whole stack unprivileged out of one directory. The
# installed daemon never has it set: systemd gives it a clean environment.
DEV = os.environ.get("PEEKABOO_DEV")


def _path(system: str, dev: str) -> Path:
    return Path(DEV) / dev if DEV else Path(system)


CONFIG = _path("/etc/peekaboo/config.toml", "config.toml")
STATE_DIR = _path("/var/lib/peekaboo", "state")
RUN_DIR = _path("/run/peekaboo", "run")
SOCKET = RUN_DIR / "peekaboo.sock"
MODEL_DIR = Path(os.environ["PEEKABOO_MODELS"]) if DEV and "PEEKABOO_MODELS" in os.environ \
    else Path("/usr/share/peekaboo/models")

# What Peekaboo can be used for. A PAM service outside these is always refused:
# common-auth is shared with remote desktops, mail, printing and more.
USES = {
    "lock": "Lock screen",
    "login": "Login screen",
    "shell": "Terminal",
    "system": "System prompts",
}

# Added to the configured threshold. Users can tighten matching, never loosen it.
LEVELS = {"standard": 0.0, "high": 0.06, "maximum": 0.12}


def parse_mode(text: str) -> tuple[str, int, int, float]:
    """"GREY 640x360@30" to (format, width, height, fps)."""
    m = re.fullmatch(r"(\S{3,4}) (\d{2,5})x(\d{2,5})(?:@(\d{1,3}))?", text)
    if not m:
        raise ValueError('mode must look like "GREY 640x360@30"')
    return m[1].ljust(4), int(m[2]), int(m[3]), float(m[4] or 0)


@dataclass
class Config:
    # camera
    device: str = "auto"           # infrared camera
    color_device: str = "auto"     # colour camera; "off" never uses it
    emitter: str = "auto"          # auto: use the Windows Hello UVC control | off
    emitter_control: str = ""      # "unit selector hexbytes" for vendor controls
    rotate: str = "auto"           # auto | 0 | 90 | 180 | 270
    mode: str = "auto"             # infrared mode, "FORMAT WIDTHxHEIGHT[@FPS]"
    exposure: int = -1             # fixed exposure; -1 leaves it to the camera
    black_level: int = 0           # of deep formats, in their own units
    # matching
    threshold: float = 0.60        # cosine similarity
    min_frames: int = 3            # matching frames needed to accept
    max_mismatch_frames: int = 8   # give up early on a stranger
    timeout: float = 5.0
    no_face_timeout: float = 3.0
    adaptive: bool = True
    # liveness (defaults; users can change them with their password)
    require_attention: bool = True
    gesture: list[str] = field(default_factory=lambda: ["shell", "system"])
    gesture_threshold: float = 0.18
    gesture_timeout: float = 8.0
    messages: bool = True          # progress messages in terminals and dialogs
    # policy
    max_failures: int = 5
    password_after_boot: bool = True
    password_max_age_hours: float = 48.0
    uses: list[str] = field(default_factory=lambda: list(USES))   # what users may turn on
    deny_services: list[str] = field(default_factory=list)
    ignore_when_lid_closed: bool = True
    idle_exit: float = 300.0

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG
        cfg = cls()
        if not path.exists():
            return cfg
        data = tomllib.loads(path.read_text())
        fields = {f.name: f for f in dataclasses.fields(cls)}
        for section in data.values():
            if not isinstance(section, dict):
                raise ValueError("config options must be inside a [section]")
            for key, value in section.items():
                if key not in fields:
                    raise ValueError(f"unknown config option {key!r}")
                want = type(getattr(cfg, key))
                if want is float and type(value) is int:
                    value = float(value)
                if type(value) is not want:
                    raise ValueError(f"{key}: expected {want.__name__}, got {value!r}")
                setattr(cfg, key, value)
        cfg.check()
        return cfg

    def check(self) -> None:
        """A typo must not silently turn authentication into a formality."""
        if not set(self.uses) <= set(USES) or not set(self.gesture) <= set(USES):
            raise ValueError(f"uses and gesture must be among {', '.join(USES)}")
        if not 0.4 <= self.threshold <= 0.95:
            raise ValueError("threshold must be between 0.4 and 0.95")
        if self.min_frames < 2:
            raise ValueError("min_frames must be at least 2")
        if not 1 <= self.max_failures <= 20:
            raise ValueError("max_failures must be between 1 and 20")
        if self.gesture_threshold < 0.1:
            raise ValueError("gesture_threshold must be at least 0.1")
        if self.rotate not in ("auto", "0", "90", "180", "270"):
            raise ValueError("rotate must be auto, 0, 90, 180 or 270")
        if self.mode != "auto":
            parse_mode(self.mode)
        if self.black_level < 0:
            raise ValueError("black_level cannot be negative")
        if self.emitter_control and len(self.emitter_control.split()) != 3:
            raise ValueError("emitter_control must be: unit selector hexbytes")
