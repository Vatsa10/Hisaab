// The food client: restaurant search, menus and the compare start (server/food_api.py, routes under
// /cart/food). Prices, pairing and every summary string are the server's; the PWA only renders them.
// Nothing here logs restaurant names, dishes or addresses.

import { apiFetch } from "@/lib/api";
import { isTerminal, watchJob, type Job } from "@/lib/jobs";
import type { AccessMap, CartItem, CheckStarted, FoodSite } from "@/lib/cart";

export const FOOD_SITES: readonly FoodSite[] = ["swiggy", "zomato"];

/** FOOD_MAX_ITEMS on the server. */
export const FOOD_MAX_ITEMS = 10;
/** Shown while a restaurant search runs. */
export const SEARCHING_LINE = "Searching Swiggy and Zomato…";
export const NO_FOOD_APP = "Connect Swiggy or Zomato to compare.";
export const MIN_QUERY = 2;
export const DISH_TEXT_MAX = 80;
export const VARIANT_MAX = 60;

export interface UsualRestaurant {
  name: string;
  sites: FoodSite[];
  orders: number;
  last_order_at?: string | null;
}

/** One app's listing of a restaurant (restaurant_match.Listing). */
export interface Listing {
  site: FoodSite;
  restaurant_id: string;
  name: string;
  locality: string | null;
  distance_km: number | null;
  is_open: boolean | null;
}

export type PairConfidence = "sure" | "ask" | "single";

export interface RestaurantPair {
  key: string;
  name: string;
  locality: string | null;
  distance_km: number | null;
  listings: Partial<Record<FoodSite, Listing | null>>;
  confidence: PairConfidence;
  /** "Check this is the same restaurant", "Only on Swiggy", "Connect Zomato to compare", or null. */
  note: string | null;
}

export type Searching = { status: "searching"; job_ids: string[] };
export type RestaurantsResult = { status: "ready"; results: RestaurantPair[] } | Searching;

export interface MenuDish {
  dish_id: string;
  name: string;
  variant: string | null;
  veg: boolean | null;
  price: number;
  price_label: string;
  in_stock: boolean;
}

export interface UsualDish {
  text: string;
  qty: number;
  times: number;
}

export interface MenuListing {
  restaurant_id: string;
  name: string;
  locality: string | null;
  distance_km: number | null;
  is_open?: boolean | null;
}

export interface MenuRestaurant {
  name: string;
  locality: string | null;
  listings: Partial<Record<FoodSite, MenuListing>>;
}

export type MenusResult =
  | { status: "ready"; restaurant: MenuRestaurant; menu: MenuDish[]; usual: UsualDish[] }
  | Searching;

/** The restaurant the user settled on, as the checks body wants it. */
export interface FoodRestaurant {
  name: string;
  swiggy_id?: string;
  zomato_id?: string;
  confirmed: boolean;
  declined: FoodSite[];
}

export interface FoodCheckBody {
  restaurant: FoodRestaurant;
  items: CartItem[];
}

export async function getUsualRestaurants(): Promise<UsualRestaurant[]> {
  const res = await apiFetch<{ restaurants?: UsualRestaurant[] }>("/cart/food/usual");
  return Array.isArray(res?.restaurants) ? res.restaurants.slice(0, 10) : [];
}

/** GET /cart/food/restaurants. 200 is ready; 202 means lookup jobs were queued (wait, then ask again). */
export async function searchRestaurants(q: string): Promise<RestaurantsResult> {
  const res = await apiFetch<{ status?: string; results?: RestaurantPair[]; job_ids?: string[] }>(
    `/cart/food/restaurants?q=${encodeURIComponent(q.trim())}`,
  );
  if (res?.status === "searching") return { status: "searching", job_ids: res.job_ids ?? [] };
  return { status: "ready", results: Array.isArray(res?.results) ? res.results : [] };
}

/** POST /cart/food/menus with the ids the user's own search returned. */
export async function loadMenus(restaurant: Pick<FoodRestaurant, "name" | "swiggy_id" | "zomato_id">): Promise<MenusResult> {
  const res = await apiFetch<{
    status?: string;
    job_ids?: string[];
    restaurant?: MenuRestaurant;
    menu?: MenuDish[];
    usual_dishes?: UsualDish[];
  }>("/cart/food/menus", {
    method: "POST",
    body: JSON.stringify({
      name: restaurant.name,
      ...(restaurant.swiggy_id ? { swiggy_id: restaurant.swiggy_id } : {}),
      ...(restaurant.zomato_id ? { zomato_id: restaurant.zomato_id } : {}),
    }),
  });
  if (res?.status === "searching") return { status: "searching", job_ids: res.job_ids ?? [] };
  return {
    status: "ready",
    restaurant: res?.restaurant ?? { name: restaurant.name, locality: null, listings: {} },
    menu: Array.isArray(res?.menu) ? res.menu : [],
    usual: Array.isArray(res?.usual_dishes) ? res.usual_dishes : [],
  };
}

/** POST /cart/food/checks → 202 {request_id, sites, cart_checks_left}. */
export function startFoodCheck(body: FoodCheckBody): Promise<CheckStarted> {
  const { restaurant, items } = body;
  return apiFetch<CheckStarted>("/cart/food/checks", {
    method: "POST",
    body: JSON.stringify({
      restaurant: {
        name: restaurant.name,
        ...(restaurant.swiggy_id ? { swiggy_id: restaurant.swiggy_id } : {}),
        ...(restaurant.zomato_id ? { zomato_id: restaurant.zomato_id } : {}),
        confirmed: restaurant.confirmed,
        declined: restaurant.declined,
      },
      items: items.map((i) => ({
        // The server caps a dish at 80 characters and a variant at 60 (the controller raises its 20 at merge).
        text: i.text.slice(0, DISH_TEXT_MAX),
        qty: i.qty,
        ...(i.variant && i.variant.length <= VARIANT_MAX ? { variant: i.variant } : {}),
        ...(typeof i.veg === "boolean" ? { veg: i.veg } : {}),
      })),
    }),
  });
}

/** Resolves once every job is done or failed (live over Realtime); `signal` stops watching early. */
export function waitForJobs(jobIds: readonly string[], signal?: AbortSignal): Promise<Job[]> {
  const ids = [...new Set(jobIds)];
  if (!ids.length) return Promise.resolve([]);
  return new Promise((resolve) => {
    const last = new Map<string, Job>();
    const stops: Array<() => void> = [];
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      stops.forEach((s) => s());
      resolve([...last.values()]);
    };
    signal?.addEventListener("abort", finish, { once: true });
    for (const id of ids) {
      stops.push(
        watchJob(id, (job) => {
          if (!isTerminal(job)) return;
          last.set(id, job);
          if (last.size === ids.length) finish();
        }),
      );
    }
    if (signal?.aborted) finish();
  });
}

/** True when neither food app can be used (not connected / logged out). Unavailable-now is the server's call. */
export function noFoodApp(access: AccessMap | null): boolean {
  if (!access) return false;
  return !FOOD_SITES.some((s) => access[s] === "ok" || access[s] === "unavailable");
}
