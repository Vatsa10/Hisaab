"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ApiError } from "@/lib/api";
import {
  checksLeftLine,
  getAccess,
  getCartChecksLeft,
  getRequest,
  type AccessMap,
  type CartItem,
  type FoodSite,
} from "@/lib/cart";
import {
  FOOD_MAX_ITEMS,
  FOOD_SITES,
  NO_FOOD_APP,
  getUsualRestaurants,
  loadMenus,
  noFoodApp,
  searchRestaurants,
  startFoodCheck,
  waitForJobs,
  type FoodRestaurant,
  type MenuDish,
  type MenusResult,
  type RestaurantPair,
  type RestaurantsResult,
  type UsualDish,
  type UsualRestaurant,
} from "@/lib/food";
import { AppAccessRow } from "../AppAccessRow";
import { MenuPicker, closedLines, menuKey, usualKey } from "./MenuPicker";
import { RestaurantSearch } from "./RestaurantSearch";
import { SameRestaurantSheet, type SameAnswer } from "./SameRestaurantSheet";
import { distanceLabel } from "./RestaurantPairCard";
import { Spinner } from "@/components/connect/icons";

export interface FoodFlowProps {
  /** `?from=<request id>`: start from that check's restaurant and dishes ("Edit"). */
  fromId?: string | null;
}

type Picked = { key: string; item: CartItem };
type Ready = Extract<MenusResult, { status: "ready" }>;

function problem(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.detail) return e.detail;
  return fallback;
}

/** A restaurant as stored on a past check (jsonb); anything unreadable gives null. */
function restoredRestaurant(raw: unknown): FoodRestaurant | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const listings = (r.listings && typeof r.listings === "object" ? r.listings : {}) as Record<string, { restaurant_id?: unknown } | undefined>;
  const id = (site: FoodSite): string | undefined => {
    const direct = r[`${site}_id`];
    const v = typeof direct === "string" ? direct : listings[site]?.restaurant_id;
    return typeof v === "string" && v ? v : undefined;
  };
  const name = typeof r.name === "string" ? r.name.trim() : "";
  const swiggy_id = id("swiggy");
  const zomato_id = id("zomato");
  if (!name || (!swiggy_id && !zomato_id)) return null;
  const declined = Array.isArray(r.declined) ? r.declined.filter((s): s is FoodSite => s === "swiggy" || s === "zomato") : [];
  return { name, swiggy_id, zomato_id, confirmed: r.confirmed === true, declined };
}

/**
 * The /cart screen in Food mode: pick the restaurant (paired across Swiggy and Zomato), pick the
 * dishes from its menu, compare. A check starts on the server (202); its result lives at /cart/{id}.
 */
export function FoodFlow({ fromId }: FoodFlowProps) {
  const router = useRouter();

  const [access, setAccess] = useState<AccessMap | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [checksLeft, setChecksLeft] = useState<number | null>(null);
  const [usual, setUsual] = useState<UsualRestaurant[]>([]);

  // Step 1
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [results, setResults] = useState<RestaurantPair[] | null>(null);
  const [asking, setAsking] = useState<RestaurantPair | null>(null);

  // Step 2
  const [restaurant, setRestaurant] = useState<FoodRestaurant | null>(null);
  const [open, setOpen] = useState<Partial<Record<FoodSite, boolean | null>>>({});
  const [menu, setMenu] = useState<Ready | null>(null);
  const [menuError, setMenuError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Picked[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only the newest search / menu load may deliver.
  const token = useRef(0);
  useEffect(() => {
    const t = token;
    return () => {
      t.current += 1;
    };
  }, []);

  useEffect(() => {
    let live = true;
    getAccess("food").then(
      (a) => live && setAccess(a),
      () => live && setAccessFailed(true),
    );
    getCartChecksLeft().then(
      (n) => live && setChecksLeft(n),
      () => {},
    );
    getUsualRestaurants().then(
      (r) => live && setUsual(r),
      () => {},
    );
    return () => {
      live = false;
    };
  }, []);

  const blocked = noFoodApp(access);

  // A lookup the server queued (202): wait for its jobs, then ask again; the answer is cached by then.
  const settle = useCallback(async <T extends { status: string }>(ask: () => Promise<T | { status: "searching"; job_ids: string[] }>) => {
    let res = await ask();
    if (res.status === "searching") {
      await waitForJobs((res as { job_ids: string[] }).job_ids);
      res = await ask();
    }
    return res as T | { status: "searching"; job_ids: string[] };
  }, []);

  const openMenu = useCallback(
    async (r: FoodRestaurant, openNow: Partial<Record<FoodSite, boolean | null>>, initial: Picked[]) => {
      const mine = ++token.current;
      setRestaurant(r);
      setOpen(openNow);
      setMenu(null);
      setMenuError(null);
      setError(null);
      setPicked(initial);
      setNote(null);
      try {
        const res = await settle<Ready>(() => loadMenus(r));
        if (mine !== token.current) return;
        if (res.status === "searching") {
          setMenuError("The menu is taking longer than usual. Try again in a moment.");
          return;
        }
        setMenu(res);
        // Dishes restored from a past check are matched to today's menu; ones no longer on it are dropped.
        const remap = (cur: Picked[]): Picked[] => {
          const next: Picked[] = [];
          for (const p of cur) {
            if (!p.key.startsWith("r:")) {
              next.push(p);
              continue;
            }
            const d = res.menu.find(
              (m) =>
                m.in_stock &&
                m.name.trim().toLowerCase() === p.item.text.trim().toLowerCase() &&
                (m.variant ?? "") === (p.item.variant ?? ""),
            );
            if (d && !next.some((x) => x.key === menuKey(d))) next.push({ key: menuKey(d), item: { ...p.item, text: d.name } });
          }
          return next;
        };
        const next = remap(initial);
        setPicked(next);
        if (next.length < initial.length) setNote("Some dishes from that check are no longer on the menu.");
      } catch (e) {
        if (mine === token.current) setMenuError(problem(e, "Couldn't load the menu. Try again."));
      }
    },
    [settle],
  );

  // "Edit" from a result: restore the restaurant and dishes, unless the user already picked a restaurant.
  useEffect(() => {
    if (!fromId) return;
    let live = true;
    getRequest(fromId).then(
      (req) => {
        if (!live || !req || req.mode !== "food") return;
        const r = restoredRestaurant(req.restaurant);
        if (!r) return;
        const restored = req.items
          .slice(0, FOOD_MAX_ITEMS)
          .map((item) => ({ key: `r:${item.text.toLowerCase()}|${item.variant ?? ""}`, item }));
        void openMenu(r, {}, restored);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [fromId, openMenu]);

  async function search(q: string) {
    const text = q.trim();
    if (!text || searching || blocked) return;
    const mine = ++token.current;
    setQuery(text);
    setSearching(true);
    setSearchError(null);
    setResults(null);
    try {
      const res = await settle<Extract<RestaurantsResult, { status: "ready" }>>(() => searchRestaurants(text));
      if (mine !== token.current) return;
      if (res.status === "searching") setSearchError("Still looking. Try again in a moment.");
      else setResults(res.results);
    } catch (e) {
      if (mine === token.current) setSearchError(problem(e, "Couldn't search right now. Try again."));
    } finally {
      if (mine === token.current) setSearching(false);
    }
  }

  function choose(pair: RestaurantPair, keep: readonly FoodSite[], confirmed: boolean) {
    const kept = FOOD_SITES.filter((s) => keep.includes(s) && pair.listings[s]);
    const first = pair.listings[kept[0]!];
    const r: FoodRestaurant = {
      name: kept.length === 1 && first ? first.name : pair.name,
      confirmed,
      declined: FOOD_SITES.filter((s) => pair.listings[s] && !kept.includes(s)),
    };
    if (kept.includes("swiggy")) r.swiggy_id = pair.listings.swiggy!.restaurant_id;
    if (kept.includes("zomato")) r.zomato_id = pair.listings.zomato!.restaurant_id;
    const openNow: Partial<Record<FoodSite, boolean | null>> = {};
    for (const s of kept) openNow[s] = pair.listings[s]!.is_open;
    void openMenu(r, openNow, []);
  }

  function pick(pair: RestaurantPair) {
    if (pair.confidence === "ask" && FOOD_SITES.every((s) => pair.listings[s])) setAsking(pair);
    else choose(pair, FOOD_SITES, false);
  }

  function answer(pair: RestaurantPair, a: SameAnswer) {
    setAsking(null);
    choose(pair, a.same ? FOOD_SITES : [a.keep], a.same);
  }

  function backToSearch() {
    token.current += 1;
    setRestaurant(null);
    setMenu(null);
    setMenuError(null);
    setError(null);
    setPicked([]);
  }

  // ---- dishes ------------------------------------------------------------------------------

  const setPick = (key: string, item: CartItem, qty: number) => {
    if (error) setError(null);
    setPicked((cur) => {
      if (qty <= 0) return cur.filter((p) => p.key !== key);
      if (cur.some((p) => p.key === key)) return cur.map((p) => (p.key === key ? { key, item: { ...p.item, qty } } : p));
      return cur.length >= FOOD_MAX_ITEMS ? cur : [...cur, { key, item: { ...item, qty } }];
    });
  };
  const qtyOf = (key: string) => picked.find((p) => p.key === key)?.item.qty ?? 0;
  const onMenuQty = (d: MenuDish, qty: number) =>
    setPick(menuKey(d), { text: d.name, qty, ...(d.variant ? { variant: d.variant } : {}), ...(typeof d.veg === "boolean" ? { veg: d.veg } : {}) }, qty);
  const onUsualQty = (u: UsualDish, qty: number) => setPick(usualKey(u), { text: u.text, qty }, qty);

  const n = picked.length;
  const outOfChecks = checksLeft === 0;
  const canCompare = restaurant != null && menu != null && n > 0 && !outOfChecks && !blocked;

  async function compare() {
    if (!canCompare || busy || !restaurant) return;
    setBusy(true);
    setError(null);
    try {
      const res = await startFoodCheck({ restaurant, items: picked.map((p) => p.item) });
      setChecksLeft(res.cart_checks_left);
      router.push(`/cart/${encodeURIComponent(res.request_id)}`);
    } catch (e) {
      setError(problem(e, "Couldn't start the check. Try again."));
      setBusy(false);
    }
  }

  const closed = FOOD_SITES.filter(
    (s) => (menu?.restaurant.listings[s]?.is_open ?? open[s]) === false && (menu ? menu.restaurant.listings[s] != null || open[s] === false : true),
  );
  const listed = menu ? menu.restaurant : null;
  const where = listed
    ? [listed.locality, distanceLabel(Object.values(listed.listings)[0]?.distance_km)].filter(Boolean).join(" · ")
    : "";

  const why = blocked
    ? NO_FOOD_APP
    : outOfChecks
      ? "You've used today's checks. Try again tomorrow."
      : n === 0
        ? "Add a dish to compare."
        : null;

  return (
    <>
      <PageHeader title="Cart" subtitle="Compare a restaurant order on Swiggy and Zomato." />

      <Card as="section" aria-label="Which apps" padding="none">
        <AppAccessRow access={access} failed={accessFailed} sites={FOOD_SITES} />
      </Card>
      {blocked ? (
        <p className="mt-3 px-1 text-body font-medium text-ink">{NO_FOOD_APP}</p>
      ) : null}

      {!restaurant ? (
        <RestaurantSearch
          disabled={blocked}
          usual={usual}
          query={query}
          onQuery={setQuery}
          searching={searching}
          error={searchError}
          results={results}
          onSearch={(q) => void search(q)}
          onPick={pick}
        />
      ) : (
        <>
          <section aria-label="Restaurant" className="mt-6">
            <div className="flex items-start gap-3 pl-1">
              <div className="min-w-0 flex-1">
                <h2 className="break-words text-headline">{restaurant.name}</h2>
                {where ? <p className="text-label text-ink-muted">{where}</p> : null}
                {closedLines(closed).map((line) => (
                  <p key={line} className="mt-0.5 text-label font-semibold text-up">
                    {line}
                  </p>
                ))}
              </div>
              <Button variant="ghost" className="-mr-2 shrink-0 px-3" onClick={backToSearch} aria-label="Change restaurant">
                Change
              </Button>
            </div>
          </section>

          {note ? <p className="mt-3 px-1 text-label text-ink-muted">{note}</p> : null}

          {menuError ? (
            <div className="mt-4 px-1">
              <p role="alert" className="text-label text-danger">
                {menuError}
              </p>
              <Button className="mt-3" variant="secondary" onClick={() => void openMenu(restaurant, open, picked)}>
                Try again
              </Button>
            </div>
          ) : !menu ? (
            <p role="status" className="mt-4 flex items-center gap-2 px-1 text-label text-ink-muted">
              <Spinner className="size-4" />
              Loading the menu…
            </p>
          ) : (
            <MenuPicker
              menu={menu.menu}
              usual={menu.usual}
              qtyOf={qtyOf}
              onMenuQty={onMenuQty}
              onUsualQty={onUsualQty}
              full={n >= FOOD_MAX_ITEMS}
            />
          )}

          {/* Pinned just above the tab bar, so Compare stays in reach however long the menu gets. */}
          <div className="sticky bottom-[calc(theme(spacing.tabbar)+env(safe-area-inset-bottom))] z-10 -mx-4 mt-3 bg-canvas px-4 pb-3 pt-3">
            <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-canvas to-canvas/0" />
            <Button size="lg" fullWidth disabled={!canCompare} loading={busy} onClick={compare}>
              {`Compare ${n} ${n === 1 ? "dish" : "dishes"}`}
            </Button>
            {error ? (
              <p role="alert" className="mt-3 text-center text-label text-danger">
                {error}
              </p>
            ) : null}
            {!error && why && !busy && menu ? <p className="mt-2 text-center text-label text-ink">{why}</p> : null}
            {checksLeft != null ? (
              <p className="mt-1 text-center text-label text-ink-muted">{checksLeftLine(checksLeft)}</p>
            ) : null}
          </div>
        </>
      )}

      <SameRestaurantSheet pair={asking} onClose={() => setAsking(null)} onAnswer={answer} />
    </>
  );
}
