"use client";

import { useState, type FormEvent } from "react";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/TextField";
import { Spinner } from "@/components/connect/icons";
import { SITE_DOTS, type FoodSite } from "@/lib/cart";
import { cn } from "@/lib/cn";
import { MIN_QUERY, SEARCHING_LINE, type RestaurantPair, type UsualRestaurant } from "@/lib/food";
import { RestaurantPairCard } from "./RestaurantPairCard";

export interface RestaurantSearchProps {
  disabled: boolean;
  usual: UsualRestaurant[];
  /** The text in the field. */
  query: string;
  onQuery: (q: string) => void;
  searching: boolean;
  error: string | null;
  /** null before the first search. */
  results: RestaurantPair[] | null;
  onSearch: (q: string) => void;
  onPick: (pair: RestaurantPair) => void;
}

function orders(n: number): string {
  return `${n} ${n === 1 ? "order" : "orders"}`;
}

/** Step 1: the restaurants you order from, a search field (submitted, never per keystroke) and the paired results. */
export function RestaurantSearch({
  disabled,
  usual,
  query,
  onQuery,
  searching,
  error,
  results,
  onSearch,
  onPick,
}: RestaurantSearchProps) {
  const [touched, setTouched] = useState(false);
  const ready = query.trim().length >= MIN_QUERY;

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (disabled || searching || !ready) return;
    onSearch(query.trim());
  }

  return (
    <>
      {usual.length ? (
        <section aria-labelledby="usual-restaurants" className="mt-6">
          <h2 id="usual-restaurants" className="mb-2 pl-1 text-headline">
            Restaurants you order from
          </h2>
          <Card as="div" padding="none">
            <ul>
              {usual.slice(0, 10).map((r) => (
                <li key={r.name} className="border-b border-line-soft last:border-b-0">
                  <button
                    type="button"
                    disabled={disabled || searching}
                    onClick={() => onSearch(r.name)}
                    className="flex min-h-14 w-full items-center gap-3 px-4 py-2 text-left transition-colors duration-150 active:bg-canvas disabled:opacity-50"
                  >
                    <span className="min-w-0 flex-1 break-words font-medium">{r.name}</span>
                    <span className="flex shrink-0 items-center gap-1" aria-hidden="true">
                      {r.sites.map((s) => (
                        <span key={s} className={cn("size-2 rounded-full", SITE_DOTS[s as FoodSite])} />
                      ))}
                    </span>
                    <span className="shrink-0 text-label tabular-nums text-ink-muted">{orders(r.orders)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        </section>
      ) : null}

      <section aria-label="Find a restaurant" className="mt-6">
        <Card padding="none" className="px-2 py-3">
          <form onSubmit={submit} noValidate>
            <TextField
              label="Search a restaurant"
              value={query}
              onChange={(e) => onQuery(e.target.value)}
              placeholder="e.g. Dum Pukht"
              maxLength={60}
              enterKeyHint="search"
              autoComplete="off"
              disabled={disabled}
              error={touched && !ready && query.trim() ? `Type at least ${MIN_QUERY} letters.` : undefined}
              trailing={
                <button
                  type="submit"
                  disabled={disabled || searching || !ready}
                  className="mr-1 min-h-11 shrink-0 rounded-tile px-3 text-body font-semibold text-accent transition-[background-color,opacity] duration-150 active:bg-accent/10 disabled:opacity-40"
                >
                  Search
                </button>
              }
            />
          </form>
        </Card>
      </section>

      {searching ? (
        <p role="status" className="mt-4 flex items-center gap-2 px-1 text-label text-ink-muted">
          <Spinner className="size-4" />
          {SEARCHING_LINE}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-4 px-1 text-label text-danger">
          {error}
        </p>
      ) : null}

      {results && !searching ? (
        results.length ? (
          <section aria-label="Results" className="mt-4">
            <Card padding="none">
              <ul>
                {results.map((p) => (
                  <RestaurantPairCard key={p.key} pair={p} onSelect={() => onPick(p)} />
                ))}
              </ul>
            </Card>
          </section>
        ) : (
          <p className="mt-4 px-1 text-label text-ink-muted">
            No restaurant found near you for that name. Try another spelling.
          </p>
        )
      ) : null}
    </>
  );
}
