import { cn } from "@/lib/cn";
import { SITE_DOTS, siteName, type FoodSite } from "@/lib/cart";
import { FOOD_SITES, type RestaurantPair } from "@/lib/food";

export function distanceLabel(km: number | null | undefined): string | null {
  return typeof km === "number" && Number.isFinite(km) ? `${Number(km.toFixed(1))} km` : null;
}

export function AppDots({ sites }: { sites: readonly FoodSite[] }) {
  return (
    <span className="flex shrink-0 items-center gap-1" aria-label={`On ${sites.map(siteName).join(" and ")}`} role="img">
      {sites.map((s) => (
        <span key={s} aria-hidden="true" className={cn("size-2.5 rounded-full", SITE_DOTS[s])} />
      ))}
    </span>
  );
}

export interface RestaurantPairCardProps {
  pair: RestaurantPair;
  onSelect: () => void;
}

/** One search result: the restaurant, where it is, which apps list it, and what to know before comparing. */
export function RestaurantPairCard({ pair, onSelect }: RestaurantPairCardProps) {
  const present = FOOD_SITES.filter((s) => pair.listings[s]);
  const where = [pair.locality, distanceLabel(pair.distance_km)].filter(Boolean).join(" · ");
  return (
    <li className="border-b border-line-soft last:border-b-0">
      <button
        type="button"
        onClick={onSelect}
        className="flex min-h-16 w-full items-center gap-3 px-4 py-3 text-left transition-colors duration-150 active:bg-canvas"
      >
        <span className="min-w-0 flex-1">
          <span className="block break-words font-medium">{pair.name}</span>
          {where ? <span className="block text-label text-ink-muted">{where}</span> : null}
          {pair.note ? (
            <span className={cn("mt-0.5 block text-label", pair.confidence === "ask" ? "font-semibold text-up" : "text-ink-muted")}>
              {pair.note}
            </span>
          ) : null}
        </span>
        <AppDots sites={present} />
        <span aria-hidden="true" className="shrink-0 font-semibold text-accent">
          ›
        </span>
      </button>
    </li>
  );
}
