"use client";

import { useId } from "react";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import { cn } from "@/lib/cn";
import { formatINR, monthLabel, pctChange } from "@/lib/format";
import { previousMonth, shortMonth, type MonthPoint } from "./summary";

/** "2026-09" -> "September" */
function monthName(month: string): string {
  return monthLabel(month).split(" ")[0] ?? month;
}

/** Placeholder silhouette for the empty chart (the approved mockup's bar shape, faded). */
const GHOST_HEIGHTS = [48, 96, 100, 50, 36, 16];

export interface TotalCardProps {
  months: MonthPoint[];
  currentMonth: string;
  selectedMonth: string;
  /** Total of the month before the selected one (0 or missing: no comparison). */
  previousTotal: number | undefined;
  onSelect?: (month: string) => void;
  /** Mockup A at zero: ₹0, faded bars that do nothing. */
  empty?: boolean;
}

/**
 * The big number: the selected month's total (the current month reads "This month so far"), its
 * change against the month before (green ▼ when less, orange ▲ when more, hidden when there is
 * nothing to compare with), and six monthly bars. Tapping a bar selects that month.
 */
export function TotalCard({ months, currentMonth, selectedMonth, previousTotal, onSelect, empty = false }: TotalCardProps) {
  const labelId = useId();
  const point = months.find((m) => m.month === selectedMonth) ?? months[months.length - 1];
  const total = empty ? 0 : (point?.total ?? 0);
  const isCurrent = selectedMonth === currentMonth;
  const pct = empty ? null : pctChange(total, previousTotal);
  const prevName = monthName(previousMonth(selectedMonth));
  const max = Math.max(...months.map((m) => m.total), 0);

  return (
    <Card as="section" aria-labelledby={labelId}>
      <h2 id={labelId} className="text-label text-ink-muted">
        {isCurrent ? "This month so far" : monthLabel(selectedMonth)}
      </h2>
      <p className="mt-0.5 text-figure">
        <Money amount={total} />
      </p>
      {pct === null ? null : Math.round(pct) === 0 ? (
        <p className="mt-0.5 text-label font-semibold text-ink-muted">Same as {prevName}</p>
      ) : (
        <p className={cn("mt-0.5 text-label font-semibold", pct < 0 ? "text-accent" : "text-up")}>
          <span aria-hidden="true">{pct < 0 ? "▼" : "▲"}</span> {Math.round(Math.abs(pct))}% vs {prevName}
          <span className="sr-only">{pct < 0 ? ", less" : ", more"}</span>
        </p>
      )}

      {empty ? (
        <div aria-hidden="true" className="mt-4 grid grid-cols-6 gap-1.5">
          {months.map((m, i) => (
            <div key={m.month} className="flex flex-col gap-1.5">
              <div className="flex h-20 items-end">
                <div className="w-full rounded-t bg-accent-bar/50" style={{ height: `${GHOST_HEIGHTS[i] ?? 40}%` }} />
              </div>
              <span className="text-center text-caption text-ink-muted">{shortMonth(m.month)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div role="group" aria-label="Last six months" className="mt-4 grid grid-cols-6 gap-1.5">
          {months.map((m) => {
            const selected = m.month === selectedMonth;
            const current = m.month === currentMonth;
            const height = max > 0 && m.total > 0 ? Math.max(4, (m.total / max) * 100) : 0;
            return (
              <button
                key={m.month}
                type="button"
                aria-pressed={selected}
                aria-label={`${monthLabel(m.month)}, ${formatINR(m.total)}`}
                onClick={() => onSelect?.(m.month)}
                className="group flex flex-col gap-1.5 rounded-tile pt-1 transition-opacity duration-150 active:opacity-70"
              >
                <span className="flex h-20 w-full items-end">
                  {height > 0 ? (
                    <span
                      className={cn(
                        "block w-full rounded-t transition-colors duration-200 ease-out-quart",
                        selected ? "bg-accent" : "bg-accent-bar group-hover:bg-accent/30",
                      )}
                      style={{ height: `${height}%` }}
                    />
                  ) : (
                    <span className={cn("block h-0.5 w-full rounded-full", selected ? "bg-accent" : "bg-line")} />
                  )}
                </span>
                <span
                  className={cn(
                    "w-full text-center text-caption",
                    selected ? "font-semibold text-ink" : current ? "font-medium text-ink" : "text-ink-muted",
                  )}
                >
                  {shortMonth(m.month)}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </Card>
  );
}
