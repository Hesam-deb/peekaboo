"""Authentication and enrolment loops. No I/O except the observation stream."""
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np

from .camera import Observation
from .config import Config
from .store import Gallery
from .vision import Face

Notify = Callable[[str, dict], None]
Cancelled = Callable[[], bool]
Latest = Callable[[], "tuple[np.ndarray, float] | None"]


class Aborted(Exception):
    """The client went away."""


# Facing the camera: allowed deviation from the pose seen at enrolment.
FACING = 0.20
# A frame this far below the threshold means the face was swapped mid-gesture.
IDENTITY_SLACK = 0.15
# The same face in infrared and in colour scores about 0.75; strangers stay below 0.3.
CROSS_CAMERA = 0.45

MESSAGES = {
    "ok": "Peekaboo!",
    "no_face": "No face found",
    "mismatch": "Face not recognised",
    "spoof": "No infrared response from this face",
    "attention": "Look at the camera with your eyes open",
    "gesture": "Head movement not seen",
    "unclear": "Could not see your face clearly",
    "timeout": "Timed out",
}
COUNTED = {"mismatch", "spoof"}   # outcomes that count towards lockout


@dataclass
class Result:
    code: str
    score: float = -1.0
    face: dict | None = None
    embedding: np.ndarray | None = None
    seconds: float = 0.0
    detail: str = ""      # what happened to the frames, for the log

    @property
    def ok(self) -> bool:
        return self.code == "ok"

    @property
    def message(self) -> str:
        return MESSAGES[self.code]


class GestureTracker:
    """Detects a head turn through nose parallax (see vision.pose_of)."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.recent: deque[np.ndarray] = deque(maxlen=3)
        self.low = np.full(2, np.inf)
        self.high = np.full(2, -np.inf)

    def reset(self) -> None:
        self.__init__(self.threshold)

    def push(self, pose: np.ndarray) -> bool:
        self.recent.append(pose)
        if len(self.recent) == 3:           # median of 3 rejects landmark glitches
            smooth = np.median(np.array(self.recent), axis=0)
            self.low = np.minimum(self.low, smooth)
            self.high = np.maximum(self.high, smooth)
        return self.done

    @property
    def progress(self) -> float:
        span = float(np.max(self.high - self.low))
        return min(max(span, 0.0) / self.threshold, 1.0) if math.isfinite(span) else 0.0

    @property
    def done(self) -> bool:
        return self.progress >= 1.0


def look(face: Face, baseline: np.ndarray) -> dict:
    """Where the person is looking, as a mirror would show it.

    yaw, pitch and the position are -1..1 with positive to the right and down on
    the screen. The camera sees the person's left on the right of its image.
    """
    def unit(value: float) -> float:
        return round(float(min(max(value, -1.0), 1.0)), 2)

    yaw, pitch = face.pose - baseline
    return {"yaw": unit(-yaw / 0.30), "pitch": unit(pitch / 0.22),
            "x": unit(1 - 2 * face.center[0]), "y": unit(2 * face.center[1] - 1),
            "open": round(face.eyes_open, 2)}


def authenticate(observations: Iterable[Observation | None], vision, gallery: Gallery,
                 cfg: Config, *, gesture: bool, notify: Notify = lambda *_: None,
                 cancelled: Cancelled = lambda: False,
                 clock: Callable[[], float] = time.monotonic) -> Result:
    start = clock()
    limit = cfg.gesture_timeout if gesture else cfg.timeout
    seen = no_response = mismatched = unattended = unclear = missing = 0
    good: list[tuple[float, dict, Face]] = []
    best = -1.0
    tracker = GestureTracker(cfg.gesture_threshold)
    baseline = np.array(gallery.faces[0]["pose"])
    prompted = False

    def finish(code: str, **kw) -> Result:
        detail = (f"seen={seen} unclear={unclear} unattended={unattended} "
                  f"mismatched={mismatched} no_response={no_response}")
        return Result(code, score=best, seconds=clock() - start, detail=detail, **kw)

    for obs in observations:
        if cancelled():
            raise Aborted
        elapsed = clock() - start
        if elapsed > limit or (not seen and elapsed > cfg.no_face_timeout):
            break
        if obs is None:
            continue
        face, _ = vision.analyse(obs.image, obs.lit)
        if face is None:
            # Losing the face restarts the gesture: otherwise swapping a frontal
            # photo for a turned one would pass as a head movement.
            missing += 1
            if missing >= 2:
                tracker.reset()
                good.clear()
            # A face that is visible but returns no emitter light is a display.
            saturated = float((obs.lit >= 250).mean()) > 0.25
            if obs.strobed and not saturated and vision.has_near_face(obs.lit):
                no_response += 1
                seen += 1
                if no_response >= cfg.max_mismatch_frames and not good:
                    return finish("spoof")
            continue
        missing = 0
        seen += 1
        notify("track", {**look(face, baseline),
                         "progress": round(tracker.progress, 2) if gesture else 0.0})
        if face.problem:
            unclear += 1
            notify("hint", {"text": face.problem})
            continue

        score, owner = gallery.match(face.embedding)
        best = max(best, score)
        notify("frame", {"score": round(score, 3), "eyes": round(face.eyes_open, 2),
                         "pose": face.pose.round(3).tolist(), "light": round(face.light)})
        if score < cfg.threshold:
            # Turning the head lowers the score, so tolerate that mid-gesture.
            if score < cfg.threshold - IDENTITY_SLACK or not good:
                mismatched += 1
                good.clear()
                tracker.reset()
                if mismatched >= cfg.max_mismatch_frames:
                    return finish("mismatch")
            continue
        tracker.push(face.pose)

        facing = np.max(np.abs(face.pose - np.array(owner["pose"]))) <= FACING
        if cfg.require_attention and not (facing and face.eyes_open >= 0.5):
            unattended += 1
        else:
            good.append((score, owner, face))
        if len(good) < cfg.min_frames:
            continue
        if gesture and not tracker.done:
            if not prompted:
                notify("hint", {"text": "Turn your head slightly to confirm"})
                prompted = True
            continue
        score, owner, winner = max(good, key=lambda g: g[0])
        return finish("ok", face=owner, embedding=winner.embedding)

    if not seen:
        return finish("no_face")
    if len(good) >= cfg.min_frames and gesture:
        return finish("gesture")
    if unattended and unattended >= mismatched:
        return finish("attention")
    if mismatched >= 3:
        return finish("mismatch")
    if no_response > seen / 2 and no_response >= 3:
        return finish("spoof")
    return finish("unclear" if unclear else "timeout")


SECTORS = 8


@dataclass
class Enrolment:
    """Collects a frontal set, then one view per direction around the circle.

    With a second camera every view is taken by both at the same moment, and the
    two must agree that it is one face. Only the infrared views are kept.
    """
    FRONTAL = 4
    SPAN = np.array([0.15, 0.11])   # pose shift that counts as a full tilt
    SAME_PERSON = 0.40
    RELAX_AFTER = 30.0              # limited neck mobility: accept fewer directions
    RELAX_SECTORS = 4
    MIN_CHECKS = 3

    frontal: list[Face] = field(default_factory=list)
    sectors: dict[int, np.ndarray] = field(default_factory=dict)
    checks: int = 0                 # views the colour camera agreed on
    baseline: np.ndarray | None = None
    centre: np.ndarray | None = None
    strobed: bool = True
    disagreed: int = 0

    @property
    def phase(self) -> str:
        return "frontal" if self.baseline is None else "ring"

    def sector_of(self, pose: np.ndarray) -> int | None:
        """Direction as seen in a mirrored preview; 0 = right, clockwise."""
        dx, dy = (pose - self.baseline) / self.SPAN
        if math.hypot(dx, dy) < 1.0:
            return None
        angle = math.atan2(dy, -dx)
        return round(angle / (2 * math.pi / SECTORS)) % SECTORS

    def push(self, face: Face, strobed: bool, color: Face | None = None) -> str | None:
        """Consume one view. Returns a hint for the user."""
        if face.problem:
            return face.problem
        if color is not None:
            if float(face.embedding @ color.embedding) < CROSS_CAMERA:
                # Two different faces, or a picture that only one camera sees.
                self.disagreed += 1
                return "Keep one face in view of both cameras"
        if self.phase == "frontal":
            if face.eyes_open < 0.5:
                return "Keep your eyes open"
            if abs(face.pose[0]) > 0.15:
                return "Look straight at the camera"
            if self.frontal and np.max(np.abs(face.pose - self.frontal[-1].pose)) > 0.08:
                self._restart()
                return "Hold still"
            if self.frontal and float(face.embedding @ self.frontal[0].embedding) < 0.6:
                self._restart()
            self.frontal.append(face)
            self.strobed &= strobed
            self.checks += color is not None
            if len(self.frontal) >= self.FRONTAL:
                self.baseline = np.median([f.pose for f in self.frontal], axis=0)
                mean = np.mean([f.embedding for f in self.frontal], axis=0)
                self.centre = mean / np.linalg.norm(mean)
            return "Hold still"
        if float(face.embedding @ self.centre) < self.SAME_PERSON:
            return "Keep the same face in view"
        sector = self.sector_of(face.pose)
        if sector is not None and sector not in self.sectors:
            self.sectors[sector] = face.embedding
            self.strobed &= strobed
            self.checks += color is not None
        return "Slowly move your head to complete the circle"

    def _restart(self) -> None:
        self.frontal.clear()
        self.checks = 0

    def push_color(self, color: Face) -> bool:
        """A colour view taken after the infrared ones (cameras that cannot run together)."""
        if float(color.embedding @ self.centre) >= CROSS_CAMERA:
            self.checks += 1
        else:
            self.disagreed += 1
        return self.checked

    def complete(self, elapsed: float) -> bool:
        if len(self.sectors) == SECTORS:
            return True
        return elapsed > self.RELAX_AFTER and len(self.sectors) >= self.RELAX_SECTORS

    @property
    def embeddings(self) -> list[np.ndarray]:
        return [f.embedding for f in self.frontal] + list(self.sectors.values())

    @property
    def checked(self) -> bool:
        """Whether the colour camera confirmed enough of the views."""
        return self.checks >= self.MIN_CHECKS


def enroll(observations: Iterable[Observation | None], vision, *,
           notify: Notify, cancelled: Cancelled, timeout: float = 90.0,
           latest: Latest | None = None,
           preview: Callable[..., None] = lambda *_: None,
           try_rotation: Callable[[], None] | None = None,
           clock: Callable[[], float] = time.monotonic) -> Enrolment | None:
    start, ring_start = clock(), None
    session, blank = Enrolment(), start
    for obs in observations:
        if cancelled():
            raise Aborted
        now = clock()
        if now - start > timeout:
            return None
        if obs is None:
            continue
        sample = latest() if latest else None
        frame = sample[0] if sample else None
        face, count = vision.analyse(obs.image, obs.lit)
        color = None
        if frame is not None and face is not None:
            color, _ = vision.analyse(frame, frame)
        preview(obs.image, frame)
        if face is None:
            hint = "Position your face in the frame"
            # Sensors mounted sideways: nothing found for a while, try turning the image.
            if try_rotation and session.phase == "frontal" and now - blank > 2.5:
                try_rotation()
                blank = now
        else:
            blank = now
            hint = "Only one face, please" if count > 1 else \
                session.push(face, obs.strobed, color)
        if session.phase == "ring" and ring_start is None:
            ring_start = now
        notify("enroll", {
            "phase": session.phase, "hint": hint, "sectors": sorted(session.sectors),
            "color": None if latest is None else color is not None,
            **({"look": look(face, session.baseline if session.baseline is not None
                             else face.pose)} if face is not None else {})})
        if ring_start is not None and session.complete(now - ring_start):
            return session
    return None


def check_color(latest: Latest, vision, session: Enrolment, *, notify: Notify,
                 cancelled: Cancelled, timeout: float = 12.0,
                 preview: Callable[..., None] = lambda *_: None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """The colour camera's agreement after the fact."""
    start, seen_at = clock(), 0.0
    while clock() - start < timeout:
        if cancelled():
            raise Aborted
        sample = latest()
        if sample is None or sample[1] == seen_at:
            sleep(0.03)
            continue
        frame, seen_at = sample
        face, _ = vision.analyse(frame, frame)
        preview(None, frame)
        notify("enroll", {"phase": "color", "hint": "Look straight at the camera",
                          "sectors": sorted(session.sectors), "color": face is not None})
        if face is not None and session.push_color(face):
            return True
    return False
