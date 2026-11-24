// Renders the animations to PNG frames. Run: gjs -m tools/make_gif.js OUTDIR
import Cairo from 'cairo';
import GLib from 'gi://GLib';

const root = GLib.path_get_dirname(GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]));
const {Gaze, STYLES, paint} = await import(`file://${root}/extension/glyphs.js`);

const FPS = 25, SECONDS = 5.6, CELL = 190, COLUMNS = 4;
const rows = Math.ceil(STYLES.length / COLUMNS);
const effects = ['confetti', 'ripple', 'glow', 'ripple', 'confetti', 'glow', 'confetti', 'ripple'];
const gazes = STYLES.map(() => new Gaze());

for (let n = 0; n < FPS * SECONDS; n++) {
    const now = n / FPS + 1;
    const surface = new Cairo.ImageSurface(Cairo.Format.ARGB32, CELL * COLUMNS, CELL * rows);
    const cr = new Cairo.Context(surface);
    const sky = new Cairo.LinearGradient(0, 0, CELL * COLUMNS, CELL * rows);
    sky.addColorStopRGBA(0, 0.17, 0.23, 0.42, 1);
    sky.addColorStopRGBA(1, 0.06, 0.08, 0.16, 1);
    cr.setSource(sky);
    cr.paint();
    const t = now - 1;
    const turn = t > 1.6 && t < 3.6, done = t >= 3.6;
    // A head that looks left, right, blinks, then turns.
    const look = {
        yaw: t < 0.5 ? 0 : 0.85 * Math.sin((t - 0.5) * 2.1), pitch: 0.35 * Math.sin(t * 1.6),
        x: 0, y: 0, open: t > 1.2 && t < 1.38 ? 0 : 1,
    };
    STYLES.forEach(([id], i) => {
        gazes[i].aim(t > 0.4 ? look : null, turn ? Math.min((t - 1.6) / 1.7, 1) : 0, now);
        gazes[i].step(now);
        cr.save();
        cr.translate(i % COLUMNS * CELL, Math.floor(i / COLUMNS) * CELL);
        paint(cr, CELL, CELL, 118, {
            state: done ? 'ok' : 'scanning', t: done ? t - 3.6 : t, life: t,
            look: gazes[i].value, seen: gazes[i].seen, progress: gazes[i].progress, turn,
            options: {style: id, effect: effects[i]},
        });
        cr.restore();
    });
    cr.$dispose();
    surface.writeToPNG(`${ARGV[0]}/f${String(n).padStart(4, '0')}.png`);
}
