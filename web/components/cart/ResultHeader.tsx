"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import type { CartSummary } from "@/lib/cart";

/** Prices older than this get "Prices may have changed." */
export const STALE_MS = 30 * 60_000;

/** "just now", "2 min ago", "3 h ago", "2 d ago". */
export function agoLabel(iso: string | null, now: number): string {
  const then = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(then)) return "";
  const m = Math.floor(Math.max(0, now - then) / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.floor(h / 24)} d ago`;
}

/** A clock that ticks every `ms` (the "2 min ago" line refreshes every minute). */
export function useNow(ms = 60_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export interface ResultHeaderProps {
  summary: CartSummary;
  onCheckAgain: () => void;
  checking: boolean;
  error: string | null;
}

export function ResultHeader({ summary, onCheckAgain, checking, error }: ResultHeaderProps) {
  const now = useNow();
  const ago = agoLabel(summary.priced_at, now);
  const priced = summary.priced_at ? Date.parse(summary.priced_at) : NaN;
  const stale = Number.isFinite(priced) && now - priced > STALE_MS;
  const meta = [summary.count_label, `priced live at ${summary.area}`, ago].filter(Boolean).join(" · ");

  return (
    <header className="mb-4">
      <h1 className="text-title">{summary.title}</h1>
      {summary.mode === "food" && summary.restaurant?.locality ? (
        <p className="text-body text-ink-muted">{summary.restaurant.locality}</p>
      ) : null}
      <p className="mt-1 text-label text-ink-muted">{meta}</p>
      {summary.price_note ? <p className="mt-0.5 text-caption text-ink-muted">{summary.price_note}</p> : null}
      {stale ? (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-control bg-up-soft py-1 pl-3 pr-1">
          <p className="text-label font-medium text-up">Prices may have changed.</p>
          <Button variant="ghost" onClick={onCheckAgain} loading={checking}>
            Check again
          </Button>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 text-label text-danger">
          {error}
        </p>
      ) : null}
    </header>
  );
}
