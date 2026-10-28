"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";
import { buttonClassName } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  startCheck,
  watchCartRequest,
  type CartPick,
  type CartRequest,
  type CartResult,
  type CartSite,
  type CartSummary,
} from "@/lib/cart";
import { AppCard } from "./AppCard";
import { ItemTable } from "./ItemTable";
import { PickSheet } from "./PickSheet";
import { ResultHeader } from "./ResultHeader";
import { FailedState, SearchingState } from "./SearchingState";
import { SplitLine } from "./SplitLine";

function editHref(req: CartRequest): string {
  return req.mode === "food" ? `/cart?mode=food&from=${req.id}` : `/cart?from=${req.id}`;
}

/** One cart check: live progress while it runs, then the ranked apps, item table and picks. */
export function CartResultView({ id }: { id: string }) {
  const router = useRouter();
  const tableId = useId();
  const [loaded, setLoaded] = useState(false);
  const [request, setRequest] = useState<CartRequest | null>(null);
  const [results, setResults] = useState<CartResult[]>([]);
  const [summary, setSummary] = useState<CartSummary | null>(null);
  const [tableOpen, setTableOpen] = useState(false);
  const [pick, setPick] = useState<CartPick | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  useEffect(
    () =>
      watchCartRequest(id, ({ request: req, results: res }) => {
        setLoaded(true);
        setRequest(req);
        setResults(res);
        setSummary(req?.summary ?? null);
      }),
    [id],
  );

  async function checkAgain() {
    if (!request || retrying) return;
    if (request.mode === "food") {
      router.push(editHref(request));
      return;
    }
    setRetrying(true);
    setRetryError(null);
    try {
      const started = await startCheck(request.items);
      router.push(`/cart/${started.request_id}`);
    } catch (e) {
      setRetryError(e instanceof ApiError ? e.detail : "Couldn't start a check. Try again.");
      setRetrying(false);
    }
  }

  if (!loaded) {
    return (
      <div aria-busy="true" aria-label="Loading" className="space-y-3">
        <div className="h-8 w-48 animate-pulse rounded-tile bg-line-soft motion-reduce:animate-none" />
        <div className="h-4 w-64 animate-pulse rounded-tile bg-line-soft motion-reduce:animate-none" />
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-24 animate-pulse rounded-card bg-surface shadow-card motion-reduce:animate-none" />
        ))}
      </div>
    );
  }

  if (!request) {
    return (
      <EmptyState title="This check isn't here">
        It may have been made on another account.{" "}
        <Link href="/cart" className="font-semibold text-accent">
          Start a new one
        </Link>
      </EmptyState>
    );
  }

  if (request.status === "failed") {
    return <FailedState onRetry={checkAgain} retrying={retrying} error={retryError} />;
  }

  if (request.status === "searching" || !summary) {
    const sites = request.sites?.length ? request.sites : results.map((r) => r.site);
    return <SearchingState sites={sites} results={results} startedAt={request.created_at} />;
  }

  const cards = [...summary.cards].sort((a, b) => a.rank - b.rank);
  const openPick = (site: CartSite, idx?: number) =>
    setPick(summary.picks.find((p) => p.site === site && (idx === undefined || p.idx === idx)) ?? null);

  return (
    <>
      <ResultHeader summary={summary} onCheckAgain={checkAgain} checking={retrying} error={retryError} />

      <div className="space-y-2.5">
        {cards.map((c) => (
          <AppCard
            key={c.site}
            card={c}
            mode={summary.mode}
            offersNote={summary.offers_note}
            onPick={() => openPick(c.site)}
          />
        ))}
      </div>

      {summary.table.columns.length ? (
        <div className="mt-4">
          <button
            type="button"
            aria-expanded={tableOpen}
            aria-controls={tableId}
            onClick={() => setTableOpen((o) => !o)}
            className="flex min-h-11 w-full items-center justify-between rounded-control px-1 text-body font-semibold text-accent hover:bg-accent/5"
          >
            See item prices
            <svg
              aria-hidden="true"
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className={cn("transition-transform duration-200 ease-out-quart", tableOpen && "rotate-180")}
            >
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
          <div id={tableId}>
            {tableOpen ? (
              <div className="mt-1 animate-fade-in">
                <ItemTable table={summary.table} cheapestSite={summary.best_site} onPick={(idx, site) => openPick(site, idx)} />
                <SplitLine split={summary.split} />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <Link href={editHref(request)} className={buttonClassName({ variant: "secondary", fullWidth: true, className: "mt-4" })}>
        Edit list
      </Link>

      <PickSheet
        requestId={summary.request_id}
        mode={summary.mode}
        pick={pick}
        onClose={() => setPick(null)}
        onChosen={setSummary}
      />
    </>
  );
}
