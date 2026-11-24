"""Finding the right cameras, driving the IR emitter, pairing lit and unlit frames."""
from __future__ import annotations

import fcntl
import os
import re
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import v4l2
from .v4l2 import CameraError, Mode, Stream

__all__ = ["CameraError", "CameraInfo", "ColorFeed", "IRCamera", "Observation", "Strober",
           "discover", "pick_color", "pick_ir", "probe"]

SYSFS = Path("/sys/class/video4linux")

# Microsoft camera extension unit. Its face-authentication control is the
# documented way to ask a Windows Hello camera to strobe its IR emitter.
MSXU_GUID = bytes.fromhex("dc953f0f32264e4c92c9a04782f43bc8")
MSXU_FACE_AUTH = 0x06
FACE_AUTH_ALTERNATE = 0x02
SET_CUR, GET_CUR, GET_MAX, GET_LEN = 0x01, 0x81, 0x83, 0x85

IR_NAME = re.compile(r"(?<![a-z])(ir|infrared|infra-red|hello|face ?auth)(?![a-z])", re.I)
DEPTH_FORMATS = {"Z16 ", "INZI", "INVZ", "INVI"}
RAW_SENSOR = re.compile(r"ipu[3-7]|isys|atomisp|rkisp|camss", re.I)
IR_CERTAIN = 50


@dataclass
class CameraInfo:
    node: str
    name: str
    driver: str
    bus: str
    usb: str = ""              # sysfs name of the USB device, "" if none
    vid: str = ""
    pid: str = ""
    interface: int = -1
    placement: str = "unknown"     # fixed | removable | unknown
    modes: list[Mode] = field(default_factory=list)
    msxu: int | None = None        # unit id of the Windows Hello control
    privacy: bool = False
    kind: str = "unknown"          # ir | maybe-ir | color | unsupported
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Identity that enrolments are tied to. Survives renumbering of /dev/videoN,
        and differs for a device plugged into another port."""
        if self.usb:
            return f"{self.vid}:{self.pid}:{self.interface}@{self.bus}"
        return f"{self.driver}@{self.bus}"

    @property
    def external(self) -> bool:
        return self.placement == "removable"

    def as_dict(self) -> dict:
        best = v4l2.choose(self.modes, self.kind == "color")
        return {"id": self.id, "node": self.node, "name": self.name, "kind": self.kind,
                "external": self.external, "emitter": self.msxu is not None,
                "privacy": self.privacy, "reasons": self.reasons,
                "mode": f"{best.width}×{best.height} {best.format.strip()}" if best else ""}


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return ""


def find_msxu(descriptors: bytes, interface: int) -> int | None:
    """Unit id of the Microsoft XU within one video function's descriptors."""
    pos, current = 0, None
    while pos + 2 <= len(descriptors) and descriptors[pos] >= 2:
        d = descriptors[pos:pos + descriptors[pos]]
        if d[1] == 4 and len(d) >= 3:
            current = d[2]
        elif d[1] == 0x24 and len(d) >= 20 and d[2] == 6 and current == interface \
                and d[4:20] == MSXU_GUID:
            return d[3]
        pos += descriptors[pos]
    return None


def classify(info: CameraInfo, interface_name: str = "") -> None:
    """Decide what a camera is from its description alone."""
    formats = {m.format for m in info.modes}
    label = f"{info.name} {interface_name}"
    if not info.modes:
        info.kind = "unsupported"
        info.reasons.append("raw sensor node: needs libcamera, which cannot drive IR"
                            if RAW_SENSOR.search(info.driver) else "offers no video formats")
        return
    if formats & DEPTH_FORMATS:
        info.kind = "unsupported"
        info.reasons.append("depth sensor")
        return
    if not formats & v4l2.SUPPORTED:
        info.kind = "unsupported"
        info.reasons.append("unsupported pixel formats: " + ", ".join(sorted(formats)))
        return
    usable = formats & v4l2.SUPPORTED
    if usable <= v4l2.GREY:
        info.score += 60
        info.reasons.append("greyscale only")
    if info.msxu is not None:
        info.score += 50
        info.reasons.append("Windows Hello control")
    if IR_NAME.search(label):
        info.score += 30
        info.reasons.append("named as infrared")
    if info.score and info.placement == "fixed":
        info.score += 5
    if info.external:
        info.score -= 10
        info.reasons.append("external")
    if info.driver != "uvcvideo":
        info.score -= 5
    if info.score >= IR_CERTAIN:
        info.kind = "ir"
    elif info.score > 0:
        info.kind = "maybe-ir"
    else:
        info.kind = "color"


def _inspect(node: Path) -> CameraInfo | None:
    dev = f"/dev/{node.name}"
    device = (node / "device").resolve()
    # Loopback and other virtual devices can replay anything: never a camera to trust.
    if not (node / "device").exists() or "/devices/virtual/" in str(node.resolve()):
        return None
    try:
        q = v4l2.query(dev)
    except OSError:
        return None
    if not q.capture and not q.mplane:
        return None                 # metadata and output nodes
    info = CameraInfo(dev, q.card, q.driver, q.bus, modes=q.modes, privacy=q.privacy)
    interface_name = ""
    if (device / "bInterfaceNumber").exists():
        usb = device.parent
        info.usb, info.vid, info.pid = usb.name, _read(usb / "idVendor"), _read(usb / "idProduct")
        info.interface = int(_read(device / "bInterfaceNumber") or "0", 16)
        info.placement = _read(usb / "removable") or "unknown"
        interface_name = _read(device / "interface")
        # The card name is cut at 31 characters, often just before "IR".
        info.name = interface_name or info.name
        try:
            info.msxu = find_msxu((usb / "descriptors").read_bytes(), info.interface)
        except OSError:
            pass
    classify(info, f"{q.card} {interface_name}")
    return info


def discover() -> list[CameraInfo]:
    """All physical cameras, most IR-like first."""
    nodes = sorted(SYSFS.glob("video*"), key=lambda p: int(p.name[5:] or 0))
    found: dict[str, CameraInfo] = {}
    for node in nodes:
        info = _inspect(node)
        if info is None:
            continue
        # One function can expose several nodes; keep the one that can stream.
        if info.id not in found or (found[info.id].kind == "unsupported"
                                    and info.kind != "unsupported"):
            found[info.id] = info
    cameras = list(found.values())
    for c in cameras:
        if c.kind == "maybe-ir" and any(o.usb and o.usb == c.usb and o.kind == "color"
                                        for o in cameras):
            c.score += 10
            c.reasons.append("shares a module with a colour camera")
    return sorted(cameras, key=lambda c: (-c.score, c.node))


def probe(info: CameraInfo, seconds: float = 3.0) -> dict:
    """Look through the camera: is it monochrome, does its emitter strobe?"""
    mode = v4l2.choose(info.modes, color=False)
    if mode is None:
        raise CameraError("no usable mode")
    if info.msxu is not None:
        enable_emitter(info)
    stream = Stream(info.node, mode)
    try:
        means, chroma, start = [], [], time.monotonic()
        while time.monotonic() - start < seconds:
            image = stream.read(color=True)
            if image is None:
                continue
            means.append(float(image.mean()))
            spread = image.max(axis=2).astype(int) - image.min(axis=2)
            chroma.append(float(spread.mean()))
    finally:
        stream.close()
    tail = means[-20:]
    steps = sorted(abs(a - b) for a, b in zip(tail, tail[1:]))
    return {"fps": round(len(means) / seconds, 1), "frames": len(means),
            "mono": bool(chroma) and float(np.median(chroma)) < 3.0,
            "strobe": bool(steps) and steps[len(steps) // 2] >= Strober.STEP,
            "bright": bool(means) and max(means) > 5.0}


def pick_ir(cameras: list[CameraInfo], wanted: str | None = None,
            device: str = "auto") -> CameraInfo:
    """The IR camera to use. `wanted` is the identity an enrolment is tied to."""
    if wanted:
        for c in cameras:
            if c.id == wanted:
                return c
        raise CameraError("the camera you enrolled with is not connected")
    if device != "auto":
        real = os.path.realpath(device)
        for c in cameras:
            if c.node == real and c.kind != "unsupported":
                return c
        raise CameraError(f"{device} is not a usable camera")
    for c in cameras:
        if c.kind == "ir":
            return c
    for c in cameras:               # uncertain by description: look through it
        if c.kind == "maybe-ir":
            try:
                result = probe(c)
            except CameraError:
                continue
            if result["mono"]:
                c.kind = "ir"
                c.reasons.append("monochrome picture")
                return c
    if any(RAW_SENSOR.search(c.driver) for c in cameras):
        raise CameraError("no infrared camera found (this laptop's MIPI camera is not supported)")
    raise CameraError("no infrared camera found")


def pick_color(cameras: list[CameraInfo], ir: CameraInfo, device: str = "auto",
               wanted: str | None = None) -> CameraInfo | None:
    """The colour camera that goes with an IR camera, if there is one."""
    if device == "off":
        return None
    colour = [c for c in cameras if c.kind == "color" and c.id != ir.id]
    if wanted:
        return next((c for c in colour if c.id == wanted), None)
    if device != "auto":
        return next((c for c in colour if c.node == os.path.realpath(device)), None)
    # Only one in the same module surely sees the same scene from the same place.
    same = [c for c in colour if c.usb and c.usb == ir.usb]
    internal = [c for c in colour if c.placement == "fixed"]
    return (same or internal or [None])[0]


# ---- emitter ---------------------------------------------------------------

def _xu(fd: int, unit: int, selector: int, code: int, data: bytes) -> bytes:
    import ctypes
    buf = (ctypes.c_uint8 * len(data))(*data)
    fcntl.ioctl(fd, v4l2.UVC_CTRL_QUERY,
                v4l2.XuQuery(unit, selector, code, len(data), ctypes.addressof(buf)))
    return bytes(buf)


def enable_emitter(info: CameraInfo, manual: str = "") -> str:
    """Turn on alternating illumination. Volatile: the camera resets itself.

    Only the documented Windows Hello control is used on its own accord. Cameras
    with a vendor-specific control can be given it as "unit selector hexbytes".
    """
    if manual:
        unit, selector, value = manual.split()
        fd = os.open(info.node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            _xu(fd, int(unit, 0), int(selector, 0), SET_CUR, bytes.fromhex(value))
            return "configured control sent"
        except OSError as e:
            return f"configured control failed: {e.strerror}"
        finally:
            os.close(fd)
    if info.msxu is None:
        return "no Windows Hello control on this camera"
    fd = os.open(info.node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        size = struct.unpack("<H", _xu(fd, info.msxu, MSXU_FACE_AUTH, GET_LEN, bytes(2)))[0]
        if not 3 <= size <= 64:
            return "unexpected control size"
        limit = _xu(fd, info.msxu, MSXU_FACE_AUTH, GET_MAX, bytes(size))
        if limit[0] != 1 or not limit[2] & FACE_AUTH_ALTERNATE:
            return "camera does not support alternating illumination"
        want = bytes([1, limit[1], FACE_AUTH_ALTERNATE]) + bytes(size - 3)
        if _xu(fd, info.msxu, MSXU_FACE_AUTH, GET_CUR, bytes(size)) != want:
            _xu(fd, info.msxu, MSXU_FACE_AUTH, SET_CUR, want)
        return "alternating illumination enabled"
    except OSError as e:
        return f"control failed: {e.strerror}"
    finally:
        os.close(fd)


# ---- frames ----------------------------------------------------------------

@dataclass
class Observation:
    image: np.ndarray      # what recognition runs on
    lit: np.ndarray        # raw illuminated frame
    strobed: bool          # image is lit minus ambient
    time: float


class Strober:
    """Turns a raw frame stream into ambient-subtracted observations.

    The emitter fires on some frames and not others, so lit minus unlit is light
    that came from *our* emitter and bounced back. Screens return next to none of
    it, and sunlight cancels out. Most cameras alternate every frame; others fire
    in pairs or one frame in three, so frames are told apart by brightness.
    """
    STEP = 3.0        # mean grey levels between lit and unlit
    WINDOW = 7
    PROBE = 20        # frames without strobing before declaring it absent

    def __init__(self, allow_unstrobed: bool):
        self.allow_unstrobed = allow_unstrobed
        self.history: deque[tuple[np.ndarray, float]] = deque(maxlen=self.WINDOW)
        self.flat = 0
        self.strobing = False

    def push(self, frame: np.ndarray, now: float) -> Observation | None:
        self.history.append((frame, float(frame.mean())))
        if len(self.history) < 3:
            return None
        means = [m for _, m in self.history]
        low, high = min(means), max(means)
        middle = (low + high) / 2
        on = [m > middle for m in means]
        # A change that goes one way only is exposure settling or a lamp, not strobing.
        edges = sum(a != b for a, b in zip(on, on[1:]))
        frame, mean = self.history[-2]
        if high - low >= self.STEP and edges >= 2:
            self.strobing, self.flat = True, 0
            if not on[-2]:
                return None
            before = next((f for (f, _), lit in zip(reversed(list(self.history)[:-2]),
                                                    reversed(on[:-2])) if not lit), None)
            after = None if on[-1] else self.history[-1][0]
            if before is None and after is None:
                return None
            if before is None or after is None:
                ambient = after if before is None else before
            else:
                ambient = cv2.addWeighted(before, .5, after, .5, 0)
            return Observation(cv2.subtract(frame, ambient), frame, True, now)
        if mean > 3.0:
            self.flat += 1
        if self.allow_unstrobed and not self.strobing and self.flat > self.PROBE:
            return Observation(frame, frame, False, now)
        return None


ROTATE = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
          270: cv2.ROTATE_90_COUNTERCLOCKWISE}


class IRCamera:
    def __init__(self, info: CameraInfo, emitter: str = "auto", control: str = "",
                 rotate: int = 0, mode: Mode | None = None, exposure: int = -1,
                 black_level: int = 0):
        if info.privacy:
            raise CameraError("the camera's privacy shutter is closed")
        mode = mode or v4l2.choose(info.modes, color=False)
        if mode is None:
            raise CameraError(f"{info.name} has no usable video mode")
        self.info, self.rotate = info, rotate
        self.emitter_status = "off"
        if emitter != "off" or control:
            try:
                self.emitter_status = enable_emitter(info, control)
            except (OSError, ValueError) as e:
                self.emitter_status = f"failed: {e}"
        self.stream = Stream(info.node, mode, exposure, black_level)

    def read(self) -> np.ndarray | None:
        frame = self.stream.read(color=False)
        code = ROTATE[self.rotate]
        return frame if frame is None or code is None else cv2.rotate(frame, code)

    def observations(self, allow_unstrobed: bool):
        """Yield an Observation, or None for frames that carry no information."""
        strober = Strober(allow_unstrobed)
        rotate = self.rotate
        while True:
            frame = self.read()
            if rotate != self.rotate:       # enrolment is trying another orientation
                strober, rotate = Strober(allow_unstrobed), self.rotate
            yield None if frame is None else strober.push(frame, time.monotonic())

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ColorFeed:
    """Keeps the newest frame of the colour camera, read on its own thread."""
    FRESH = 0.25

    def __init__(self, info: CameraInfo, rotate: int = 0):
        if info.privacy:
            raise CameraError("the camera's privacy shutter is closed")
        mode = v4l2.choose(info.modes, color=True)
        if mode is None:
            raise CameraError(f"{info.name} has no usable video mode")
        self.info, self.rotate = info, rotate
        self.stream = Stream(info.node, mode)
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._time = 0.0
        self._stop = threading.Event()
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                frame = self.stream.read(color=True)
                if frame is None:
                    continue
                height, width = frame.shape[:2]
                if height > 540:
                    frame = cv2.resize(frame, (width * 540 // height // 2 * 2, 540),
                                       interpolation=cv2.INTER_AREA)
                code = ROTATE[self.rotate]
                if code is not None:
                    frame = cv2.rotate(frame, code)
                with self._lock:
                    self._frame, self._time = frame, time.monotonic()
        except CameraError as e:
            self.error = str(e)

    def latest(self) -> tuple[np.ndarray, float] | None:
        """(frame, capture time) if it is recent enough to pair with an IR frame."""
        with self._lock:
            if self._frame is None or time.monotonic() - self._time > self.FRESH:
                return None
            return self._frame, self._time

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
