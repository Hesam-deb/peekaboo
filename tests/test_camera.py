"""Other people's laptops, as far as they can be simulated."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from peekaboo import camera, config, v4l2
from peekaboo.camera import CameraError, CameraInfo, classify, find_msxu, pick_color, pick_ir
from peekaboo.v4l2 import Mode, choose, decode


def cam(node="/dev/video0", name="Integrated Camera", formats=("YUYV", "MJPG"), usb="1-5",
        interface=0, placement="fixed", driver="uvcvideo", msxu=None, size=(640, 480), bus=None):
    info = CameraInfo(node, name, driver, bus or f"usb-0000:00:14.0-{usb[-1:]}", usb, "04f2",
                      "b6d9", interface, placement,
                      [Mode(f, *size, 30.0) for f in formats], msxu)
    classify(info)
    return info


class Classify(unittest.TestCase):
    def kind(self, **kw):
        return cam(**kw).kind

    def test_infrared(self):
        self.assertEqual(self.kind(formats=["GREY"]), "ir")
        self.assertEqual(self.kind(formats=["Y16 "], name="whatever"), "ir")
        # Compressed or YUYV infrared, known by its Windows Hello control.
        self.assertEqual(self.kind(formats=["MJPG"], msxu=14), "ir")
        self.assertEqual(self.kind(formats=["YUYV"], msxu=4, name="USB Camera"), "ir")

    def test_uncertain(self):
        for name in ("Integrated IR Camera", "HP IR Camera", "Infrared Camera",
                     "USB2.0 IR UVC WebCam", "Integrated_Webcam_HD: IR Camera"):
            self.assertEqual(self.kind(name=name), "maybe-ir", name)

    def test_colour(self):
        for name in ("Integrated Camera", "HD Webcam", "FaceTime HD Camera", "Iriun Webcam",
                     "Virtual Mirror", "Circle Cam", "Logitech BRIO", "First Camera"):
            self.assertEqual(self.kind(name=name), "color", name)

    def test_not_usable(self):
        self.assertEqual(self.kind(formats=["Z16 "]), "unsupported")
        self.assertEqual(self.kind(formats=[], driver="intel-ipu6"), "unsupported")
        self.assertEqual(self.kind(formats=["BG10", "ip3B"], driver="isys"), "unsupported")
        self.assertEqual(self.kind(formats=["H264"]), "unsupported")

    def test_colour_camera_with_a_grey_mode_is_colour(self):
        self.assertEqual(self.kind(formats=["YUYV", "GREY"]), "color")


class Pick(unittest.TestCase):
    def setUp(self):
        self.color = cam()
        self.ir = cam("/dev/video2", "Integrated IR Camera", ["GREY"], interface=2)
        self.usb_ir = cam("/dev/video4", "Brio IR", ["GREY"], "3-1", 4, "removable",
                          bus="usb-0000:00:14.0-1")
        self.usb_color = cam("/dev/video6", "Brio", usb="3-1", placement="removable",
                             bus="usb-0000:00:14.0-1")

    @staticmethod
    def ranked(*cameras):
        return sorted(cameras, key=lambda c: (-c.score, c.node))

    def test_built_in_beats_external(self):
        found = self.ranked(self.usb_ir, self.usb_color, self.color, self.ir)
        self.assertIs(pick_ir(found), self.ir)
        self.assertIs(pick_color(found, self.ir), self.color)

    def test_external_when_it_is_all_there_is(self):
        self.assertIs(pick_ir(self.ranked(self.usb_ir, self.color)), self.usb_ir)
        self.assertIs(pick_color([self.usb_color, self.color], self.usb_ir), self.usb_color)

    def test_enrolled_camera_only(self):
        found = self.ranked(self.ir, self.usb_ir)
        self.assertIs(pick_ir(found, self.usb_ir.id), self.usb_ir)
        with self.assertRaises(CameraError):
            pick_ir([self.ir], self.usb_ir.id)

    def test_same_model_on_another_port_is_another_camera(self):
        clone = cam("/dev/video9", self.ir.name, ["GREY"], "3-2", 2, "removable",
                    bus="usb-0000:00:14.0-2")
        self.assertNotEqual(clone.id, self.ir.id)
        with self.assertRaises(CameraError):
            pick_ir([clone], self.ir.id)

    def test_renumbering_keeps_the_identity(self):
        moved = cam("/dev/video7", self.ir.name, ["GREY"], interface=2)
        self.assertIs(pick_ir([moved], self.ir.id), moved)

    def test_uncertain_camera_is_looked_through(self):
        maybe = cam("/dev/video2", "Integrated IR Camera", ["MJPG"], interface=2)
        for result, ok in (({"mono": True}, True), ({"mono": False}, False)):
            maybe.kind = "maybe-ir"
            with mock.patch.object(camera, "probe", return_value=result):
                if ok:
                    self.assertIs(pick_ir([maybe, self.color]), maybe)
                else:
                    with self.assertRaises(CameraError):
                        pick_ir([maybe, self.color])

    def test_uncertain_camera_that_fails(self):
        maybe = cam("/dev/video2", "IR Camera", ["MJPG"], interface=2)
        with mock.patch.object(camera, "probe", side_effect=CameraError("x")), \
                self.assertRaises(CameraError):
            pick_ir([maybe])

    def test_no_infrared_says_why(self):
        with self.assertRaisesRegex(CameraError, "no infrared"):
            pick_ir([self.color])
        with self.assertRaisesRegex(CameraError, "MIPI"):
            pick_ir([cam(formats=[], driver="intel-ipu6", usb="")])
        with self.assertRaises(CameraError):
            pick_ir([])

    def test_configured_device(self):
        self.assertIs(pick_ir([self.ir, self.color], device="/dev/video0"), self.color)
        with self.assertRaises(CameraError):
            pick_ir([self.ir], device="/dev/video42")

    def test_colour_camera_choices(self):
        self.assertIsNone(pick_color([self.ir, self.color], self.ir, "off"))
        self.assertIsNone(pick_color([self.ir], self.ir))
        self.assertIsNone(pick_color([self.ir, self.usb_color], self.ir))   # another viewpoint
        lone = cam("/dev/video1", usb="1-7")
        self.assertIs(pick_color([self.ir, lone], self.ir), lone)     # separate built-in module
        self.assertIsNone(pick_color([self.ir, lone], self.ir, wanted=self.color.id))


class Faces(unittest.TestCase):
    """Which enrolled faces count with the cameras that are there."""

    def usable(self, faces, cameras):
        from peekaboo.config import Config
        from peekaboo.daemon import Daemon
        d = Daemon.__new__(Daemon)
        d.cfg = Config()
        return d.usable_faces(faces, cameras)

    def face(self, camera=None, rotate=0):
        return {"camera": camera, "rotate": rotate}

    def test_tied_to_the_camera(self):
        ir = cam(formats=["GREY"], interface=2)
        other = cam("/dev/video4", formats=["GREY"], usb="3-1", placement="removable",
                    bus="usb-x-9")
        info, faces = self.usable([self.face(ir.id), self.face(other.id)], [ir])
        self.assertIs(info, ir)
        self.assertEqual(faces, [self.face(ir.id)])

    def test_plugged_in_camera_cannot_stand_in(self):
        ir = cam(formats=["GREY"], interface=2)
        fake = cam("/dev/video4", formats=["GREY"], usb="3-1", placement="removable",
                   bus="usb-x-9")
        with self.assertRaises(CameraError):
            self.usable([self.face(ir.id)], [fake])
        with self.assertRaises(CameraError):        # faces from before 1.0 have no camera
            self.usable([self.face()], [fake])

    def test_old_faces_use_the_built_in_camera(self):
        ir = cam(formats=["GREY"], interface=2)
        info, faces = self.usable([self.face()], [ir])
        self.assertIs(info, ir)
        self.assertEqual(len(faces), 1)


class Descriptors(unittest.TestCase):
    @staticmethod
    def blob(interface, guid=camera.MSXU_GUID, unit=14):
        head = bytes([9, 4, interface, 0, 0, 14, 1, 0, 0])
        xu = bytes([27, 0x24, 6, unit]) + guid + bytes(7)
        return head + xu

    def test_found_on_the_right_function(self):
        blob = self.blob(0, bytes(16)) + self.blob(2)
        self.assertEqual(find_msxu(blob, 2), 14)
        self.assertIsNone(find_msxu(blob, 0))

    def test_rubbish(self):
        for blob in (b"", b"\x00", b"\x01\x02", bytes(50), b"\xff" * 40, self.blob(2)[:-9]):
            find_msxu(blob, 2)


class Discover(unittest.TestCase):
    """Against a fake /sys, as laid out by the kernel."""

    def build(self, root, entries):
        infos = {}
        for e in entries:
            node = root / e["node"]
            if e.get("virtual"):
                target = root / "devices/virtual/video4linux" / e["node"]
                target.mkdir(parents=True)
                (target / "device").mkdir()
                node.symlink_to(target)
            else:
                usb = root / "devices/pci" / e["usb"]
                iface = usb / f"{e['usb']}:1.{e['interface']}"
                (iface / "video4linux" / e["node"]).mkdir(parents=True)
                (iface / "bInterfaceNumber").write_text(f"{e['interface']:02x}\n")
                if "label" in e or not (iface / "interface").exists():
                    (iface / "interface").write_text(e.get("label", "") + "\n")
                for k, v in (("idVendor", "04f2"), ("idProduct", "b6d9"),
                             ("removable", e.get("removable", "fixed"))):
                    (usb / k).write_text(v + "\n")
                (usb / "descriptors").write_bytes(e.get("descriptors", b""))
                (iface / "video4linux" / e["node"] / "device").symlink_to(iface)
                node.symlink_to(iface / "video4linux" / e["node"])
            infos[f"/dev/{e['node']}"] = v4l2.NodeInfo(
                e.get("driver", "uvcvideo"), e["card"], e.get("bus", "usb-0000:00:14.0-5"),
                e.get("capture", True), False,
                [Mode(f, 640, 480, 30.0) for f in e.get("formats", [])], e.get("privacy", False))
        return infos

    def discover(self, entries):
        with tempfile.TemporaryDirectory() as d:
            infos = self.build(Path(d), entries)
            with mock.patch.object(camera, "SYSFS", Path(d)), \
                    mock.patch.object(v4l2, "query", lambda p: infos[p]):
                return camera.discover()

    def test_typical_windows_hello_laptop(self):
        found = self.discover([
            {"node": "video0", "usb": "3-9", "interface": 0, "card": "Integrated Camera: Integrated C",
             "formats": ["MJPG", "YUYV"]},
            {"node": "video1", "usb": "3-9", "interface": 0, "card": "Integrated Camera: Integrated C",
             "capture": False},
            {"node": "video2", "usb": "3-9", "interface": 2, "card": "Integrated Camera: Integrated I",
             "label": "Integrated IR Camera", "formats": ["GREY"]},
            {"node": "video3", "usb": "3-9", "interface": 2, "card": "Integrated Camera: Integrated I",
             "capture": False},
        ])
        self.assertEqual([(c.node, c.kind) for c in found],
                         [("/dev/video2", "ir"), ("/dev/video0", "color")])
        self.assertEqual(found[0].name, "Integrated IR Camera")
        self.assertIs(pick_color(found, pick_ir(found)), found[1])

    def test_virtual_cameras_are_never_trusted(self):
        found = self.discover([
            {"node": "video0", "virtual": True, "card": "IR Camera", "formats": ["GREY"],
             "driver": "v4l2 loopback"},
        ])
        self.assertEqual(found, [])

    def test_tenth_node_sorts_after_second(self):
        found = self.discover([
            {"node": f"video{n}", "usb": f"3-{n}", "interface": 0, "card": "IR Camera",
             "formats": ["GREY"], "bus": f"usb-{n}"} for n in (10, 2)])
        self.assertEqual(found[0].node, "/dev/video10")      # same score: by name
        self.assertEqual(len(found), 2)

    def test_unreadable_node_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self.build(Path(d), [{"node": "video0", "usb": "3-1", "interface": 0, "card": "x"}])
            with mock.patch.object(camera, "SYSFS", Path(d)), \
                    mock.patch.object(v4l2, "query", side_effect=PermissionError):
                self.assertEqual(camera.discover(), [])

    def test_privacy_shutter(self):
        found = self.discover([{"node": "video2", "usb": "3-9", "interface": 2, "card": "IR",
                                "formats": ["GREY"], "privacy": True}])
        with self.assertRaisesRegex(CameraError, "shutter"):
            camera.IRCamera(found[0])


class Decode(unittest.TestCase):
    W, H = 8, 4

    def setUp(self):
        self.grey = np.arange(self.W * self.H, dtype=np.uint8).reshape(self.H, self.W) * 7

    def check(self, fmt, data, stride, tolerance=0):
        out = decode(np.frombuffer(data, np.uint8), fmt, self.W, self.H, stride, color=False)
        self.assertEqual(out.shape, (self.H, self.W), fmt)
        self.assertLessEqual(int(np.abs(out.astype(int) - self.grey).max()), tolerance, fmt)
        bgr = decode(np.frombuffer(data, np.uint8), fmt, self.W, self.H, stride, color=True)
        self.assertEqual(bgr.shape, (self.H, self.W, 3), fmt)

    def test_grey(self):
        self.check("GREY", self.grey.tobytes(), 8)

    def test_padded_rows(self):
        padded = np.zeros((self.H, 16), np.uint8)
        padded[:, :8] = self.grey
        self.check("GREY", padded.tobytes(), 16)
        self.check("GREY", padded.tobytes()[:-8], 16)        # last row not padded

    def test_deep_grey(self):
        for fmt, shift in v4l2.DEEP.items():
            self.check(fmt, (self.grey.astype("<u2") << shift).tobytes(), 16)

    def test_black_level(self):
        raw = ((self.grey.astype("<u2") << 2) + 64).tobytes()
        out = decode(np.frombuffer(raw, np.uint8), "Y10 ", self.W, self.H, 16, False, 64)
        self.assertTrue((out == self.grey).all())
        dark = decode(np.full(self.W * self.H * 2, 0, np.uint8), "Y10 ", self.W, self.H, 16,
                      False, 64)
        self.assertEqual(int(dark.max()), 0)        # no wrapping below black

    def test_interleaved_stereo(self):
        both = np.zeros((self.H, 16), np.uint8)
        both[:, ::2] = self.grey
        self.check("Y8I ", both.tobytes(), 16)

    def test_packed(self):
        for fmt, offset in (("YUYV", 0), ("YVYU", 0), ("UYVY", 1)):
            data = np.full((self.H, 16), 128, np.uint8)
            data[:, offset::2] = self.grey
            self.check(fmt, data.tobytes(), 16)

    def test_planar(self):
        for fmt in v4l2.PLANAR:
            self.check(fmt, self.grey.tobytes() + bytes([128] * (self.W * self.H // 2)), 8)

    def test_rgb(self):
        for fmt in v4l2.RGB:
            self.check(fmt, np.repeat(self.grey[..., None], 3, 2).tobytes(), 24, tolerance=1)

    def test_jpeg(self):
        ok, jpeg = cv2.imencode(".jpg", self.grey, [cv2.IMWRITE_JPEG_QUALITY, 100])
        self.check("MJPG", jpeg.tobytes(), 0, tolerance=4)

    def test_truncated_frames_are_dropped(self):
        for fmt in sorted(v4l2.SUPPORTED):
            for size in (0, 1, 9):
                self.assertIsNone(decode(np.zeros(size, np.uint8), fmt, self.W, self.H, 0,
                                         color=False) if size or fmt not in v4l2.JPEG else None)

    def test_unknown_format(self):
        with self.assertRaises(CameraError):
            decode(np.zeros(99, np.uint8), "H264", 8, 4, 0, False)


class Forced(unittest.TestCase):
    def test_mode(self):
        self.assertEqual(config.parse_mode("Y10 640x480@30"), ("Y10 ", 640, 480, 30.0))
        self.assertEqual(config.parse_mode("MJPG 1280x720"), ("MJPG", 1280, 720, 0.0))

    def test_reaches_the_camera(self):
        info = cam(formats=["GREY", "Y10 "], interface=2)
        with mock.patch.object(camera, "Stream") as stream:
            camera.IRCamera(info, mode=Mode("Y10 ", 640, 480, 30.0), exposure=200, black_level=64)
        stream.assert_called_once_with(info.node, Mode("Y10 ", 640, 480, 30.0), 200, 64)


class Choose(unittest.TestCase):
    def test_infrared(self):
        self.assertEqual(choose([Mode("GREY", 340, 340, 30), Mode("GREY", 640, 360, 15)], False),
                         Mode("GREY", 640, 360, 15))
        self.assertEqual(choose([Mode("GREY", 340, 340, 30)], False).width, 340)
        self.assertEqual(choose([Mode("MJPG", 640, 360, 30), Mode("GREY", 640, 360, 30)],
                                False).format, "GREY")

    def test_colour_avoids_slow_raw_modes(self):
        modes = [Mode("YUYV", 1920, 1080, 5), Mode("YUYV", 1280, 720, 10),
                 Mode("MJPG", 1280, 720, 30), Mode("YUYV", 160, 120, 30)]
        self.assertEqual(choose(modes, True), Mode("MJPG", 1280, 720, 30))
        self.assertEqual(choose([Mode("YUYV", 640, 480, 30), Mode("YUYV", 1920, 1080, 5)],
                                True).width, 640)

    def test_unknown_rate(self):
        self.assertIsNotNone(choose([Mode("GREY", 640, 480, 0.0)], False))

    def test_nothing_usable(self):
        self.assertIsNone(choose([Mode("H264", 640, 480, 30)], True))
        self.assertIsNone(choose([], False))


if __name__ == "__main__":
    unittest.main()
