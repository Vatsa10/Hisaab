import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import type { BudgetLevel } from "@/lib/admin";
import { cn } from "@/lib/cn";

export interface BudgetBarProps {
  spent: number;
  budget: number;
  pct: number;
  level: BudgetLevel | string;
}

const LEVELS: Record<BudgetLevel, { fill: string; text: string; label: string }> = {
  ok: { fill: "bg-accent", text: "text-accent", label: "On track" },
  alert: { fill: "bg-up", text: "text-up", label: "Over the alert line" },
  paused: { fill: "bg-danger", text: "text-danger", label: "Budget reached" },
};

/**
 * This month's spend against the budget: the figure, a bar filled to the percentage, and the
 * level in words. Green while ok, orange from the alert line, red once paused (with what that
 * pauses). The bar caps at 100% wide; the percentage shows the real number.
 */
export function BudgetBar({ spent, budget, pct, level }: BudgetBarProps) {
  const l = LEVELS[(level in LEVELS ? level : "ok") as BudgetLevel];
  const width = Math.min(100, Math.max(0, Number.isFinite(pct) ? pct : 0));

  return (
    <Card as="section" aria-label="Budget">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-label text-ink-muted">Spent this month</h2>
        <p className={cn("text-label font-semibold", l.text)}>{l.label}</p>
      </div>
      <p className="mt-1 flex flex-wrap items-baseline gap-x-1.5">
        <Money amount={spent} className="text-figure" />
        <span className="text-body text-ink-muted">
          of <Money amount={budget} />
        </span>
      </p>
      <div
        role="meter"
        aria-label="Budget used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(width)}
        aria-valuetext={`${Math.round(pct)}% of the monthly budget`}
        className="mt-3 h-2 overflow-hidden rounded-full bg-line-soft"
      >
        <div
          data-testid="budget-fill"
          className={cn("h-full rounded-full", l.fill)}
          style={{ width: `${width > 0 ? Math.max(2, width) : 0}%` }}
        />
      </div>
      <p className="mt-2 flex justify-between gap-3 text-label text-ink-muted">
        <span className="tabular-nums">{Math.round(pct)}% used</span>
        <span className="tabular-nums">
          {budget - spent >= 0 ? (
            <>
              <Money amount={budget - spent} /> left
            </>
          ) : (
            <>
              <Money amount={spent - budget} /> over
            </>
          )}
        </span>
      </p>
      {level === "paused" ? (
        <p className="mt-3 rounded-field bg-danger-soft px-3.5 py-2.5 text-label font-semibold text-danger">
          Sign-ups and cart checks paused
        </p>
      ) : null}
    </Card>
  );
}
