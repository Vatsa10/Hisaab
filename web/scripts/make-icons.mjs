// Generates the PWA icons (public/icons/) and the iOS launch images (public/splash/) from the
// shared rupee glyph in lib/brand-glyph.mjs, the same geometry <BrandMark> draws as SVG.
// No dependencies and no fonts, so the output is identical on every machine.
//
//   node scripts/make-icons.mjs
//
// Icons are full-bleed #0a7c55 squares (iOS rounds the corners itself; Android may mask them).
// Launch images are the brand background #f6f7f9 with the mark centred, so iOS never flashes
// white while the app starts.

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";
import { insideBrandGlyph } from "../lib/brand-glyph.mjs";
import { STARTUP_SCREENS, startupImageFile } from "../lib/startup-images.mjs";

const PUBLIC = join(dirname(fileURLToPath(import.meta.url)), "..", "public");
const ACCENT = [0x0a, 0x7c, 0x55];
const WHITE = [0xff, 0xff, 0xff];
const CANVAS = [0xf6, 0xf7, 0xf9];
const SS = 4; // 4x4 supersampling for smooth edges

const mix = (a, b, t) => a.map((v, i) => Math.round(v * (1 - t) + b[i] * t));

/** Raw RGB scanlines (filter byte 0 per row) filled with one colour. */
function canvas(width, height, rgb) {
  const rowLen = width * 3 + 1;
  const raw = Buffer.alloc(rowLen * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) raw.set(rgb, y * rowLen + 1 + x * 3);
  }
  return { raw, rowLen };
}

/** Inside a rounded square of side 1 and corner radius r (unit coordinates). */
function insideRoundedSquare(x, y, r) {
  if (r <= 0) return x >= 0 && x <= 1 && y >= 0 && y <= 1;
  const qx = Math.max(Math.abs(x - 0.5) - (0.5 - r), 0);
  const qy = Math.max(Math.abs(y - 0.5) - (0.5 - r), 0);
  return Math.hypot(qx, qy) <= r && Math.abs(x - 0.5) <= 0.5 && Math.abs(y - 0.5) <= 0.5;
}

/**
 * Draw the brand tile (accent square with the white glyph) into `img` at (left, top), `size`
 * pixels square, with corner radius `radius` as a fraction of the size (0 = full bleed).
 */
function drawMark(img, left, top, size, radius, background) {
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let tile = 0;
      let glyph = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const u = (px + (sx + 0.5) / SS) / size;
          const v = (py + (sy + 0.5) / SS) / size;
          if (!insideRoundedSquare(u, v, radius)) continue;
          tile++;
          if (insideBrandGlyph(u, v)) glyph++;
        }
      }
      const n = SS * SS;
      const onTile = mix(ACCENT, WHITE, tile ? glyph / tile : 0);
      const rgb = mix(background, onTile, tile / n);
      img.raw.set(rgb, (top + py) * img.rowLen + 1 + (left + px) * 3);
    }
  }
}

const CRC_TABLE = Array.from({ length: 256 }, (_, k) => {
  let c = k;
  for (let i = 0; i < 8; i++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
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

function png(width, height, raw) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // colour type: RGB (opaque, as iOS wants)
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function write(rel, data) {
  const file = join(PUBLIC, rel);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, data);
  console.log(`wrote public/${rel.replace(/\\/g, "/")}`);
}

// Home-screen and manifest icons: full bleed.
for (const [name, size] of [
  ["icon-192.png", 192],
  ["icon-512.png", 512],
  ["apple-touch-icon.png", 180],
]) {
  const img = canvas(size, size, ACCENT);
  drawMark(img, 0, 0, size, 0, ACCENT);
  write(join("icons", name), png(size, size, img.raw));
}

// Launch images: the 56pt login mark scaled to 96pt, centred on the brand background.
const MARK_PT = 96;
const MARK_RADIUS = 16 / 56; // same proportion as the login tile (rounded-logo on a 56px mark)
for (const screen of STARTUP_SCREENS) {
  const w = screen.width * screen.dpr;
  const h = screen.height * screen.dpr;
  const size = MARK_PT * screen.dpr;
  const img = canvas(w, h, CANVAS);
  drawMark(img, Math.round((w - size) / 2), Math.round((h - size) / 2), size, MARK_RADIUS, CANVAS);
  write(join("splash", startupImageFile(screen)), png(w, h, img.raw));
}
