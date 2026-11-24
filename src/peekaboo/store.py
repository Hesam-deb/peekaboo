"""Encrypted template storage. Only embeddings are kept, never images."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .vision import MODEL_ID

MAX_FACES = 5
MAX_ADAPTIVE = 5


class StoreError(Exception):
    pass


def load_key(dev_dir: Path | None = None) -> bytes:
    """systemd hands us the key, unsealed from the TPM, as a credential."""
    if dev_dir:
        path = dev_dir / "dev.key"
        if not path.exists():
            dev_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
    else:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not directory:
            raise StoreError("no template key: not started by systemd?")
        path = Path(directory) / "peekaboo.key"
    key = path.read_bytes()
    if len(key) != 32:
        raise StoreError("template key has the wrong size")
    return key


class Store:
    def __init__(self, directory: Path, key: bytes):
        self.dir = directory
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._aead = AESGCM(key)

    def _file(self, uid: int) -> Path:
        return self.dir / f"{uid}.face"

    @staticmethod
    def _aad(uid: int, name: str = "peekaboo:v2") -> bytes:
        # Binds the file to its owner: renaming it to another uid fails to decrypt.
        return f"{name}:{uid}".encode()

    def load(self, uid: int) -> list[dict]:
        try:
            blob = self._file(uid).read_bytes()
        except FileNotFoundError:
            return []
        for aad in (self._aad(uid), self._aad(uid, "faceid:v1")):     # pre-1.0 files
            try:
                faces = json.loads(self._aead.decrypt(blob[:12], blob[12:], aad))
                break
            except (InvalidTag, ValueError):
                continue
        else:
            raise StoreError("stored faces cannot be decrypted; enroll again")
        for face in faces:
            face.setdefault("camera", None)
            face.setdefault("rotate", 0)
            face.setdefault("checked", False)
        return [f for f in faces if f["model"] == MODEL_ID]

    def save(self, uid: int, faces: list[dict]) -> None:
        path = self._file(uid)
        if not faces:
            path.unlink(missing_ok=True)
            return
        nonce = secrets.token_bytes(12)
        blob = nonce + self._aead.encrypt(nonce, json.dumps(faces).encode(), self._aad(uid))
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def uids(self) -> list[int]:
        return [int(p.stem) for p in self.dir.glob("*.face") if p.stem.isdigit()]

    def add(self, uid: int, label: str, embeddings: list[np.ndarray],
            pose: np.ndarray, strobed: bool, camera: str | None = None, rotate: int = 0,
            checked: bool = False) -> dict:
        """Only infrared embeddings are kept. checked: the colour camera agreed."""
        faces = self.load(uid)
        if len(faces) >= MAX_FACES:
            raise StoreError(f"at most {MAX_FACES} faces; remove one first")
        face = {"id": max((f["id"] for f in faces), default=0) + 1,
                "label": label, "created": int(time.time()), "model": MODEL_ID,
                "strobed": strobed, "pose": pose.tolist(), "camera": camera, "rotate": rotate,
                "checked": checked,
                "embeddings": [e.tolist() for e in embeddings],
                "adaptive": [], "adapted_at": 0}
        self.save(uid, faces + [face])
        return face

    def remove(self, uid: int, face_id: int) -> bool:
        faces = self.load(uid)
        kept = [f for f in faces if f["id"] != face_id]
        self.save(uid, kept)
        return len(kept) != len(faces)

    def adapt(self, uid: int, face_id: int, embedding: np.ndarray) -> None:
        """Follow slow change (beard, glasses). At most hourly, newest few kept."""
        faces = self.load(uid)
        for face in faces:
            if face["id"] == face_id and time.time() - face["adapted_at"] > 3600:
                face["adaptive"] = (face["adaptive"] + [embedding.tolist()])[-MAX_ADAPTIVE:]
                face["adapted_at"] = int(time.time())
                self.save(uid, faces)


def _rows(vectors: list) -> np.ndarray:
    return np.array(vectors, dtype=np.float32).reshape(len(vectors), -1) if vectors \
        else np.zeros((0, 128), dtype=np.float32)


class Gallery:
    """One user's templates in matchable form."""
    ADAPT_ANCHOR = 0.50   # adaptive templates must stay close to enrolment

    def __init__(self, faces: list[dict]):
        self.faces = faces
        rows, owners, anchors = [], [], []
        for i, face in enumerate(faces):
            for key in ("embeddings", "adaptive"):
                for vector in face[key]:
                    rows.append(vector)
                    owners.append(i)
                    anchors.append(key == "embeddings")
        self.matrix = _rows(rows)
        self.owners = np.array(owners, dtype=int)
        self.anchors = np.array(anchors, dtype=bool)

    def __len__(self) -> int:
        return len(self.faces)

    @property
    def strobed(self) -> bool:
        return all(f["strobed"] for f in self.faces)

    def match(self, embedding: np.ndarray) -> tuple[float, dict | None]:
        if not len(self.matrix):
            return -1.0, None
        scores = self.matrix @ embedding
        best = int(np.argmax(scores))
        return float(scores[best]), self.faces[self.owners[best]]

    def anchor_score(self, embedding: np.ndarray, face: dict) -> float:
        mask = self.anchors & (self.owners == self.faces.index(face))
        return float((self.matrix[mask] @ embedding).max())
