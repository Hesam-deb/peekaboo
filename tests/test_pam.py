"""Runs the real module in a copy of Ubuntu's common-auth stack, with a fake daemon."""
import contextlib
import getpass
import json
import os
import pty
import select
import signal
import socket
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
USER = getpass.getuser()


class Pam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="fp")
        d = cls.dir = Path(cls.tmp.name)
        cls.sock = d / "s"
        flags = ["-Wall", "-Wextra", "-Werror", "-g", "-fPIC"]
        subprocess.run(["gcc", *flags, "-shared", "-o", d / "pam_peekaboo.so",
                        f'-DPEEKABOO_SOCKET="{cls.sock}"', f"-DPEEKABOO_SERVER_UID={os.getuid()}",
                        ROOT / "pam/pam_peekaboo.c", "-lpam"], check=True)
        # Same module compiled as shipped: it must distrust a socket not owned by root.
        subprocess.run(["gcc", *flags, "-shared", "-o", d / "pam_strict.so",
                        f'-DPEEKABOO_SOCKET="{cls.sock}"', ROOT / "pam/pam_peekaboo.c", "-lpam"],
                       check=True)
        subprocess.run(["gcc", "-o", d / "harness", ROOT / "tests/pam_harness.c", "-lpam"],
                       check=True)
        # pam_exec stands in for pam_unix: it reads the token and checks it.
        check = d / "check.sh"
        check.write_text("#!/bin/sh\n[ \"$(tr -d '\\0')\" = secret ]\n")
        check.chmod(0o755)
        for name, module in (("sudo", "pam_peekaboo.so"), ("strict", "pam_strict.so")):
            (d / name).write_text(
                f"auth [success=2 default=ignore] {d}/{module}\n"
                f"auth [success=1 default=ignore] pam_exec.so expose_authtok quiet {check}\n"
                "auth requisite pam_deny.so\n"
                "auth required pam_permit.so\n"
                f"auth optional {d}/{module} note_password\n")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.requests = []

    def serve(self, reply):
        """Answer `auth` with the given bytes (None: hang up) and record requests."""
        self.sock.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.sock))
        server.listen()

        def loop():
            while True:
                try:
                    c, _ = server.accept()
                except OSError:
                    return
                with c, contextlib.suppress(OSError, ValueError):
                    r = json.loads(c.makefile().readline())
                    self.requests.append(r["op"])
                    if r["op"] == "note_password":
                        c.sendall(b"OK\n")
                    elif callable(reply):
                        reply(c)
                    elif reply:
                        c.sendall(reply)
        threading.Thread(target=loop, daemon=True).start()

        def stop():
            server.shutdown(socket.SHUT_RDWR)
            server.close()
            self.sock.unlink(missing_ok=True)
        self.addCleanup(stop)

    def login(self, password="secret", service="sudo", user=USER, rhost=None, env=None):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
        out = subprocess.run([self.dir / "harness", self.dir, service, user, password,
                              *([rhost] if rhost else [])], text=True, timeout=30,
                             stdout=subprocess.PIPE, env={**clean, **(env or {})}).stdout
        return out

    def test_face_skips_password(self):
        self.serve(b"INFO look here\nOK {}\n")
        out = self.login("wrong")
        self.assertIn("AUTH yes\nCRED 0", out)
        self.assertNotIn("PROMPT", out)
        self.assertIn("MSG look here", out)
        self.assertEqual(self.requests, ["auth"])     # a face never counts as a password

    def test_sudo_silences_pam_but_the_prompt_still_shows(self):
        self.serve(b"INFO turn your head\nOK {}\n")
        out = self.login("wrong", env={"HARNESS_SILENT": "1"})
        self.assertIn("MSG turn your head", out)
        self.assertIn("AUTH yes", out)
        self.serve(b"FAIL mismatch Face not recognised\n")
        self.assertIn("MSG Face not recognised", self.login(env={"HARNESS_SILENT": "1"}))

    # ---- typing the password while the camera looks

    def terminal(self, keys, service="sudo", until=b"look", signal_exit=False):
        """Run a login on its own terminal, type `keys` once the prompt shows.

        Returns (output, exit status, terminal echoes afterwards, unread input)."""
        pid, master = pty.fork()
        if pid == 0:
            env = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
            os.execve(self.dir / "harness", ["harness", str(self.dir), service, USER,
                                             "prompted-wrong"], env)
        out, typed, end = b"", False, time.monotonic() + 15
        slave = os.open(os.ptsname(master), os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, slave)
        self.addCleanup(os.close, master)
        status = None
        while time.monotonic() < end:
            if select.select([master], [], [], 0.05)[0]:
                out += os.read(master, 4096)
            if until in out and not typed:
                typed = True
                time.sleep(0.1)
                os.write(master, keys)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            os.kill(pid, 9)
            self.fail(f"hung: {out}")
        while select.select([master], [], [], 0.1)[0]:
            out += os.read(master, 4096)
        echo = bool(termios.tcgetattr(slave)[3] & termios.ECHO)
        os.set_blocking(slave, False)
        os.write(master, b"\n")
        time.sleep(0.05)
        left = os.read(slave, 100)
        return out.decode().replace("\r\n", "\n"), status, echo, left.strip()

    def slow(self, end=b"FAIL timeout Timed out\n", delay=3.0):
        def reply(c):
            c.sendall(b"INFO look here\n")
            self.hung_up = bool(select.select([c], [], [], delay)[0])
            if not self.hung_up:
                c.sendall(end)
        return reply

    def test_typed_password_is_used_and_not_shown(self):
        self.serve(self.slow())
        out, status, echo, left = self.terminal(b"secret\n")
        self.assertIn("look here, or type your password", out)
        self.assertIn("AUTH yes", out)
        self.assertNotIn("PROMPT", out)
        self.assertNotIn("secret", out)
        self.assertTrue(echo)
        self.assertEqual(left, b"")
        self.assertTrue(self.hung_up)          # the camera stops at once
        self.assertEqual(self.requests, ["auth", "note_password"])

    def test_typed_password_can_be_edited(self):
        self.serve(self.slow())
        self.assertIn("AUTH yes", self.terminal(b"wrong\x15secrex\x7ft\n")[0])

    def test_typed_wrong_password(self):
        self.serve(self.slow())
        out, _, echo, _ = self.terminal(b"nonsense\n")
        self.assertIn("AUTH no", out)
        self.assertTrue(echo)
        self.assertEqual(self.requests, ["auth"])

    def test_enter_goes_to_the_prompt(self):
        self.serve(self.slow())
        out, *_ = self.terminal(b"\n")
        self.assertIn("PROMPT", out)

    def test_half_a_password_does_not_leak(self):
        self.serve(self.slow(end=b"OK {}\n", delay=0.6))
        out, _, echo, left = self.terminal(b"secr")
        self.assertIn("AUTH yes", out)
        self.assertEqual((echo, left), (True, b""))
        self.assertNotIn("secr", out)

    def test_ctrl_c(self):
        self.serve(self.slow())
        out, status, echo, _ = self.terminal(b"\x03")
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGINT)
        self.assertTrue(echo)
        self.assertNotIn("AUTH", out)
        self.assertTrue(self.hung_up)

    def test_over_long_line(self):
        self.serve(self.slow())
        out, _, echo, _ = self.terminal(b"x" * 900 + b"\n")
        self.assertIn("AUTH no", out)
        self.assertTrue(echo)

    def test_only_terminal_programs_are_typed_into(self):
        (self.dir / "gdm-password").write_text((self.dir / "sudo").read_text())
        self.serve(self.slow(delay=0.8))
        out, *_ = self.terminal(b"secret\n", service="gdm-password")
        self.assertNotIn("or type", out)
        self.assertIn("PROMPT", out)

    def test_one_look_per_prompt(self):
        self.serve(b"FAIL mismatch Face not recognised\n")
        out = self.login("wrong", env={"HARNESS_TRIES": "3"})
        self.assertEqual(out.count("PROMPT"), 3)
        self.assertEqual(self.requests, ["auth"])

    def test_switched_off_stays_quiet(self):
        for code in ("denied", "not_enrolled", "lid"):
            self.serve(f"FAIL {code} some reason\n".encode())
            self.assertNotIn("MSG", self.login())
            self.doCleanups()

    def test_no_match_then_password(self):
        self.serve(b"FAIL mismatch Face not recognised\n")
        out = self.login()
        self.assertIn("PROMPT", out)
        self.assertIn("AUTH yes\nCRED 0", out)
        self.assertEqual(self.requests, ["auth", "note_password"])

    def test_no_match_then_wrong_password(self):
        self.serve(b"FAIL mismatch Face not recognised\n")
        self.assertIn("AUTH no", self.login("wrong"))
        self.assertEqual(self.requests, ["auth"])     # must not clear the lockout

    def test_daemon_absent(self):
        self.assertIn("AUTH yes", self.login())
        self.assertIn("AUTH no", self.login("wrong"))

    def test_bad_answers_never_authenticate(self):
        flood = b"A" * 100000
        for reply in (None, b"", b"OKAY\n", b"OK", b"\n\n", b"FAIL\n", b"FAIL x\n", flood,
                      b"INFO OK\n", b"\x00OK\n", b"FAIL a b\nOK\n"):
            with self.subTest(reply=(reply or b"")[:20]):
                self.serve(reply)
                self.assertIn("AUTH no", self.login("wrong"))
                self.doCleanups()

    def test_split_packets(self):
        def drip(c):
            for byte in b"INFO hi\nOK {}\n":
                c.sendall(bytes([byte]))
                time.sleep(0.005)
        self.serve(drip)
        self.assertIn("AUTH yes", self.login("wrong"))

    def test_escape_sequences_are_stripped(self):
        self.serve(b"INFO a\x1b[31mb\nOK\n")
        self.assertNotIn("\x1b", self.login())

    def test_remote_is_skipped(self):
        self.serve(b"OK {}\n")
        self.assertIn("AUTH no", self.login("wrong", rhost="10.0.0.5"))
        self.assertIn("AUTH no", self.login("wrong", env={"SSH_CONNECTION": "x"}))
        self.assertEqual(self.requests, [])

    @unittest.skipIf(os.getuid() == 0, "any socket made here would belong to root")
    def test_socket_must_belong_to_root(self):
        self.serve(b"OK {}\n")
        self.assertIn("AUTH no", self.login("wrong", service="strict"))
        self.assertEqual(self.requests, [])

    def test_hostile_user_name(self):
        self.serve(b"OK {}\n")
        self.assertIn("AUTH no", self.login("wrong", user='x","user":"root'))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
