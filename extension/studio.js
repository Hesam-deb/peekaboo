// The animation settings, with a live preview. Used by the extension's preferences
// and by studio-app.js, which also works before GNOME has loaded the extension.
import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';

import {BLEED, EFFECTS, Gaze, STYLES, optionsFrom, paint, parseColor} from './glyphs.js';

const POSITIONS = [['top', 'Top'], ['middle', 'Middle'], ['bottom', 'Bottom']];
const COLORS = [['white', 'White'], ['accent', 'System accent'], ['custom', 'Custom']];
const now = () => GLib.get_monotonic_time() / 1e6;

// Runs `peekaboo`'s head tracking for the preview, if the service is there.
class Tracking {
    constructor(onLook, onEnd) {
        this._connection = null;
        this._cancellable = new Gio.Cancellable();
        const address = Gio.UnixSocketAddress.new('/run/peekaboo/peekaboo.sock');
        new Gio.SocketClient().connect_async(address, this._cancellable, (client, result) => {
            try {
                this._connection = client.connect_finish(result);
                this._connection.get_output_stream().write_all(
                    new TextEncoder().encode('{"op":"demo"}\n'), null);
                const lines = new Gio.DataInputStream({
                    base_stream: this._connection.get_input_stream(),
                });
                const next = () => lines.read_line_async(0, this._cancellable, (stream, res) => {
                    try {
                        const [line] = stream.read_line_finish_utf8(res);
                        if (line === null || line.startsWith('FAIL') || line.startsWith('OK')) {
                            onEnd(line?.startsWith('FAIL')
                                ? line.split(' ').slice(2).join(' ') : null);
                            return;
                        }
                        if (line.startsWith('LOOK '))
                            onLook(JSON.parse(line.slice(5)));
                        next();
                    } catch (e) {
                        if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                            onEnd(`${e.message}`);
                    }
                });
                next();
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    onEnd('The Peekaboo service is not running');
            }
        });
    }

    stop() {
        this._cancellable.cancel();
        this._connection?.close_async(0, null, null);
    }
}

// A figure that plays through its states, or follows the pointer or a real head.
function figure(settings, desktop, {size, style, follow = null, loop = true}) {
    const area = new Gtk.DrawingArea({
        content_width: Math.round(size * BLEED), content_height: Math.round(size * BLEED),
        halign: Gtk.Align.CENTER,
    });
    const gaze = new Gaze();
    const born = now();
    let state = 'scanning', since = born, forced = false;
    area.play = (wanted, turn = false) => {
        state = wanted;
        since = now();
        forced = true;
        area.turn = turn;
    };
    area.gaze = gaze;
    area.set_draw_func((_area, cr, w, h) => {
        const time = now(), options = optionsFrom(settings, desktop.get_string('accent-color'));
        if (style)
            options.style = style;
        if (forced && state !== 'scanning' && time - since > 2) {
            state = 'scanning';
            since = time;
            forced = false;
        } else if (!forced && loop) {
            // Looks around, recognises somebody, starts over.
            const phase = (time - born) % 6;
            const wanted = phase < 4 ? 'scanning' : 'ok';
            if (wanted !== state) {
                state = wanted;
                since = time;
            }
        }
        follow?.(gaze, time);
        gaze.step(time, true);
        paint(cr, w, h, size, {
            state, t: (time - since) * options.speed, life: (time - born) * options.speed,
            look: gaze.value, seen: gaze.seen, progress: gaze.progress,
            turn: Boolean(area.turn), options,
        });
        cr.$dispose();
    });
    area.add_tick_callback(() => {
        area.queue_draw();
        return GLib.SOURCE_CONTINUE;
    });
    return area;
}

function choice(title, subtitle, options, settings, key) {
    const row = new Adw.ComboRow({
        title, subtitle, model: Gtk.StringList.new(options.map(([, label]) => label)),
    });
    const sync = () => {
        row.selected = Math.max(0, options.findIndex(([id]) => id === settings.get_string(key)));
    };
    sync();
    settings.connect(`changed::${key}`, sync);
    row.connect('notify::selected', () => {
        const id = options[row.selected][0];
        if (id !== settings.get_string(key))
            settings.set_string(key, id);
    });
    return row;
}

function toggle(title, subtitle, settings, key) {
    const row = new Adw.SwitchRow({title, subtitle});
    settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
    return row;
}

function colorRow(title, settings, key) {
    const button = new Gtk.ColorDialogButton({
        dialog: new Gtk.ColorDialog({with_alpha: false}), valign: Gtk.Align.CENTER,
    });
    const sync = () => {
        const [red, green, blue] = parseColor(settings.get_string(key));
        button.rgba = new Gdk.RGBA({red, green, blue, alpha: 1});
    };
    sync();
    settings.connect(`changed::${key}`, sync);
    button.connect('notify::rgba', () => {
        const {red, green, blue} = button.rgba;
        const hex = `#${[red, green, blue].map(v =>
            Math.round(v * 255).toString(16).padStart(2, '0')).join('')}`;
        if (hex !== settings.get_string(key))
            settings.set_string(key, hex);
    });
    const row = new Adw.ActionRow({title, activatable_widget: button});
    row.add_suffix(button);
    return row;
}

function slider(title, settings, key, lower, upper, step, format) {
    const scale = new Gtk.Scale({
        adjustment: new Gtk.Adjustment({lower, upper, step_increment: step}),
        hexpand: true, width_request: 220, draw_value: true, value_pos: Gtk.PositionType.LEFT,
        valign: Gtk.Align.CENTER,
    });
    scale.set_format_value_func((_s, value) => format(value));
    settings.bind(key, scale.adjustment, 'value', Gio.SettingsBindFlags.DEFAULT);
    const row = new Adw.ActionRow({title});
    row.add_suffix(scale);
    return row;
}

export function buildPage(settings) {
    const desktop = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
    const page = new Adw.PreferencesPage({
        title: 'Animation', icon_name: 'preferences-desktop-appearance-symbolic',
    });

    // Preview
    const stage = new Adw.PreferencesGroup();
    const card = new Gtk.Box({
        orientation: Gtk.Orientation.VERTICAL, spacing: 6, css_classes: ['card', 'peekaboo-stage'],
        overflow: Gtk.Overflow.HIDDEN,
    });
    const css = new Gtk.CssProvider();
    css.load_from_string('.peekaboo-stage { background: linear-gradient(160deg, #2b3a67, #16213e 55%, #0f1526); } ' +
        '.peekaboo-caption { color: white; font-weight: bold; } .peekaboo-tile { padding: 4px; }');
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 600);

    let pointer = null, tracked = null, tracking = null;
    const follow = (gaze, time) => {
        if (tracked && time - tracked.at < 0.7)
            gaze.aim(tracked.look, 0, tracked.at);
        else if (pointer)
            gaze.aim(pointer, 0, time);
    };
    const preview = figure(settings, desktop, {size: 150, follow, loop: false});
    preview.margin_top = 6;
    const motion = new Gtk.EventControllerMotion();
    motion.connect('motion', (_c, x, y) => {
        const w = card.get_width(), h = card.get_height();
        pointer = {yaw: 2 * x / w - 1, pitch: 2 * y / h - 1, x: 0, y: 0, open: 1};
    });
    motion.connect('leave', () => (pointer = null));
    card.add_controller(motion);
    const caption = new Gtk.Label({
        label: 'Move the pointer here, or try it with your head',
        css_classes: ['peekaboo-caption'], wrap: true, justify: Gtk.Justification.CENTER,
    });
    const buttons = new Gtk.Box({
        spacing: 6, halign: Gtk.Align.CENTER, margin_top: 6, margin_bottom: 14,
    });
    const button = (label, run) => {
        const b = new Gtk.Button({label, css_classes: ['osd', 'pill']});
        b.connect('clicked', run);
        buttons.append(b);
        return b;
    };
    button('Success', () => {
        preview.play('ok');
        caption.label = settings.get_string('greeting') || ' ';
    });
    button('Failure', () => {
        preview.play('fail');
        caption.label = 'Face not recognised';
    });
    const live = button('Follow me', () => {
        if (tracking) {
            tracking.stop();
            end(null);
            return;
        }
        live.label = 'Stop';
        caption.label = 'Look around, turn your head, blink';
        tracking = new Tracking(look => (tracked = {look, at: now()}), end);
    });
    function end(error) {
        tracking = null;
        tracked = null;
        live.label = 'Follow me';
        caption.label = error ?? 'Move the pointer here, or try it with your head';
    }
    card.connect('unmap', () => {
        tracking?.stop();
        tracking = null;
    });
    card.append(preview);
    card.append(caption);
    card.append(buttons);
    stage.add(card);
    page.add(stage);

    // Styles
    const styles = new Adw.PreferencesGroup({title: 'Style'});
    const grid = new Gtk.FlowBox({
        selection_mode: Gtk.SelectionMode.NONE, min_children_per_line: 2,
        max_children_per_line: 4, homogeneous: true, row_spacing: 6, column_spacing: 6,
    });
    let first = null;
    for (const [id, label, description] of STYLES) {
        const box = new Gtk.Box({orientation: Gtk.Orientation.VERTICAL});
        box.append(figure(settings, desktop, {size: 62, style: id}));
        box.append(new Gtk.Label({label, css_classes: ['caption-heading']}));
        const tile = new Gtk.ToggleButton({
            child: box, tooltip_text: description, css_classes: ['flat', 'peekaboo-tile'],
        });
        if (first)
            tile.group = first;
        first ??= tile;
        const sync = () => (tile.active = settings.get_string('style') === id);
        sync();
        settings.connect('changed::style', sync);
        tile.connect('toggled', () => {
            if (tile.active && settings.get_string('style') !== id)
                settings.set_string('style', id);
        });
        grid.append(tile);
    }
    styles.add(grid);
    page.add(styles);

    const behaviour = new Adw.PreferencesGroup({title: 'Behaviour'});
    behaviour.add(toggle('Follow my head',
        'Mirror where you look, how you turn and when you blink', settings, 'follow'));
    behaviour.add(choice('Celebration', 'When you are recognised', EFFECTS, settings, 'effect'));
    behaviour.add(slider('Speed', settings, 'speed', 0.5, 2, 0.1, v => `${v.toFixed(1)}×`));
    page.add(behaviour);

    const look = new Adw.PreferencesGroup({title: 'Appearance'});
    look.add(choice('Colour', '', COLORS, settings, 'color-mode'));
    const custom = colorRow('Custom colour', settings, 'custom-color');
    const showCustom = () => (custom.visible = settings.get_string('color-mode') === 'custom');
    showCustom();
    settings.connect('changed::color-mode', showCustom);
    look.add(custom);
    look.add(colorRow('Recognised', settings, 'success-color'));
    look.add(colorRow('Not recognised', settings, 'failure-color'));
    look.add(slider('Size', settings, 'size', 64, 240, 4, v => `${Math.round(v)}`));
    look.add(slider('Backdrop', settings, 'backdrop', 0, 1, 0.05, v => `${Math.round(v * 100)}%`));
    look.add(choice('Position', '', POSITIONS, settings, 'position'));
    page.add(look);

    const words = new Adw.PreferencesGroup({title: 'Words'});
    words.add(toggle('Captions', 'Hints below the figure', settings, 'captions'));
    const greeting = new Adw.EntryRow({title: 'Greeting', show_apply_button: true});
    greeting.text = settings.get_string('greeting');
    greeting.connect('apply', () => settings.set_string('greeting', greeting.text.slice(0, 60)));
    words.add(greeting);
    page.add(words);

    const places = new Adw.PreferencesGroup({title: 'Show On'});
    places.add(toggle('Lock screen', '', settings, 'on-lock'));
    places.add(toggle('System prompts', 'Password dialogs from apps and settings',
        settings, 'on-system'));
    places.add(toggle('Terminal', 'sudo and su', settings, 'on-shell'));
    const reset = new Gtk.Button({
        label: 'Reset to Defaults', halign: Gtk.Align.CENTER, margin_top: 18,
        css_classes: ['pill'],
    });
    reset.connect('clicked', () => {
        for (const key of settings.settings_schema.list_keys())
            settings.reset(key);
        greeting.text = settings.get_string('greeting');
    });
    places.add(reset);
    page.add(places);
    return page;
}
