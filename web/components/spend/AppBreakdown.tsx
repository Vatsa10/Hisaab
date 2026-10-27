import Link from "next/link";
import { useId } from "react";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import { APPS, type SiteId } from "@/lib/apps";
import { formatINR } from "@/lib/format";
import { AppDot, Chevron } from "./parts";
import type { AppAmounts } from "./summary";

/** Tie order when amounts are equal (the mockup's order). */
const TIE_ORDER: readonly SiteId[] = ["swiggy", "zomato", "zepto", "blinkit"];

export interface AppBreakdownProps {
  byApp: AppAmounts;
  /** Instamart's share of Swiggy, shown as a sub-label under Swiggy. */
  instamart: number;
  /** "YYYY-MM" the amounts are for; rows with spend link to that month's orders for the app. */
  month?: string;
}

/** One row per app, largest first, with its colour dot and amount. */
export function AppBreakdown({ byApp, instamart, month }: AppBreakdownProps) {
  const headingId = useId();
  const rows = [...APPS].sort(
    (a, b) => byApp[b.id] - byApp[a.id] || TIE_ORDER.indexOf(a.id) - TIE_ORDER.indexOf(b.id),
  );

  // When any row links (and so has a chevron), the others keep their amounts in the same column.
  const anyLink = Boolean(month) && rows.some((a) => byApp[a.id] > 0);

  return (
    <Card as="section" aria-labelledby={headingId} padding="none" className="overflow-hidden">
      <h2 id={headingId} className="sr-only">
        By app
      </h2>
      <ul>
        {rows.map((app) => {
          const amount = byApp[app.id];
          const body = (
            <>
              <AppDot className={app.dotClass} />
              <span className="min-w-0 flex-1">
                <span data-app-name="" className="block text-body">
                  {app.name}
                </span>
                {app.id === "swiggy" && instamart > 0 ? (
                  <span className="block text-label text-ink-muted">Instamart {formatINR(instamart)}</span>
                ) : null}
              </span>
              <Money amount={amount} className="text-body font-semibold" />
            </>
          );
          const row = "flex min-h-12 items-center gap-3 px-4 py-2.5";
          return (
            <li key={app.id} className="border-b border-line-soft last:border-b-0">
              {month && amount > 0 ? (
                <Link
                  href={`/orders?site=${app.id}&month=${month}`}
                  className={`${row} transition-colors duration-150 hover:bg-canvas active:bg-line-soft`}
                >
                  {body}
                  <Chevron className="-mr-1" />
                </Link>
              ) : (
                <div className={anyLink ? `${row} pr-10` : row}>{body}</div>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
