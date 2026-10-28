"use client";

import { QTY_MAX, QTY_MIN, type CartItem } from "@/lib/cart";

export interface ItemRowProps {
  item: CartItem;
  onQty: (qty: number) => void;
  onRemove: () => void;
}

// 44px hit areas; the glyphs stay small so the row reads as text first.
const ICON_BUTTON =
  "grid size-11 shrink-0 place-items-center rounded-full transition-[background-color,opacity] duration-150 " +
  "active:bg-line-soft disabled:pointer-events-none disabled:opacity-35";

function Glyph({ d }: { d: string }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d={d} stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/** One line of the list: the name, a 1–20 quantity stepper and remove. */
export function ItemRow({ item, onQty, onRemove }: ItemRowProps) {
  const { text, qty } = item;
  return (
    <li aria-label={text} className="flex min-h-14 items-center gap-1 border-b border-line-soft py-1.5 pl-4 pr-1 last:border-b-0">
      <span className="min-w-0 flex-1 break-words py-1 pr-1 font-medium">{text}</span>
      <div role="group" aria-label={`Quantity of ${text}`} className="flex shrink-0 items-center rounded-full bg-canvas">
        <button
          type="button"
          aria-label={`Fewer ${text}`}
          disabled={qty <= QTY_MIN}
          onClick={() => onQty(qty - 1)}
          className={`${ICON_BUTTON} text-ink`}
        >
          <Glyph d="M6 12h12" />
        </button>
        <span className="min-w-5 text-center font-semibold tabular-nums">{qty}</span>
        <button
          type="button"
          aria-label={`More ${text}`}
          disabled={qty >= QTY_MAX}
          onClick={() => onQty(qty + 1)}
          className={`${ICON_BUTTON} text-ink`}
        >
          <Glyph d="M12 6v12M6 12h12" />
        </button>
      </div>
      <button
        type="button"
        aria-label={`Remove ${text}`}
        onClick={onRemove}
        className={`${ICON_BUTTON} text-ink-muted hover:text-danger`}
      >
        <Glyph d="M7 7l10 10M17 7L7 17" />
      </button>
    </li>
  );
}
