"use client";

import { Button } from "@/components/ui/Button";
import type { AppInfo } from "@/lib/apps";
import { formatINR } from "@/lib/format";
import { CheckIcon } from "./icons";
import styles from "./connect.module.css";

/** An indeterminate bar: the work has no known length, so no aria-valuenow. */
export function IndeterminateBar({ label }: { label: string }) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      className="h-1.5 w-full overflow-hidden rounded-full bg-line-soft"
    >
      <div className={`h-full rounded-full bg-accent ${styles.sweep}`} />
    </div>
  );
}

/** "43 orders" / "1 order". */
export function ordersLabel(orders: number): string {
  return `${orders} ${orders === 1 ? "order" : "orders"}`;
}

/** "₹13,240 · Oct 2025 – today", or whichever half is known (null when neither is). */
export function foundDetail(amount?: number | null, since?: string | null): string | null {
  const parts: string[] = [];
  if (typeof amount === "number" && Number.isFinite(amount)) parts.push(formatINR(amount));
  if (since) parts.push(`${since} – today`);
  return parts.length ? parts.join(" · ") : null;
}

function FoundCard({
  label,
  orders,
  amount,
  since,
}: {
  label: string;
  orders: number;
  amount?: number;
  since?: string | null;
}) {
  const detail = foundDetail(amount, since);
  return (
    <div className="rounded-control bg-canvas px-4 py-3.5">
      <p className="text-label text-ink-muted">{label}</p>
      <p className="mt-0.5 text-title tabular-nums">{ordersLabel(orders)}</p>
      {detail ? <p className="mt-0.5 text-label tabular-nums text-ink-muted">{detail}</p> : null}
    </div>
  );
}

export interface SyncProgressProps {
  app: AppInfo;
  state: "syncing" | "synced";
  ordersFound?: number;
  amountFound?: number;
  /** Month of the earliest order found, e.g. "Oct 2025". */
  since?: string | null;
  /** The next app to connect, if any is left. */
  nextApp?: AppInfo;
  onNext: (app: AppInfo) => void;
  onDone: () => void;
}

/** The first sync after signing in: a live bar (and count so far) while it runs, then what was found. */
export function SyncProgress({ app, state, ordersFound, amountFound, since, nextApp, onNext, onDone }: SyncProgressProps) {
  const done = state === "synced";
  const orders = ordersFound ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="grid size-10 shrink-0 place-items-center rounded-full bg-accent-soft text-accent"
        >
          <CheckIcon size={22} strokeWidth={2.25} />
        </span>
        <p className="text-body text-ink-muted">
          Signed in to {app.name}. {done ? "Your order history is in." : "Reading your order history now."}
        </p>
      </div>

      {done ? (
        <div role="status">
          <FoundCard label="Found" orders={orders} amount={amountFound} since={since} />
        </div>
      ) : (
        <div className="flex flex-col gap-2.5">
          <IndeterminateBar label={`Syncing ${app.name}`} />
          <p className="text-body font-medium">Finding your orders…</p>
          <div aria-live="polite">
            {orders > 0 ? <FoundCard label="Found so far" orders={orders} amount={amountFound} /> : null}
          </div>
          <p className="text-label text-ink-muted">
            Takes up to a minute. You can close this; we&apos;ll keep going and update your apps list.
          </p>
        </div>
      )}

      {done ? (
        <div className="flex flex-col gap-2">
          {nextApp ? (
            <>
              <Button size="lg" fullWidth onClick={() => onNext(nextApp)}>
                Connect {nextApp.name}
              </Button>
              <Button variant="ghost" fullWidth onClick={onDone}>
                Done
              </Button>
            </>
          ) : (
            <Button size="lg" fullWidth onClick={onDone}>
              Done
            </Button>
          )}
        </div>
      ) : null}
    </div>
  );
}
