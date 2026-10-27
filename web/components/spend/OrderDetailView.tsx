"use client";

import Link from "next/link";
import { useId, type ReactNode } from "react";
import { buttonClassName } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Money } from "@/components/ui/Money";
import { cn } from "@/lib/cn";
import { formatINR } from "@/lib/format";
import { getOrder } from "@/lib/queries";
import { appLabel, distinctMerchant, longDateTime, qtyLabel, statusLabel, voidedLabel } from "./display";
import { AppDot, LoadError, Skeleton, VoidBadge } from "./parts";
import { useLoad } from "./useLoad";

export interface OrderDetailViewProps {
  site: string;
  orderId: string;
}

/** One order: app, merchant, total, when it was placed, status, and every item with qty and price. */
export function OrderDetailView({ site, orderId }: OrderDetailViewProps) {
  const res = useLoad(`${site}\u0000${orderId}`, () => getOrder(site, orderId));

  return (
    <>
      <Link
        href="/orders"
        className="-ml-2 mb-1 inline-flex min-h-11 items-center gap-0.5 rounded-control pl-1 pr-2 text-body font-medium text-accent transition-opacity active:opacity-60"
      >
        <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M15 6l-6 6 6 6" />
        </svg>
        Orders
      </Link>

      {res.status === "loading" ? (
        <DetailSkeleton />
      ) : res.status === "error" ? (
        <LoadError what="this order" onRetry={res.retry} />
      ) : res.data === null ? (
        <EmptyState
          title="Order not found"
          action={
            <Link href="/orders" className={buttonClassName({ variant: "secondary", fullWidth: true })}>
              Back to orders
            </Link>
          }
        >
          It may have been removed, or it belongs to another account.
        </EmptyState>
      ) : (
        <Detail order={res.data} />
      )}
    </>
  );
}

function Detail({ order }: { order: NonNullable<Awaited<ReturnType<typeof getOrder>>> }) {
  const itemsId = useId();
  const app = appLabel(order.site, order.subPlatform);
  const merchant = distinctMerchant(order.merchant, app);
  const voided = voidedLabel(order.status);
  const status = statusLabel(order.status);
  const anyPrice = order.items.some((i) => i.price !== null);
  // Title: the restaurant or store; for grocery orders without one, the app (Instamart for
  // Swiggy Instamart). The eyebrow names the app only when the title doesn't already.
  const title = merchant ?? app.sub ?? app.name;
  const eyebrow = merchant ? (app.sub ? `${app.name} · ${app.sub}` : app.name) : app.sub ? app.name : null;

  return (
    <>
      <header className="mb-4">
        {eyebrow ? (
          <p className="mb-0.5 flex items-center gap-1.5 text-label text-ink-muted">
            <AppDot className={app.dotClass} />
            {eyebrow}
          </p>
        ) : null}
        <h1 className="flex items-center gap-2.5 text-title">
          {eyebrow ? null : <AppDot className={app.dotClass} />}
          {title}
        </h1>
      </header>

      <div className="space-y-3">
        <Card as="section" aria-label="Summary">
          <div className="flex items-center justify-between gap-3">
            {order.total === null ? (
              <p className="text-figure text-ink-muted">—</p>
            ) : (
              <p className={cn("text-figure", voided && "text-ink-muted line-through decoration-2")}>
                <Money amount={order.total} />
              </p>
            )}
            {voided ? <VoidBadge>{voided}</VoidBadge> : null}
          </div>
          {voided ? <p className="mt-1 text-label text-ink-muted">Not counted in your spending.</p> : null}
          <dl className="mt-3 border-t border-line-soft">
            <Fact label="Placed">{longDateTime(order.placedAt)}</Fact>
            {status && !voided ? <Fact label="Status">{status}</Fact> : null}
            <Fact label="Order ID">
              <span className="select-all break-all text-label">{order.orderId}</span>
            </Fact>
          </dl>
        </Card>

        <Card as="section" aria-labelledby={itemsId} padding="none" className="overflow-hidden">
          <h2 id={itemsId} className="px-4 pb-1 pt-4 text-label text-ink-muted">
            {order.items.length} {order.items.length === 1 ? "item" : "items"}
          </h2>
          {order.items.length === 0 ? (
            <p className="px-4 pb-4 pt-1 text-body text-ink-muted">{app.name} didn&apos;t list the items for this order.</p>
          ) : (
            <ul>
              {order.items.map((item) => (
                <li key={item.idx} className="flex items-start gap-3 border-b border-line-soft px-4 py-3 last:border-b-0">
                  <span className="min-w-0 flex-1">
                    <span className="block break-words text-body">{item.name ?? "Unnamed item"}</span>
                    {item.qty !== null ? (
                      <span className="mt-0.5 block text-label tabular-nums text-ink-muted">{qtyLine(item.qty, item.price)}</span>
                    ) : null}
                  </span>
                  {item.price !== null ? (
                    <Money amount={item.price} className="shrink-0 text-body font-medium" />
                  ) : (
                    <span className="shrink-0 text-body text-ink-muted">
                      <span aria-hidden="true">—</span>
                      <span className="sr-only">No price listed</span>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
        {order.items.length > 0 && !anyPrice ? (
          <p className="px-4 text-label text-ink-muted">{app.name} doesn&apos;t list item prices, only the order total.</p>
        ) : null}
      </div>
    </>
  );
}

/**
 * The line under an item's name. A stored `price` is the line total: recipes.py multiplies a
 * per-unit price by qty before saving (`price_is_unit`), and the category_spend view sums it
 * as-is. So with qty above 1 the unit price is derived ("2 × ₹199", with ₹398 on the right);
 * otherwise just the quantity ("×1").
 */
function qtyLine(qty: number, price: number | null): string {
  if (price !== null && qty > 1) return `${qtyLabel(qty)} × ${formatINR(price / qty)}`;
  return `×${qtyLabel(qty)}`;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line-soft py-2.5 last:border-b-0 last:pb-0">
      <dt className="shrink-0 text-label text-ink-muted">{label}</dt>
      <dd className="min-w-0 text-right text-body">{children}</dd>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div aria-busy="true">
      <p className="sr-only" role="status">
        Loading this order
      </p>
      <div aria-hidden="true">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="mb-4 mt-2 h-7 w-48" />
        <Card>
          <Skeleton className="h-8 w-32" />
          <div className="mt-4 space-y-3">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-full" />
          </div>
        </Card>
        <Card className="mt-3">
          <Skeleton className="h-3.5 w-16" />
          {[0, 1, 2].map((i) => (
            <div key={i} className="mt-4 flex justify-between">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-4 w-12" />
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
