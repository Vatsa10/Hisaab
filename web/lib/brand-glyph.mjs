// The one source of the brand's rupee glyph (₹), shared by the inline SVG <BrandMark> and by
// scripts/make-icons.mjs, which rasterises the same geometry into the PWA icons and the iOS
// launch images. Plain JS (not TS) so Node can run the script without a build step.
//
// Coordinates are in a unit square (0..1) that is the whole icon/tile; the glyph is drawn as
// round-capped, round-joined strokes of width STROKE and stays inside the central 64%, so it
// survives Android's maskable crop and iOS's corner rounding.

export const STROKE = 0.09;

/** Two horizontal bars: [x1, y1, x2, y2]. */
export const BARS = [
  [0.33, 0.23, 0.67, 0.23],
  [0.33, 0.37, 0.67, 0.37],
];

/** The bowl: the right half of this circle, from its top (on the top bar) to its bottom. */
export const BOWL = { cx: 0.46, cy: 0.37, r: 0.14 };

/** From the bottom of the bowl back to the left, then the diagonal leg. */
export const TAIL = [
  [0.46, 0.51],
  [0.355, 0.51],
  [0.63, 0.775],
];

const n = (v, scale) => String(Math.round(v * scale * 100) / 100);

/** SVG path data for the glyph in a viewBox of `0 0 scale scale` (stroke it, don't fill). */
export function brandGlyphPathData(scale = 100) {
  // Bars are horizontal (y1 === y2), so each is a move plus a horizontal line.
  const bars = BARS.map(([x1, y1, x2]) => `M${n(x1, scale)} ${n(y1, scale)}H${n(x2, scale)}`).join("");
  const { cx, cy, r } = BOWL;
  const bowl =
    `M${n(cx, scale)} ${n(cy - r, scale)}` +
    `A${n(r, scale)} ${n(r, scale)} 0 0 1 ${n(cx, scale)} ${n(cy + r, scale)}`;
  const tail = TAIL.slice(1)
    .map(([x, y]) => `L${n(x, scale)} ${n(y, scale)}`)
    .join("");
  return `${bars}${bowl}${tail}`;
}

function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function distToBowl(px, py) {
  const { cx, cy, r } = BOWL;
  if (px >= cx) return Math.abs(Math.hypot(px - cx, py - cy) - r);
  return Math.min(Math.hypot(px - cx, py - (cy - r)), Math.hypot(px - cx, py - (cy + r)));
}

/** True when the unit-square point (x, y) is inside the stroked glyph. */
export function insideBrandGlyph(x, y) {
  const half = STROKE / 2;
  if (distToBowl(x, y) <= half) return true;
  for (const [x1, y1, x2, y2] of BARS) if (distToSegment(x, y, x1, y1, x2, y2) <= half) return true;
  for (let i = 1; i < TAIL.length; i++) {
    const [x1, y1] = TAIL[i - 1];
    const [x2, y2] = TAIL[i];
    if (distToSegment(x, y, x1, y1, x2, y2) <= half) return true;
  }
  return false;
}
