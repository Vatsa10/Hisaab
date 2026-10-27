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

/** "43 orders · ₹13,240 found", or "43 orders found" when the total is not known. */
export function foundLine(orders: number, amount?: number | null): { count: string; rest: string } {
  const count = `${orders} ${orders === 1 ? "order" : "orders"}`;
  const rest = typeof amount === "number" && Number.isFinite(amount) ? ` · ${formatINR(amount)} found` : " found";
  return { count, rest };
}

export interface SyncProgressProps {
  app: AppInfo;
  state: "syncing" | "synced";
  ordersFound?: number;
  amountFound?: number;
  /** The next app to connect, if any is left. */
  nextApp?: AppInfo;
  onNext: (app: AppInfo) => void;
  onDone: () => void;
}

/** The first sync after signing in: a live bar while it runs, then what was found. */
export function SyncProgress({ app, state, ordersFound, amountFound, nextApp, onNext, onDone }: SyncProgressProps) {
  const done = state === "synced";
  const found = foundLine(ordersFound ?? 0, amountFound);

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
        <div className="rounded-control bg-canvas px-4 py-3.5" role="status">
          <p className="text-label text-ink-muted">First sync</p>
          <p className="mt-0.5 text-title tabular-nums">
            {found.count}
            <span className="font-semibold text-ink-muted">{found.rest}</span>
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2.5" role="status">
          <IndeterminateBar label={`Syncing ${app.name}`} />
          <p className="text-body font-medium">Finding your orders…</p>
          <p className="text-label text-ink-muted">
            This takes up to a minute. You can close this; it carries on and your apps list updates when
            it&apos;s done.
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
