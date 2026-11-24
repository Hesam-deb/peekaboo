// The animations. Pure cairo: no Shell and no GTK, so the lock screen and the
// studio draw exactly the same thing.
//
// Figures live in a 104 unit square centred on the origin. A scene is
//   state     'scanning' | 'ok' | 'fail'
//   t         seconds in this state        life   seconds since it appeared
//   look      {yaw, pitch, x, y, open} smoothed, -1..1, as a mirror shows it
//   seen      0..1, how sure we are that somebody is being tracked
//   turn      a head turn is wanted        progress  0..1 of that turn
//   motion    false when the user asked for reduced motion
//   options   colours and choices, see DEFAULTS
import Cairo from 'cairo';

export const STYLES = [
    ['peekaboo', 'Peekaboo', 'Peeks over your laptop and looks back at you'],
    ['buddy', 'Buddy', 'A friendly face that mirrors your head'],
    ['mesh', 'Mesh', 'A point cloud head that turns with yours'],
    ['iris', 'Iris', 'An eye that meets your gaze and blinks with you'],
    ['padlock', 'Padlock', 'Its keyhole watches you, the shackle pops open'],
    ['radar', 'Radar', 'A sweep that pins down where you are'],
    ['orbit', 'Orbit', 'Moons circling a planet that follows you'],
    ['ring', 'Ring', 'Minimal: only light on a ring of ticks'],
];
export const EFFECTS = [
    ['none', 'None'], ['ripple', 'Ripple'], ['glow', 'Glow'], ['confetti', 'Confetti'],
];
export const DEFAULTS = {
    style: 'peekaboo', effect: 'confetti', follow: true, backdrop: 0.6, speed: 1,
    color: [1, 1, 1], success: [0.34, 0.89, 0.54], failure: [1, 0.48, 0.39],
};
export const UNIT = 104;

const TAU = 2 * Math.PI;
export const clamp = (x, a = 0, b = 1) => Math.min(Math.max(x, a), b);
const smooth = x => { x = clamp(x); return x * x * (3 - 2 * x); };
const lerp = (a, b, t) => a + (b - a) * t;
const mix = (a, b, t) => a.map((v, i) => lerp(v, b[i], t));
const backOut = x => { x = clamp(x) - 1; return 1 + 2.7 * x * x * x + 1.7 * x * x; };
const rand = seed => { const v = Math.sin(seed * 127.1 + 311.7) * 43758.5453; return v - Math.floor(v); };

export function parseColor(text, fallback = [1, 1, 1]) {
    const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(text ?? '');
    return m ? m.slice(1).map(h => parseInt(h, 16) / 255) : fallback;
}

const ACCENTS = {
    blue: '#62a0ea', teal: '#3fc1b0', green: '#57e389', yellow: '#f8e45c', orange: '#ffbe6f',
    red: '#ff7b63', pink: '#f292c3', purple: '#c88ce0', slate: '#a5b1c2',
};

// Reads the settings into options. `accent` is the desktop's accent colour name.
export function optionsFrom(settings, accent) {
    const mode = settings.get_string('color-mode');
    return {
        style: settings.get_string('style'),
        effect: settings.get_string('effect'),
        follow: settings.get_boolean('follow'),
        backdrop: settings.get_double('backdrop'),
        speed: settings.get_double('speed'),
        color: mode === 'custom' ? parseColor(settings.get_string('custom-color'))
            : mode === 'accent' ? parseColor(ACCENTS[accent] ?? ACCENTS.blue) : [1, 1, 1],
        success: parseColor(settings.get_string('success-color'), DEFAULTS.success),
        failure: parseColor(settings.get_string('failure-color'), DEFAULTS.failure),
    };
}

// Turns ~7 measurements a second into fluid motion, and wanders when nobody is there.
export class Gaze {
    constructor() {
        this.value = {yaw: 0, pitch: 0, x: 0, y: 0, open: 1};
        this.seen = 0;
        this.progress = 0;
        this._target = null;
        this._at = -10;
        this._now = null;
        this._progress = 0;
    }

    aim(look, progress, now) {
        if (look) {
            this._target = look;
            this._at = now;
        }
        this._progress = progress ?? 0;
    }

    step(now, follow = true) {
        const dt = this._now === null ? 0 : clamp(now - this._now, 0, 0.1);
        this._now = now;
        const live = follow && this._target !== null && now - this._at < 0.7;
        this.seen += ((live ? 1 : 0) - this.seen) * (1 - Math.exp(-dt * 7));
        const idle = {
            yaw: 0.45 * Math.sin(now * 1.3) * Math.sin(now * 0.37),
            pitch: 0.2 * Math.sin(now * 0.9), x: 0, y: 0,
            open: (now % 2.9) < 0.15 ? 0 : 1,
        };
        const target = live ? this._target : idle;
        for (const key of Object.keys(this.value)) {
            const rate = key === 'open' ? 26 : 11;
            this.value[key] += ((target[key] ?? 0) - this.value[key]) * (1 - Math.exp(-dt * rate));
        }
        this.progress += (this._progress - this.progress) * (1 - Math.exp(-dt * 10));
        return this;
    }
}

// Where the figure points: head turn mostly, position in the picture a little.
const aimX = s => clamp(s.look.yaw + 0.35 * s.look.x * s.seen, -1, 1);
const aimY = s => clamp(s.look.pitch + 0.2 * s.look.y * s.seen, -1, 1);
const lids = s => clamp(0.08 + s.look.open, 0.08, 1);

function dot(cr, x, y, r) {
    cr.newSubPath();
    cr.arc(x, y, Math.max(r, 0), 0, TAU);
}

function rounded(cr, x, y, w, h, r) {
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

function check(cr, color, progress, scale = 1) {
    if (progress <= 0)
        return;
    const a = clamp(progress / 0.32), b = clamp((progress - 0.32) / 0.68);
    cr.save();
    cr.scale(scale, scale);
    cr.setLineCap(1);
    cr.setLineJoin(1);
    cr.setLineWidth(5);
    cr.setSourceRGBA(...color, 1);
    cr.moveTo(-15, 1);
    cr.lineTo(-15 + 10 * a, 1 + 10 * a);
    if (b > 0)
        cr.lineTo(-5 + 21 * b, 11 - 22 * b);
    cr.stroke();
    cr.restore();
}

// Ring of ticks. level(place 0..1) gives the brightness of each.
function ticks(cr, color, level, radius = 41, count = 48) {
    cr.setLineCap(1);
    for (let i = 0; i < count; i++) {
        const v = clamp(level(i / count));
        const angle = TAU * i / count - Math.PI / 2;
        const outer = radius + 3.2 + 3.6 * v;
        cr.setLineWidth(1.5 + 0.9 * v);
        cr.setSourceRGBA(...color, 0.2 + 0.8 * v);
        cr.moveTo(radius * Math.cos(angle), radius * Math.sin(angle));
        cr.lineTo(outer * Math.cos(angle), outer * Math.sin(angle));
        cr.stroke();
    }
}

// What the ring shows: a comet, the progress of a head turn, or the outcome.
function ringLevel(s) {
    const head = (s.life * 0.85) % 1;
    if (s.state === 'ok')
        return p => smooth((s.t - p * 0.2) / 0.12);
    if (s.state === 'fail')
        return () => 0.5 * (1 - smooth(s.t / 0.25)) + 0.12;
    if (s.turn && s.progress > 0.02) {
        // Fills from the top down both sides, like the turn itself.
        return p => smooth((s.progress * 0.5 - Math.abs((p + 0.5) % 1 - 0.5)) / 0.03 + 0.5);
    }
    return p => s.motion ? Math.pow(1 - (head - p + 1) % 1, 3) : 0.5;
}

function eyes(cr, s, spread, y, size, sad, color) {
    const ax = aimX(s), ay = aimY(s);
    const open = sad > 0 ? lerp(1, 0.35, sad) : lids(s);
    for (const side of [-1, 1]) {
        cr.save();
        // The far eye moves in and shrinks as the head turns away.
        const depth = 1 - 0.18 * Math.max(0, side * -ax);
        cr.translate(side * spread * (1 - 0.1 * Math.abs(ax)) + ax * 3.5, y + ay * 2.5);
        cr.scale(depth, depth * Math.max(open, 0.1));
        dot(cr, 0, 0, size);
        cr.restore();       // before painting: the source colour is part of the saved state
        cr.setSourceRGBA(...color, 1);
        cr.fill();
    }
}

function mouth(cr, s, y, width, bend, color, alpha = 1) {
    const ax = aimX(s), ay = aimY(s);
    cr.setLineCap(1);
    cr.setLineWidth(3.2);
    cr.setSourceRGBA(...color, alpha);
    const cx = ax * 5, w = width * (1 - 0.15 * Math.abs(ax));
    y += ay * 3;
    cr.moveTo(cx - w, y);
    cr.curveTo(cx - w / 2, y + bend, cx + w / 2, y + bend, cx + w, y);
    cr.stroke();
}

const draw = {
    peekaboo(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        ticks(cr, s.color, ringLevel(s));
        cr.save();
        cr.arc(0, 0, 37.5, 0, TAU);
        cr.clip();

        // How far the head is out: bobs while looking for you, rises when it sees you.
        const bob = s.motion ? 0.5 + 0.5 * Math.sin(s.life * 2.4 - 1.2) : 0.7;
        const waiting = lerp(lerp(0.25, 0.8, smooth(bob)), 0.95, s.seen);
        let out = waiting * (s.motion ? smooth(s.life / 0.35) : 1);
        if (ok)
            out = lerp(waiting, 1.75, s.motion ? backOut(s.t / 0.32) : 1);
        if (fail)
            out = lerp(waiting, 0.12, smooth(s.t / 0.35));
        const ax = aimX(s), edge = 13;
        const hx = ok ? 0 : ax * 9, hy = edge + 17 - out * 25;
        cr.save();
        cr.translate(hx, hy);
        cr.rotate(ok ? 0 : ax * 0.14);
        const skin = ok ? s.options.success : s.color;
        // Head with two tufts of hair.
        cr.setSourceRGBA(...skin, 1);
        dot(cr, 0, 0, 20);
        cr.fill();
        cr.setLineCap(1);
        cr.setLineWidth(2.6);
        cr.moveTo(-1, -19);
        cr.curveTo(-3, -24, -7, -24, -8, -22);
        cr.moveTo(1.5, -19);
        cr.curveTo(2, -24, 6, -26, 8, -23);
        cr.stroke();
        const ink = [0.07, 0.08, 0.1];
        eyes(cr, {...s, seen: s.seen}, 7.2, -4, 3.5, fail ? smooth(s.t / 0.25) : 0, ink);
        cr.setSourceRGBA(1, 0.45, 0.45, ok ? 0.5 : 0.28);
        dot(cr, -12 + ax * 3, 3.5, 3);
        dot(cr, 12 + ax * 3, 3.5, 3);
        cr.fill();
        if (ok) {
            // A laugh.
            const open = smooth(s.t / 0.2);
            cr.setSourceRGBA(...ink, 1);
            cr.save();
            cr.translate(0, 4);
            cr.scale(1, 0.25 + 0.75 * open);
            cr.newSubPath();
            cr.arc(0, 0, 8, 0, Math.PI);
            cr.closePath();
            cr.restore();
            cr.fill();
        } else {
            mouth(cr, s, 5, 5, 4, ink);
        }
        cr.restore();

        // The laptop lid it hides behind, and the fingers holding on.
        const drop = ok ? 14 * smooth(s.t / 0.3) : 0;
        cr.setSourceRGBA(0.16, 0.18, 0.22, 1);
        rounded(cr, -44, edge + drop, 88, 60, 5);
        cr.fill();
        cr.setSourceRGBA(0.3, 0.33, 0.39, 1);
        rounded(cr, -44, edge + drop, 88, 2.6, 1.3);
        cr.fill();
        cr.setSourceRGBA(...mix(s.color, [0.3, 0.33, 0.39], 0.45), 1);
        dot(cr, 0, edge + drop + 11, 2.2);
        cr.fill();
        cr.setSourceRGBA(...skin, 1);
        if (ok) {
            // Both hands up.
            const up = s.motion ? backOut((s.t - 0.05) / 0.3) : 1;
            const wave = s.motion ? 3 * Math.sin(s.t * 18) * Math.exp(-s.t * 1.5) : 0;
            for (const side of [-1, 1])
                dot(cr, side * (26 + wave * side), lerp(edge, -8, up) - wave, 5.2);
        } else {
            const grip = smooth(out / 0.3);
            for (const side of [-1, 1]) {
                for (let i = 0; i < 3; i++)
                    rounded(cr, hx * 0.5 + side * 25 - 5.6 + i * 4, edge - 3.4 * grip, 3.2, 7, 1.6);
            }
        }
        cr.fill();
        cr.restore();
    },

    buddy(cr, s) {
        ticks(cr, s.color, ringLevel(s));
        const ok = s.state === 'ok';
        const sad = s.state === 'fail' ? smooth(s.t / 0.25) : 0;
        const face = ok ? 1 - smooth(s.t / 0.1) : 1;
        if (face > 0.01) {
            cr.save();
            cr.scale(0.8 + 0.2 * face, 0.8 + 0.2 * face);
            cr.translate(aimX(s) * 8, aimY(s) * 6);
            cr.rotate(aimX(s) * 0.12);
            eyes(cr, s, 11.5, -7, 4.2, sad, s.color);
            mouth(cr, s, 10 + 3 * sad, 11, lerp(8, -5, sad), s.color, face);
            cr.restore();
        }
        if (ok)
            check(cr, s.color, smooth((s.t - 0.08) / 0.2));
    },

    mesh(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        ticks(cr, s.color, ringLevel(s), 44, 60);
        const yaw = aimX(s) * 0.9, pitch = aimY(s) * 0.6;
        const gather = ok ? smooth((s.t - 0.05) / 0.3) : 0;
        const scan = s.motion ? Math.sin(s.life * 2.6) * 34 : 0;
        const points = 210;
        for (let i = 0; i < points; i++) {
            // Front half of an egg, evenly spread.
            const v = (i + 0.5) / points, phi = i * 2.399963;
            const z0 = 1 - v * 1.05, r0 = Math.sqrt(Math.max(0, 1 - z0 * z0));
            let x = r0 * Math.cos(phi) * 25, y = r0 * Math.sin(phi) * 33, z = z0 * 24;
            const nose = Math.exp(-(x * x / 22 + (y - 2) * (y - 2) / 70));
            z += 11 * nose;
            const socket = Math.exp(-((Math.abs(x) - 10) ** 2 / 16 + (y + 7) ** 2 / 12));
            z -= 7 * socket;
            const lips = Math.exp(-(x * x / 60 + (y - 16) ** 2 / 5));
            let bright = 0.35 + 0.5 * nose + 0.6 * lips;
            bright -= 0.3 * socket * (1 - s.look.open * 0.6);
            let px = x * Math.cos(yaw) + z * Math.sin(yaw);
            z = -x * Math.sin(yaw) + z * Math.cos(yaw);
            const py = y * Math.cos(pitch) + z * Math.sin(pitch);
            z = -y * Math.sin(pitch) + z * Math.cos(pitch);
            if (z < -6)
                continue;
            const depth = clamp((z + 8) / 38);
            if (!ok && !fail)
                bright += 0.9 * Math.exp(-((py - scan) ** 2) / 18);
            let sx = px, sy = py;
            if (fail) {
                const e = smooth(s.t / 0.5);
                sx += (rand(i) - 0.5) * 26 * e;
                sy += rand(i + 99) * 22 * e;
            }
            if (gather > 0) {
                const a = TAU * i / points;
                sx = lerp(sx, 33 * Math.cos(a), gather);
                sy = lerp(sy, 33 * Math.sin(a), gather);
            }
            const alpha = clamp((0.4 + 0.9 * depth) * (bright + 0.1)) * (fail ? 1 - 0.5 * smooth(s.t) : 1);
            cr.setSourceRGBA(...s.color, gather > 0 ? lerp(alpha, 0.9, gather) : alpha);
            dot(cr, sx, sy, 0.8 + 0.9 * depth + 0.4 * bright);
            cr.fill();
        }
        if (ok)
            check(cr, s.color, smooth((s.t - 0.16) / 0.22));
    },

    iris(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        ticks(cr, s.color, ringLevel(s));
        let open = fail ? 1 - 0.92 * smooth(s.t / 0.3) : lids(s);
        if (ok)
            open = 1;
        const h = 22 * open, w = 34;
        const almond = () => {
            cr.newPath();
            cr.moveTo(-w, 0);
            cr.curveTo(-w * 0.5, -h * 1.35, w * 0.5, -h * 1.35, w, 0);
            cr.curveTo(w * 0.5, h * 1.35, -w * 0.5, h * 1.35, -w, 0);
            cr.closePath();
        };
        cr.save();
        almond();
        cr.clip();
        const centre = ok ? smooth(s.t / 0.15) : 0;
        cr.translate(lerp(aimX(s) * 14, 0, centre), lerp(aimY(s) * 7, 0, centre));
        const radius = 15.5 + (ok ? 1.5 * smooth(s.t / 0.2) : 0);
        cr.setLineCap(0);
        for (let i = 0; i < 40; i++) {
            const a = TAU * i / 40 + (s.motion ? s.life * 0.35 : 0);
            const inner = 7.5 + 2.2 * rand(i);
            cr.setLineWidth(1.6);
            cr.setSourceRGBA(...s.color, 0.35 + 0.5 * rand(i + 7));
            cr.moveTo(inner * Math.cos(a), inner * Math.sin(a));
            cr.lineTo(radius * Math.cos(a), radius * Math.sin(a));
            cr.stroke();
        }
        cr.setLineWidth(1.8);
        cr.setSourceRGBA(...s.color, 0.9);
        dot(cr, 0, 0, radius);
        cr.stroke();
        // The pupil widens when it sees somebody.
        const pupil = ok ? lerp(6.5, 0, smooth(s.t / 0.12)) : 5 + 2 * s.seen +
            (s.motion ? 0.5 * Math.sin(s.life * 5) : 0);
        cr.setSourceRGBA(...s.color, 1);
        dot(cr, 0, 0, pupil);
        cr.fill();
        if (!ok) {
            cr.setSourceRGBA(0.05, 0.05, 0.06, 0.9);
            dot(cr, 2.2, -2.2, 1.7);
            cr.fill();
        }
        cr.restore();
        almond();
        cr.setLineWidth(3);
        cr.setLineJoin(1);
        cr.setSourceRGBA(...s.color, 1);
        cr.stroke();
        if (ok)
            check(cr, s.color, smooth((s.t - 0.1) / 0.2), 0.72);
    },

    padlock(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        ticks(cr, s.color, ringLevel(s));
        cr.save();
        cr.translate(0, 6);
        const lift = ok ? (s.motion ? backOut(s.t / 0.3) : 1) : 0;
        // Shackle: lifts, then swings open.
        cr.save();
        cr.translate(11, -8);
        cr.scale(1 - 1.7 * smooth((lift - 0.5) / 0.5), 1);
        cr.translate(-11, 8 - 7 * smooth(lift / 0.5));
        cr.setLineWidth(4.6);
        cr.setLineCap(1);
        cr.setSourceRGBA(...s.color, 1);
        cr.moveTo(-11, -8);
        cr.lineTo(-11, -15);
        cr.arc(0, -15, 11, Math.PI, TAU);
        cr.lineTo(11, -8);
        cr.stroke();
        cr.restore();
        cr.setSourceRGBA(...s.color, 1);
        rounded(cr, -18, -8, 36, 28, 6);
        cr.fill();
        // The keyhole is an eye.
        const ink = [0.07, 0.08, 0.1];
        if (ok) {
            cr.translate(0, 6);
            check(cr, ink, smooth((s.t - 0.12) / 0.2), 0.62);
        } else {
            cr.save();
            cr.translate(0, 5);
            cr.setSourceRGBA(...ink, 1);
            const wide = fail ? 1 - 0.7 * smooth(s.t / 0.2) : lids(s);
            cr.save();
            cr.scale(1, wide);
            dot(cr, 0, 0, 8);
            cr.restore();
            cr.fill();
            if (wide > 0.3) {
                cr.setSourceRGBA(...s.color, 1);
                dot(cr, aimX(s) * 4, aimY(s) * 3.4, 3);
                cr.fill();
            }
            cr.restore();
        }
        cr.restore();
    },

    radar(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        cr.setLineWidth(1.4);
        for (const r of [14, 27, 40]) {
            cr.setSourceRGBA(...s.color, r === 40 ? 0.8 : 0.3);
            dot(cr, 0, 0, r);
            cr.stroke();
        }
        cr.setSourceRGBA(...s.color, 0.2);
        cr.moveTo(-40, 0); cr.lineTo(40, 0);
        cr.moveTo(0, -40); cr.lineTo(0, 40);
        cr.stroke();
        if (!ok && !fail) {
            const a = s.motion ? s.life * 3.4 : -0.8;
            for (let i = 0; i < 28; i++) {
                cr.setSourceRGBA(...s.color, 0.3 * (1 - i / 28) ** 2);
                cr.moveTo(0, 0);
                cr.arc(0, 0, 40, a - (i + 1) * 0.045, a - i * 0.045 + 0.005);
                cr.closePath();
                cr.fill();
            }
        }
        const lock = ok ? smooth(s.t / 0.18) : 0;
        const x = lerp(aimX(s) * 26, 0, lock), y = lerp(aimY(s) * 22, 0, lock);
        const pulse = s.motion ? (s.life * 1.6) % 1 : 0;
        const show = Math.max(s.seen, ok ? 1 : 0);
        if (!fail && show > 0.02) {
            cr.setSourceRGBA(...s.color, show);
            if (!ok) {
                dot(cr, x, y, 4);
                cr.fill();
                cr.setSourceRGBA(...s.color, show * (1 - pulse));
                cr.setLineWidth(1.6);
                dot(cr, x, y, 4 + 10 * pulse);
                cr.stroke();
            }
            // Crosshair closing in.
            const r = lerp(13, 20, lock) - 2 * Math.sin(s.life * 5) * (1 - lock);
            cr.setLineWidth(2.2);
            cr.setLineCap(1);
            cr.setSourceRGBA(...s.color, show);
            for (let i = 0; i < 4; i++) {
                cr.newSubPath();
                cr.arc(x, y, r, i * TAU / 4 + 0.35, i * TAU / 4 + 1.22);
                cr.stroke();
            }
        }
        if (s.turn && !ok && !fail && s.progress > 0.02) {
            cr.setLineWidth(3.4);
            cr.setLineCap(1);
            cr.setSourceRGBA(...s.color, 1);
            cr.newSubPath();
            cr.arc(0, 0, 45, -Math.PI / 2, -Math.PI / 2 + TAU * s.progress);
            cr.stroke();
        }
        if (ok)
            check(cr, s.color, smooth((s.t - 0.1) / 0.2), 0.8);
        if (fail) {
            const e = smooth(s.t / 0.2) * 9;
            cr.setLineWidth(4.5);
            cr.setLineCap(1);
            cr.moveTo(-e, -e); cr.lineTo(e, e);
            cr.moveTo(e, -e); cr.lineTo(-e, e);
            cr.setSourceRGBA(...s.color, 1);
            cr.stroke();
        }
    },

    orbit(cr, s) {
        const ok = s.state === 'ok', fail = s.state === 'fail';
        const fall = fail ? smooth(s.t / 0.5) : 0;
        const close = ok ? smooth(s.t / 0.25) : 0;
        for (let k = 0; k < 3; k++) {
            const rx = lerp(42, 24, close), tilt = k * Math.PI / 3 + 0.25 * aimX(s);
            const ry = rx * lerp(0.42 + 0.12 * aimY(s), 1, close);
            cr.save();
            cr.rotate(tilt);
            cr.save();
            cr.scale(1, ry / rx);
            dot(cr, 0, 0, rx);
            cr.restore();
            cr.setLineWidth(1.2);
            cr.setSourceRGBA(...s.color, 0.28 * (1 - close) * (1 - fall));
            cr.stroke();
            const base = (s.motion ? s.life * (2.2 + 0.35 * k) : 0.9) + k * 2.1;
            for (let i = 0; i < 14; i++) {
                const a = base - i * 0.07;
                cr.setSourceRGBA(...s.color, (1 - i / 14) ** 2 * (1 - fall));
                dot(cr, rx * Math.cos(a), ry * Math.sin(a) + 30 * fall * fall, 3.6 * (1 - i / 18));
                cr.fill();
            }
            cr.restore();
        }
        const r = ok ? lerp(13, 25, s.motion ? backOut(s.t / 0.3) : 1) : 13 - 3 * fall;
        const x = ok ? 0 : aimX(s) * 5, y = ok ? 0 : aimY(s) * 5;
        cr.setSourceRGBA(...s.color, 1);
        dot(cr, x, y, r);
        cr.fill();
        const ink = [0.07, 0.08, 0.1];
        if (ok) {
            check(cr, ink, smooth((s.t - 0.14) / 0.2), 0.8);
        } else {
            cr.save();
            cr.translate(x, y);
            cr.scale(0.62, 0.62);
            eyes(cr, s, 7, -1, 3.6, fail ? smooth(s.t / 0.2) : 0, ink);
            cr.restore();
        }
        if (s.turn && !ok && !fail && s.progress > 0.02) {
            cr.setLineWidth(3);
            cr.setLineCap(1);
            cr.setSourceRGBA(...s.color, 1);
            cr.newSubPath();
            cr.arc(x, y, 18, -Math.PI / 2, -Math.PI / 2 + TAU * s.progress);
            cr.stroke();
        }
    },

    ring(cr, s) {
        const base = ringLevel(s);
        const angle = Math.atan2(aimY(s), aimX(s)), pull = Math.hypot(aimX(s), aimY(s)) * s.seen;
        const level = s.state === 'scanning' && !(s.turn && s.progress > 0.02)
            ? p => {
                // The light leans to where you look.
                let d = Math.abs(TAU * p - Math.PI / 2 - angle) % TAU;
                d = Math.min(d, TAU - d);
                return Math.max(base(p) * (1 - s.seen), clamp(0.25 * s.seen + pull * 2 * Math.exp(-d * d * 3)));
            } : base;
        ticks(cr, s.color, level, 38, 56);
        if (s.state === 'ok')
            check(cr, s.color, smooth((s.t - 0.08) / 0.2));
    },
};

function effectBehind(cr, s) {
    if (s.state !== 'ok' || !s.motion)
        return;
    const {effect} = s.options, c = s.options.success;
    if (effect === 'glow') {
        const e = smooth(s.t / 0.2) * (1 - smooth((s.t - 0.35) / 0.5));
        const g = new Cairo.RadialGradient(0, 0, 20, 0, 0, 64);
        g.addColorStopRGBA(0, ...c, 0.65 * e);
        g.addColorStopRGBA(1, ...c, 0);
        cr.setSource(g);
        dot(cr, 0, 0, 64);
        cr.fill();
    } else if (effect === 'ripple') {
        for (let i = 0; i < 3; i++) {
            const e = clamp((s.t - 0.1 * i) / 0.6);
            if (e <= 0 || e >= 1)
                continue;
            cr.setLineWidth(3 * (1 - e));
            cr.setSourceRGBA(...c, 0.7 * (1 - e));
            dot(cr, 0, 0, 50 + 28 * smooth(e));
            cr.stroke();
        }
    }
}

function effectFront(cr, s) {
    if (s.state !== 'ok' || s.options.effect !== 'confetti' || !s.motion)
        return;
    const palette = [s.options.success, [1, 0.84, 0.3], [0.45, 0.7, 1], [1, 0.55, 0.7]];
    for (let i = 0; i < 44; i++) {
        const t = s.t - 0.06 * rand(i);
        if (t <= 0)
            continue;
        const a = TAU * rand(i + 3), v = 120 + 130 * rand(i + 11);
        const drag = (1 - Math.exp(-3.2 * t)) / 3.2;
        const x = Math.cos(a) * v * drag, y = Math.sin(a) * v * drag - 14 * t + 85 * t * t;
        const alpha = 1 - smooth((t - 0.45) / 0.45);
        if (alpha <= 0)
            continue;
        cr.save();
        cr.translate(x, y);
        cr.rotate(rand(i + 5) * TAU + t * 9 * (rand(i + 8) - 0.5));
        cr.scale(1, Math.cos(t * 12 + i) * 0.7 + 0.3);
        cr.rectangle(-2.8, -1.7, 5.6, 3.4);
        cr.restore();
        cr.setSourceRGBA(...palette[i % palette.length], alpha);
        cr.fill();
    }
}

// Room the effects need around the figure, as a multiple of its size.
export const BLEED = 1.6;

// Paints one frame centred in a width x height area, the figure being `size` wide.
export function paint(cr, width, height, size, scene) {
    const s = {motion: true, seen: 0, progress: 0, turn: false, ...scene,
        options: {...DEFAULTS, ...scene.options}};
    if (!s.motion)
        s.t = 10;       // straight to the end of any transition
    const enter = smooth(s.t / 0.25);
    s.color = s.state === 'ok' ? mix(s.options.color, s.options.success, enter)
        : s.state === 'fail' ? mix(s.options.color, s.options.failure, enter) : s.options.color;
    cr.save();
    cr.translate(width / 2, height / 2);
    cr.scale(size / UNIT, size / UNIT);
    if (s.state === 'fail' && s.motion)
        cr.translate(9 * Math.sin(s.t * 34) * Math.exp(-s.t * 5.5), 0);
    effectBehind(cr, s);
    if (s.options.backdrop > 0) {
        cr.setSourceRGBA(0.05, 0.05, 0.065, s.options.backdrop);
        dot(cr, 0, 0, UNIT / 2 - 1);
        cr.fill();
    }
    (draw[s.options.style] ?? draw.peekaboo)(cr, s);
    effectFront(cr, s);
    cr.restore();
}
