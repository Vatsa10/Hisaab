import Link from "next/link";
import { cn } from "@/lib/cn";
import type { CartMode } from "@/lib/cart";

const MODES: ReadonlyArray<{ id: CartMode; label: string; href: string }> = [
  { id: "grocery", label: "Groceries", href: "/cart" },
  { id: "food", label: "Food", href: "/cart?mode=food" },
];

/** Groceries | Food, a segmented control at the top of /cart. `?mode=food` selects Food; Groceries is the default. */
export function ModeSwitch({ mode }: { mode: CartMode }) {
  return (
    <div role="tablist" aria-label="Cart mode" className="mb-4 grid grid-cols-2 gap-1 rounded-control bg-line-soft p-1">
      {MODES.map((m) => (
        <Link
          key={m.id}
          href={m.href}
          role="tab"
          aria-selected={mode === m.id}
          replace
          scroll={false}
          className={cn(
            "grid min-h-11 place-items-center rounded-tile px-2 text-body font-semibold transition-[background-color,color,box-shadow] duration-150",
            mode === m.id ? "bg-surface text-ink shadow-card" : "text-ink-muted active:text-ink",
          )}
        >
          {m.label}
        </Link>
      ))}
    </div>
  );
}
