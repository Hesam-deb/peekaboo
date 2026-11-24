// Just enough of cairo on a 2D canvas to run extension/glyphs.js unchanged.
const CAPS = ['butt', 'round', 'square'], JOINS = ['miter', 'round', 'bevel'];
const css = (r, g, b, a) =>
    `rgba(${[r, g, b].map(v => Math.round(Math.min(Math.max(v, 0), 1) * 255))},${Math.min(Math.max(a, 0), 1).toFixed(4)})`;

export class Context {
    constructor(ctx) {
        this.ctx = ctx;
        this.newPath();
    }

    newPath() { this.ctx.beginPath(); this._lift = false; }
    newSubPath() { this._lift = true; }
    closePath() { this.ctx.closePath(); }
    moveTo(x, y) { this._lift = false; this.ctx.moveTo(x, y); }
    lineTo(x, y) { this.ctx.lineTo(x, y); }
    curveTo(...p) { this.ctx.bezierCurveTo(...p); }
    rectangle(...p) { this.ctx.rect(...p); }
    arc(x, y, r, from, to) {
        if (this._lift)
            this.ctx.moveTo(x + r * Math.cos(from), y + r * Math.sin(from));
        this._lift = false;
        this.ctx.arc(x, y, r, from, to);
    }

    // Unlike a canvas, cairo forgets the path once it is used.
    fill() { this.ctx.fill(); this.newPath(); }
    stroke() { this.ctx.stroke(); this.newPath(); }
    clip() { this.ctx.clip(); this.newPath(); }

    save() { this.ctx.save(); }
    restore() { this.ctx.restore(); }
    translate(x, y) { this.ctx.translate(x, y); }
    scale(x, y) { this.ctx.scale(x, y); }
    rotate(a) { this.ctx.rotate(a); }

    setLineWidth(w) { this.ctx.lineWidth = w; }
    setLineCap(i) { this.ctx.lineCap = CAPS[i]; }
    setLineJoin(i) { this.ctx.lineJoin = JOINS[i]; }
    setSourceRGBA(...c) { this.ctx.fillStyle = this.ctx.strokeStyle = css(...c); }
    setSource(pattern) { this.ctx.fillStyle = this.ctx.strokeStyle = pattern.on(this.ctx); }
}

class RadialGradient {
    constructor(...geometry) { this.geometry = geometry; this.stops = []; }
    addColorStopRGBA(at, ...c) { this.stops.push([at, css(...c)]); }
    on(ctx) {
        const g = ctx.createRadialGradient(...this.geometry);
        this.stops.forEach(s => g.addColorStop(...s));
        return g;
    }
}

export default {Context, RadialGradient};
