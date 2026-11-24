"""Minimal V4L2 capture.

Written against the kernel interface instead of OpenCV's VideoCapture for control
over format choice, bounded waits (OpenCV blocks for ten seconds when a camera
stalls) and errors that say what went wrong.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import mmap
import os
import select
import time
from ctypes import c_int32, c_long, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p
from dataclasses import dataclass

import cv2
import numpy as np


class CameraError(Exception):
    pass


class _Capability(ctypes.Structure):
    _fields_ = [("driver", c_uint8 * 16), ("card", c_uint8 * 32), ("bus_info", c_uint8 * 32),
                ("version", c_uint32), ("capabilities", c_uint32), ("device_caps", c_uint32),
                ("reserved", c_uint32 * 3)]


class _FmtDesc(ctypes.Structure):
    _fields_ = [("index", c_uint32), ("type", c_uint32), ("flags", c_uint32),
                ("description", c_uint8 * 32), ("pixelformat", c_uint32),
                ("mbus_code", c_uint32), ("reserved", c_uint32 * 3)]


class _FrmSize(ctypes.Structure):
    _fields_ = [("index", c_uint32), ("pixelformat", c_uint32), ("type", c_uint32),
                ("values", c_uint32 * 6), ("reserved", c_uint32 * 2)]


class _FrmInterval(ctypes.Structure):
    _fields_ = [("index", c_uint32), ("pixelformat", c_uint32), ("width", c_uint32),
                ("height", c_uint32), ("type", c_uint32), ("values", c_uint32 * 6),
                ("reserved", c_uint32 * 2)]


class _Pix(ctypes.Structure):
    _fields_ = [(n, c_uint32) for n in (
        "width", "height", "pixelformat", "field", "bytesperline", "sizeimage",
        "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class _FmtUnion(ctypes.Union):
    _fields_ = [("pix", _Pix), ("raw", c_uint8 * 200), ("align", c_void_p)]


class _Format(ctypes.Structure):
    _fields_ = [("type", c_uint32), ("fmt", _FmtUnion)]


class _StreamParm(ctypes.Structure):
    _fields_ = [("type", c_uint32), ("capability", c_uint32), ("capturemode", c_uint32),
                ("numerator", c_uint32), ("denominator", c_uint32), ("rest", c_uint8 * 184)]


class _ReqBufs(ctypes.Structure):
    _fields_ = [("count", c_uint32), ("type", c_uint32), ("memory", c_uint32),
                ("capabilities", c_uint32), ("flags", c_uint32)]


class _Timeval(ctypes.Structure):
    _fields_ = [("sec", c_long), ("usec", c_long)]


class _BufUnion(ctypes.Union):
    _fields_ = [("offset", c_uint32), ("userptr", ctypes.c_ulong), ("pad", c_uint64)]


class _Buffer(ctypes.Structure):
    _fields_ = [("index", c_uint32), ("type", c_uint32), ("bytesused", c_uint32),
                ("flags", c_uint32), ("field", c_uint32), ("timestamp", _Timeval),
                ("timecode", c_uint32 * 4), ("sequence", c_uint32), ("memory", c_uint32),
                ("m", _BufUnion), ("length", c_uint32), ("reserved2", c_uint32),
                ("request_fd", c_uint32)]


class _Control(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("value", c_int32)]


class XuQuery(ctypes.Structure):
    _fields_ = [("unit", c_uint8), ("selector", c_uint8), ("query", c_uint8),
                ("size", c_uint16), ("data", c_void_p)]


def _ioc(direction: int, number: int, size: int, kind: str = "V") -> int:
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | number


_R, _W, _RW = 2, 1, 3
QUERYCAP = _ioc(_R, 0, ctypes.sizeof(_Capability))
ENUM_FMT = _ioc(_RW, 2, ctypes.sizeof(_FmtDesc))
S_FMT = _ioc(_RW, 5, ctypes.sizeof(_Format))
REQBUFS = _ioc(_RW, 8, ctypes.sizeof(_ReqBufs))
QUERYBUF = _ioc(_RW, 9, ctypes.sizeof(_Buffer))
QBUF = _ioc(_RW, 15, ctypes.sizeof(_Buffer))
DQBUF = _ioc(_RW, 17, ctypes.sizeof(_Buffer))
STREAMON = _ioc(_W, 18, 4)
STREAMOFF = _ioc(_W, 19, 4)
S_PARM = _ioc(_RW, 22, ctypes.sizeof(_StreamParm))
G_CTRL = _ioc(_RW, 27, ctypes.sizeof(_Control))
S_CTRL = _ioc(_RW, 28, ctypes.sizeof(_Control))
ENUM_FRAMESIZES = _ioc(_RW, 74, ctypes.sizeof(_FrmSize))
ENUM_FRAMEINTERVALS = _ioc(_RW, 75, ctypes.sizeof(_FrmInterval))
UVC_CTRL_QUERY = _ioc(_RW, 0x21, ctypes.sizeof(XuQuery), "u")

CAP_CAPTURE, CAP_CAPTURE_MPLANE, CAP_META = 0x1, 0x1000, 0x00800000
CAP_STREAMING, CAP_DEVICE_CAPS = 0x04000000, 0x80000000
TYPE_CAPTURE, MEMORY_MMAP, BUF_FLAG_ERROR = 1, 1, 0x40
CID_PRIVACY = 0x009A0910
CID_EXPOSURE_AUTO, CID_EXPOSURE_ABSOLUTE, CID_EXPOSURE = 0x009A0901, 0x009A0902, 0x00980911
EXPOSURE_MANUAL = 1


def fourcc(code: int) -> str:
    return (code & 0x7FFFFFFF).to_bytes(4, "little").decode("latin-1")


def code_of(name: str) -> int:
    return int.from_bytes(name.encode("latin-1"), "little")


def _text(raw) -> str:
    return bytes(raw).split(b"\0")[0].decode("utf-8", "replace")


@dataclass(frozen=True)
class Mode:
    format: str
    width: int
    height: int
    fps: float


@dataclass
class NodeInfo:
    driver: str
    card: str
    bus: str
    capture: bool       # single-planar video capture with streaming
    mplane: bool
    modes: list[Mode]
    privacy: bool       # shutter closed


def query(path: str) -> NodeInfo:
    """Describe a /dev/videoN. Raises OSError."""
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        cap = _Capability()
        fcntl.ioctl(fd, QUERYCAP, cap)
        caps = cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
        capture = bool(caps & CAP_CAPTURE and caps & CAP_STREAMING)
        control = _Control(CID_PRIVACY, 0)
        try:
            fcntl.ioctl(fd, G_CTRL, control)
            privacy = bool(control.value)
        except OSError:
            privacy = False
        return NodeInfo(_text(cap.driver), _text(cap.card), _text(cap.bus_info), capture,
                        bool(caps & CAP_CAPTURE_MPLANE), _modes(fd) if capture else [], privacy)
    finally:
        os.close(fd)


def _modes(fd: int) -> list[Mode]:
    modes = []
    for i in range(64):
        desc = _FmtDesc(index=i, type=TYPE_CAPTURE)
        try:
            fcntl.ioctl(fd, ENUM_FMT, desc)
        except OSError:
            break
        name = fourcc(desc.pixelformat)
        for w, h in _sizes(fd, desc.pixelformat):
            modes.append(Mode(name, w, h, _fps(fd, desc.pixelformat, w, h)))
    return modes


def _sizes(fd: int, code: int) -> list[tuple[int, int]]:
    sizes = []
    for i in range(64):
        size = _FrmSize(index=i, pixelformat=code)
        try:
            fcntl.ioctl(fd, ENUM_FRAMESIZES, size)
        except OSError:
            break
        v = size.values
        if size.type == 1:
            sizes.append((v[0], v[1]))
        else:       # continuous or stepwise: min_w max_w step_w min_h max_h step_h
            for w, h in ((640, 360), (640, 480), (1280, 720)):
                if v[0] <= w <= v[1] and v[3] <= h <= v[4]:
                    sizes.append((w, h))
            if not sizes:
                sizes.append((v[1], v[4]))
            break
    return sizes or [(640, 480)]      # drivers without enumeration still take S_FMT


def _fps(fd: int, code: int, width: int, height: int) -> float:
    best = 0.0
    for i in range(32):
        iv = _FrmInterval(index=i, pixelformat=code, width=width, height=height)
        try:
            fcntl.ioctl(fd, ENUM_FRAMEINTERVALS, iv)
        except OSError:
            break
        if iv.values[0]:
            best = max(best, iv.values[1] / iv.values[0])
        if iv.type != 1:
            break
    return best


# ---- pixel formats ---------------------------------------------------------

DEEP = {"Y10 ": 2, "Y12 ": 4, "Y14 ": 6, "Y16 ": 8}
GREY = {"GREY", "Y8I ", *DEEP}
PACKED = {"YUYV": (0, cv2.COLOR_YUV2BGR_YUYV), "YVYU": (0, cv2.COLOR_YUV2BGR_YVYU),
          "UYVY": (1, cv2.COLOR_YUV2BGR_UYVY)}
PLANAR = {"NV12": cv2.COLOR_YUV2BGR_NV12, "NV21": cv2.COLOR_YUV2BGR_NV21,
          "YU12": cv2.COLOR_YUV2BGR_I420, "YV12": cv2.COLOR_YUV2BGR_YV12}
JPEG = {"MJPG", "JPEG"}
RGB = {"RGB3": cv2.COLOR_RGB2BGR, "BGR3": None}
SUPPORTED = GREY | set(PACKED) | set(PLANAR) | JPEG | set(RGB)


def decode(data: np.ndarray, fmt: str, width: int, height: int, stride: int,
           color: bool, black: int = 0) -> np.ndarray | None:
    """Raw bytes to a grey or BGR image. None for a truncated frame."""
    if fmt in JPEG:
        image = cv2.imdecode(data, cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)
        return None if image is None or image.shape[:2] != (height, width) else image

    def rows(bytes_per_row: int, count: int = height) -> np.ndarray | None:
        step = max(stride, bytes_per_row)
        if data.size < step * (count - 1) + bytes_per_row:
            return None
        if data.size < step * count:
            padded = np.zeros(step * count, np.uint8)
            padded[:data.size] = data
            return padded.reshape(count, step)[:, :bytes_per_row]
        return data[:step * count].reshape(count, step)[:, :bytes_per_row]

    if fmt == "GREY":
        grey = rows(width)
    elif fmt == "Y8I ":                     # left and right sensor, interleaved
        both = rows(width * 2)
        grey = None if both is None else both[:, ::2]
    elif fmt in DEEP:
        wide = rows(width * 2)
        grey = None
        if wide is not None:
            values = np.ascontiguousarray(wide).view("<u2")
            if black:
                values = np.where(values > black, values - black, 0)
            grey = np.clip(values >> DEEP[fmt], 0, 255).astype(np.uint8)
    elif fmt in PACKED:
        offset, conversion = PACKED[fmt]
        both = rows(width * 2)
        if both is None:
            return None
        if color:
            return cv2.cvtColor(np.ascontiguousarray(both).reshape(height, width, 2), conversion)
        return np.ascontiguousarray(both[:, offset::2])
    elif fmt in PLANAR:
        if color:
            if stride not in (0, width):
                return None
            size = width * height * 3 // 2
            if data.size < size:
                return None
            return cv2.cvtColor(data[:size].reshape(height * 3 // 2, width), PLANAR[fmt])
        grey = rows(width)
    elif fmt in RGB:
        flat = rows(width * 3)
        if flat is None:
            return None
        bgr = np.ascontiguousarray(flat).reshape(height, width, 3)
        if RGB[fmt] is not None:
            bgr = cv2.cvtColor(bgr, RGB[fmt])
        return bgr if color else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        raise CameraError(f"unsupported pixel format {fmt!r}")
    if grey is None:
        return None
    grey = np.ascontiguousarray(grey)
    return cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR) if color else grey.copy()


def choose(modes: list[Mode], color: bool) -> Mode | None:
    """The mode to stream in: enough pixels for a face, a usable frame rate."""
    target = 960 * 540 if color else 640 * 400
    rank = ["MJPG", "JPEG", "NV12", "YUYV"] if color else ["GREY", "Y8I ", "Y16 ", "Y12 ", "Y10 "]

    def cost(m: Mode) -> float:
        size = abs(np.log(m.width * m.height / target))
        small = 2.0 if min(m.width, m.height) < 200 else 0.0
        slow = max(0.0, (24 if color else 10) - (m.fps or 30)) * 0.25
        return size + small + slow + 0.05 * (rank.index(m.format) if m.format in rank else 8)

    usable = [m for m in modes if m.format in SUPPORTED]
    return min(usable, key=cost) if usable else None


_WHY = {errno.EBUSY: "it is in use by another program",
        errno.ENOSPC: "there is not enough USB bandwidth",
        errno.ENODEV: "it was unplugged", errno.EACCES: "permission denied",
        errno.EPERM: "permission denied", errno.EIO: "the camera reported an error"}


class Stream:
    BUFFERS = 4
    STALL = 6.0        # some cameras need seconds for their first frame

    def __init__(self, path: str, mode: Mode, exposure: int = -1, black: int = 0):
        self.path, self.fd = path, -1
        self.exposure, self.black = exposure, black
        self._maps: list[mmap.mmap] = []
        self._on = False
        try:
            self._open(mode)
        except OSError as e:
            self.close()
            self.errno = e.errno
            error = CameraError(f"cannot use {path}: {_WHY.get(e.errno, e.strerror)}")
            error.errno = e.errno
            raise error from None
        except BaseException:
            self.close()
            raise
        self.last = time.monotonic()

    def _open(self, mode: Mode) -> None:
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        fmt = _Format(type=TYPE_CAPTURE)
        fmt.fmt.pix.width, fmt.fmt.pix.height = mode.width, mode.height
        fmt.fmt.pix.pixelformat = code_of(mode.format)
        fmt.fmt.pix.field = 1
        fcntl.ioctl(self.fd, S_FMT, fmt)
        pix = fmt.fmt.pix
        self.format = fourcc(pix.pixelformat)
        self.width, self.height, self.stride = pix.width, pix.height, pix.bytesperline
        if self.format not in SUPPORTED:
            raise CameraError(f"{self.path} insists on pixel format {self.format!r}")
        if mode.fps:
            parm = _StreamParm(type=TYPE_CAPTURE, numerator=1, denominator=round(mode.fps))
            try:
                fcntl.ioctl(self.fd, S_PARM, parm)
            except OSError:
                pass        # fixed rate
        req = _ReqBufs(count=self.BUFFERS, type=TYPE_CAPTURE, memory=MEMORY_MMAP)
        fcntl.ioctl(self.fd, REQBUFS, req)
        for i in range(req.count):
            buf = _Buffer(index=i, type=TYPE_CAPTURE, memory=MEMORY_MMAP)
            fcntl.ioctl(self.fd, QUERYBUF, buf)
            self._maps.append(mmap.mmap(self.fd, buf.length, mmap.MAP_SHARED,
                                        mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset))
            fcntl.ioctl(self.fd, QBUF, buf)
        fcntl.ioctl(self.fd, STREAMON, c_int32(TYPE_CAPTURE))
        self._on = True
        if self.exposure >= 0:          # some cameras only take it while streaming
            self._set(CID_EXPOSURE_AUTO, EXPOSURE_MANUAL)
            if not self._set(CID_EXPOSURE_ABSOLUTE, self.exposure):
                self._set(CID_EXPOSURE, self.exposure)

    def _set(self, control: int, value: int) -> bool:
        try:
            fcntl.ioctl(self.fd, S_CTRL, _Control(control, value))
            return True
        except OSError:
            return False

    def read(self, timeout: float = 0.25, color: bool = False) -> np.ndarray | None:
        """Next frame, or None if none came in time."""
        if not select.select([self.fd], [], [], timeout)[0]:
            if time.monotonic() - self.last > self.STALL:
                raise CameraError(f"{self.path} stopped delivering frames")
            return None
        buf = _Buffer(type=TYPE_CAPTURE, memory=MEMORY_MMAP)
        try:
            fcntl.ioctl(self.fd, DQBUF, buf)
        except BlockingIOError:
            return None
        except OSError as e:
            raise CameraError(f"cannot use {self.path}: {_WHY.get(e.errno, e.strerror)}") from None
        try:
            if buf.flags & BUF_FLAG_ERROR or not buf.bytesused:
                return None
            view = np.frombuffer(self._maps[buf.index], np.uint8, buf.bytesused)
            # decode() may return a view of the buffer the driver is about to reuse.
            image = decode(view.copy(), self.format, self.width, self.height, self.stride, color,
                           self.black)
            del view
        finally:
            fcntl.ioctl(self.fd, QBUF, buf)
        if image is not None:
            self.last = time.monotonic()
        return image

    def close(self) -> None:
        if self.fd < 0:
            return
        if self._on:
            try:
                fcntl.ioctl(self.fd, STREAMOFF, c_int32(TYPE_CAPTURE))
            except OSError:
                pass
            self._on = False
        for m in self._maps:
            try:
                m.close()
            except BufferError:
                pass
        self._maps.clear()
        os.close(self.fd)
        self.fd = -1
