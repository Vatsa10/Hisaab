"use client";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import { monthLabel } from "@/lib/format";
import { getCategorySpend } from "@/lib/queries";
import { categoryLabel } from "./display";
import { Skeleton } from "./parts";
import { useLoad } from "./useLoad";

export interface CategoryListProps {
  /** "YYYY-MM" */
  month: string;
  /** The month's order total, to tell "no orders" from "no item details". */
  monthTotal: number;
}

/** Where the selected month's money went, by item category, largest first ("Other" last). */
export function CategoryList({ month, monthTotal }: CategoryListProps) {
  const cats = useLoad(month, () => getCategorySpend(month));
  const name = monthLabel(month).split(" ")[0] ?? month;

  const rows =
    cats.status === "ready"
      ? [...cats.data]
          .filter((c) => c.amount > 0)
          .sort((a, b) => {
            const ao = categoryLabel(a.category) === "Other";
            const bo = categoryLabel(b.category) === "Other";
            return ao === bo ? b.amount - a.amount : ao ? 1 : -1;
          })
      : [];
  const max = Math.max(...rows.map((r) => r.amount), 0);

  return (
    <Card as="section" aria-label={`By category, ${monthLabel(month)}`} aria-busy={cats.status === "loading"}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-label text-ink-muted">By category</h2>
        <p aria-live="polite" className="text-label font-medium text-ink">
          {name}
        </p>
      </div>

      {cats.status === "loading" ? (
        <ul aria-hidden="true" className="mt-1">
          {[0, 1, 2].map((i) => (
            <li key={i} className="py-2.5">
              <div className="flex justify-between">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="h-4 w-14" />
              </div>
              <Skeleton className="mt-2 h-1 w-full rounded-full" />
            </li>
          ))}
        </ul>
      ) : cats.status === "error" ? (
        <div className="mt-2 flex items-center justify-between gap-3">
          <p role="alert" className="text-body text-ink-muted">
            Couldn&apos;t load categories.
          </p>
          <Button variant="ghost" className="-mr-3" onClick={cats.retry}>
            Try again
          </Button>
        </div>
      ) : rows.length === 0 ? (
        <p className="mt-2 text-body text-ink-muted">
          {monthTotal > 0 ? `No item details for ${name} yet.` : `No orders in ${name}.`}
        </p>
      ) : (
        <>
          <ul className="mt-1">
            {rows.map((c) => (
              <li key={c.category} className="py-2.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span data-category="" className="min-w-0 truncate text-body">
                    {categoryLabel(c.category)}
                  </span>
                  <Money amount={c.amount} className="shrink-0 text-body font-semibold" />
                </div>
                <div aria-hidden="true" className="mt-1.5 h-1 overflow-hidden rounded-full bg-line-soft">
                  <div
                    className="h-full rounded-full bg-accent/70"
                    style={{ width: `${max > 0 ? Math.max(2, (c.amount / max) * 100) : 0}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-caption text-ink-muted">Based on item prices, so it can differ from order totals.</p>
        </>
      )}
    </Card>
  );
}
