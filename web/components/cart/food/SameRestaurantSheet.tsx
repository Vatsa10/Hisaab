"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet } from "@/components/ui/Sheet";
import { cn } from "@/lib/cn";
import { SITE_DOTS, siteName, type FoodSite } from "@/lib/cart";
import { FOOD_SITES, type RestaurantPair } from "@/lib/food";
import { distanceLabel } from "./RestaurantPairCard";

export type SameAnswer = { same: true } | { same: false; keep: FoodSite };

export interface SameRestaurantSheetProps {
  /** The "ask" pair to settle; null closes the sheet. */
  pair: RestaurantPair | null;
  onClose: () => void;
  onAnswer: (pair: RestaurantPair, answer: SameAnswer) => void;
}

function Body({ pair, onAnswer }: { pair: RestaurantPair; onAnswer: SameRestaurantSheetProps["onAnswer"] }) {
  const sites = FOOD_SITES.filter((s) => pair.listings[s]);
  const [keep, setKeep] = useState<FoodSite>(sites[0]!);
  return (
    <>
      <ul className="grid grid-cols-2 gap-2">
        {sites.map((s) => {
          const l = pair.listings[s]!;
          const where = [l.locality, distanceLabel(l.distance_km)].filter(Boolean).join(" · ");
          const selected = keep === s;
          return (
            <li key={s} className="min-w-0">
              <button
                type="button"
                aria-pressed={selected}
                aria-label={`${siteName(s)} listing`}
                onClick={() => setKeep(s)}
                className={cn(
                  "flex h-full w-full flex-col gap-1 rounded-card p-3 text-left ring-1 ring-inset transition-[box-shadow,background-color] duration-150",
                  selected ? "bg-accent-soft ring-2 ring-accent" : "bg-surface ring-line active:bg-canvas",
                )}
              >
                <span className="flex items-center gap-1.5 text-label font-semibold text-ink-muted">
                  <span aria-hidden="true" className={cn("size-2 rounded-full", SITE_DOTS[s])} />
                  {siteName(s)}
                </span>
                <span className="break-words font-medium">{l.name}</span>
                {where ? <span className="text-label text-ink-muted">{where}</span> : null}
                <span className={cn("text-label font-semibold", l.is_open === false ? "text-danger" : "text-accent")}>
                  {l.is_open === false ? "Closed" : l.is_open ? "Open" : "Hours unknown"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      <p className="mt-2 px-1 text-label text-ink-muted">
        If they are different, tap the app whose listing you want to compare.
      </p>
      <div className="mt-4 flex flex-col gap-2">
        <Button size="lg" fullWidth onClick={() => onAnswer(pair, { same: true })}>
          Yes, same restaurant
        </Button>
        <Button variant="secondary" size="lg" fullWidth onClick={() => onAnswer(pair, { same: false, keep })}>
          No, different
        </Button>
      </div>
    </>
  );
}

/** Both listings side by side, for a pair the server was not sure about. */
export function SameRestaurantSheet({ pair, onClose, onAnswer }: SameRestaurantSheetProps) {
  return (
    <Sheet open={pair != null} onClose={onClose} title="Same restaurant?" description="The two apps list it a little differently.">
      {pair ? <Body pair={pair} onAnswer={onAnswer} /> : null}
    </Sheet>
  );
}
