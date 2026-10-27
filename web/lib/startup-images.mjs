// iOS launch images ("apple-touch-startup-image"), portrait, one per iPhone screen size.
// Shared by app/layout.tsx (the <link> tags) and scripts/make-icons.mjs (which draws them).

/** CSS-pixel screen sizes at 3x, with the phones that use them. */
export const STARTUP_SCREENS = [
  { width: 375, height: 812, dpr: 3 }, // 12 mini, 13 mini, X, XS, 11 Pro
  { width: 390, height: 844, dpr: 3 }, // 12, 12 Pro, 13, 13 Pro, 14
  { width: 393, height: 852, dpr: 3 }, // 14 Pro, 15, 15 Pro
  { width: 428, height: 926, dpr: 3 }, // 12 Pro Max, 13 Pro Max, 14 Plus
  { width: 430, height: 932, dpr: 3 }, // 14 Pro Max, 15 Plus, 15 Pro Max
];

export function startupImageFile({ width, height, dpr }) {
  return `apple-splash-${width * dpr}x${height * dpr}.png`;
}

export function startupImageMedia({ width, height, dpr }) {
  return (
    `(device-width: ${width}px) and (device-height: ${height}px) and ` +
    `(-webkit-device-pixel-ratio: ${dpr}) and (orientation: portrait)`
  );
}
