import { brandGlyphPathData, STROKE } from "@/lib/brand-glyph.mjs";
import { cn } from "@/lib/cn";

export interface BrandMarkProps {
  /** Edge length in CSS pixels. */
  size?: number;
  className?: string;
}

const GLYPH_PATH = brandGlyphPathData(100);

/**
 * The Hisaab mark: a green tile with the white tally-H, drawn from the same geometry as the
 * home-screen icon (lib/brand-glyph.mjs), so the login screen and the icon always match.
 * Decorative: the page title names the app.
 */
export function BrandMark({ size = 56, className }: BrandMarkProps) {
  return (
    <span
      aria-hidden="true"
      className={cn("inline-block shrink-0 overflow-hidden rounded-logo bg-accent text-white shadow-card", className)}
      style={{ width: size, height: size }}
    >
      <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden="true" focusable="false">
        <path
          d={GLYPH_PATH}
          fill="none"
          stroke="currentColor"
          strokeWidth={STROKE * 100}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}
