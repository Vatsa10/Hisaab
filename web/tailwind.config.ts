import type { Config } from "tailwindcss";

// Design tokens. Colours are CSS variables (RGB channels, defined in app/globals.css) so they
// work with Tailwind opacity modifiers (bg-accent/10) and stay usable from plain CSS.
const rgb = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  // iOS keeps :hover "stuck" after a tap; only apply hover styles where hover really exists.
  future: { hoverOnlyWhenSupported: true },
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: rgb("canvas"), // page background #f6f7f9
        surface: rgb("surface"), // cards, sheets, tab bar #ffffff
        ink: { DEFAULT: rgb("ink"), muted: rgb("ink-muted") },
        line: { DEFAULT: rgb("line"), soft: rgb("line-soft"), field: rgb("field-border") },
        accent: {
          DEFAULT: rgb("accent"), // #0a7c55
          pressed: rgb("accent-pressed"),
          soft: rgb("accent-soft"), // pill / note background
          strong: rgb("accent-strong"), // text on accent-soft
          bar: rgb("accent-bar"), // inactive chart bars
        },
        up: { DEFAULT: rgb("up"), soft: rgb("up-soft") }, // spending went up
        danger: { DEFAULT: rgb("danger"), soft: rgb("danger-soft") },
        app: {
          zepto: "#5b21b6",
          blinkit: "#f8cb46",
          swiggy: "#fc8019",
          instamart: "#fc8019",
          zomato: "#e23744",
        },
      },
      fontFamily: {
        sans: ["var(--font-text)", "-apple-system", '"SF Pro Text"', '"Segoe UI"', "sans-serif"],
        display: ["var(--font-display)", "var(--font-text)", "-apple-system", '"Segoe UI"', "sans-serif"],
      },
      fontSize: {
        // [size, line-height]; tuned for an iPhone at 1x text size
        caption: ["0.6875rem", { lineHeight: "1rem" }], // 11px: tab labels, chart axes
        label: ["0.8125rem", { lineHeight: "1.125rem" }], // 13px: secondary lines
        body: ["0.9375rem", { lineHeight: "1.375rem" }], // 15px
        field: ["1rem", { lineHeight: "1.5rem" }], // 16px: inputs (no iOS zoom on focus)
        headline: ["1.0625rem", { lineHeight: "1.5rem", fontWeight: "600" }], // 17px: card / sheet titles, large buttons
        title: ["1.5rem", { lineHeight: "1.875rem", letterSpacing: "-0.01em", fontWeight: "700" }],
        figure: ["1.875rem", { lineHeight: "2.25rem", letterSpacing: "-0.02em", fontWeight: "700" }],
      },
      borderRadius: {
        card: "14px",
        control: "12px",
        field: "10px",
        sheet: "20px",
        tile: "8px", // inner segments and small tiles (concentric inside control + 4px)
        logo: "16px", // brand mark tile
      },
      boxShadow: {
        card: "0 1px 2px rgb(17 24 39 / 0.06)",
        sheet: "0 -8px 32px rgb(17 24 39 / 0.14)",
        bar: "0 -1px 0 rgb(var(--line))",
      },
      spacing: {
        tabbar: "3.5rem", // 56px visible tab bar height (plus the safe-area inset)
      },
      transitionTimingFunction: {
        "out-quart": "cubic-bezier(0.25, 1, 0.5, 1)",
      },
      keyframes: {
        "sheet-in": { from: { transform: "translateY(100%)" }, to: { transform: "translateY(0)" } },
        "sheet-out": { from: { transform: "translateY(0)" }, to: { transform: "translateY(100%)" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "fade-out": { from: { opacity: "1" }, to: { opacity: "0" } },
      },
      animation: {
        "sheet-in": "sheet-in 280ms cubic-bezier(0.25, 1, 0.5, 1)",
        "sheet-out": "sheet-out 200ms cubic-bezier(0.5, 0, 0.75, 0) forwards",
        "fade-in": "fade-in 200ms ease-out",
        "fade-out": "fade-out 200ms ease-in forwards",
      },
    },
  },
  plugins: [],
};

export default config;
