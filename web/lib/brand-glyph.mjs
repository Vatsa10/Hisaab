// The one source of the Hisaab mark, shared by the inline SVG <BrandMark> and by
// scripts/make-icons.mjs, which rasterises the same geometry into the PWA icons, favicon, push badge and
// the iOS launch images. Plain JS (not TS) so Node can run the script without a build step.
//
// The mark is an "H" built from tally strokes: two uprights and one diagonal slash through both, the way a
// tally gate of five is struck through ("hisaab" = accounts). Coordinates are in a unit square (0..1) that is
// the whole icon/tile; the glyph is drawn as round-capped strokes of width STROKE and stays inside the
// central 62%, so it survives Android's maskable crop and iOS's corner rounding. Thick strokes and no
// counters, so it still reads at 16px.

export const STROKE = 0.1;

/** The three strokes: [x1, y1, x2, y2]. */
export const SEGMENTS = [
  [0.35, 0.25, 0.35, 0.75],
  [0.65, 0.25, 0.65, 0.75],
  [0.24, 0.64, 0.76, 0.36],
];

const n = (v, scale) => String(Math.round(v * scale * 100) / 100);

/** SVG path data for the glyph in a viewBox of `0 0 scale scale` (stroke it, don't fill). */
export function brandGlyphPathData(scale = 100) {
  return SEGMENTS.map(([x1, y1, x2, y2]) => `M${n(x1, scale)} ${n(y1, scale)}L${n(x2, scale)} ${n(y2, scale)}`).join("");
}

function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/**
 * True when the unit-square point (x, y) is inside the stroked glyph. `zoom` scales the glyph about the
 * centre (below 1 leaves a wider margin, for the maskable icon).
 */
export function insideBrandGlyph(x, y, zoom = 1) {
  const half = STROKE / 2;
  const px = (x - 0.5) / zoom + 0.5;
  const py = (y - 0.5) / zoom + 0.5;
  for (const [x1, y1, x2, y2] of SEGMENTS) if (distToSegment(px, py, x1, y1, x2, y2) <= half) return true;
  return false;
}
