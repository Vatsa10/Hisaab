// Generates the PWA icons in public/icons/: a full-bleed #0a7c55 square with a white rupee
// sign (₹) drawn from strokes. No dependencies and no fonts, so the output is identical on
// every machine. Full-bleed because iOS rounds the corners itself and Android may mask it;
// the glyph stays inside the central 60% safe zone.
//
//   node scripts/make-icons.mjs

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "icons");
const BG = [0x0a, 0x7c, 0x55];
const FG = [0xff, 0xff, 0xff];

// The ₹ glyph in unit coordinates (0..1), as round-capped strokes of width W.
const W = 0.062;
const segments = [
  [0.33, 0.27, 0.67, 0.27], // top bar
  [0.33, 0.385, 0.67, 0.385], // second bar
  [0.46, 0.5, 0.36, 0.5], // bottom of the bowl back to the left
  [0.36, 0.5, 0.63, 0.77], // diagonal leg
];
// The bowl: right half of a circle joining the top bar to the bottom stroke.
const bowl = { cx: 0.46, cy: 0.385, r: 0.115 };

function distToSegment(px, py, [x1, y1, x2, y2]) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function distToBowl(px, py) {
  const { cx, cy, r } = bowl;
  if (px >= cx) return Math.abs(Math.hypot(px - cx, py - cy) - r);
  // Left of centre the arc has ended; distance to its two end points.
  return Math.min(Math.hypot(px - cx, py - (cy - r)), Math.hypot(px - cx, py - (cy + r)));
}

function inGlyph(x, y) {
  if (distToBowl(x, y) <= W / 2) return true;
  return segments.some((s) => distToSegment(x, y, s) <= W / 2);
}

function render(size) {
  const SS = 4; // 4x4 supersampling for smooth edges
  const rowLen = size * 3 + 1;
  const raw = Buffer.alloc(rowLen * size);
  for (let y = 0; y < size; y++) {
    raw[y * rowLen] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      let hits = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          if (inGlyph((x + (sx + 0.5) / SS) / size, (y + (sy + 0.5) / SS) / size)) hits++;
        }
      }
      const a = hits / (SS * SS);
      const o = y * rowLen + 1 + x * 3;
      for (let c = 0; c < 3; c++) raw[o + c] = Math.round(BG[c] * (1 - a) + FG[c] * a);
    }
  }
  return raw;
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function png(size) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // colour type: RGB (opaque, as iOS wants)
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(render(size), { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

mkdirSync(OUT, { recursive: true });
for (const [name, size] of [
  ["icon-192.png", 192],
  ["icon-512.png", 512],
  ["apple-touch-icon.png", 180],
]) {
  writeFileSync(join(OUT, name), png(size));
  console.log(`wrote public/icons/${name} (${size}x${size})`);
}
