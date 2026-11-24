"""peekaboo: command line front end."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config
from .client import DaemonError, Request, call
from .config import LEVELS, USES


def default_user() -> str | None:
    """`sudo peekaboo ...` should act on the person, not on root."""
    return os.environ.get("SUDO_USER") if os.geteuid() == 0 else None


def get_status(args) -> dict:
    return json.loads(call("status", user=args.user))


def status(args) -> None:
    s = get_status(args)
    settings = s["settings"]
    on = [u for u in settings["uses"] if u in s["allowed_uses"]]
    print(f"Peekaboo {s['version']}  user {s['user']}")
    print(f"  infrared   {s['ir']['name'] if s['ir'] else 'no camera found'}"
          + ("  (Windows Hello emitter)" if s["ir"] and s["ir"]["emitter"] else ""))
    print(f"  colour     {s['color']['name'] if s['color'] else 'none'}")
    print(f"  used for   {', '.join(on) or 'nothing'}")
    print(f"  head turn  {', '.join(settings['gesture']) or 'never'}")
    print(f"  attention  {'required' if settings['attention'] else 'off'}"
          f"   level {settings['level']}")
    print(f"  state      {s['blocked'] or 'ready'}  "
          f"({s['failures']}/{s['max_failures']} failed attempts)")
    if not s["faces"]:
        print("  no face enrolled; run: peekaboo enroll")
    for f in s["faces"]:
        when = time.strftime("%Y-%m-%d", time.localtime(f["created"]))
        notes = ["checked by both cameras" if f["checked"] else "infrared camera only"]
        if not f["strobed"]:
            notes.append("no strobe: weaker liveness")
        if not f["connected"]:
            notes.append("its camera is not connected")
        print(f"  face {f['id']}     {f['label']}  {when}  {f['views']} views"
              f"  ({', '.join(notes)})")


def enroll(args) -> None:
    last = None
    for kind, payload in Request("enroll", user=args.user, label=args.label,
                                 ir_only=args.ir_only or None, camera=args.camera):
        if kind == "STATE":
            s = json.loads(payload)
            bar = "".join("●" if i in s["sectors"] else "○" for i in range(8))
            eye = {None: "", True: "  ◉ both cameras", False: "  ○ infrared only"}[s["color"]]
            line = f"{bar}  {s['hint']}{eye}"
            if line != last:
                print(f"\r\033[K{line}", end="", flush=True)
                last = line
        elif kind == "OK":
            r = json.loads(payload)
            print(f"\nEnrolled “{r['label']}”"
                  + (", checked by both cameras." if r["checked"] else
                     " with the infrared camera."))
            if r["external"]:
                print("Note: this is an external camera. Someone could swap it for a device "
                      "that replays your face.")
            if not r["strobed"]:
                print("Note: this camera's emitter does not strobe, so photographs are "
                      "harder to tell apart. Consider the head turn everywhere.")


def test(args) -> None:
    started = time.monotonic()
    for kind, payload in Request("test", user=args.user, gesture=args.gesture):
        if kind == "FRAME":
            f = json.loads(payload)
            print(f"  score {f['score']:.3f}  eyes {f['eyes']:.2f}  light {f['light']:>3}"
                  f"  pose {f['pose'][0]:+.2f} {f['pose'][1]:+.2f}")
        elif kind == "INFO":
            print(payload)
        elif kind == "OK":
            r = json.loads(payload)
            print(f"Peekaboo! “{r['label']}”: score {r['score']}, "
                  f"{time.monotonic() - started:.2f}s in total")


def remove(args) -> None:
    if args.all:
        call("remove", user=args.user, all=True)
    elif args.id is None:
        raise SystemExit("give a face id, or --all")
    else:
        call("remove", user=args.user, id=args.id)
    print("Removed.")


def change(args, **settings) -> dict:
    return json.loads(call("settings", user=args.user, set=settings))


def pick(what: list[str]) -> list[str]:
    if what in (["all"], ["none"]):
        return list(USES) if what == ["all"] else []
    if not set(what) <= set(USES):
        raise SystemExit(f"choose from: {', '.join(USES)}, or all, or none")
    return what


def use(args) -> None:
    if args.what:
        change(args, uses=pick(args.what))
    s = get_status(args)
    for name, title in USES.items():
        state = "on" if name in s["settings"]["uses"] else "off"
        if name not in s["allowed_uses"]:
            state = "off (by the administrator)"
        turn = "head turn" if name in s["settings"]["gesture"] else ""
        print(f"  {name:<8}{title:<16}{state:<6}{turn}")


def set_option(args) -> None:
    name, value = args.option, args.value
    if name == "head-turn":
        change(args, gesture=pick(value))
    elif name == "attention":
        if value not in (["on"], ["off"]):
            raise SystemExit("on or off")
        change(args, attention=value == ["on"])
    elif name == "level":
        if len(value) != 1 or value[0] not in LEVELS:
            raise SystemExit(f"choose from: {', '.join(LEVELS)}")
        change(args, level=value[0])
    print("Saved.")


def cameras(args) -> None:
    found = []
    for kind, payload in Request("cameras", probe=args.probe or None):
        if kind == "INFO":
            print(f"\r\033[K{payload} …", end="", flush=True)
        elif kind == "OK":
            found = json.loads(payload)
    if args.probe:
        print("\r\033[K", end="")
    kinds = {"ir": "infrared", "maybe-ir": "infrared?", "color": "colour",
             "unsupported": "not usable"}
    for c in found:
        print(f"{kinds[c['kind']]:<11} {c['node']:<13} {c['name']}")
        print(f"{'':<11} {c['id']}  {c['mode']}")
        notes = list(c["reasons"])
        if c["privacy"]:
            notes.append("privacy shutter closed")
        if "error" in c:
            notes.append(c["error"])
        if "probe" in c:
            p = c["probe"]
            notes += [f"{p['fps']} fps", "monochrome" if p["mono"] else "colour picture",
                      "emitter strobes" if p["strobe"] else "no strobe"]
        if notes:
            print(f"{'':<11} {', '.join(notes)}")
    if not found:
        print("no cameras found")


def history(args) -> None:
    from .engine import MESSAGES
    for e in json.loads(call("history", user=args.user, clear=args.clear or None)):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["time"]))
        mark = "✓" if e["result"] == "ok" else "✗"
        what = "unlocked" if e["result"] == "ok" else MESSAGES.get(e["result"], e["result"])
        print(f"{when}  {mark}  {e['service']:<14} {e['seconds']:>4}s  {what}")


def doctor(args) -> None:
    """Check each link of the chain, so a failure points at its cause."""
    from .camera import CameraError, discover, pick_color, pick_ir, probe
    from .vision import verify_models
    failed = False

    def report(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal failed
        failed |= ok is False
        mark = {True: "ok  ", False: "FAIL", None: "note"}[ok]
        print(f"[{mark}] {what}" + (f": {detail}" if detail else ""))

    cfg = config.Config()
    try:
        cfg = config.Config.load()
        report(True, "configuration")
    except (ValueError, OSError) as e:
        report(False, "configuration", str(e))
    import cv2
    version = tuple(int(x) for x in cv2.__version__.split(".")[:2])
    report(version >= (4, 8), "OpenCV", cv2.__version__ +
           ("" if version >= (4, 8) else " is too old, 4.8 or newer is needed"))
    try:
        verify_models(config.MODEL_DIR)
        report(True, "models", str(config.MODEL_DIR))
    except (ValueError, OSError) as e:
        report(False, "models", str(e))
    found = discover()
    for c in found:
        report(None, f"camera {c.node}", f"{c.kind}, {c.name} ({', '.join(c.reasons) or 'plain'})")
    try:
        ir = pick_ir(found, None, cfg.device)
        report(True, "infrared camera", ir.id)
        if ir.placement == "removable":
            report(None, "placement", "external camera: weaker than a built-in one")
        report(True if ir.msxu else None, "Windows Hello emitter control",
               f"unit {ir.msxu}" if ir.msxu else "absent")
        try:
            p = probe(ir)
            report(p["frames"] > 0, "capture", f"{p['fps']} fps")
            report(p["bright"], "illumination", "" if p["bright"] else
                   "the picture stays black: the emitter is off (see emitter_control)")
            report(True if p["strobe"] else None, "emitter strobe",
                   "yes" if p["strobe"] else "no, liveness checks are weaker")
        except CameraError as e:
            report(False, "capture", str(e))
        color = pick_color(found, ir, cfg.color_device)
        report(True if color else None, "colour camera", color.id if color else "none")
    except CameraError as e:
        report(False, "infrared camera", str(e))
    try:
        report(True, "service", call("ping"))
    except DaemonError as e:
        report(False, "service", str(e))
    tpm = bool(list(Path("/dev").glob("tpmrm*")))
    report(True if tpm else None, "TPM", "" if tpm else "absent: key tied to this disk only")
    try:
        pam = Path("/etc/pam.d/common-auth").read_text()
        active = "pam_peekaboo.so" in pam
        report(True if active else None, "PAM", "enabled" if active else
               "not enabled (sudo pam-auth-update --enable peekaboo peekaboo-password)")
        if active and "note_password" not in pam:
            report(False, "PAM", "peekaboo-password is missing, so passwords go unnoticed")
    except OSError:
        report(None, "PAM", "this distribution has no common-auth; see the README")
    if failed:
        raise SystemExit(1)


def animation(args) -> None:
    if os.geteuid() == 0:
        raise SystemExit("run this as yourself, not with sudo")
    from . import animation as anim
    if not anim.available():
        raise SystemExit("the animation needs GNOME Shell")
    if args.state == "studio":
        anim.open_studio()
        return
    if args.state:
        anim.set_enabled(args.state == "on")
    print("Animation:", "on" if anim.enabled() else "off")
    if anim.enabled() and not anim.loaded():
        print("Log out and in once for GNOME to load it.")


def gui(args) -> None:
    from .gui import main as run
    sys.exit(run())


def logs(args) -> None:
    subprocess.run(["journalctl", "-u", "peekaboo.service", "-n", "40", "--no-pager"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="peekaboo", description="Infrared face unlock")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--user", default=default_user(), help="account (root only)")
    sub = parser.add_subparsers(required=True, metavar="COMMAND")

    def command(name, run, help, **kw):
        p = sub.add_parser(name, help=help, **kw)
        p.set_defaults(run=run)
        return p

    command("gui", gui, "open the app")
    command("status", status, "enrolled faces, settings and state")
    p = command("enroll", enroll, "enroll a face with both cameras")
    p.add_argument("label", nargs="?", default="Face")
    p.add_argument("--ir-only", action="store_true", help="leave the colour camera out")
    p.add_argument("--camera", help="identity of the infrared camera (see: cameras)")
    p = command("test", test, "try recognition and show scores")
    p.add_argument("--gesture", action="store_true", help="also require the head turn")
    p = command("remove", remove, "remove a face")
    p.add_argument("id", type=int, nargs="?")
    p.add_argument("--all", action="store_true")
    p = command("use", use, "choose what Peekaboo is used for",
                description="Use Peekaboo only for the things listed: lock, login, shell, "
                            "system. Also: all, none. With nothing listed, show the choice.")
    p.add_argument("what", nargs="*", metavar="USE")
    p = command("set", set_option, "change a security setting",
                description="set head-turn USE...|all|none, set attention on|off, "
                            "set level standard|high|maximum")
    p.add_argument("option", choices=["head-turn", "attention", "level"])
    p.add_argument("value", nargs="+")
    p = command("cameras", cameras, "list cameras and how they were recognised")
    p.add_argument("--probe", action="store_true", help="look through each one")
    p = command("history", history, "recent unlocks")
    p.add_argument("--clear", action="store_true")
    p = command("animation", animation, "GNOME animation: on, off, or studio to customise")
    p.add_argument("state", nargs="?", choices=["on", "off", "studio"])
    command("doctor", doctor, "check hardware and installation")
    command("logs", logs, "service log")
    args = parser.parse_args()
    try:
        args.run(args)
    except DaemonError as e:
        print(f"\r\033[K{e}" if sys.stdout.isatty() else e, file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
