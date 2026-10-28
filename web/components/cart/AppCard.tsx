import Link from "next/link";
import { buttonClassName } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { SITE_DOTS, type CartCard, type CartMode } from "@/lib/cart";
import type { FillOption } from "@/lib/fill";
import { FillButton } from "./FillButton";

export interface AppCardProps {
  card: CartCard;
  mode: CartMode;
  offersNote: string | null;
  /** Opens the pick-one sheet for this app's first open pick. */
  onPick: () => void;
  /** The server's Fill button state for this app (null until loaded). */
  fill?: FillOption | null;
  /** Opens the confirm sheet. */
  onFill?: () => void;
  /** Opens the live fill sheet. */
  onViewFill?: (fillId: string) => void;
}

// "1 out of stock · 1 to pick": the server's stock line may already carry the pick count.
const TO_PICK = /(?:\s*·\s*)?\d+\s+to pick\s*$/;

/**
 * The stock line, with "{n} to pick ›" as the button that opens the pick sheet. When the
 * server's line already says "n to pick", that part becomes the button instead of repeating it.
 * The button keeps a 44px hit area without adding height to the line (negative margins).
 */
function StockLine({ line, toPick, onPick }: { line: string | null; toPick: number; onPick: () => void }) {
  const rest = toPick > 0 && line ? line.replace(TO_PICK, "") : line;
  return (
    <p className="text-ink-muted">
      {rest}
      {toPick > 0 ? (
        <>
          {rest ? " · " : null}
          <button
            type="button"
            onClick={onPick}
            className="-mx-1.5 -my-3 inline-flex min-h-11 items-center rounded-tile px-1.5 font-semibold text-accent hover:bg-accent/5 active:bg-accent/10"
          >
            {toPick} to pick ›
          </button>
        </>
      ) : null}
    </p>
  );
}

/** One app's total with fees (mockup A). Prices and every line come from the server summary. */
export function AppCard({
  card,
  mode,
  offersNote,
  onPick,
  fill = null,
  onFill = () => {},
  onViewFill = () => {},
}: AppCardProps) {
  const ok = card.status === "ok";
  const best = ok && card.cheapest;

  return (
    <article
      aria-label={card.name}
      className={cn("rounded-card bg-surface p-4 shadow-card", best && "ring-2 ring-accent")}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", SITE_DOTS[card.site])} />
          <h2 className={cn("truncate text-body", best ? "font-semibold" : "font-medium")}>{card.name}</h2>
          {best ? (
            <span className="shrink-0 rounded-full bg-accent-soft px-2 py-0.5 text-caption font-semibold text-accent-strong">
              Cheapest
            </span>
          ) : null}
        </div>
        {ok ? (
          <p className="shrink-0 text-right tabular-nums">
            <span className={cn("text-headline", !best && "font-medium")}>{card.total_label}</span>
            {!best && card.delta_label ? (
              <span className="ml-1.5 text-label text-ink-muted">{card.delta_label}</span>
            ) : null}
          </p>
        ) : null}
      </div>

      {ok ? (
        <div className="mt-1.5 space-y-1 text-label">
          {card.fee_line || card.fees_estimated ? (
            <p className="text-ink-muted">
              {card.fee_line}
              {card.fees_estimated ? (
                <span className={cn("inline-block rounded-full bg-canvas px-1.5 text-caption text-ink-muted", card.fee_line && "ml-1.5")}>
                  fees estimated
                </span>
              ) : null}
            </p>
          ) : null}
          {card.member_note ? <p className="font-medium text-accent-strong">{card.member_note}</p> : null}
          {card.fee_hint ? <p className="text-ink-muted">{card.fee_hint}</p> : null}
          {card.stock_line || card.to_pick > 0 ? (
            <StockLine line={card.stock_line} toPick={card.to_pick} onPick={onPick} />
          ) : null}
          {mode === "food" && offersNote ? <p className="text-ink-muted">{offersNote}</p> : null}
          {card.swaps.length ? (
            <ul className="flex flex-wrap gap-1.5 pt-1">
              {card.swaps.map((s) => (
                <li key={s} className="warn rounded-full bg-up-soft px-2.5 py-0.5 text-caption font-semibold text-up">
                  {s}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : (
        <div className="mt-1.5 text-label">
          {card.note ? <p className="text-ink-muted">{card.note}</p> : null}
          {card.action ? (
            <Link href={card.action.href} className={buttonClassName({ variant: "secondary", className: "mt-3" })}>
              {card.action.label}
            </Link>
          ) : null}
        </div>
      )}

      {best ? <FillButton option={fill} appName={card.name} onStart={onFill} onView={onViewFill} /> : null}
    </article>
  );
}
