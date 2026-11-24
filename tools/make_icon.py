"""Draws the app icon. Run: python3 tools/make_icon.py data/icons"""
import math
import sys
from pathlib import Path

ID = "io.github.hesam_deb.Peekaboo"

ticks = []
for i in range(44):
    a = math.tau * i / 44 - math.pi / 2
    # Brightest at the top right, like the comet in the animation.
    level = (1 - ((0.14 - i / 44) % 1)) ** 1.6
    r0, r1 = 42.5, 46.0 + 3.5 * level
    ticks.append(
        f'<line x1="{64 + r0 * math.cos(a):.2f}" y1="{60 + r0 * math.sin(a):.2f}" '
        f'x2="{64 + r1 * math.cos(a):.2f}" y2="{60 + r1 * math.sin(a):.2f}" '
        f'stroke-opacity="{0.28 + 0.72 * level:.2f}"/>')

fingers = "".join(
    f'<rect x="{cx - 8.6 + k * 6:.1f}" y="75" width="5.2" height="12.5" rx="2.6"/>'
    for cx in (33.5, 94.5) for k in range(3))

ICON = f'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.5" y2="1">
      <stop offset="0" stop-color="#6b5bff"/><stop offset="0.55" stop-color="#4632c7"/>
      <stop offset="1" stop-color="#2a1a86"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.46" r="0.5">
      <stop offset="0" stop-color="#ff6f91" stop-opacity="0.85"/>
      <stop offset="0.55" stop-color="#c04bd8" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#6b5bff" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="ring" gradientUnits="userSpaceOnUse" x1="20" y1="104" x2="108" y2="16">
      <stop offset="0" stop-color="#ff9ec4"/><stop offset="1" stop-color="#ffd166"/>
    </linearGradient>
    <linearGradient id="head" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#e9e6ff"/>
    </linearGradient>
    <linearGradient id="lid" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3a3f55"/><stop offset="1" stop-color="#1e2133"/>
    </linearGradient>
    <clipPath id="tile"><rect x="8" y="8" width="112" height="108" rx="26"/></clipPath>
  </defs>
  <rect x="8" y="12" width="112" height="108" rx="26" fill="#1d1160"/>
  <rect x="8" y="8" width="112" height="108" rx="26" fill="url(#bg)"/>
  <g clip-path="url(#tile)">
    <circle cx="64" cy="60" r="60" fill="url(#glow)"/>
    <g stroke="url(#ring)" stroke-width="2.6" stroke-linecap="round">{"".join(ticks)}</g>
    <path d="M62.5 32.5c-2-9-9-11-13-7M66 32.5c1-9 9-13 14-7" fill="none" stroke="#fff"
          stroke-width="4.4" stroke-linecap="round"/>
    <circle cx="64" cy="64" r="32" fill="url(#head)"/>
    <ellipse cx="44.5" cy="72" rx="5.4" ry="4.2" fill="#ff8fab" opacity="0.6"/>
    <ellipse cx="83.5" cy="72" rx="5.4" ry="4.2" fill="#ff8fab" opacity="0.6"/>
    <ellipse cx="53.5" cy="60" rx="6" ry="7.6" fill="#1b1638"/>
    <ellipse cx="78" cy="60" rx="6" ry="7.6" fill="#1b1638"/>
    <circle cx="56" cy="57" r="2.3" fill="#fff"/><circle cx="80.5" cy="57" r="2.3" fill="#fff"/>
    <path d="M14 88a8 8 0 0 1 8-8h84a8 8 0 0 1 8 8v36H14z" fill="url(#lid)"/>
    <path d="M14 88a8 8 0 0 1 8-8h84a8 8 0 0 1 8 8v1.5a8 8 0 0 0-8-6.5H22a8 8 0 0 0-8 6.5z"
          fill="#6a7190"/>
    <circle cx="64" cy="100" r="5.2" fill="#ff6f91"/>
    <circle cx="64" cy="100" r="8.6" fill="none" stroke="#ff6f91" stroke-opacity="0.4"
            stroke-width="2"/>
    <g fill="url(#head)">{fingers}</g>
  </g>
</svg>
'''

SYMBOLIC = '''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <path fill="#222" fill-rule="evenodd" d="M8 2a6 6 0 0 0-6 6v2h12V8a6 6 0 0 0-6-6zM5.6 5.6a1.1 1.4 0 0 1 0 2.8
1.1 1.4 0 0 1 0-2.8zm4.8 0a1.1 1.4 0 0 1 0 2.8 1.1 1.4 0 0 1 0-2.8z"/>
  <rect x="0" y="11" width="16" height="2" rx="1" fill="#222"/>
  <rect x="2" y="14" width="12" height="1.4" rx=".7" fill="#222" opacity=".5"/>
</svg>
'''

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
(out / f"{ID}.svg").write_text(ICON)
(out / f"{ID}-symbolic.svg").write_text(SYMBOLIC)
