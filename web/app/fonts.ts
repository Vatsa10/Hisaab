import localFont from "next/font/local";

// Self-hosted (public/fonts, licences next to them), so no request leaves for a font CDN.
// Display: Bricolage Grotesque, for titles and rupee totals. Text: Geist, for everything else.
// Two weights and three weights; the rupee sign is not in Bricolage's Latin file, so it falls through to Geist.
export const display = localFont({
  src: [
    { path: "../public/fonts/BricolageGrotesque-600.woff2", weight: "600", style: "normal" },
    { path: "../public/fonts/BricolageGrotesque-700.woff2", weight: "700", style: "normal" },
  ],
  variable: "--font-display",
  display: "swap",
  fallback: ["Geist", "-apple-system", "Segoe UI", "sans-serif"],
});

export const text = localFont({
  src: [
    { path: "../public/fonts/Geist-400.woff2", weight: "400", style: "normal" },
    { path: "../public/fonts/Geist-500.woff2", weight: "500", style: "normal" },
    { path: "../public/fonts/Geist-600.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-text",
  display: "swap",
  fallback: ["-apple-system", "SF Pro Text", "Segoe UI", "sans-serif"],
});
