"use client";

import Link from "next/link";
import { useMemo, useState, useSyncExternalStore } from "react";
import { buttonClassName } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { monthLabel } from "@/lib/format";
import { getMonthlySpend, getTopMerchants } from "@/lib/queries";
import { AppBreakdown } from "./AppBreakdown";
import { CategoryList } from "./CategoryList";
import { LoadError, Skeleton } from "./parts";
import { buildSpendSummary, monthKey, previousMonth } from "./summary";
import { TopMerchantCard } from "./TopMerchantCard";
import { TotalCard } from "./TotalCard";
import { useLoad } from "./useLoad";

const noopSubscribe = () => () => {};

export interface SpendDashboardProps {
  /** "Now", for tests; defaults to the phone's clock. */
  today?: Date;
}

/**
 * The home screen (mockup A, "dashboard first"): month eyebrow and "Spending" title, the total
 * card with six monthly bars, the per-app card, "Top this year" and the selected month's
 * categories. With no spend at all it is the same layout at zero, plus "Connect an app".
 */
export function SpendDashboard({ today }: SpendDashboardProps) {
  // The month comes from the phone's clock and time zone, never the server's, so it is only
  // read once mounted; until then the screen shows its skeleton.
  const isClient = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const now = today ?? (isClient ? new Date() : null);
  const current = now ? monthKey(now) : null;
  const year = current ? Number(current.slice(0, 4)) : null;

  const monthly = useLoad(current === null ? null : "monthly", () => getMonthlySpend());
  const top = useLoad(year === null ? null : String(year), () => getTopMerchants(year ?? 0));
  const [picked, setPicked] = useState<string | null>(null);

  const summary = useMemo(() => {
    if (monthly.status !== "ready" || current === null) return null;
    const [y, m] = current.split("-").map(Number);
    return buildSpendSummary(monthly.data, new Date(y ?? 1970, (m ?? 1) - 1, 1));
  }, [monthly, current]);

  const header = (
    <PageHeader
      eyebrow={current ? monthLabel(current) : <Skeleton className="my-0.5 h-3.5 w-24" />}
      title="Spending"
    />
  );

  if (monthly.status === "error") {
    return (
      <>
        {header}
        <LoadError what="your spending" onRetry={monthly.retry} />
      </>
    );
  }

  if (!summary || current === null) {
    return (
      <>
        {header}
        <DashboardSkeleton />
      </>
    );
  }

  if (!summary.hasData) {
    const zero = summary.months[summary.months.length - 1]!;
    return (
      <>
        {header}
        <div className="space-y-3">
          <TotalCard months={summary.months} currentMonth={current} selectedMonth={current} previousTotal={undefined} empty />
          <AppBreakdown byApp={zero.byApp} instamart={0} />
          <div className="pt-2">
            <Link href="/accounts" className={buttonClassName({ size: "lg", fullWidth: true })}>
              Connect an app
            </Link>
            <p className="mt-2.5 text-center text-label text-ink-muted">Your orders fill this in, month by month.</p>
          </div>
        </div>
      </>
    );
  }

  const selected = picked && summary.months.some((m) => m.month === picked) ? picked : current;
  const point = summary.months.find((m) => m.month === selected)!;
  const best = top.status === "ready" ? top.data[0] : undefined;

  return (
    <>
      {header}
      <div className="space-y-3">
        <TotalCard
          months={summary.months}
          currentMonth={current}
          selectedMonth={selected}
          previousTotal={summary.monthTotals[previousMonth(selected)]}
          onSelect={setPicked}
        />
        <AppBreakdown byApp={point.byApp} instamart={point.instamart} month={selected} />
        {top.status === "loading" ? (
          <Card aria-hidden="true">
            <Skeleton className="h-3.5 w-24" />
            <div className="mt-3 flex justify-between">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-4 w-16" />
            </div>
          </Card>
        ) : top.status === "error" ? (
          <Card className="flex items-center justify-between gap-3 py-2">
            <p role="alert" className="text-body text-ink-muted">
              Couldn&apos;t load your top place.
            </p>
            <button
              type="button"
              onClick={top.retry}
              className="-mr-2 min-h-11 rounded-control px-2 text-body font-semibold text-accent active:opacity-60"
            >
              Retry
            </button>
          </Card>
        ) : best ? (
          <TopMerchantCard top={best} />
        ) : null}
        <CategoryList month={selected} monthTotal={point.total} />
      </div>
    </>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true">
      <p className="sr-only" role="status">
        Loading your spending
      </p>
      <Card aria-hidden="true">
        <Skeleton className="h-3.5 w-28" />
        <Skeleton className="mt-2.5 h-8 w-36" />
        <Skeleton className="mt-2 h-3.5 w-32" />
        <div className="mt-4 grid grid-cols-6 items-end gap-1.5">
          {[48, 80, 100, 56, 40, 24].map((h, i) => (
            <div key={i} className="flex flex-col gap-1.5 pt-1">
              <div className="flex h-20 items-end">
                <Skeleton className="w-full rounded-b-none" style={{ height: `${h}%` }} />
              </div>
              <Skeleton className="mx-auto h-3 w-6" />
            </div>
          ))}
        </div>
      </Card>
      <Card aria-hidden="true" padding="none">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex min-h-12 items-center gap-3 border-b border-line-soft px-4 py-2.5 last:border-b-0">
            <Skeleton className="size-2 rounded-full" />
            <Skeleton className="h-4 w-20" />
            <Skeleton className="ml-auto h-4 w-14" />
          </div>
        ))}
      </Card>
      <Card aria-hidden="true">
        <Skeleton className="h-3.5 w-24" />
        <div className="mt-3 flex justify-between">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-16" />
        </div>
      </Card>
    </div>
  );
}
