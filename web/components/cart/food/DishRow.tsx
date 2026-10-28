"use client";

import { cn } from "@/lib/cn";
import { QTY_MAX, QTY_MIN } from "@/lib/cart";

/** What a row shows; the picker maps menu dishes and usual dishes onto it. */
export interface DishView {
  key: string;
  name: string;
  variant?: string | null;
  veg?: boolean | null;
  priceLabel?: string | null;
  /** Shown instead of the price, e.g. "Ordered 4 times". */
  meta?: string | null;
  available: boolean;
}

export interface DishRowProps {
  dish: DishView;
  /** 0 when not picked. */
  qty: number;
  onQty: (qty: number) => void;
  /** Overrides the "Add {name}" label (the usual list says "Add {name} (usual)"). */
  addLabel?: string;
}

/** The veg / non-veg mark: a framed dot, green or red, with a text label for screen readers. */
export function VegDot({ veg }: { veg: boolean | null | undefined }) {
  if (veg == null) return <span aria-hidden="true" className="size-4 shrink-0" />;
  return (
    <span
      role="img"
      aria-label={veg ? "Vegetarian" : "Non-vegetarian"}
      className={cn(
        "grid size-4 shrink-0 place-items-center rounded-[4px] border-[1.5px]",
        veg ? "border-accent" : "border-danger",
      )}
    >
      <span className={cn("size-2 rounded-full", veg ? "bg-accent" : "bg-danger")} />
    </span>
  );
}

const STEP =
  "grid size-11 shrink-0 place-items-center rounded-full text-ink transition-[background-color,opacity] duration-150 " +
  "active:bg-line-soft disabled:pointer-events-none disabled:opacity-35";

function Glyph({ d }: { d: string }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d={d} stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/** One dish: the veg mark, name, variant and price, then Add (or a quantity stepper once picked). Unavailable dishes are greyed. */
export function DishRow({ dish, qty, onQty, addLabel }: DishRowProps) {
  const { name, variant, veg, priceLabel, meta, available } = dish;
  return (
    <li
      aria-label={name}
      className={cn(
        "flex min-h-14 items-center gap-3 border-b border-line-soft py-1.5 pl-4 pr-2 last:border-b-0",
        !available && "opacity-60",
      )}
    >
      <VegDot veg={veg} />
      <span className="min-w-0 flex-1 py-1">
        <span className={cn("block break-words font-medium", !available && "text-ink-muted")}>{name}</span>
        <span className="flex flex-wrap items-center gap-x-2 text-label text-ink-muted">
          {variant ? <span className="rounded-full bg-line-soft px-2 py-px font-medium text-ink">{variant}</span> : null}
          {available ? (
            priceLabel ? <span className="tabular-nums">{priceLabel}</span> : meta ? <span>{meta}</span> : null
          ) : (
            <span className="font-medium">Not available</span>
          )}
        </span>
      </span>
      {!available ? null : qty > 0 ? (
        <div role="group" aria-label={`Quantity of ${name}`} className="flex shrink-0 items-center rounded-full bg-canvas">
          <button
            type="button"
            aria-label={qty <= QTY_MIN ? `Remove ${name}` : `Fewer ${name}`}
            onClick={() => onQty(qty - 1)}
            className={STEP}
          >
            <Glyph d={qty <= QTY_MIN ? "M7 7l10 10M17 7L7 17" : "M6 12h12"} />
          </button>
          <span className="min-w-5 text-center font-semibold tabular-nums" aria-live="polite">
            {qty}
          </span>
          <button
            type="button"
            aria-label={`More ${name}`}
            disabled={qty >= QTY_MAX}
            onClick={() => onQty(qty + 1)}
            className={STEP}
          >
            <Glyph d="M12 6v12M6 12h12" />
          </button>
        </div>
      ) : (
        <button
          type="button"
          aria-label={addLabel ?? `Add ${name}`}
          onClick={() => onQty(1)}
          className="min-h-11 shrink-0 rounded-full px-4 text-body font-semibold text-accent ring-1 ring-inset ring-accent transition-[background-color,transform] duration-150 active:scale-[0.97] active:bg-accent-soft"
        >
          Add
        </button>
      )}
    </li>
  );
}
