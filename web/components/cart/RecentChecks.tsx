"use client";

import Link from "next/link";
import { useId } from "react";
import { Card } from "@/components/ui/Card";
import { recentLine, type CartRequest } from "@/lib/cart";

export interface RecentChecksProps {
  checks: readonly CartRequest[];
  now?: number;
}

/** The last few checks (groceries and food), each opening its result. Hidden when there are none. */
export function RecentChecks({ checks, now }: RecentChecksProps) {
  const headingId = useId();
  if (!checks.length) return null;
  return (
    <section aria-labelledby={headingId} className="mt-5">
      <h2 id={headingId} className="mb-2 px-1 text-label font-semibold text-ink-muted">
        Recent checks
      </h2>
      <Card padding="none">
        <ul>
          {checks.map((req) => (
            <li key={req.id} className="group">
              <Link
                href={`/cart/${encodeURIComponent(req.id)}`}
                className="flex min-h-12 items-center gap-3 pl-4 transition-colors duration-150 first:rounded-t-card active:bg-canvas group-first:rounded-t-card group-last:rounded-b-card"
              >
                <span
                  className="flex min-h-12 min-w-0 flex-1 items-center justify-between gap-3 border-b border-line-soft pr-4 group-last:border-b-0"
                  suppressHydrationWarning
                >
                  <span className="min-w-0 truncate tabular-nums">{recentLine(req, now)}</span>
                  <span aria-hidden="true" className="text-ink-muted">
                    ›
                  </span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </section>
  );
}
