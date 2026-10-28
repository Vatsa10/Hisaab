"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api";
import { confirmFill, type CartFill, type FillSummary } from "@/lib/fill";

export interface RepriceNoticeProps {
  fill: CartFill;
  summary: FillSummary;
  appName: string;
  onConfirmed: (newFillId: string) => void;
  onNotNow: () => void;
}

/** The app is set to another location: its prices there, and a fill pinned to them if the user agrees. */
export function RepriceNotice({ fill, summary, appName, onConfirmed, onNotNow }: RepriceNoticeProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const started = await confirmFill(fill.id);
      onConfirmed(started.fill_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Couldn't start the fill. Try again.");
      setBusy(false);
    }
  }

  return (
    <div>
      <h3 className="text-headline text-up">{summary.title}</h3>
      {summary.location_line ? <p className="mt-1 text-body">{summary.location_line}</p> : null}
      {summary.reprice ? (
        <div className="mt-3">
          <ul className="divide-y divide-line-soft">
            {summary.reprice.rows.map((r, i) => (
              <li key={`${r.label}-${i}`} className="flex min-h-11 items-center justify-between gap-3 py-2 text-body">
                <span className="min-w-0 flex-1 break-words">{r.label}</span>
                <span className="shrink-0 tabular-nums text-ink-muted">
                  {r.old} → <span className="font-semibold text-ink">{r.new}</span>
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-body font-semibold tabular-nums">{summary.reprice.total_line}</p>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="mt-3 rounded-tile bg-danger-soft px-3 py-2 text-label text-danger">
          {error}
        </p>
      ) : null}
      <div className="mt-4 flex flex-col gap-2">
        {summary.confirm_label ? (
          <Button fullWidth loading={busy} onClick={confirm}>
            {summary.confirm_label}
          </Button>
        ) : null}
        <Button variant={summary.confirm_label ? "ghost" : "secondary"} fullWidth onClick={onNotNow}>
          Not now
        </Button>
      </div>
      <p className="mt-3 text-center text-caption text-ink-muted">Or change the location in {appName}, then check again.</p>
    </div>
  );
}
