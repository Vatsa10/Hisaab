import type { CartSplit } from "@/lib/cart";

/** The server's "split the order?" verdict, shown only when it wrote one (never for food). */
export function SplitLine({ split }: { split: CartSplit | null }) {
  if (!split?.line) return null;
  return (
    <p data-testid="split-line" className="mt-3 px-1 text-label text-ink-muted">
      {split.line}
    </p>
  );
}
