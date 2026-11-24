import {Context} from './cairo.js';
import {BLEED, Gaze, STYLES, paint} from './glyphs.js';

const $ = id => document.getElementById(id);
const still = matchMedia('(prefers-reduced-motion: reduce)');
const gaze = new Gaze();
const hero = $('friend');
const EFFECTS = {peekaboo: 'confetti', orbit: 'confetti', iris: 'glow', mesh: 'glow'};

let style = 'peekaboo', state = 'scanning', since = 0, born = 0, turning = null;
let pointer = null, pointedAt = -10;
const clock = () => performance.now() / 1000;

function enter(next) {
    state = next;
    since = clock();
    if (next === 'scanning')
        born = since;
    $('status').textContent = {
        scanning: 'Looking for you. Your pointer is your head.',
        ok: 'Hello, you. Unlocked.',
        fail: 'Not a face it knows. Your password still works.',
    }[next];
}

function turn() {
    enter('scanning');
    turning = clock();
    $('status').textContent = 'Turn your head a little…';
}

// The style picker: every thumbnail is live too.
const targets = [{canvas: hero, id: null, visible: true}];
for (const [id, name, about] of STYLES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'style';
    button.setAttribute('aria-pressed', id === style);
    const canvas = document.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    const label = document.createElement('b'), note = document.createElement('span');
    label.textContent = name;
    note.textContent = about;
    button.append(canvas, label, note);
    button.addEventListener('click', () => {
        style = id;
        turning = null;
        for (const other of $('styles').children)
            other.setAttribute('aria-pressed', other === button);
        enter('ok');
    });
    $('styles').append(button);
    targets.push({canvas, id, visible: true});
}

const watcher = new IntersectionObserver(entries => {
    for (const e of entries)
        targets.find(t => t.canvas === e.target).visible = e.isIntersecting;
});
for (const t of targets) {
    t.cr = new Context(t.canvas.getContext('2d'));
    watcher.observe(t.canvas);
}

addEventListener('pointermove', e => { pointer = [e.clientX, e.clientY]; pointedAt = clock(); });
addEventListener('pointerdown', e => { pointer = [e.clientX, e.clientY]; pointedAt = clock(); });
$('yes').addEventListener('click', () => { turning = null; enter('ok'); });
$('no').addEventListener('click', () => { turning = null; enter('fail'); });
$('turn').addEventListener('click', turn);
hero.addEventListener('click', () => { turning = null; enter('ok'); });

function lookAt(canvas, now) {
    if (turning !== null)
        return {yaw: 0.9 * Math.sin((now - turning) * 2.4), pitch: 0, x: 0, y: 0, open: 1};
    if (!pointer || now - pointedAt > 5)
        return null;
    const box = canvas.getBoundingClientRect();
    const reach = Math.max(innerWidth, innerHeight) * 0.4;
    const on = v => Math.max(-1, Math.min(1, v / reach));
    return {
        yaw: on(pointer[0] - box.left - box.width / 2), pitch: on(pointer[1] - box.top - box.height / 2),
        x: 0, y: 0, open: now % 3.7 < 0.15 ? 0 : 1,
    };
}

function frame() {
    const now = clock();
    if (state !== 'scanning' && now - since > 1.9)
        enter('scanning');
    const progress = turning === null ? 0 : Math.min((now - turning) / 1.5, 1);
    if (turning !== null && now - turning > 1.7) {
        turning = null;
        enter('ok');
    }
    const anchor = targets.find(t => t.visible)?.canvas ?? hero;
    gaze.aim(lookAt(anchor, now), progress, now);
    gaze.step(now);
    for (const {canvas, cr, id, visible} of targets) {
        const width = canvas.clientWidth, height = canvas.clientHeight;
        if (!visible || !width)
            continue;
        const dpr = Math.min(devicePixelRatio || 1, 2);
        const w = Math.round(width * dpr), h = Math.round(height * dpr);
        if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w;
            canvas.height = h;
        }
        cr.ctx.setTransform(1, 0, 0, 1, 0, 0);
        cr.ctx.clearRect(0, 0, w, h);
        const live = id === null || (id === style && state === 'ok');
        paint(cr, w, h, id === null ? h / BLEED : h * 0.96, {
            state: live ? state : 'scanning', t: now - (live ? since : born), life: now - born,
            look: gaze.value, seen: gaze.seen, progress: live ? gaze.progress : 0,
            turn: live && turning !== null, motion: !still.matches,
            options: {style: id ?? style, effect: EFFECTS[id ?? style] ?? 'ripple', backdrop: 0.55},
        });
    }
    requestAnimationFrame(frame);
}
enter('scanning');
frame();

for (const button of document.querySelectorAll('[data-copy]')) {
    button.addEventListener('click', async () => {
        const code = $(button.dataset.copy);
        try {
            await navigator.clipboard.writeText(code.textContent.replace(/\s+#.*$/gm, '').trim());
            button.textContent = 'Copied';
        } catch {
            getSelection().selectAllChildren(code);
            button.textContent = 'Selected';
        }
        setTimeout(() => { button.textContent = 'Copy'; }, 1600);
    });
}
