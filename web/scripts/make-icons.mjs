// Generates the app icon set from the shared tally-H mark in lib/brand-glyph.mjs (the geometry <BrandMark> draws
// as SVG): app/icon.svg, app/favicon.ico, the PWA icons incl. a maskable one and the monochrome push badge
// (public/icons/), and the iOS launch images (public/splash/).
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
import { brandGlyphPathData, insideBrandGlyph, STROKE } from "../lib/brand-glyph.mjs";
import { STARTUP_SCREENS, startupImageFile } from "../lib/startup-images.mjs";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = join(WEB, "public");
const APP = join(WEB, "app");
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
function drawMark(img, left, top, size, radius, background, zoom = 1) {
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
          if (insideBrandGlyph(u, v, zoom)) glyph++;
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

// Home-screen and manifest icons: full bleed. The maskable one draws the mark at 80% so that every launcher
// mask (circle, squircle) keeps it inside the safe zone.
for (const [name, size, zoom] of [
  ["icon-192.png", 192, 1],
  ["icon-512.png", 512, 1],
  ["icon-512-maskable.png", 512, 0.8],
  ["apple-touch-icon.png", 180, 1],
]) {
  const img = canvas(size, size, ACCENT);
  drawMark(img, 0, 0, size, 0, ACCENT, zoom);
  write(join("icons", name), png(size, size, img.raw));
}

/** RGBA PNG of the white mark on a transparent background: the monochrome push-notification badge. */
function badgePng(size) {
  const rowLen = size * 4 + 1;
  const raw = Buffer.alloc(rowLen * size);
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let hit = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          if (insideBrandGlyph((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size, 1.15)) hit++;
        }
      }
      raw.set([255, 255, 255, Math.round((hit / (SS * SS)) * 255)], py * rowLen + 1 + px * 4);
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}
write(join("icons", "badge-96.png"), badgePng(96));

/** The mark as SVG: an accent tile with the white strokes (the favicon and the vector source of the icons). */
function iconSvg({ radius, mono }) {
  const d = brandGlyphPathData(100);
  const tile = mono ? "" : `<rect width="100" height="100" rx="${radius}" fill="#0a7c55"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">${tile}<path d="${d}" fill="none" stroke="${mono ? "#000" : "#fff"}" stroke-width="${Math.round(STROKE * 1000) / 10}" stroke-linecap="round" stroke-linejoin="round"/></svg>
`;
}
mkdirSync(APP, { recursive: true });
writeFileSync(join(APP, "icon.svg"), iconSvg({ radius: 22 }));
console.log("wrote app/icon.svg");
write(join("icons", "badge.svg"), iconSvg({ mono: true }));

// favicon.ico: PNG-compressed 16, 32 and 48 pixel images (rounded tile, like icon.svg).
const ico = [16, 32, 48].map((size) => {
  const img = canvas(size, size, WHITE);
  drawMark(img, 0, 0, size, 0.22, WHITE);
  return { size, data: png(size, size, img.raw) };
});
const header = Buffer.alloc(6);
header.writeUInt16LE(1, 2);
header.writeUInt16LE(ico.length, 4);
let offset = 6 + 16 * ico.length;
const entries = ico.map(({ size, data }) => {
  const e = Buffer.alloc(16);
  e[0] = size;
  e[1] = size;
  e.writeUInt16LE(1, 4);
  e.writeUInt16LE(32, 6);
  e.writeUInt32LE(data.length, 8);
  e.writeUInt32LE(offset, 12);
  offset += data.length;
  return e;
});
writeFileSync(join(APP, "favicon.ico"), Buffer.concat([header, ...entries, ...ico.map((i) => i.data)]));
console.log("wrote app/favicon.ico");

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
