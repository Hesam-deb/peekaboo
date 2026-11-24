// Shows what Peekaboo is doing: on the lock screen, over system prompts and for sudo.
//
// Deliberately passive: it watches a status file written by peekabood and draws in
// its own non-reactive actor. No dialog is patched or touched, so nothing here can
// get between the user and a password prompt.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Layout from 'resource:///org/gnome/shell/ui/layout.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {BLEED, Gaze, optionsFrom, paint} from './glyphs.js';

const STATUS_DIR = '/run/peekaboo';
const STALE_US = 10 * 1000 * 1000;
const WATCHDOG_MS = 15 * 1000;       // peekabood never scans longer than this
const LINGER_MS = {ok: 1100, fail: 1900};
const now = () => GLib.get_monotonic_time() / 1e6;

const Figure = GObject.registerClass(
class PeekabooFigure extends St.DrawingArea {
    _init(size, options) {
        const {scaleFactor} = St.ThemeContext.get_for_stage(global.stage);
        const box = Math.round(size * BLEED * scaleFactor);
        super._init({width: box, height: box, x_align: Clutter.ActorAlign.CENTER});
        this._fraction = 1 / BLEED;
        this._options = options;
        this._gaze = new Gaze();
        this._scene = {state: 'scanning', turn: false};
        this._born = this._since = now();
        this._timeline = null;
        this.connect('destroy', () => this.stop());
    }

    update({state, look, progress, turn}) {
        if (state !== this._scene.state)
            this._since = now();
        this._scene = {state, turn};
        this._gaze.aim(look, progress, now());
        this.queue_repaint();
        if (!St.Settings.get().enable_animations) {
            this.stop();
        } else if (!this._timeline) {
            this._timeline = new Clutter.Timeline({
                actor: this, duration: 1000, repeat_count: -1,
            });
            this._timeline.connect('new-frame', () => this.queue_repaint());
            this._timeline.start();
        }
    }

    stop() {
        this._timeline?.stop();
        this._timeline = null;
    }

    vfunc_repaint() {
        const cr = this.get_context();
        try {
            const [w, h] = this.get_surface_size();
            const time = now(), {speed, follow} = this._options;
            this._gaze.step(time, follow);
            paint(cr, w, h, Math.min(w, h) * this._fraction, {
                ...this._scene,
                t: (time - this._since) * speed, life: (time - this._born) * speed,
                look: this._gaze.value, seen: this._gaze.seen, progress: this._gaze.progress,
                motion: this._timeline !== null, options: this._options,
            });
        } catch (e) {
            this.stop();
            console.error(`Peekaboo: ${e}`);
        } finally {
            cr.$dispose();
        }
    }
});

const Indicator = GObject.registerClass(
class PeekabooIndicator extends St.Widget {
    _init(settings, accent) {
        super._init({reactive: false, can_focus: false, layout_manager: new Clutter.BinLayout()});
        this.add_constraint(new Layout.MonitorConstraint({primary: true}));
        const position = settings.get_string('position');
        const size = settings.get_int('size');
        const {scaleFactor} = St.ThemeContext.get_for_stage(global.stage);
        this._box = new St.Widget({
            layout_manager: new Clutter.BoxLayout({orientation: Clutter.Orientation.VERTICAL}),
            x_expand: true, y_expand: true, opacity: 0,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: {
                middle: Clutter.ActorAlign.CENTER, bottom: Clutter.ActorAlign.END,
            }[position] ?? Clutter.ActorAlign.START,
        });
        // The drawing is larger than the figure, to leave room for the effects.
        const bleed = size * (BLEED - 1) / 2 * scaleFactor;
        const height = Main.layoutManager.primaryMonitor?.height ?? 900;
        this._box.translation_y = position === 'top' ? height * 0.07 - bleed
            : position === 'bottom' ? -(height * 0.06 - bleed) : 0;
        this._box.set_pivot_point(0.5, 0.5);
        this.figure = new Figure(size, optionsFrom(settings, accent));
        this._label = new St.Label({
            style: 'color: white; font-weight: bold; text-align: center;' +
                'text-shadow: 0 1px 4px rgba(0,0,0,0.75);',
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._label.translation_y = 8 * scaleFactor - bleed;
        this._captions = settings.get_boolean('captions');
        this._greeting = settings.get_string('greeting');
        this._box.add_child(this.figure);
        this._box.add_child(this._label);
        this._box.set_scale(0.8, 0.8);
        this.add_child(this._box);
        this._shown = false;
    }

    update(status) {
        const text = status.state === 'ok' ? this._greeting : status.text;
        this._label.text = this._captions ? text : '';
        this._label.visible = this._label.text !== '';
        this.figure.update(status);
        if (this._shown)
            return;
        this._shown = true;
        this._box.remove_all_transitions();
        this._box.ease({
            opacity: 255, scale_x: 1, scale_y: 1,
            duration: 240, mode: Clutter.AnimationMode.EASE_OUT_BACK,
        });
    }

    dismiss(done) {
        this._shown = false;
        this._box.remove_all_transitions();
        this._box.ease({
            opacity: 0, scale_x: 0.88, scale_y: 0.88,
            duration: 220, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onStopped: done,
        });
    }
});

export default class PeekabooExtension extends Extension {
    // Enabled in unlock-dialog mode too: the animation belongs on the lock screen,
    // and has to outlive the switch back to the user session to finish.
    enable() {
        this._indicator = null;
        this._where = null;
        this._timeout = 0;
        this._lastSeen = 0;
        this._settings = this.getSettings();
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._cancellable = new Gio.Cancellable();
        const uid = new Gio.Credentials().get_unix_user();
        this._file = Gio.File.new_for_path(`${STATUS_DIR}/${uid}.state`);
        try {
            this._monitor = this._file.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitor.set_rate_limit(20);
            this._monitor.connect('changed', () => this._read());
        } catch (e) {
            console.error(`Peekaboo: ${e}`);
        }
    }

    disable() {
        this._monitor?.cancel();
        this._monitor = null;
        this._cancellable?.cancel();
        this._cancellable = null;
        this._file = null;
        this._clear();
        this._settings = null;
        this._interface = null;
    }

    _read() {
        this._file?.load_contents_async(this._cancellable, (file, result) => {
            try {
                const [, bytes] = file.load_contents_finish(result);
                this._apply(JSON.parse(new TextDecoder().decode(bytes)));
            } catch (e) {
                if (!(e instanceof GLib.Error))     // missing or cancelled: nothing to show
                    console.error(`Peekaboo: ${e}`);
            }
        });
    }

    // The file is the daemon's, but this runs inside the shell: trust nothing about it.
    _parse(data) {
        const number = v => Number.isFinite(v) ? Math.min(Math.max(v, -1), 1) : 0;
        const look = data?.look && typeof data.look === 'object' ? {
            yaw: number(data.look.yaw), pitch: number(data.look.pitch),
            x: number(data.look.x), y: number(data.look.y), open: number(data.look.open ?? 1),
        } : null;
        return {
            state: data?.state, use: data?.use, time: data?.time, look,
            text: typeof data?.text === 'string' ? data.text.slice(0, 80) : '',
            turn: data?.turn === true, progress: number(data?.progress),
        };
    }

    _apply(data) {
        const status = this._parse(data);
        const age = GLib.get_monotonic_time() - status.time;
        if (!this._settings || !(status.time > this._lastSeen) || !(age < STALE_US))
            return;
        this._lastSeen = status.time;
        const locked = Main.sessionMode.isLocked;
        const wanted = ['lock', 'system', 'shell'].includes(status.use) &&
            (status.use === 'lock') === locked && this._settings.get_boolean(`on-${status.use}`);
        if (!wanted || !['scanning', 'ok', 'fail'].includes(status.state)) {
            this._dismiss();
            return;
        }
        const where = locked ? Main.layoutManager.screenShieldGroup : Main.layoutManager.uiGroup;
        if (this._indicator && this._where !== where)
            this._clear();
        if (!this._indicator) {
            const indicator = new Indicator(this._settings,
                this._interface.get_string('accent-color'));
            indicator.connect('destroy', () => {
                if (this._indicator === indicator)
                    this._indicator = null;
            });
            this._indicator = indicator;
            this._where = where;
            where.add_child(indicator);
        }
        // Dialogs opened since then are above us.
        where.set_child_above_sibling(this._indicator, null);
        this._indicator.update(status);
        this._after(LINGER_MS[status.state] ?? WATCHDOG_MS, () => this._dismiss());
    }

    _after(ms, callback) {
        if (this._timeout)
            GLib.source_remove(this._timeout);
        this._timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeout = 0;
            callback();
            return GLib.SOURCE_REMOVE;
        });
    }

    _dismiss() {
        const indicator = this._indicator;
        if (!indicator)
            return;
        // Re-checked on completion: a new scan may have revived the indicator.
        const asOf = this._lastSeen;
        indicator.dismiss(() => {
            if (this._indicator === indicator && this._lastSeen === asOf)
                this._clear();
        });
    }

    _clear() {
        if (this._timeout)
            GLib.source_remove(this._timeout);
        this._timeout = 0;
        const indicator = this._indicator;
        this._indicator = null;       // first: destroying ends its transitions
        this._where = null;
        indicator?.destroy();
    }
}
