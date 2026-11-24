"""Detection, recognition and per-frame measurements."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Pinned: a swapped model file could accept anyone.
MODELS = {
    "yunet.onnx": ("8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
                   "https://huggingface.co/opencv/face_detection_yunet/resolve/main/"
                   "face_detection_yunet_2023mar.onnx"),
    "sface.onnx": ("0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
                   "https://huggingface.co/opencv/face_recognition_sface/resolve/main/"
                   "face_recognition_sface_2021dec.onnx"),
    "eye.onnx": ("4daa100034482525a26c9afb9297c16580a531189e66e3d2b2ac7d32becfd593",
                 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/public/"
                 "2022.1/open-closed-eye-0001/open_closed_eye.onnx"),
}
MODEL_ID = "sface-2021dec/strobe-diff"

MIN_FACE = 70          # px; smaller faces are too coarse to trust
MIN_LIGHT = 25.0       # mean emitter response on the face
MAX_SATURATED = 0.30


@dataclass
class Face:
    box: tuple[int, int, int, int]
    score: float
    embedding: np.ndarray     # unit length
    pose: np.ndarray          # (a, b), see pose_of()
    eyes_open: float          # 0..1
    light: float
    saturated: float
    center: tuple[float, float] = (0.5, 0.5)      # in the frame, 0..1

    @property
    def problem(self) -> str | None:
        if self.box[2] < MIN_FACE:
            return "Move closer"
        if self.saturated > MAX_SATURATED:
            return "Move back a little"
        if self.light < MIN_LIGHT:
            return "Too dark, move closer"
        return None


def pose_of(points: np.ndarray) -> np.ndarray:
    """Nose position in the affine frame spanned by the eyes and the mouth.

    These coordinates do not change when a flat object (a photo) is moved, scaled
    or tilted. On a real head the nose stands off that plane, so they shift as
    the head turns. That makes them a pose estimate and a flatness test at once.
    """
    right_eye, left_eye, nose, right_mouth, left_mouth = points
    eyes = (right_eye + left_eye) / 2
    basis = np.stack([left_eye - right_eye, (right_mouth + left_mouth) / 2 - eyes], axis=1)
    if abs(np.linalg.det(basis)) < 1e-3:
        return np.zeros(2)
    return np.linalg.solve(basis, nose - eyes)


def verify_models(directory: Path) -> None:
    for name, (digest, _) in MODELS.items():
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(f"missing model {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"checksum mismatch for {path}")


class Vision:
    def __init__(self, directory: Path):
        verify_models(directory)
        cv2.setNumThreads(4)
        self._size = (640, 360)
        self.detector = cv2.FaceDetectorYN.create(
            str(directory / "yunet.onnx"), "", self._size, 0.75)
        self.recogniser = cv2.FaceRecognizerSF.create(str(directory / "sface.onnx"), "")
        self.eyes = cv2.dnn.readNetFromONNX(str(directory / "eye.onnx"))
        self._lock = threading.Lock()      # OpenCV networks are not thread safe

    def _detect(self, image: np.ndarray):
        """Faces in a grey or BGR image. Call with the lock held."""
        size = (image.shape[1], image.shape[0])
        if size != self._size:
            self.detector.setInputSize(size)
            self._size = size
        bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image
        _, found = self.detector.detect(bgr)
        return bgr, [] if found is None else found

    def has_near_face(self, image: np.ndarray) -> bool:
        with self._lock:
            return any(r[2] >= MIN_FACE for r in self._detect(image)[1])

    def analyse(self, image: np.ndarray, lit: np.ndarray) -> tuple[Face | None, int]:
        """Measure the largest face. Also returns how many were found.

        Works on infrared observations and, with lit=image, on colour frames.
        """
        with self._lock:
            return self._analyse(image, lit)

    def _analyse(self, image: np.ndarray, lit: np.ndarray) -> tuple[Face | None, int]:
        bgr, found = self._detect(image)
        if len(found) == 0:
            return None, 0
        row = max(found, key=lambda r: r[2] * r[3])
        x, y, w, h = (int(v) for v in row[:4])
        # Central part of the box: ears and background would skew brightness.
        ys = slice(max(y + h // 6, 0), max(y + h - h // 6, 1))
        xs = slice(max(x + w // 6, 0), max(x + w - w // 6, 1))
        core, raw = image[ys, xs], lit[ys, xs]
        if core.size == 0:
            return None, len(found)
        points = row[4:14].reshape(5, 2).astype(np.float64)
        vector = self.recogniser.feature(self.recogniser.alignCrop(bgr, row)).ravel()
        vector = vector / max(float(np.linalg.norm(vector)), 1e-9)
        return Face((x, y, w, h), float(row[14]), vector, pose_of(points),
                    self._eyes_open(bgr, points), float(core.mean()),
                    float((raw >= 250).mean()),
                    ((x + w / 2) / image.shape[1], (y + h / 2) / image.shape[0])), len(found)

    def _eyes_open(self, bgr: np.ndarray, points: np.ndarray) -> float:
        half = max(int(np.linalg.norm(points[1] - points[0]) * .35), 8)
        total = 0.0
        for cx, cy in points[:2].astype(int):
            patch = bgr[max(cy - half, 0):cy + half, max(cx - half, 0):cx + half]
            if patch.size == 0:
                return 0.0
            patch = (cv2.resize(patch, (32, 32)).astype(np.float32) - 127) / 255
            self.eyes.setInput(patch.transpose(2, 0, 1)[None])
            total += float(self.eyes.forward().ravel()[1])
        return total / 2
