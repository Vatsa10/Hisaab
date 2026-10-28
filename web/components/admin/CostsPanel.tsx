"use client";

import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import { Skeleton } from "@/components/spend/parts";
import {
  currentMonthIST,
  formatGB,
  formatMinutes,
  formatTokens,
  getCosts,
  maskEmail,
  shiftMonth,
  type Costs,
} from "@/lib/admin";
import { formatINR, monthLabel } from "@/lib/format";
import { BudgetBar } from "./BudgetBar";
import { ListSkeleton, LoadError, SectionLabel, useAdminLoad, type PanelProps } from "./parts";

function MonthStepper({ month, onChange }: { month: string; onChange: (m: string) => void }) {
  const latest = month >= currentMonthIST();
  const btn =
    "grid size-11 place-items-center rounded-full text-ink transition-colors duration-150 " +
    "hover:bg-surface active:bg-line-soft " +
    "disabled:cursor-not-allowed disabled:text-line disabled:hover:bg-transparent";
  return (
    <div className="mb-3 flex items-center justify-between gap-2">
      <button type="button" className={btn} aria-label="Previous month" onClick={() => onChange(shiftMonth(month, -1))}>
        <Arrow dir="left" />
      </button>
      <p aria-live="polite" className="text-headline">
        {monthLabel(month)}
      </p>
      <button
        type="button"
        className={btn}
        aria-label="Next month"
        disabled={latest}
        onClick={() => onChange(shiftMonth(month, 1))}
      >
        <Arrow dir="right" />
      </button>
    </div>
  );
}

function Arrow({ dir }: { dir: "left" | "right" }) {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={dir === "left" ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6"} />
    </svg>
  );
}

function unitLine(c: Costs): string {
  const u = c.unit_costs;
  const parts = [
    `${formatINR(u.proxy_inr_per_gb)} / GB proxy`,
    `${formatINR(u.ai_inr_per_1k_tokens)} / 1k AI tokens`,
    `${formatINR(u.worker_inr_per_minute)} / worker min`,
  ];
  if (c.fixed_inr > 0) parts.push(`${formatINR(c.fixed_inr)} fixed / month`);
  return parts.join(" · ");
}

/** Costs tab: the month's budget, the unit costs behind it, and what each user cost. */
export function CostsPanel({ onForbidden, initialMonth }: PanelProps & { initialMonth?: string }) {
  const [month, setMonth] = useState(() => initialMonth ?? currentMonthIST());
  const costs = useAdminLoad(month, () => getCosts(month), onForbidden);

  return (
    <div>
      <MonthStepper month={month} onChange={setMonth} />

      {costs.status === "loading" ? (
        <>
          <Card aria-busy="true" aria-label="Budget">
            <Skeleton className="h-4 w-28" />
            <Skeleton className="mt-2 h-8 w-40" />
            <Skeleton className="mt-3 h-2 w-full rounded-full" />
          </Card>
          <div className="mt-6">
            <ListSkeleton label="Cost per user" />
          </div>
        </>
      ) : costs.status === "error" ? (
        <LoadError what="costs" onRetry={costs.reload} />
      ) : (
        <CostsBody costs={costs.data} />
      )}
    </div>
  );
}

function CostsBody({ costs }: { costs: Costs }) {
  const users = [...costs.users].sort((a, b) => b.inr - a.inr);
  return (
    <>
      <BudgetBar spent={costs.spent_inr} budget={costs.budget_inr} pct={costs.pct} level={costs.level} />
      <p className="mt-2 px-1 text-label text-ink-muted">{unitLine(costs)}</p>

      <SectionLabel aside={users.length ? `${users.length} ${users.length === 1 ? "user" : "users"}` : undefined}>
        Cost per user
      </SectionLabel>
      {users.length === 0 ? (
        <Card>
          <p className="text-body text-ink-muted">No usage recorded in {monthLabel(costs.month)}.</p>
        </Card>
      ) : (
        <Card padding="none">
          <ul aria-label="Cost per user">
            {users.map((u) => (
              <li key={u.user_id} className="flex items-center gap-3 border-b border-line-soft px-4 py-3 last:border-b-0">
                <div className="min-w-0 flex-1">
                  <p data-email="" className="truncate font-medium" title={maskEmail(u.email)}>
                    {maskEmail(u.email)}
                  </p>
                  <p className="mt-0.5 text-label tabular-nums text-ink-muted">
                    {formatGB(u.proxy_gb)} · {formatTokens(u.ai_tokens)} · {formatMinutes(u.worker_minutes)}
                  </p>
                  <p className="text-label font-light tabular-nums text-ink-muted">
                    {u.syncs} {u.syncs === 1 ? "sync" : "syncs"} · {u.cart_checks}{" "}
                    {u.cart_checks === 1 ? "cart check" : "cart checks"} · {u.cart_fills ?? 0}{" "}
                    {(u.cart_fills ?? 0) === 1 ? "fill" : "fills"}
                  </p>
                </div>
                <Money amount={u.inr} className="shrink-0 text-body font-semibold" />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
