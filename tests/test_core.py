import json
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from peekaboo import engine
from peekaboo.camera import Observation, Strober
from peekaboo.config import Config
from peekaboo.engine import Enrolment, authenticate
from peekaboo.daemon import Daemon, Refuse, Status
from peekaboo.policy import History, Policy, Settings, use_of
from peekaboo.store import Gallery, Store, StoreError
from peekaboo.vision import MODEL_ID, Face, pose_of

Config_ = Config
RNG = np.random.default_rng(1)
IMG = np.full((36, 64), 80, np.uint8)
POSE = np.array([0.0, 0.5])


def unit(v):
    return v / np.linalg.norm(v)


ME = unit(RNG.normal(size=128))


def like(score):
    """A vector at a chosen cosine similarity to ME."""
    other = RNG.normal(size=128)
    other = unit(other - (other @ ME) * ME)
    return score * ME + np.sqrt(1 - score ** 2) * other


def face(score=0.9, pose=POSE, eyes=1.0, light=100.0, saturated=0.0, width=120):
    return Face((0, 0, width, width), .9, like(score), np.array(pose), eyes, light, saturated)


class FakeVision:
    def __init__(self, faces, near=False):
        self.faces, self.near = iter(faces), near

    def analyse(self, image, lit):
        f = next(self.faces)
        return f, int(f is not None)

    def has_near_face(self, image):
        return self.near


class Clock:
    def __init__(self, step=0.13):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def gallery(strobed=True):
    return Gallery([stored(strobed=strobed)])


def stored(**kw):
    return {"id": 1, "label": "Me", "pose": POSE.tolist(), "strobed": True, "camera": None,
            "rotate": 0, "checked": False, "embeddings": [ME.tolist()], "adaptive": [], **kw}


def run(faces, gesture=False, near=False, notify=lambda *_: None, **kw):
    obs = (Observation(IMG, IMG, True, 0.0) for _ in faces)
    return authenticate(obs, FakeVision(faces, near), gallery(), Config(**kw),
                        gesture=gesture, notify=notify, clock=Clock())


class Authenticate(unittest.TestCase):
    def test_accepts_me(self):
        r = run([face()] * 3)
        self.assertTrue(r.ok)
        self.assertEqual(r.face["id"], 1)

    def test_needs_min_frames(self):
        self.assertFalse(run([face()] * 2).ok)

    def test_stranger(self):
        self.assertEqual(run([face(0.2)] * 20).code, "mismatch")

    def test_just_below_threshold(self):
        self.assertFalse(run([face(0.59)] * 30).ok)

    def test_lucky_frames_do_not_accumulate(self):
        self.assertFalse(run([face(0.65), face(0.65), face(0.3)] * 10).ok)

    def test_no_face(self):
        self.assertEqual(run([None] * 60).code, "no_face")

    def test_closed_eyes(self):
        self.assertEqual(run([face(eyes=0.1)] * 60).code, "attention")

    def test_closed_eyes_allowed_when_off(self):
        self.assertTrue(run([face(eyes=0.1)] * 3, require_attention=False).ok)

    def test_looking_away(self):
        self.assertEqual(run([face(pose=[.4, .5])] * 60).code, "attention")

    def test_display_gives_no_ir_response(self):
        self.assertEqual(run([None] * 20, near=True).code, "spoof")

    def test_poor_light_is_not_a_failure(self):
        r = run([face(light=5)] * 60)
        self.assertNotIn(r.code, engine.COUNTED | {"ok"})

    def test_gesture_needs_movement(self):
        self.assertEqual(run([face()] * 90, gesture=True).code, "gesture")

    def test_gesture_ignores_landmark_noise(self):
        faces = [face(pose=POSE + RNG.normal(scale=.027, size=2)) for _ in range(90)]
        self.assertEqual(run(faces, gesture=True).code, "gesture")

    def test_gesture_head_turn(self):
        faces = [face()] * 4 + [face(0.7, pose=[x, .5]) for x in np.linspace(0, .3, 8)]
        self.assertTrue(run(faces, gesture=True).ok)

    def test_gesture_survives_score_dip(self):
        faces = [face()] * 4 + [face(0.5, pose=[.2, .5])] * 3 + [face(0.7, pose=[.25, .5])] * 4
        self.assertTrue(run(faces, gesture=True).ok)

    def test_gesture_by_swapping_photos(self):
        faces = ([face()] * 5 + [None] * 2 + [face(pose=[.3, .5])] * 5 + [None] * 2) * 8
        self.assertFalse(run(faces[:80], gesture=True).ok)

    def test_gesture_by_swapping_people(self):
        faces = [face()] * 4 + [face(.1, pose=[.3, .5])] * 3 + [face()] * 70
        self.assertFalse(run(faces, gesture=True).ok)

    def test_cancel(self):
        with self.assertRaises(engine.Aborted):
            authenticate([None], FakeVision([]), gallery(), Config(), gesture=False,
                         cancelled=lambda: True)


class Strobe(unittest.TestCase):
    def feed(self, levels, allow=False):
        s = Strober(allow)
        return [s.push(np.full((4, 4), v, np.uint8), 0.0) for v in levels]

    def test_ambient_is_subtracted(self):
        out = [o for o in self.feed([40, 100, 44, 100, 40]) if o]
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].strobed)
        self.assertEqual(int(out[0].image[0, 0]), 58)

    def test_constant_light_is_never_accepted(self):
        self.assertFalse(any(self.feed([120] * 60)))

    def test_unstrobed_camera_only_if_allowed(self):
        out = [o for o in self.feed([120] * 60, allow=True) if o]
        self.assertTrue(out and not any(o.strobed for o in out))

    def test_no_downgrade_once_strobing(self):
        out = [o for o in self.feed([10, 60] * 4 + [120] * 60, allow=True) if o]
        self.assertTrue(all(o.strobed for o in out))

    def test_black_frames(self):
        self.assertFalse(any(self.feed([0] * 60, allow=True)))


    def lit(self, levels, **kw):
        return [int(o.image[0, 0]) for o in self.feed(levels, **kw) if o]

    def test_fires_in_pairs(self):
        out = self.lit([10, 10, 60, 60] * 6)
        self.assertGreaterEqual(len(out), 8)
        self.assertEqual(set(out), {50})

    def test_fires_one_frame_in_three(self):
        out = self.lit([10, 10, 70] * 8)
        self.assertGreaterEqual(len(out), 5)
        self.assertEqual(set(out), {60})

    def test_dark_one_frame_in_three(self):
        self.assertEqual(set(self.lit([10, 70, 70] * 8)), {60})

    def test_exposure_settling_is_not_strobing(self):
        self.assertFalse(any(self.feed(range(20, 200, 6))))
        self.assertFalse(any(self.feed(range(200, 20, -6))))

    def test_lamp_switched_on(self):
        self.assertFalse(any(self.feed([20] * 10 + [90] * 20)))

    def test_exposure_settling_while_strobing(self):
        levels = []
        for i in range(20):
            levels += [8 + i, 150 - 4 * i]        # ambient rising, lit falling
        out = [o for o in self.feed(levels) if o]
        self.assertTrue(out)
        self.assertTrue(all(o.lit[0, 0] >= 60 for o in out))      # never an ambient frame

    def test_dropped_frames(self):
        levels = [10, 60] * 12
        del levels[7], levels[13]
        out = [o for o in self.feed(levels) if o]
        self.assertGreater(len(out), 6)
        self.assertTrue(all(o.lit[0, 0] == 60 for o in out))


class Looking(unittest.TestCase):
    def test_directions_are_as_in_a_mirror(self):
        base = np.array([0.0, 0.5])
        left = engine.look(face(pose=[.2, .5]), base)     # own left is image right
        self.assertLess(left["yaw"], -.5)
        self.assertGreater(engine.look(face(pose=[0, .7]), base)["pitch"], .5)
        f = face()
        f.center = (0.9, 0.2)
        at = engine.look(f, base)
        self.assertLess(at["x"], -.5)
        self.assertLess(at["y"], -.5)

    def test_bounded(self):
        at = engine.look(face(pose=[50, -50]), np.zeros(2))
        self.assertEqual((at["yaw"], at["pitch"]), (-1, -1))

    def test_progress_of_the_head_turn(self):
        seen = []
        faces = [face()] * 4 + [face(0.7, pose=[x, .5]) for x in np.linspace(0, .3, 8)]
        run(faces, gesture=True,
            notify=lambda k, d: seen.append(d["progress"]) if k == "track" else None)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], .5)

    def test_no_scores_in_what_the_animation_gets(self):
        keys = set()
        run([face()] * 3, notify=lambda k, d: keys.update(d) if k == "track" else None)
        self.assertEqual(keys, {"yaw", "pitch", "x", "y", "open", "progress"})


class Pose(unittest.TestCase):
    # eyes, nose, mouth corners (mm); z towards the camera
    HEAD = np.array([[-31, -20, 0], [31, -20, 0], [0, 10, 22], [-24, 45, 2], [24, 45, 2]], float)

    def project(self, points, yaw, pitch, distance=450):
        cy, sy, cp, sp = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch)
        p = points @ np.array([[cy, 0, -sy], [0, 1, 0], [sy, 0, cy]]).T
        p = p @ np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]).T
        return p[:, :2] * (600 / (distance - p[:, 2:]))

    def span(self, points):
        angles = np.radians(np.linspace(-25, 25, 11))
        a = [pose_of(self.project(points, y, 0)) for y in angles]
        b = [pose_of(self.project(points, 0, p)) for p in angles]
        return max(np.ptp(a, axis=0).max(), np.ptp(b, axis=0).max())

    def test_head_turn_passes_and_tilted_photo_does_not(self):
        flat = self.HEAD * [1, 1, 0]
        self.assertGreater(self.span(self.HEAD), Config().gesture_threshold)
        self.assertLess(self.span(flat), Config().gesture_threshold)


class Enrol(unittest.TestCase):
    def test_full_circle(self):
        e = Enrolment()
        for _ in range(e.FRONTAL):
            e.push(face(), True)
        self.assertEqual(e.phase, "ring")
        for k in range(8):
            t = k * np.pi / 4
            e.push(face(pose=POSE + 1.3 * e.SPAN * [-np.cos(t), np.sin(t)]), True)
            self.assertIn(k, e.sectors)
        self.assertTrue(e.complete(1.0))
        self.assertEqual(len(e.embeddings), e.FRONTAL + 8)

    def test_other_person_cannot_join(self):
        e = Enrolment()
        for _ in range(e.FRONTAL):
            e.push(face(), True)
        e.push(face(0.1, pose=POSE + 2 * e.SPAN * [1, 0]), True)
        self.assertFalse(e.sectors)

    def test_turn_left_fills_left_in_mirror(self):
        e = Enrolment()
        for _ in range(e.FRONTAL):
            e.push(face(), True)
        # The person's left is image right, and screen left in the mirrored preview.
        self.assertEqual(e.sector_of(POSE + np.array([.3, 0])), 4)
        self.assertEqual(e.sector_of(POSE + np.array([0, .3])), 2)    # down

    def with_color(self, color):
        e = Enrolment()
        for _ in range(e.FRONTAL):
            e.push(face(), True, color() if color else None)
        return e

    def test_colour_camera_confirms_but_leaves_nothing_behind(self):
        color = face(.75)
        e = self.with_color(lambda: color)
        self.assertTrue(e.checked)
        self.assertEqual(len(e.embeddings), e.FRONTAL)
        self.assertFalse(any(np.allclose(v, color.embedding) for v in e.embeddings))

    def test_cameras_must_agree(self):
        # A screen or a picture seen by only one of them, or two people.
        e = self.with_color(lambda: face(.1))
        self.assertEqual((e.frontal, e.phase, e.disagreed), ([], "frontal", e.FRONTAL))
        self.assertFalse(e.checked)

    def test_infrared_alone_in_the_dark(self):
        e = self.with_color(None)
        self.assertEqual((e.phase, e.checked), ("ring", False))

    def test_one_agreement_is_not_enough(self):
        e = Enrolment()
        for i in range(e.FRONTAL):
            e.push(face(), True, face(.75) if i == 0 else None)
        self.assertFalse(e.checked)

    def test_moving_restarts_both(self):
        e = Enrolment()
        e.push(face(), True, face(.75))
        e.push(face(pose=POSE + [.1, 0]), True, face(.75))
        self.assertEqual(e.checks, len(e.frontal))

    def taking_turns(self, views):
        e = self.with_color(None)
        views, frames = iter(views), iter(range(1, 100))
        vision = mock.Mock()
        vision.analyse.side_effect = lambda *_: (next(views, None), 1)
        engine.check_color(lambda: (IMG, next(frames)), vision, e, notify=lambda *_: None,
                           cancelled=lambda: False, clock=Clock())
        return e

    def test_cameras_taking_turns(self):
        self.assertTrue(self.taking_turns([None, face(.1), *[face(.75)] * 6]).checked)
        dark = self.taking_turns([])
        self.assertEqual((dark.checked, dark.disagreed), (False, 0))
        other = self.taking_turns([face(.1)] * 80)
        self.assertFalse(other.checked)
        self.assertGreaterEqual(other.disagreed, Enrolment.MIN_CHECKS)

    def test_sideways_sensor_is_turned(self):
        turns = []
        obs = (Observation(IMG, IMG, True, 0.0) for _ in range(200))
        vision = mock.Mock()
        vision.analyse.side_effect = lambda *_: (face(), 1) if len(turns) == 1 else (None, 0)
        with mock.patch.object(Enrolment, "complete", lambda self, t: True):
            result = engine.enroll(obs, vision, notify=lambda *_: None, cancelled=lambda: False,
                                   try_rotation=lambda: turns.append(1), clock=Clock())
        self.assertIsNotNone(result)
        self.assertEqual(len(turns), 1)

    def test_limited_movement(self):
        e = Enrolment()
        for _ in range(e.FRONTAL):
            e.push(face(), True)
        for x in (-1, 1):
            e.push(face(pose=POSE + 2 * e.SPAN * [x, 0]), True)
            e.push(face(pose=POSE + 2 * e.SPAN * [0, x]), True)
        self.assertFalse(e.complete(5))
        self.assertTrue(e.complete(31))


class Storage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = Store(self.dir, b"k" * 32)

    def add(self, uid=1000):
        return self.store.add(uid, "Me", [ME], POSE, True)

    def test_round_trip_and_no_plaintext(self):
        self.add()
        self.assertEqual(self.store.load(1000)[0]["label"], "Me")
        blob = (self.dir / "1000.face").read_bytes()
        self.assertNotIn(b"label", blob)
        self.assertEqual((self.dir / "1000.face").stat().st_mode & 0o777, 0o600)

    def test_file_cannot_be_moved_to_another_user(self):
        self.add()
        (self.dir / "1000.face").rename(self.dir / "0.face")
        with self.assertRaises(StoreError):
            self.store.load(0)

    def test_tampering_and_wrong_key(self):
        self.add()
        with self.assertRaises(StoreError):
            Store(self.dir, b"x" * 32).load(1000)
        p = self.dir / "1000.face"
        blob = bytearray(p.read_bytes())
        blob[40] ^= 1
        p.write_bytes(blob)
        with self.assertRaises(StoreError):
            self.store.load(1000)

    def test_faces_from_before_1_0_still_open(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        old = {"id": 1, "label": "Old", "created": 0, "model": MODEL_ID, "strobed": True,
               "pose": POSE.tolist(), "embeddings": [ME.tolist()], "adaptive": [],
               "adapted_at": 0}
        blob = b"n" * 12 + AESGCM(b"k" * 32).encrypt(b"n" * 12, json.dumps([old]).encode(),
                                                     b"faceid:v1:1000")
        (self.dir / "1000.face").write_bytes(blob)
        face = self.store.load(1000)[0]
        self.assertEqual((face["label"], face["camera"], face["checked"]), ("Old", None, False))
        self.assertGreater(Gallery([face]).match(ME)[0], .99)

    def test_empty_gallery(self):
        self.assertEqual(Gallery([]).match(ME), (-1.0, None))

    def test_only_infrared_is_stored(self):
        face = self.store.add(1000, "Me", [ME], POSE, True, "cam", 90, True)
        saved = self.store.load(1000)[0]
        self.assertEqual((saved["camera"], saved["rotate"], saved["checked"]), ("cam", 90, True))
        vectors = [k for k, v in saved.items() if isinstance(v, (list, dict)) and k != "pose"]
        self.assertEqual(vectors, ["embeddings", "adaptive"])
        self.assertEqual(face["embeddings"], [ME.tolist()])

    def test_remove(self):
        a, b = self.add(), self.add()
        self.assertTrue(self.store.remove(1000, a["id"]))
        self.assertEqual([f["id"] for f in self.store.load(1000)], [b["id"]])
        self.store.remove(1000, b["id"])
        self.assertFalse((self.dir / "1000.face").exists())

    def test_adaptive_is_rate_limited_and_bounded(self):
        f = self.add()
        self.store.adapt(1000, f["id"], ME)
        self.store.adapt(1000, f["id"], ME)
        self.assertEqual(len(self.store.load(1000)[0]["adaptive"]), 1)
        for i in range(1, 9):
            with mock.patch("time.time", return_value=4e9 + i * 4000):
                self.store.adapt(1000, f["id"], ME)
        self.assertEqual(len(self.store.load(1000)[0]["adaptive"]), 5)


class Policies(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.policy = Policy(Path(tmp.name), Config())

    def test_password_first_after_boot(self):
        self.assertEqual(self.policy.blocked(1)[0], "password")
        self.policy.note_password(1)
        self.assertIsNone(self.policy.blocked(1))
        self.assertIsNotNone(self.policy.blocked(2))

    def test_lockout_until_password(self):
        self.policy.note_password(1)
        for _ in range(5):
            self.assertIsNone(self.policy.blocked(1))
            self.policy.note_failure(1)
        self.assertEqual(self.policy.blocked(1)[0], "locked")
        self.policy.note_password(1)
        self.assertIsNone(self.policy.blocked(1))

    def test_password_expires(self):
        self.policy.note_password(1)
        later = Policy._now() + 49 * 3600
        with mock.patch.object(Policy, "_now", staticmethod(lambda: later)):
            self.assertEqual(self.policy.blocked(1)[0], "password")

    def test_corrupt_state_fails_closed(self):
        self.policy.note_password(1)
        self.policy.path.write_text("{")
        self.assertEqual(self.policy.blocked(1)[0], "password")


class Uses(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        d = self.daemon = Daemon.__new__(Daemon)
        d.cfg, d.logind, d.store = Config(), mock.Mock(), mock.Mock()
        d.settings = Settings(self.dir, d.cfg)
        d.logind.at_greeter.return_value = False
        d.store.load.return_value = [stored()]
        d.store_lock = threading.Lock()
        d.target = lambda client, request: mock.Mock(pw_uid=1, pw_name="me")
        d.authorize = mock.Mock()

    def allowed(self, service, greeter=False):
        self.daemon.logind.at_greeter.return_value = greeter
        try:
            self.daemon.check_use(1, service)
            return True
        except Refuse as e:
            self.assertEqual(e.code, "denied")     # pam_peekaboo keeps quiet on this code
            return False

    def set(self, **changes):
        self.daemon.op_settings(mock.Mock(), {"set": changes})

    def get(self):
        return self.daemon.settings.get(1)

    def needs_password(self, **changes):
        self.daemon.authorize.reset_mock()
        self.set(**changes)
        return self.daemon.authorize.called

    def test_gdm_login_and_unlock_are_told_apart(self):
        self.assertEqual(use_of("gdm-password", at_greeter=True), "login")
        self.assertEqual(use_of("gdm-password", at_greeter=False), "lock")

    def test_everything_on_by_default(self):
        for service in ("gdm-password", "sudo", "sudo-i", "su", "polkit-1"):
            self.assertTrue(self.allowed(service))
        self.assertTrue(self.allowed("gdm-password", greeter=True))

    def test_other_services_are_never_served(self):
        for service in ("sshd", "dorsandesk", "cups", "samba", "remote", "ppp", "cron",
                        "chsh", "login", "other", "", "SUDO", "test"):
            self.assertFalse(self.allowed(service), service)
            self.assertFalse(self.allowed(service, greeter=True), service)

    def test_only_one_use(self):
        everything = {"lock": ("gdm-password", False), "login": ("gdm-password", True),
                      "shell": ("sudo", False), "system": ("polkit-1", False)}
        for only in everything:
            self.set(uses=[only])
            self.assertEqual([u for u, a in everything.items() if self.allowed(*a)], [only])

    def test_none(self):
        self.set(uses=[])
        self.assertFalse(any(map(self.allowed, ("gdm-password", "sudo", "polkit-1"))))

    def test_tightening_is_free_loosening_needs_the_password(self):
        self.assertFalse(self.needs_password(uses=["lock", "shell"]))
        self.assertTrue(self.needs_password(uses=["lock", "system"]))      # a swap
        self.assertFalse(self.needs_password(gesture=["lock", "shell", "system"]))
        self.assertTrue(self.needs_password(gesture=["lock"]))
        self.assertTrue(self.needs_password(attention=False))
        self.assertFalse(self.needs_password(attention=True))
        self.assertFalse(self.needs_password(level="maximum"))
        self.assertTrue(self.needs_password(level="high"))

    def test_tightening_one_thing_does_not_hide_loosening_another(self):
        self.assertTrue(self.needs_password(uses=[], attention=False))

    def test_refused_password_changes_nothing(self):
        self.set(uses=["lock"])
        before = self.get()
        self.daemon.authorize.side_effect = Refuse("denied", "no")
        with self.assertRaises(Refuse):
            self.set(uses=["lock", "shell"])
        self.assertEqual(self.get(), before)

    def test_race_cannot_loosen(self):
        wide = self.get()
        answers = iter([wide, {**wide, "uses": ["lock"]}])
        self.daemon.settings = mock.Mock(get=lambda uid: next(answers))
        with self.assertRaises(Refuse):
            self.set(uses=["lock", "shell"])
        self.daemon.settings.save.assert_not_called()

    def test_bad_requests(self):
        for bad in (None, [], "lock", {"uses": "lock"}, {"uses": ["everything"]}, {"uses": [1]},
                    {"uses": [["lock"]]}, {"uses": {"lock": 1}}, {"attention": 1},
                    {"level": "none"}, {"level": ["high"]}, {"threshold": 0.1}):
            with self.assertRaises(Refuse, msg=bad):
                self.daemon.op_settings(mock.Mock(), {"set": bad})

    def test_damaged_settings_turn_everything_off(self):
        for text in ("{", "[]", '{"uses": 3}', "null", '{"uses": ["lock"], "level": "x"}'):
            (self.dir / "1.json").write_text(text)
            self.assertEqual(self.get()["uses"], [], text)

    def test_settings_from_before_1_0(self):
        (self.dir / "1.json").write_text('{"uses": ["lock"]}')
        self.assertEqual(self.get()["uses"], ["lock"])
        self.assertEqual(self.get()["gesture"], ["shell", "system"])

    def test_administrator_cap(self):
        self.daemon.cfg.uses = ["lock"]
        self.assertTrue(self.allowed("gdm-password"))
        self.assertFalse(self.allowed("sudo"))

    def test_deny_single_service(self):
        self.daemon.cfg.deny_services = ["su"]
        self.assertFalse(self.allowed("su"))
        self.assertTrue(self.allowed("sudo"))

    def test_camera_is_for_the_person_in_front(self):
        self.daemon.logind.refuse.return_value = "remote session"
        for op, request in ((self.daemon.op_test, {}), (self.daemon.op_demo, {}),
                            (self.daemon.op_cameras, {"probe": True})):
            with self.assertRaises(Refuse, msg=op):
                op(mock.Mock(uid=1), request)


class Recorded(unittest.TestCase):
    def test_bounded_and_survives_damage(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(Path(d))
            for i in range(130):
                h.add(1, service="sudo", use="shell", result="ok", seconds=i)
            entries = h.read(1)
            self.assertEqual(len(entries), History.KEEP)
            self.assertEqual(entries[-1]["seconds"], 129)
            self.assertEqual((Path(d) / "1.history").stat().st_mode & 0o777, 0o600)
            self.assertEqual(h.read(2), [])
            h.clear(1)
            self.assertEqual(h.read(1), [])

    def test_unwritable_never_fails_a_login(self):
        History(Path("/nonexistent")).add(1, service="sudo")


class LockStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch("peekaboo.daemon.config.RUN_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def read(self):
        return json.loads((self.dir / "7.state").read_text())

    def test_contents(self):
        Status(7, "lock").set("fail", text="Face not recognised")
        data = self.read()
        self.assertEqual(set(data), {"state", "text", "turn", "time", "use", "look", "progress"})
        self.assertLess(abs(data["time"] - time.monotonic_ns() // 1000), 5e6)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["7.state"])

    def test_readable_by_the_shell_despite_the_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        Status(7, "shell").set("scanning")
        self.assertEqual((self.dir / "7.state").stat().st_mode & 0o777, 0o644)

    def test_written_only_where_it_is_shown(self):
        for use in (None, "login"):
            Status(7, use).set("ok")
        self.assertFalse(list(self.dir.iterdir()))

    def test_unchanged_is_not_rewritten(self):
        s = Status(7, "lock")
        s.set("scanning")
        before = self.read()
        s.set("scanning")
        self.assertEqual(self.read(), before)

    def test_write_failure_never_fails_a_login(self):
        with mock.patch("peekaboo.daemon.config.RUN_DIR", self.dir / "gone"), \
                self.assertLogs("peekabood", "WARNING"):
            Status(7, "lock").set("ok")

    def auth(self, look, service="gdm-password"):
        """Run a real request up to the camera; returns what the client was sent, with
        the animation state at that moment."""
        d = Daemon.__new__(Daemon)
        d.cfg, d.store, d.policy, d.logind = Config(adaptive=False), mock.Mock(), mock.Mock(), \
            mock.Mock()
        d.settings, d.password_only, d.history = Settings(self.dir, d.cfg), set(), mock.Mock()
        d.logind.refuse.return_value, d.logind.at_greeter.return_value = None, False
        d.store.load.return_value = [stored()]
        d.policy.blocked.return_value = None
        d._vision, d._vision_lock, d.camera_lock = mock.Mock(), threading.Lock(), threading.Lock()
        d.open_ir = mock.MagicMock()
        d.target = lambda *_: mock.Mock(pw_uid=7, pw_name="me")
        client, sent = mock.Mock(uid=0), []
        client.send.side_effect = lambda kind, *a: sent.append((kind, self.read()["state"]))
        info = mock.Mock(external=False)
        with mock.patch("peekaboo.daemon.lid_closed", return_value=False), \
                mock.patch("peekaboo.daemon.discover", return_value=[]), \
                mock.patch("peekaboo.daemon.pick_ir", return_value=info), \
                mock.patch("peekaboo.daemon.engine.authenticate", look):
            try:
                d.op_auth(client, {"service": service})
            except (Refuse, engine.Aborted, RuntimeError):
                pass
        self.history = d.history
        self.assertFalse(d.camera_lock.locked())
        return sent

    @staticmethod
    def success(*_, notify=None, **__):
        notify("track", {"yaw": .5, "pitch": 0, "x": 0, "y": 0, "open": 1, "progress": .4})
        notify("hint", {"text": "Move closer"})
        return engine.Result("ok", .9, {"id": 1, "label": "Me"}, ME, .5)

    def test_success_shows_before_the_screen_unlocks(self):
        self.assertEqual(self.auth(self.success), [("OK", "ok")])
        self.history.add.assert_called_once()

    def test_no_messages_on_the_lock_screen(self):
        # GNOME delays the unlock by two seconds for each one.
        self.assertEqual([k for k, _ in self.auth(self.success)], ["OK"])

    def test_messages_in_the_terminal(self):
        self.assertEqual([k for k, _ in self.auth(self.success, "sudo")], ["INFO", "INFO", "OK"])

    def test_messages_can_be_turned_off(self):
        quiet = lambda **kw: Config_(messages=False, **kw)       # noqa: E731
        with mock.patch.dict(globals(), Config=quiet):
            self.assertEqual([k for k, _ in self.auth(self.success, "sudo")], ["OK"])

    def test_tracking_reaches_the_animation(self):
        seen = []

        def look(*a, notify, **k):
            notify("track", {"yaw": .5, "pitch": 0, "x": 0, "y": 0, "open": 1, "progress": .4})
            seen.append(self.read())
            return engine.Result("ok", .9, {"id": 1, "label": "Me"}, ME, .5)
        self.auth(look, "polkit-1")
        self.assertEqual(seen[0]["look"]["yaw"], .5)
        self.assertEqual((seen[0]["progress"], seen[0]["use"], seen[0]["turn"]),
                         (.4, "system", True))
        self.assertIsNone(self.read()["look"])       # nothing left behind afterwards

    def test_failure_and_abort(self):
        self.auth(lambda *a, **k: engine.Result("mismatch"))
        self.assertEqual(self.read()["state"], "fail")
        for error in (engine.Aborted, RuntimeError):
            self.auth(mock.Mock(side_effect=error))
            self.assertEqual(self.read()["state"], "idle")


class Configs(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write(text)
            f.flush()
            return Config.load(Path(f.name))

    def test_shipped_file_matches_defaults(self):
        root = Path(__file__).parent.parent
        text = "\n".join(line[1:] if line.startswith("#") and " = " in line else line
                         for line in (root / "data/config.toml").read_text().splitlines())
        self.assertEqual(self.load(text), Config())

    def test_rejects_dangerous_or_unknown(self):
        for bad in ("threshold = 0.0", "min_frames = 0", "thresold = 0.7",
                    "max_failures = 100000", 'threshold = "high"',
                    "require_attention = 0", 'uses = ["everything"]', 'gesture = ["all"]',
                    "rotate = 90", 'rotate = "45"', 'mode = "big"', 'mode = "GREY 640"',
                    "black_level = -1", 'emitter_control = "14 6"'):
            with self.assertRaises(ValueError, msg=bad):
                self.load("[x]\n" + bad)


if __name__ == "__main__":
    unittest.main()
