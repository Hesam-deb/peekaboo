"""Peekaboo app: enrol, configure, test (GTK 4 / libadwaita)."""
from __future__ import annotations

import base64
import json
import math
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import __version__, animation, config  # noqa: E402
from .client import DaemonError, Request  # noqa: E402
from .engine import MESSAGES  # noqa: E402

APP_ID = "io.github.hesam_deb.Peekaboo"
WEBSITE = "https://github.com/Hesam-deb/peekaboo"
USE_HELP = {
    "lock": ("system-lock-screen-symbolic", "Unlock your session"),
    "login": ("avatar-default-symbolic", "Leaves the login keyring locked"),
    "shell": ("utilities-terminal-symbolic", "sudo and su"),
    "system": ("dialog-password-symbolic", "Password dialogs from apps and settings"),
}
LEVEL_NAMES = [("standard", "Standard"), ("high", "High"), ("maximum", "Maximum")]
TICKS = 96
SECTORS = 8
ACCENT = Gdk.RGBA(0.42, 0.36, 1.0, 1)


def pam_enabled() -> bool | None:
    """None where PAM is organised differently and we cannot tell."""
    try:
        return "pam_peekaboo.so" in open("/etc/pam.d/common-auth").read()
    except OSError:
        return None


class Job:
    """Runs a daemon request on a thread and delivers lines on the main loop."""

    def __init__(self, on_line, on_done, op: str, **args):
        self.request: Request | None = None
        self.stopped = False
        threading.Thread(target=self._run, args=(on_line, on_done, op, args),
                         daemon=True).start()

    def _run(self, on_line, on_done, op, args) -> None:
        error = None
        try:
            self.request = Request(op, **args)
            for kind, payload in self.request:
                GLib.idle_add(self._deliver, on_line, kind, payload)
        except DaemonError as e:
            error = e
        except OSError as e:
            error = DaemonError("unavailable", str(e))
        GLib.idle_add(self._deliver, on_done, error)

    def _deliver(self, callback, *args) -> bool:
        if not self.stopped:
            callback(*args)
        return GLib.SOURCE_REMOVE

    def stop(self) -> None:
        self.stopped = True
        if self.request:
            self.request.cancel()


def fetch(op: str, done, **args) -> Job:
    """Run a request and call done(payload, error) with its answer."""
    answer: list[str] = []
    return Job(lambda kind, p: answer.append(p) if kind == "OK" else None,
               lambda error: done(answer[0] if answer else "", error), op, **args)


class Ring(Gtk.Widget):
    """Round preview inside a ring of ticks that fills as the head turns.

    With two cameras the colour picture fills the circle and infrared sits in a
    small one beside it.
    """

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN,
                         accessible_role=Gtk.AccessibleRole.IMG)
        self.set_size_request(280, 280)
        self.infrared: Gdk.Texture | None = None
        self.color: Gdk.Texture | None = None
        self.color_at = 0.0
        self.sectors: set[int] = set()
        self.look = (0.0, 0.0)
        self.pointer = [0.0, 0.0]
        self.level = [0.0] * TICKS
        self.done = False
        self.last = time.monotonic()
        self.add_tick_callback(self._animate)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Face enrolment progress"])

    def set_image(self, jpeg: bytes, color: bool) -> None:
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(jpeg))
        except GLib.Error:
            return
        if color:
            self.color, self.color_at = texture, time.monotonic()
        else:
            self.infrared = texture
        self.queue_draw()

    def _animate(self, *_):
        now = time.monotonic()
        step, self.last = min((now - self.last) * 4, 1.0), now
        for i in range(TICKS):
            sector = round(i / TICKS * SECTORS) % SECTORS
            target = 1.0 if self.done or sector in self.sectors else 0.0
            self.level[i] += (target - self.level[i]) * step
        for i in (0, 1):
            self.pointer[i] += (self.look[i] - self.pointer[i]) * min(step * 2.5, 1)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _picture(self, snapshot, texture, cx, cy, radius) -> None:
        circle = Graphene.Rect().init(cx - radius, cy - radius, 2 * radius, 2 * radius)
        clip = Gsk.RoundedRect()
        clip.init_from_rect(circle, radius)
        snapshot.push_rounded_clip(clip)
        snapshot.append_color(Gdk.RGBA(.06, .06, .07, 1), circle)
        if texture:
            w, h = texture.get_width(), texture.get_height()
            scale = 2 * radius / min(w, h)
            snapshot.append_texture(texture, Graphene.Rect().init(
                cx - w * scale / 2, cy - h * scale / 2, w * scale, h * scale))
        snapshot.pop()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        cx, cy = width / 2, height / 2
        outer = min(width, height) / 2 - 6
        inner = outer - 34
        if inner <= 0:
            return
        both = self.color and time.monotonic() - self.color_at < 1.5
        self._picture(snapshot, self.color if both else self.infrared, cx, cy, inner)

        fg = self.get_color()
        px, py = self.pointer
        aim, pull = math.atan2(py, px), min(math.hypot(px, py), 1.0)
        for i, level in enumerate(self.level):
            angle = 2 * math.pi * i / TICKS
            c, s = math.cos(angle), math.sin(angle)
            # A soft highlight shows which way the head points right now.
            near = math.cos(angle - aim)
            hint = 0.0 if self.done else max(0.0, near) ** 10 * pull * (1 - level)
            start, end = inner + 10, inner + 18 + 10 * level + 6 * hint
            path = Gsk.PathBuilder()
            path.move_to(cx + start * c, cy + start * s)
            path.line_to(cx + end * c, cy + end * s)
            stroke = Gsk.Stroke(2 + level)
            stroke.set_line_cap(Gsk.LineCap.ROUND)
            alpha = .22 + .78 * level
            color = Gdk.RGBA(*(a + (b - a) * max(level, hint * .8) for a, b in (
                (fg.red, ACCENT.red), (fg.green, ACCENT.green), (fg.blue, ACCENT.blue),
                (alpha, 1.0))))
            snapshot.append_stroke(path.to_path(), stroke, color)
        if both and self.infrared:
            r = inner * 0.27
            x, y = cx + inner * 0.72, cy + inner * 0.72
            path = Gsk.PathBuilder()
            path.add_circle(Graphene.Point().init(x, y), r + 3)
            snapshot.append_fill(path.to_path(), Gsk.FillRule.WINDING, ACCENT)
            self._picture(snapshot, self.infrared, x, y, r)


class EnrollPage(Adw.NavigationPage):
    def __init__(self, window: "Window", label: str, ir_only: bool, camera: str | None):
        super().__init__(title="Say Peekaboo", tag="enroll")
        self.window = window
        self.ring = Ring()
        self.hint = Gtk.Label(label="Waiting for your password…", wrap=True,
                              justify=Gtk.Justification.CENTER, css_classes=["title-3"],
                              height_request=64, valign=Gtk.Align.CENTER)
        self.cameras = Gtk.Label(css_classes=["dim-label"])
        self.button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER, css_classes=["pill"])
        self.button.connect("clicked", lambda *_: window.nav.pop())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=36, margin_start=24, margin_end=24)
        for child in (self.ring, self.cameras, self.hint, self.button):
            box.append(child)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.job = Job(self._line, self._done, "enroll", label=label, preview=True,
                       ir_only=ir_only or None, camera=camera)
        self.connect("hidden", lambda *_: self.job.stop())

    def _line(self, kind: str, payload: str) -> None:
        if kind in ("IMAGE", "COLOR"):
            self.ring.set_image(base64.b64decode(payload), kind == "COLOR")
        elif kind == "STATE":
            state = json.loads(payload)
            self.hint.set_label(state["hint"] or "")
            self.ring.sectors = set(state["sectors"])
            look = state.get("look")
            self.ring.look = (look["yaw"], look["pitch"]) if look else (0, 0)
            self.cameras.set_label({None: "Infrared camera", True: "Infrared and colour cameras",
                                    False: "Infrared camera (too dark for colour)"
                                    }[state["color"]])
        elif kind == "OK":
            result = json.loads(payload)
            notes = []
            if result["external"]:
                notes.append("This is an external camera: someone could swap it for a device "
                             "that replays your face.")
            if not result["strobed"]:
                notes.append("This camera’s emitter does not strobe, so pictures are harder to "
                             "tell apart. Consider the head turn everywhere.")
            self.cameras.set_label(" ".join(notes) or (
                "Both cameras agreed. Only infrared was stored." if result["checked"]
                else "Registered with the infrared camera"))
            self.cameras.set_wrap(True)

    def _done(self, error) -> None:
        self.window.refresh()
        self.ring.look = (0, 0)
        if error:
            self.hint.set_label(str(error))
            self.button.set_label("Close")
            return
        self.ring.done = True
        self.hint.set_label("Peekaboo! You’re all set.")
        self.button.set_label("Done")
        self.button.add_css_class("suggested-action")


class TestDialog(Adw.Dialog):
    def __init__(self):
        super().__init__(title="Test", content_width=380, content_height=360)
        self.status = Adw.StatusPage(icon_name=APP_ID, title="Look at the camera")
        view = Adw.ToolbarView(content=self.status)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.job = Job(self._line, self._done, "test")
        self.connect("closed", lambda *_: self.job.stop())

    def _line(self, kind: str, payload: str) -> None:
        if kind == "FRAME":
            self.status.set_description(f"Similarity {json.loads(payload)['score']:.2f}")
        elif kind == "INFO":
            self.status.set_description(payload)
        elif kind == "OK":
            result = json.loads(payload)
            self.status.set_icon_name("object-select-symbolic")
            self.status.set_title(f"Peekaboo, {GLib.markup_escape_text(result['label'])}!")
            self.status.set_description(
                f"Similarity {result['score']:.2f} in {result['seconds']:.1f} s")

    def _done(self, error) -> None:
        if error:
            self.status.set_icon_name("dialog-warning-symbolic")
            self.status.set_title(GLib.markup_escape_text(str(error)))


class Page(Adw.PreferencesPage):
    """A preferences page whose groups are rebuilt on every refresh."""

    def __init__(self, window: "Window", **kw):
        super().__init__(**kw)
        self.window = window
        self.groups: list[Adw.PreferencesGroup] = []

    def clear(self) -> None:
        for g in self.groups:
            self.remove(g)
        self.groups.clear()

    def group(self, title: str = "", description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.add(g)
        self.groups.append(g)
        return g


def button_row(title: str, icon: str, run) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, activatable=True)
    row.add_prefix(Gtk.Image(icon_name=icon))
    row.connect("activated", lambda *_: run())
    return row


def info_row(title: str, value: str, icon: str | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value),
                        css_classes=["property"])
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    return row


class HomePage(Page):
    def fill(self, s: dict) -> None:
        self.clear()
        hero = self.group()
        problem = None
        if not s["ir"]:
            problem = "No infrared camera found"
        elif s["ir"]["privacy"]:
            problem = "The camera’s privacy shutter is closed"
        elif s["blocked"]:
            problem = s["blocked"]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                      margin_bottom=6)
        box.append(Gtk.Image(icon_name=APP_ID, pixel_size=112))
        title = "Set up Peekaboo" if not s["faces"] else \
            "Peekaboo is ready" if not problem else "Password needed"
        if not s["ir"]:
            title = "Not available"
        box.append(Gtk.Label(label=title, css_classes=["title-1"]))
        box.append(Gtk.Label(
            label=problem or ("Look at your laptop. That’s it." if s["faces"] else
                              "Both cameras get to know you. Then infrared does the rest."),
            css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER))
        hero.add(box)

        faces = self.group("Faces", "Only an encrypted numeric description is stored, "
                                    "on this laptop. No pictures are kept.")
        for face in s["faces"]:
            when = time.strftime("%-d %B %Y", time.localtime(face["created"]))
            what = "checked by both cameras" if face["checked"] else "infrared camera only"
            if not face["connected"]:
                what = "its camera is not connected"
            row = Adw.ActionRow(title=GLib.markup_escape_text(face["label"]),
                                subtitle=f"{when} · {face['views']} views, {what}")
            row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove face", css_classes=["flat"])
            remove.connect("clicked", lambda _b, f=face: self.confirm_remove(f))
            row.add_suffix(remove)
            faces.add(row)
        add = button_row("Add a face" if s["faces"] else "Set up your face",
                         "list-add-symbolic", lambda: self.ask(s))
        add.set_sensitive(bool(s["ir"]))
        faces.add(add)
        if s["faces"]:
            faces.add(button_row("Test recognition", "media-playback-start-symbolic",
                                 lambda: TestDialog().present(self.window)))

    def ask(self, s: dict) -> None:
        group = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Name", text="Face" if not s["faces"] else "Alternate look",
                            max_length=40)
        group.add(name)
        both = Adw.SwitchRow(title="Use both cameras", active=bool(s["color"]),
                             sensitive=bool(s["color"]),
                             subtitle="Colour checks infrared while you register. Only "
                             "infrared is stored and used to unlock." if s["color"]
                             else "This laptop has no matching colour camera")
        group.add(both)
        infrared = [c for c in s["cameras"] if c["kind"] in ("ir", "maybe-ir")]
        chooser = None
        if len(infrared) > 1:
            chooser = Adw.ComboRow(title="Infrared camera", model=Gtk.StringList.new(
                [c["name"] + (" (external)" if c["external"] else "") for c in infrared]))
            group.add(chooser)
        dialog = Adw.AlertDialog(
            heading="Say Peekaboo", extra_child=group,
            body="Sit at arm’s length in a lit room, then slowly move your head in a circle.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def respond(_dialog, response: str) -> None:
            if response == "ok":
                camera = infrared[chooser.get_selected()]["id"] if chooser else None
                self.window.nav.push(EnrollPage(self.window, name.get_text().strip() or "Face",
                                                not both.get_active(), camera))

        dialog.connect("response", respond)
        dialog.present(self.window)

    def confirm_remove(self, face: dict) -> None:
        dialog = Adw.AlertDialog(heading="Remove Face?",
                                 body=f"“{face['label']}” will no longer unlock this laptop.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def respond(_dialog, response: str) -> None:
            if response == "remove":
                fetch("remove", lambda _p, e: self.window.finished(e), id=face["id"])

        dialog.connect("response", respond)
        dialog.present(self.window)


class SecurityPage(Page):
    def fill(self, s: dict) -> None:
        self.clear()
        settings, allowed = s["settings"], set(s["allowed_uses"])
        if s["faces"] and pam_enabled() is False:
            off = self.group()
            row = info_row("Not switched on for this system yet",
                           "sudo pam-auth-update --enable peekaboo peekaboo-password",
                           "dialog-warning-symbolic")
            row.set_subtitle_selectable(True)
            off.add(row)

        uses = self.group("Use Peekaboo For", "Anything that is off asks for your password.")
        for name, title in config.USES.items():
            icon, help_text = USE_HELP[name]
            row = Adw.SwitchRow(title=title, sensitive=name in allowed,
                                active=name in allowed and name in settings["uses"],
                                subtitle=help_text if name in allowed
                                else "Turned off by the administrator")
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.connect("notify::active", lambda r, _p, n=name: self.change(
                uses=self.toggled(settings["uses"], n, r.get_active())))
            uses.add(row)

        turn = self.group("Ask for a Head Turn",
                          "A small turn proves you are not a picture, and that you mean it.")
        for name, title in config.USES.items():
            row = Adw.SwitchRow(title=title, active=name in settings["gesture"])
            row.connect("notify::active", lambda r, _p, n=name: self.change(
                gesture=self.toggled(settings["gesture"], n, r.get_active())))
            turn.add(row)

        checks = self.group("Checks")
        row = Adw.SwitchRow(title="Attention", active=settings["attention"],
                            subtitle="Eyes open and facing the camera")
        row.connect("notify::active", lambda r, _p: self.change(attention=r.get_active()))
        checks.add(row)
        level = Adw.ComboRow(title="Strictness", subtitle="Higher rejects strangers, and "
                             "sometimes you, more readily",
                             model=Gtk.StringList.new([n for _i, n in LEVEL_NAMES]))
        level.set_selected([i for i, _n in LEVEL_NAMES].index(settings["level"]))
        level.connect("notify::selected", lambda r, _p: self.change(
            level=LEVEL_NAMES[r.get_selected()][0]))
        checks.add(level)
        checks.add(info_row("Failed attempts", f"{s['failures']} of {s['max_failures']}"))

    @staticmethod
    def toggled(current: list[str], name: str, on: bool) -> list[str]:
        return [u for u in config.USES if (u == name and on) or (u != name and u in current)]

    def change(self, **settings) -> None:
        self.set_sensitive(False)      # one change at a time; a prompt may be open

        def done(_payload, error) -> None:
            self.set_sensitive(True)
            self.window.finished(error)

        fetch("settings", done, set=settings)


class AnimationPage(Page):
    def fill(self, _s: dict) -> None:
        self.clear()
        g = self.group("Animation", "A little friend that follows your head, on the lock "
                                    "screen, in system prompts and for the terminal.")
        if not animation.available():
            g.add(info_row("Not available", "The animation needs GNOME Shell.",
                           "dialog-information-symbolic"))
            return
        row = Adw.SwitchRow(title="Show the animation", active=animation.enabled(),
                            subtitle=self.help())

        def changed(r, _p) -> None:
            animation.set_enabled(r.get_active())
            r.set_subtitle(self.help())

        row.connect("notify::active", changed)
        g.add(row)
        g.add(button_row("Open Peekaboo Studio", "preferences-desktop-appearance-symbolic",
                         self.studio))
        about = self.group()
        about.add(info_row("In the studio", "8 styles, celebrations, colours, size, position, "
                                            "speed, captions and where it appears, with a live "
                                            "preview that follows your head."))

    @staticmethod
    def help() -> str:
        if animation.enabled() and not animation.loaded():
            return "Log out and in once to load it"
        return "Draws only: it cannot get in the way of a password prompt"

    def studio(self) -> None:
        try:
            animation.open_studio()
        except (RuntimeError, OSError) as e:
            self.window.toast(str(e))


class ActivityPage(Page):
    def fill(self, _s: dict) -> None:
        fetch("history", self.show)

    def show(self, payload: str, error) -> None:
        self.clear()
        entries = [] if error else json.loads(payload)
        if not entries:
            g = self.group()
            g.add(Adw.StatusPage(icon_name="document-open-recent-symbolic",
                                 title="Nothing Yet", description="Unlocks will show up here."))
            return
        day, g = None, None
        for e in reversed(entries):
            moment = time.localtime(e["time"])
            today = time.strftime("%A, %-d %B", moment)
            if today != day:
                day, g = today, self.group(today)
            ok = e["result"] == "ok"
            row = Adw.ActionRow(
                title=config.USES.get(e["use"], e["service"]),
                subtitle=("Recognised in %.1f s" % e["seconds"]) if ok
                else MESSAGES.get(e["result"], e["result"]))
            row.add_prefix(Gtk.Image(icon_name="object-select-symbolic" if ok
                                     else "window-close-symbolic",
                                     css_classes=["success" if ok else "warning"]))
            row.add_suffix(Gtk.Label(label=time.strftime("%H:%M", moment),
                                     css_classes=["dim-label", "numeric"]))
            g.add(row)
        end = self.group()
        end.add(button_row("Clear activity", "user-trash-symbolic", lambda: fetch(
            "history", lambda _p, e: self.window.finished(e), clear=True)))


class CameraPage(Page):
    KINDS = {"ir": ("camera-video-symbolic", "Infrared"),
             "maybe-ir": ("dialog-question-symbolic", "Possibly infrared"),
             "color": ("camera-photo-symbolic", "Colour"),
             "unsupported": ("action-unavailable-symbolic", "Not usable")}

    def fill(self, s: dict) -> None:
        self.show(s["cameras"], s)

    def show(self, cameras: list[dict], s: dict) -> None:
        self.clear()
        used = {c["id"] for c in (s["ir"], s["color"]) if c}
        g = self.group("Cameras", "Found by what each camera says about itself. Enrolled faces "
                                  "only work with the camera they were enrolled with.")
        for c in cameras:
            icon, kind = self.KINDS[c["kind"]]
            notes = [c["mode"]] if c["mode"] else []
            notes += c["reasons"]
            if c["privacy"]:
                notes.append("privacy shutter closed")
            if "probe" in c:
                p = c["probe"]
                notes += [f"{p['fps']} fps", "monochrome" if p["mono"] else "colour picture",
                          "emitter strobes" if p["strobe"] else "no strobe"]
            if "error" in c:
                notes.append(c["error"])
            row = Adw.ActionRow(title=GLib.markup_escape_text(c["name"]),
                                subtitle=GLib.markup_escape_text(" · ".join(notes)))
            row.add_prefix(Gtk.Image(icon_name=icon))
            label = Gtk.Label(label=kind, css_classes=["dim-label"])
            row.add_suffix(label)
            if c["id"] in used:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic",
                                         css_classes=["success"], tooltip_text="In use"))
            g.add(row)
        if not cameras:
            g.add(info_row("No cameras found", "", "dialog-warning-symbolic"))
        check = button_row("Look through the cameras", "view-reveal-symbolic", lambda: None)
        spinner = Gtk.Spinner()
        check.add_suffix(spinner)

        def run(*_) -> None:
            check.set_sensitive(False)
            spinner.start()
            fetch("cameras", done, probe=True)

        def done(payload: str, error) -> None:
            if error:
                self.window.finished(error)
            else:
                self.show(json.loads(payload), s)

        check.connect("activated", run)
        g.add(check)
        if not s["ir"]:
            h = self.group("No Infrared Camera?")
            h.add(info_row("Peekaboo needs one", "A colour camera alone cannot tell a face from "
                           "a photograph. Some recent laptops have MIPI cameras that Linux "
                           "cannot use for infrared yet."))


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Peekaboo", default_width=780,
                         default_height=780, icon_name=APP_ID)
        self.nav = Adw.NavigationView()
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.pages = []
        for cls, name, title, icon in (
                (HomePage, "home", "Home", "go-home-symbolic"),
                (SecurityPage, "security", "Security", "security-high-symbolic"),
                (AnimationPage, "animation", "Animation", "applications-graphics-symbolic"),
                (ActivityPage, "activity", "Activity", "document-open-recent-symbolic"),
                (CameraPage, "cameras", "Cameras", "camera-web-symbolic")):
            page = cls(self)
            self.stack.add_titled_with_icon(page, name, title, icon)
            self.pages.append(page)
        self.missing = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Not Available")
        retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.refresh())
        self.missing.set_child(retry)
        self.switch = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.switch.add_named(Gtk.Box(), "loading")
        self.switch.add_named(self.stack, "pages")
        self.switch.add_named(self.missing, "missing")

        tabs = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=tabs)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About Peekaboo")
        about.connect("clicked", lambda *_: self.about())
        header.pack_end(about)
        view = Adw.ToolbarView(content=self.switch)
        view.add_top_bar(header)
        bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bar)
        # Narrow windows move the switcher to the bottom.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(bar, "reveal", True)
        narrow.connect("apply", lambda *_: header.set_title_widget(None))
        narrow.connect("unapply", lambda *_: header.set_title_widget(tabs))
        self.add_breakpoint(narrow)
        self.set_size_request(360, 420)
        self.nav.add(Adw.NavigationPage(child=view, title="Peekaboo", tag="main"))
        self.refresh()

    def refresh(self) -> None:
        fetch("status", self.show_status)

    def show_status(self, payload: str, error) -> None:
        if error:
            self.missing.set_description(str(error))
            self.switch.set_visible_child_name("missing")
            return
        status = json.loads(payload)
        for page in self.pages:
            page.fill(status)
        self.switch.set_visible_child_name("pages")

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))

    def finished(self, error) -> None:
        if error:
            self.toast(str(error))
        self.refresh()

    def about(self) -> None:
        Adw.AboutDialog(
            application_icon=APP_ID, application_name="Peekaboo", version=__version__,
            developer_name="The Peekaboo contributors", website=WEBSITE,
            issue_url=WEBSITE + "/issues", license_type=Gtk.License.MIT_X11,
            comments="Unlock your laptop by looking at it. Infrared face unlock for Linux.",
        ).present(self)


def main() -> int:
    app = Adw.Application(application_id=APP_ID)
    app.connect("activate", lambda a: (a.get_active_window() or Window(a)).present())
    return app.run([])
