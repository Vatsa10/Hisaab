"use client";

import { useEffect, useState } from "react";
import type { SupabaseClient } from "@supabase/supabase-js";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { JOB_COLUMNS } from "@/lib/jobs";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { SITE_DOTS, siteName, type CartResult, type CartSite } from "@/lib/cart";

/** After this long, say the check is slow and that the user may leave. */
export const SLOW_MS = 120_000;

/** The login profile each priced site runs on (Instamart uses the Swiggy session). */
const PROFILE: Record<CartSite, string> = {
  zepto: "zepto",
  blinkit: "blinkit",
  instamart: "swiggy",
  swiggy: "swiggy",
  zomato: "zomato",
};

/** Sites (profiles) with a queued or running sync / connect job for this user (RLS). */
export async function busyProfiles(client: SupabaseClient = getSupabaseBrowserClient()): Promise<Set<string>> {
  try {
    const { data } = await client
      .from("jobs")
      .select(JOB_COLUMNS)
      .in("status", ["queued", "running"])
      .in("type", ["sync", "connect"]);
    return new Set(Array.isArray(data) ? (data as Array<{ site?: unknown }>).map((r) => String(r.site ?? "")) : []);
  } catch {
    return new Set();
  }
}

export interface SearchingStateProps {
  sites: CartSite[];
  results: CartResult[];
  /** When the check started (ISO). */
  startedAt: string;
}

function Row({ site, result, busy }: { site: CartSite; result: CartResult | undefined; busy: boolean }) {
  const name = siteName(site);
  let status: string;
  let tone = "text-ink-muted";
  if (!result) status = busy ? `Waiting for your ${name} sync to finish…` : "Checking…";
  else if (result.status === "failed") status = "Couldn't check";
  else {
    status = "✓ Done";
    tone = "text-accent font-semibold";
  }
  return (
    <li className="flex min-h-12 items-center justify-between gap-3 border-t border-line-soft first:border-t-0">
      <span className="flex items-center gap-2 text-body font-medium">
        <span aria-hidden="true" className={cn("size-2 rounded-full", SITE_DOTS[site])} />
        {name}
      </span>
      <span className={cn("text-right text-label", tone)}>
        {!result && !busy ? (
          <span aria-hidden="true" className="mr-1.5 inline-block size-1.5 animate-pulse rounded-full bg-accent align-middle motion-reduce:animate-none" />
        ) : null}
        {status}
      </span>
    </li>
  );
}

/** "Checking your prices…" with one live row per app the check prices. */
export function SearchingState({ sites, results, startedAt }: SearchingStateProps) {
  const [busy, setBusy] = useState<Set<string>>(() => new Set());
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      void busyProfiles().then((b) => {
        if (alive) setBusy(b);
      });
    load();
    const poll = setInterval(load, 15_000);
    return () => {
      alive = false;
      clearInterval(poll);
    };
  }, [results.length]);

  useEffect(() => {
    const started = Date.parse(startedAt);
    if (!Number.isFinite(started)) return;
    const t = setTimeout(() => setSlow(true), Math.max(0, started + SLOW_MS - Date.now()));
    return () => clearTimeout(t);
  }, [startedAt]);

  return (
    <section aria-live="polite">
      <h1 className="text-title">Checking your prices…</h1>
      <p className="mt-1 text-label text-ink-muted">Live from each app, with fees. Usually under a minute.</p>
      <ul className="mt-4 rounded-card bg-surface px-4 shadow-card">
        {sites.map((s) => (
          <Row key={s} site={s} result={results.find((r) => r.site === s)} busy={busy.has(PROFILE[s])} />
        ))}
      </ul>
      {slow ? (
        <p className="mt-4 px-1 text-label text-ink-muted">
          Taking longer than usual. You can leave this screen. Your result will be here.
        </p>
      ) : null}
    </section>
  );
}

export function FailedState({ onRetry, retrying, error }: { onRetry: () => void; retrying: boolean; error: string | null }) {
  return (
    <section className="rounded-card bg-surface p-5 shadow-card">
      <h1 className="text-headline">No prices this time</h1>
      <p className="mt-1 text-body text-ink-muted">We couldn&apos;t get prices right now. This check wasn&apos;t counted.</p>
      {error ? (
        <p role="alert" className="mt-2 text-label text-danger">
          {error}
        </p>
      ) : null}
      <Button className="mt-4" fullWidth onClick={onRetry} loading={retrying}>
        Try again
      </Button>
    </section>
  );
}
