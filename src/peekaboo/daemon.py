"""peekabood: owns the cameras, the models and the templates.

Protocol: the client sends one JSON line, the daemon answers with lines of
"KIND payload". A request ends with OK or FAIL. Hanging up cancels it.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import pwd
import re
import select
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager

import cv2
import numpy as np

from . import __version__, config, engine
from .camera import (CameraError, CameraInfo, ColorFeed, IRCamera, discover, pick_color,
                     pick_ir, probe)
from .config import LEVELS, USES, Config
from .policy import History, Logind, Policy, Settings, lid_closed, use_of
from .store import Gallery, Store, StoreError, load_key
from .v4l2 import Mode
from .vision import Vision

log = logging.getLogger("peekabood")
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,63}\$?")
LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
POLKIT = "io.github.hesam-deb.peekaboo."


class Refuse(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Client:
    """One connection."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.pid, self.uid, self.gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        self.gone = False
        self._lock = threading.Lock()

    def read_request(self) -> dict:
        self.sock.settimeout(5)
        data = b""
        while not data.endswith(b"\n"):
            chunk = self.sock.recv(4096)
            if not chunk or len(data) > 4096:
                raise Refuse("bad_request", "Malformed request")
            data += chunk
        request = json.loads(data)
        if not isinstance(request, dict):
            raise Refuse("bad_request", "Malformed request")
        return request

    def send(self, kind: str, payload: str = "") -> None:
        with self._lock:
            if self.gone:
                return
            try:
                self.sock.sendall(f"{kind} {payload}".rstrip().encode() + b"\n")
            except OSError:
                self.gone = True

    def cancelled(self) -> bool:
        if not self.gone and select.select([self.sock], [], [], 0)[0]:
            try:
                self.gone = not self.sock.recv(64)
            except OSError:
                self.gone = True
        return self.gone


class Status:
    """Feeds the animation: what is going on and where the person is looking.

    A file rather than a subscription: the shell keeps no connection, and a watcher
    cannot keep the daemon alive. Readable by all, so it holds no scores or names.
    """

    def __init__(self, uid: int, use: str | None):
        self.path = config.RUN_DIR / f"{uid}.state"
        self.use = use if use in ("lock", "shell", "system") else None
        self.state = {"state": "idle", "text": "", "turn": False, "look": None, "progress": 0.0}

    def set(self, state: str | None = None, **changes) -> None:
        if state is not None:
            changes["state"] = state
        new = {**self.state, **changes}
        if self.use is None or new == self.state:
            return
        self.state = new
        tmp = self.path.with_suffix(".state.tmp")
        try:
            with open(tmp, "w") as f:
                os.fchmod(f.fileno(), 0o644)
                json.dump({**new, "use": self.use, "time": time.monotonic_ns() // 1000}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot write status: %s", e)     # decoration: never fail a login


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(config.STATE_DIR, load_key(config.STATE_DIR if config.DEV else None))
        config.RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.policy = Policy(config.RUN_DIR, cfg)
        self.settings = Settings(config.STATE_DIR, cfg)
        self.history = History(config.STATE_DIR)
        self.logind = Logind()
        self.camera_lock = threading.Lock()
        self.store_lock = threading.Lock()
        self.password_only: set[int] = set()   # uids with a polkit prompt of ours open
        self.active = 0
        self.last_used = time.monotonic()
        self._count = threading.Lock()
        self._vision: Vision | None = None
        self._vision_lock = threading.Lock()

    @property
    def vision(self) -> Vision:
        with self._vision_lock:
            if self._vision is None:
                self._vision = Vision(config.MODEL_DIR)
            return self._vision

    def warm(self) -> None:
        try:
            self.vision
        except Exception:
            log.exception("cannot load models")

    # ---- connection handling -------------------------------------------------

    def handle(self, sock: socket.socket) -> None:
        with self._count:
            self.active += 1
        client = None
        try:
            client = Client(sock)
            request = client.read_request()
            handler = getattr(self, "op_" + str(request.get("op")), None)
            if handler is None:
                raise Refuse("bad_request", "Unknown operation")
            handler(client, request)
        except Refuse as e:
            client.send("FAIL", f"{e.code} {e}")
        except (CameraError, StoreError) as e:
            log.warning("%s", e)
            client.send("FAIL", f"unavailable {e}")
        except engine.Aborted:
            log.info("client hung up")
        except (OSError, ValueError) as e:
            log.info("dropped connection: %s", e)
            if client:
                client.send("FAIL", "bad_request Malformed request")
        except Exception:
            log.exception("request failed")
            if client:
                client.send("FAIL", "internal Internal error")
        finally:
            sock.close()
            with self._count:
                self.active -= 1
                self.last_used = time.monotonic()

    def target(self, client: Client, request: dict) -> pwd.struct_passwd:
        """The account a request is about; only root may name someone else's."""
        name = request.get("user")
        try:
            if name is None:
                account = pwd.getpwuid(client.uid)
            elif isinstance(name, str) and NAME.fullmatch(name):
                account = pwd.getpwnam(name)
            else:
                raise Refuse("bad_request", "Invalid user name")
        except KeyError:
            raise Refuse("unknown_user", "No such user") from None
        if client.uid not in (0, account.pw_uid):
            raise Refuse("denied", "Not allowed")
        return account

    def at_laptop(self, client: Client, uid: int) -> None:
        """Looking through the camera is for the person in front of it: over ssh it
        would tell whether anybody is there."""
        if client.uid != 0 and (reason := self.logind.refuse(client.sock.fileno(), uid)):
            log.warning("refused camera to uid %d: %s", client.uid, reason)
            raise Refuse("denied", "Peekaboo only works at the laptop")

    # ---- cameras -------------------------------------------------------------

    @contextmanager
    def cameras(self, client: Client):
        """Exclusive use of the cameras."""
        deadline = time.monotonic() + 3
        while not self.camera_lock.acquire(timeout=0.1):
            if client.cancelled():
                raise engine.Aborted
            if time.monotonic() > deadline:
                raise Refuse("busy", "The camera is in use")
        try:
            yield
        finally:
            self.camera_lock.release()

    def open_ir(self, info: CameraInfo, rotate: int = 0) -> IRCamera:
        mode = None if self.cfg.mode == "auto" else Mode(*config.parse_mode(self.cfg.mode))
        return IRCamera(info, self.cfg.emitter, self.cfg.emitter_control, rotate, mode,
                        self.cfg.exposure, self.cfg.black_level)

    def usable_faces(self, faces: list[dict], cameras: list[CameraInfo]
                     ) -> tuple[CameraInfo, list[dict]]:
        """The camera to look through, and the faces enrolled with it.

        Enrolments are tied to their camera: plugging in another one that plays back
        a face must not get anybody in.
        """
        error = None
        for wanted in dict.fromkeys(f["camera"] for f in faces):
            try:
                info = pick_ir(cameras, wanted, self.cfg.device)
            except CameraError as e:
                error = error or e
                continue
            if wanted is None and info.external:
                error = error or CameraError("no built-in infrared camera found")
                continue
            return info, [f for f in faces if f["camera"] in (wanted, None)
                          and f["rotate"] == next(g["rotate"] for g in faces
                                                  if g["camera"] == wanted)]
        raise error or CameraError("no infrared camera found")

    # ---- operations ----------------------------------------------------------

    def op_ping(self, client: Client, request: dict) -> None:
        client.send("OK", __version__)

    def op_status(self, client: Client, request: dict) -> None:
        account = self.target(client, request)
        uid = account.pw_uid
        cameras = discover()
        try:
            ir = pick_ir([c for c in cameras if c.kind != "maybe-ir"], None, self.cfg.device)
        except CameraError:
            ir = next((c for c in cameras if c.kind == "maybe-ir"), None)
        color = pick_color(cameras, ir, self.cfg.color_device) if ir else None
        blocked = self.policy.blocked(uid)
        client.send("OK", json.dumps({
            "version": __version__, "user": account.pw_name,
            "cameras": [c.as_dict() for c in cameras],
            "ir": ir and ir.as_dict(), "color": color and color.as_dict(),
            "faces": [{"id": f["id"], "label": f["label"], "created": f["created"],
                       "strobed": f["strobed"], "camera": f["camera"],
                       "connected": f["camera"] in (None, *(c.id for c in cameras)),
                       "checked": f["checked"],
                       "views": len(f["embeddings"]) + len(f["adaptive"])}
                      for f in self.store.load(uid)],
            "blocked": blocked and blocked[1],
            "failures": self.policy.failures(uid),
            "max_failures": self.cfg.max_failures,
            "settings": self.settings.get(uid),
            "allowed_uses": self.cfg.uses,
        }))

    def op_cameras(self, client: Client, request: dict) -> None:
        """Describe every camera; with probe, also look through each."""
        found = []
        if request.get("probe"):
            self.at_laptop(client, client.uid)
        with self.cameras(client):
            for info in discover():
                entry = info.as_dict()
                if request.get("probe") and info.kind != "unsupported":
                    if client.cancelled():
                        raise engine.Aborted
                    client.send("INFO", f"Checking {info.name}")
                    try:
                        entry["probe"] = probe(info, 3.0 if info.kind != "color" else 1.0)
                    except CameraError as e:
                        entry["error"] = str(e)
                found.append(entry)
        client.send("OK", json.dumps(found))

    def op_auth(self, client: Client, request: dict) -> None:
        account = self.target(client, request)
        uid, service = account.pw_uid, str(request.get("service", ""))[:64]
        use = self.check_use(uid, service)
        if uid in self.password_only:
            raise Refuse("password", "Enter your password.")
        reason = self.logind.refuse(client.sock.fileno(), uid)
        if reason:
            log.warning("refused %s for %s: %s", service, account.pw_name, reason)
            raise Refuse("denied", "Peekaboo only works at the laptop")
        self._scan(client, account, service, use,
                   gesture=use in self.settings.get(uid)["gesture"], real=True)

    def effective_uses(self, uid: int) -> set[str]:
        return set(self.settings.get(uid)["uses"]) & set(self.cfg.uses)

    def check_use(self, uid: int, service: str) -> str:
        """The use this service falls under. "denied" keeps pam_peekaboo silent: the
        password prompt simply appears."""
        use = use_of(service, self.logind.at_greeter())
        if use is None or service in self.cfg.deny_services:
            raise Refuse("denied", f"Peekaboo does not serve {service}")
        if use not in self.effective_uses(uid):
            raise Refuse("denied", f"Peekaboo is off for: {USES[use]}")
        return use

    def op_test(self, client: Client, request: dict) -> None:
        """Same checks as op_auth with scores shown, granting nothing."""
        account = self.target(client, request)
        self.at_laptop(client, account.pw_uid)
        self._scan(client, account, "test", None,
                   gesture=bool(request.get("gesture")), real=False)

    def op_demo(self, client: Client, request: dict) -> None:
        """Head tracking without recognition, to try the animations."""
        account = self.target(client, request)
        self.at_laptop(client, account.pw_uid)
        faces = self.store.load(account.pw_uid)
        with self.cameras(client):
            info, usable = self.usable_faces(faces, discover()) if faces \
                else (pick_ir(discover(), None, self.cfg.device), [])
            baseline = np.array(usable[0]["pose"] if usable else (0.0, 0.5))
            end = time.monotonic() + 60
            with self.open_ir(info, usable[0]["rotate"] if usable else 0) as camera:
                for obs in camera.observations(allow_unstrobed=True):
                    if client.cancelled():
                        raise engine.Aborted
                    if time.monotonic() > end:
                        break
                    if obs is None:
                        continue
                    face, _ = self.vision.analyse(obs.image, obs.lit)
                    if face is not None:
                        client.send("LOOK", json.dumps(engine.look(face, baseline)))
        client.send("OK")

    def _scan(self, client: Client, account, service: str, use: str | None, *,
              gesture: bool, real: bool) -> None:
        uid = account.pw_uid
        faces = self.store.load(uid)
        if not faces:
            raise Refuse("not_enrolled", "No face enrolled")
        if real:
            blocked = self.policy.blocked(uid)
            if blocked:
                raise Refuse(*blocked)
        if self.cfg.ignore_when_lid_closed and lid_closed():
            raise Refuse("lid", "The lid is closed")

        status = Status(uid, use)
        # GNOME keeps the screen locked until every PAM message has been up for two
        # seconds, so there progress messages only cost time.
        talk = self.cfg.messages and (use not in ("lock", "login") or gesture)

        def notify(kind: str, data: dict) -> None:
            if kind == "track":
                progress = data.pop("progress")
                status.set(look=data, progress=progress)
                if not real:
                    client.send("LOOK", json.dumps({**data, "progress": progress}))
            elif kind == "hint":
                status.set(text=data["text"])
                if talk:
                    client.send("INFO", data["text"])
            elif not real:
                client.send("FRAME", json.dumps(data))

        status.set("scanning", text="Turn your head slightly" if gesture else "", turn=gesture)
        try:
            result = self._look(client, uid, faces, gesture=gesture, real=real,
                                notify=notify, talk=talk)
        except BaseException:
            status.set("idle", text="", look=None)
            raise
        if result.ok:
            status.set("ok", text="", look=None)      # before the answer, which unlocks
        else:
            status.set("fail", text=result.message, look=None)

        log.info("%s user=%s service=%s result=%s score=%.3f time=%.2fs %s",
                 "auth" if real else "test", account.pw_name, service,
                 result.code, result.score, result.seconds, result.detail)
        if real:
            self.history.add(uid, service=service, use=use, result=result.code,
                             seconds=round(result.seconds, 1))
        if not result.ok:
            raise Refuse(result.code, result.message)
        self._adapt(uid, result, real)
        client.send("OK", json.dumps({"label": result.face["label"],
                                      "score": round(result.score, 3),
                                      "seconds": round(result.seconds, 2)}))

    def _adapt(self, uid: int, result: engine.Result, real: bool) -> None:
        if not (real and self.cfg.adaptive and result.score >= self.cfg.threshold + 0.10):
            return
        gallery = Gallery([result.face])
        if gallery.anchor_score(result.embedding, result.face) >= Gallery.ADAPT_ANCHOR:
            with self.store_lock:
                self.store.adapt(uid, result.face["id"], result.embedding)

    def _look(self, client: Client, uid: int, faces: list[dict], *, gesture: bool, real: bool,
              notify, talk: bool) -> engine.Result:
        settings = self.settings.get(uid)
        params = dataclasses.replace(
            self.cfg, require_attention=settings["attention"],
            threshold=min(self.cfg.threshold + LEVELS[settings["level"]], 0.95))
        with self.cameras(client):
            # Another request may have used up the attempts while this one waited.
            blocked = self.policy.blocked(uid) if real else None
            if blocked:
                raise Refuse(*blocked)
            info, usable = self.usable_faces(faces, discover())
            gallery = Gallery(usable)
            with self.open_ir(info, usable[0]["rotate"]) as camera:
                if talk:
                    client.send("INFO", "Peekaboo: look at the camera"
                                + (" and turn your head slightly" if gesture else ""))
                # If enrolled with a strobing emitter, never accept a plain image:
                # flooding the sensor with IR must not downgrade the check.
                result = engine.authenticate(
                    camera.observations(allow_unstrobed=not gallery.strobed), self.vision,
                    gallery, params, gesture=gesture, notify=notify,
                    cancelled=client.cancelled)
            if real:
                if result.ok:
                    self.policy.note_success(uid)
                elif result.code in engine.COUNTED:
                    self.policy.note_failure(uid)
            return result

    def op_note_password(self, client: Client, request: dict) -> None:
        if client.uid != 0 and not config.DEV:
            raise Refuse("denied", "Not allowed")
        self.policy.note_password(self.target(client, request).pw_uid)
        client.send("OK")

    def op_settings(self, client: Client, request: dict) -> None:
        """Change settings. Loosening them needs the password, tightening does not."""
        account = self.target(client, request)
        uid = account.pw_uid
        try:
            changes = Settings.validate(request.get("set"))
        except ValueError as e:
            raise Refuse("bad_request", str(e)) from None
        authorized = Settings.loosens(old := self.settings.get(uid), {**old, **changes})
        if authorized:
            self.authorize(client, uid, "settings")
        with self.store_lock:
            # Another request may have tightened things since the check above.
            old = self.settings.get(uid)
            new = {**old, **changes}
            if not authorized and Settings.loosens(old, new):
                raise Refuse("busy", "The settings changed, try again")
            self.settings.save(uid, new)
        log.info("%s changed settings: %s", account.pw_name, json.dumps(changes))
        client.send("OK", json.dumps(new))

    def op_history(self, client: Client, request: dict) -> None:
        uid = self.target(client, request).pw_uid
        if request.get("clear"):
            self.history.clear(uid)
        client.send("OK", json.dumps(self.history.read(uid)))

    def authorize(self, client: Client, uid: int, action: str) -> None:
        """Ask for the password through polkit; a face may not extend its own reach."""
        if client.uid == 0 or config.DEV:
            return
        try:
            stat = open(f"/proc/{client.pid}/stat").read()
            started = stat.rpartition(")")[2].split()[19]
        except (OSError, IndexError):
            raise Refuse("denied", "Cannot identify the requesting process") from None
        self.password_only.add(uid)
        try:
            proc = subprocess.Popen(
                ["/usr/bin/pkcheck", "--action-id", POLKIT + action, "--process",
                 f"{client.pid},{started},{client.uid}", "--allow-user-interaction"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while proc.poll() is None:
                if client.cancelled():
                    proc.terminate()
                    raise engine.Aborted
                time.sleep(0.1)
        finally:
            self.password_only.discard(uid)
        if proc.returncode != 0:
            raise Refuse("denied", "Authorization was not given")

    def op_enroll(self, client: Client, request: dict) -> None:
        account = self.target(client, request)
        uid, label = account.pw_uid, request.get("label") or "Face"
        if not isinstance(label, str) or not LABEL.fullmatch(label):
            raise Refuse("bad_request", "Invalid label")
        self.authorize(client, uid, "enroll")
        show = bool(request.get("preview"))
        sent = {"IMAGE": 0.0, "COLOR": 0.0}

        def show_image(kind: str, image, gain: float) -> None:
            now = time.monotonic()
            if image is None or now - sent[kind] < 0.06:
                return
            sent[kind] = now
            scale = 360 / image.shape[0]
            frame = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            frame = cv2.flip(cv2.convertScaleAbs(frame, alpha=gain), 1)
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                client.send(kind, base64.b64encode(jpeg).decode())

        def preview(ir, color) -> None:
            if show:
                show_image("IMAGE", ir, 1.4)
                show_image("COLOR", color, 1.0)

        with self.cameras(client):
            cameras = discover()
            info = pick_ir(cameras, request.get("camera"), self.cfg.device)
            color = None if request.get("ir_only") else \
                pick_color(cameras, info, self.cfg.color_device)
            session, rotate = self._enroll(client, info, color, preview)
        if session is None:
            raise Refuse("timeout", "Enrolment timed out")
        with self.store_lock:
            for other in self.store.uids():
                if other == uid:
                    continue
                try:
                    score, _ = Gallery(self.store.load(other)).match(session.centre)
                except StoreError:
                    continue
                if score >= self.cfg.threshold:
                    raise Refuse("duplicate", "This face is too similar to another account's")
            face = self.store.add(uid, label, session.embeddings, session.baseline,
                                  session.strobed, info.id, rotate, session.checked)
        log.info("enrolled face %d for %s: %d views, colour camera agreed on %d, strobed=%s, "
                 "camera=%s rotate=%d", face["id"], account.pw_name, len(face["embeddings"]),
                 session.checks, face["strobed"], info.id, rotate)
        client.send("OK", json.dumps({"id": face["id"], "label": label,
                                      "checked": session.checked,
                                      "strobed": session.strobed, "external": info.external}))

    def _enroll(self, client: Client, info: CameraInfo, color: CameraInfo | None, preview):
        def notify(_kind: str, data: dict) -> None:
            client.send("STATE", json.dumps(data))

        rotations = [0, 90, 270, 180]
        rotate = 0 if self.cfg.rotate == "auto" else int(self.cfg.rotate)
        camera = self.open_ir(info, rotate)
        feed = None
        try:
            if color:
                try:
                    feed = ColorFeed(color, rotate)
                except CameraError as e:
                    log.info("cameras cannot run together, taking turns: %s", e)

            def try_rotation() -> None:
                nonlocal rotate
                rotate = rotations[(rotations.index(rotate) + 1) % len(rotations)]
                camera.rotate = rotate
                if feed:
                    feed.rotate = rotate

            session = engine.enroll(
                camera.observations(allow_unstrobed=True), self.vision, notify=notify,
                cancelled=client.cancelled, latest=feed.latest if feed else None,
                preview=preview,
                try_rotation=try_rotation if self.cfg.rotate == "auto" else None)
        finally:
            camera.close()
            if feed:
                feed.close()
        if session is not None and color and feed is None:      # take turns
            try:
                with ColorFeed(color, rotate) as feed:
                    engine.check_color(feed.latest, self.vision, session, notify=notify,
                                       cancelled=client.cancelled, preview=preview)
            except CameraError as e:
                log.warning("enrolling without the colour camera: %s", e)
            # Not seeing anybody (too dark) is fine, seeing somebody else is not.
            if not session.checked and session.disagreed >= session.MIN_CHECKS:
                raise Refuse("cameras", "The two cameras do not agree on your face")
        return session, rotate

    def op_remove(self, client: Client, request: dict) -> None:
        account = self.target(client, request)
        with self.store_lock:
            if request.get("all"):
                self.store.save(account.pw_uid, [])
            elif not self.store.remove(account.pw_uid, int(request.get("id", -1))):
                raise Refuse("not_found", "No such face")
        log.info("removed face(s) of %s", account.pw_name)
        client.send("OK")


def listen() -> tuple[socket.socket, bool]:
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3), True
    if not config.DEV:
        raise SystemExit("peekabood must be started through peekaboo.socket")
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    config.SOCKET.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(config.SOCKET))
    sock.listen(8)
    return sock, False


MAX_CLIENTS = 16


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if config.DEV and os.geteuid() == 0:
        raise SystemExit("PEEKABOO_DEV disables access checks; refusing to run as root")
    os.umask(0o077)
    daemon = Daemon(Config.load())
    threading.Thread(target=daemon.warm, daemon=True).start()
    sock, activated = listen()
    log.info("peekabood %s ready", __version__)
    while True:
        if not select.select([sock], [], [], 10)[0]:
            if activated and not daemon.active \
                    and time.monotonic() - daemon.last_used > daemon.cfg.idle_exit:
                return      # systemd keeps the socket and restarts us on demand
            continue
        conn, _ = sock.accept()
        if daemon.active >= MAX_CLIENTS:
            conn.close()
            continue
        threading.Thread(target=daemon.handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
