"use client";

import Link from "next/link";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Button, buttonClassName } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Money } from "@/components/ui/Money";
import { PageHeader } from "@/components/ui/PageHeader";
import { cn } from "@/lib/cn";
import { monthLabel } from "@/lib/format";
import { getOrders, type OrderSummary, type OrdersQuery } from "@/lib/queries";
import { appLabel, distinctMerchant, orderMonth, shortDate, voidedLabel } from "./display";
import { AppDot, Chevron, LoadError, Skeleton, VoidBadge } from "./parts";

export interface OrdersListProps {
  /** Only this app's orders ("swiggy" includes Instamart). */
  site?: string;
  /** Only this month ("YYYY-MM"). */
  month?: string;
}

type Phase = "loading" | "error" | "idle" | "more" | "more-error";

interface MonthGroup {
  key: string;
  label: string;
  orders: OrderSummary[];
}

function groupByMonth(orders: OrderSummary[]): MonthGroup[] {
  const groups: MonthGroup[] = [];
  for (const o of orders) {
    const m = orderMonth(o.placedAt);
    const key = m ?? "unknown";
    let g = groups[groups.length - 1];
    if (!g || g.key !== key) {
      g = { key, label: m ? monthLabel(m) : "Date unknown", orders: [] };
      groups.push(g);
    }
    g.orders.push(o);
  }
  return groups;
}

/** Load more this far before the end of the list, so scrolling rarely has to wait. */
const PREFETCH_MARGIN = "800px 0px";

/**
 * Every order, newest first, grouped by month under sticky month headings. Pages are fetched
 * with a keyset cursor; the next page loads by itself as the end of the list comes near (the
 * "Show older orders" button does the same by hand, and is the fallback without
 * IntersectionObserver).
 */
export function OrdersList({ site, month }: OrdersListProps) {
  const query = useMemo<OrdersQuery>(() => ({ ...(site ? { site } : {}), ...(month ? { month } : {}) }), [site, month]);
  const [orders, setOrders] = useState<OrderSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const busy = useRef(false);
  const alive = useRef(true);
  const sentinel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const fetchPage = useCallback(
    async (after: string | null) => {
      if (busy.current) return;
      busy.current = true;
      setPhase(after ? "more" : "loading");
      try {
        const page = await getOrders(after ? { ...query, cursor: after } : query);
        if (!alive.current) return;
        setOrders((prev) => (after ? [...prev, ...page.orders] : page.orders));
        setCursor(page.nextCursor);
        setPhase("idle");
      } catch {
        if (alive.current) setPhase(after ? "more-error" : "error");
      } finally {
        busy.current = false;
      }
    },
    [query],
  );

  useEffect(() => {
    void fetchPage(null);
  }, [fetchPage]);

  useEffect(() => {
    if (phase !== "idle" || !cursor || typeof IntersectionObserver === "undefined") return;
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) void fetchPage(cursor);
      },
      { rootMargin: PREFETCH_MARGIN },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [phase, cursor, fetchPage]);

  const groups = useMemo(() => groupByMonth(orders), [orders]);

  const filterLabel = [site ? appLabel(site).name : null, month ? monthLabel(month) : null].filter(Boolean).join(" · ");
  const header = (
    <PageHeader
      title="Orders"
      subtitle={filterLabel || undefined}
      action={
        filterLabel ? (
          <Link
            href="/orders"
            className="-mr-2 inline-flex min-h-11 items-center rounded-control px-2 text-body font-semibold text-accent transition-opacity active:opacity-60"
          >
            Show all
          </Link>
        ) : undefined
      }
    />
  );

  if (phase === "loading") {
    return (
      <>
        {header}
        <div aria-busy="true">
          <p className="sr-only" role="status">
            Loading your orders
          </p>
          <Skeleton className="mb-2 mt-3 h-3.5 w-28" />
          <RowsSkeleton count={7} />
        </div>
      </>
    );
  }

  if (phase === "error") {
    return (
      <>
        {header}
        <LoadError what="your orders" onRetry={() => void fetchPage(null)} />
      </>
    );
  }

  if (orders.length === 0) {
    return (
      <>
        {header}
        {filterLabel ? (
          <EmptyState
            title="No orders here"
            action={
              <Link href="/orders" className={buttonClassName({ variant: "secondary", fullWidth: true })}>
                Show all orders
              </Link>
            }
          >
            Nothing from {filterLabel}.
          </EmptyState>
        ) : (
          <EmptyState
            title="No orders yet"
            action={
              <Link href="/accounts" className={buttonClassName({ fullWidth: true })}>
                Connect an app
              </Link>
            }
          >
            Every order from your connected apps shows up here, newest first, with what you bought.
          </EmptyState>
        )}
      </>
    );
  }

  return (
    <>
      {header}
      <div className="-mt-3">
        {groups.map((g) => (
          <MonthSection key={g.key} group={g} />
        ))}
      </div>

      <div ref={sentinel} className="mt-3">
        {phase === "more" ? (
          <div aria-busy="true">
            <p className="sr-only" role="status">
              Loading older orders
            </p>
            <RowsSkeleton count={3} />
          </div>
        ) : phase === "more-error" ? (
          <Card className="flex items-center justify-between gap-3 py-2">
            <p role="alert" className="text-body text-ink-muted">
              Couldn&apos;t load older orders.
            </p>
            <Button variant="ghost" className="-mr-3" onClick={() => void fetchPage(cursor)}>
              Try again
            </Button>
          </Card>
        ) : cursor ? (
          <Button variant="ghost" fullWidth onClick={() => void fetchPage(cursor)}>
            Show older orders
          </Button>
        ) : (
          <p className="py-2 text-center text-label text-ink-muted">No older orders</p>
        )}
      </div>
    </>
  );
}

function MonthSection({ group }: { group: MonthGroup }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId}>
      <h2
        id={headingId}
        className="sticky top-0 z-10 bg-canvas pb-2 pt-4 text-label font-semibold text-ink-muted"
      >
        {group.label}
      </h2>
      <Card padding="none" className="overflow-hidden">
        <ul>
          {group.orders.map((o) => (
            <OrderRow key={`${o.site}/${o.orderId}`} order={o} />
          ))}
        </ul>
      </Card>
    </section>
  );
}

const OrderRow = memo(function OrderRow({ order }: { order: OrderSummary }) {
  const app = appLabel(order.site, order.subPlatform);
  const merchant = distinctMerchant(order.merchant, app);
  const voided = voidedLabel(order.status);
  const meta = [merchant ? app.name : null, app.sub, shortDate(order.placedAt)].filter(Boolean).join(" · ");

  return (
    <li className="border-b border-line-soft last:border-b-0">
      <Link
        href={`/orders/${encodeURIComponent(order.site)}/${encodeURIComponent(order.orderId)}`}
        className="flex min-h-16 items-center gap-3 py-3 pl-4 pr-3 transition-colors duration-150 hover:bg-canvas active:bg-line-soft"
      >
        <AppDot className={app.dotClass} />
        <span className="min-w-0 flex-1">
          <span data-title="" className="block truncate text-body font-medium">
            {merchant ?? app.name}
          </span>
          <span className="mt-0.5 flex min-w-0 items-center gap-2">
            <span className="truncate text-label text-ink-muted">{meta}</span>
            {voided ? <VoidBadge>{voided}</VoidBadge> : null}
          </span>
        </span>
        {order.total === null ? (
          <span className="shrink-0 text-body text-ink-muted">—</span>
        ) : (
          <Money
            amount={order.total}
            className={cn("shrink-0 text-body font-semibold", voided && "text-ink-muted line-through decoration-ink-muted/60")}
          />
        )}
        <Chevron />
      </Link>
    </li>
  );
});

function RowsSkeleton({ count }: { count: number }) {
  return (
    <Card padding="none" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex min-h-16 items-center gap-3 border-b border-line-soft px-4 py-3 last:border-b-0">
          <Skeleton className="size-2 rounded-full" />
          <div className="flex-1">
            <Skeleton className="h-4 w-36" />
            <Skeleton className="mt-1.5 h-3 w-24" />
          </div>
          <Skeleton className="h-4 w-14" />
        </div>
      ))}
    </Card>
  );
}
