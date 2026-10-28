// The cart client: the grocery list, the cart API (server/cart_api.py) and live cart checks over
// Supabase Realtime. Prices and every summary string are computed on the server
// (server/cart_rank.build_summary); the PWA only renders them and never does price math.
// Nothing here logs list items, areas or addresses.

import type { RealtimeChannel, SupabaseClient } from "@supabase/supabase-js";
import { apiFetch } from "@/lib/api";
import { relativeTime } from "@/components/connect/copy";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

// ---- sites --------------------------------------------------------------------------------

export type CartMode = "grocery" | "food";
export type GrocerySite = "zepto" | "blinkit" | "instamart";
export type FoodSite = "swiggy" | "zomato";
export type CartSite = GrocerySite | FoodSite;

/** The grocery apps a check prices, in the server's site order (search.SEARCH_SITES). */
export const GROCERY_SITES: readonly GrocerySite[] = ["zepto", "blinkit", "instamart"];

export const SITE_NAMES: Record<CartSite, string> = {
  zepto: "Zepto",
  blinkit: "Blinkit",
  instamart: "Instamart",
  swiggy: "Swiggy",
  zomato: "Zomato",
};

/** Tailwind background class for each app's colour dot. */
export const SITE_DOTS: Record<CartSite, string> = {
  zepto: "bg-app-zepto",
  blinkit: "bg-app-blinkit",
  instamart: "bg-app-instamart",
  swiggy: "bg-app-swiggy",
  zomato: "bg-app-zomato",
};

export function siteName(site: string): string {
  return SITE_NAMES[site as CartSite] ?? site.charAt(0).toUpperCase() + site.slice(1);
}

// ---- list items ---------------------------------------------------------------------------

export interface CartItem {
  text: string;
  qty: number;
  /** Food only: half / full / regular … */
  variant?: string;
  /** Food only. */
  veg?: boolean;
}

/** CART_MAX_ITEMS on the server. */
export const CART_MAX_ITEMS = 30;
export const QTY_MIN = 1;
export const QTY_MAX = 20;
/** The API accepts item texts of 1..80 characters. */
export const ITEM_TEXT_MAX = 80;

function clampQty(n: number): number {
  if (!Number.isFinite(n)) return QTY_MIN;
  return Math.min(QTY_MAX, Math.max(QTY_MIN, Math.round(n)));
}

function tidy(s: string): string {
  return s.replace(/\s+/g, " ").trim();
}

// "Milk ×2", "Milk x 2", "Milk * 2", "Milk×2" (a letter x needs a space before it, so "Box"
// and "1Lx" stay part of the name).
const TRAILING_QTY = /^(.*?)(?:\s*[×*]|\s+[xX])\s*(\d{1,3})$/;
// "2 x Bread", "2x Bread", "2 × Bread", "2* Bread".
const LEADING_QTY = /^(\d{1,3})\s*[×xX*]\s+(.+)$/;

/** One typed line as a list item. Quantity markers are taken off the name; the default is 1. */
export function parseLine(s: string): CartItem {
  const line = tidy(s);
  const trailing = TRAILING_QTY.exec(line);
  if (trailing && tidy(trailing[1] ?? "")) {
    return { text: tidy(trailing[1] ?? ""), qty: clampQty(Number(trailing[2])) };
  }
  const leading = LEADING_QTY.exec(line);
  if (leading && tidy(leading[2] ?? "")) {
    return { text: tidy(leading[2] ?? ""), qty: clampQty(Number(leading[1])) };
  }
  return { text: line, qty: 1 };
}

function sameItem(a: CartItem, b: CartItem): boolean {
  return a.text.toLowerCase() === b.text.toLowerCase() && a.variant === b.variant && a.veg === b.veg;
}

/**
 * Add an item to the list: a line already there gets the quantity added (capped at 20), a new
 * one goes at the end. A full list (30 items) comes back unchanged (the same array).
 */
export function addItem(list: readonly CartItem[], item: CartItem): CartItem[] {
  const text = tidy(item.text).slice(0, ITEM_TEXT_MAX);
  if (!text) return list as CartItem[];
  const next = { ...item, text, qty: clampQty(item.qty) };
  const at = list.findIndex((it) => sameItem(it, next));
  if (at >= 0) {
    return list.map((it, i) => (i === at ? { ...it, qty: clampQty(it.qty + next.qty) } : it));
  }
  if (list.length >= CART_MAX_ITEMS) return list as CartItem[];
  return [...list, next];
}

export function setQty(list: readonly CartItem[], index: number, qty: number): CartItem[] {
  return list.map((it, i) => (i === index ? { ...it, qty: clampQty(qty) } : it));
}

export function removeAt(list: readonly CartItem[], index: number): CartItem[] {
  return list.filter((_, i) => i !== index);
}

/** Items stored on a request (jsonb) back into list items; anything malformed is dropped. */
export function itemsFrom(raw: unknown): CartItem[] {
  if (!Array.isArray(raw)) return [];
  let list: CartItem[] = [];
  for (const r of raw) {
    if (!r || typeof r !== "object") continue;
    const { text, qty, variant, veg } = r as Record<string, unknown>;
    if (typeof text !== "string") continue;
    const item: CartItem = { text, qty: typeof qty === "number" ? qty : 1 };
    if (typeof variant === "string") item.variant = variant;
    if (typeof veg === "boolean") item.veg = veg;
    list = addItem(list, item);
  }
  return list;
}

// ---- copy ---------------------------------------------------------------------------------

const LATLON = /^\s*-?\d{1,3}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?\s*$/;
const PINCODE = /\b\d{3}\s?\d{3}\b/g;

/**
 * A short area for "Delivering to …", the same rule as server/cart_rank.area_label: drop a
 * trailing "India" and pincodes; 3+ parts give the 3rd- and 2nd-last ("Gorwa, Vadodara"), fewer
 * give the first. Never the street.
 */
export function areaLabel(address: string | null | undefined): string {
  if (typeof address !== "string" || !address.trim() || LATLON.test(address)) return "your address";
  const parts = address
    .split(",")
    .map((p) => tidy(p.replace(PINCODE, " ")).replace(/^[\s-]+|[\s-]+$/g, ""))
    .filter(Boolean);
  while (parts.length && parts[parts.length - 1]!.toLowerCase() === "india") parts.pop();
  if (!parts.length) return "your address";
  if (parts.length >= 3) return `${parts[parts.length - 3]}, ${parts[parts.length - 2]}`;
  return parts[0]!;
}

export function checksLeftLine(n: number): string {
  return `${n} ${n === 1 ? "check" : "checks"} left today`;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

// ---- the summary shape (server/cart_rank.build_summary) ------------------------------------

export type CardStatus =
  | "ok"
  | "failed"
  | "unserviceable"
  | "unavailable"
  | "not_connected"
  | "needs_login"
  | "closed"
  | "not_listed";

/** Fee components (delivery, handling, small_cart, surge; food: packaging, platform, small_order, gst) plus the total. */
export type CartFees = Record<string, number> & { total: number };

export interface CartAction {
  label: "Connect" | "Log in again";
  href: string;
}

export interface CartCard {
  site: CartSite;
  name: string;
  rank: number;
  status: CardStatus;
  cheapest: boolean;
  subtotal: number | null;
  fees: CartFees | null;
  fees_estimated: boolean;
  total: number | null;
  /** "₹412" or "—". */
  total_label: string;
  /** "+₹37", "Same total" or null. */
  delta_label: string | null;
  /** "Items ₹397 + delivery ₹0 + handling ₹15" … */
  fee_line: string | null;
  /** "Zepto Pass applied" */
  member_note: string | null;
  /** "Free delivery above ₹199" */
  fee_hint: string | null;
  /** "all 7 in stock" | "1 out of stock · 1 to pick" | "all 3 available" … */
  stock_line: string | null;
  swaps: string[];
  to_pick: number;
  note: string | null;
  action: CartAction | null;
}

export interface CartCell {
  /** matched | swapped | chosen | out_of_stock | not_found | pick … */
  status: string;
  name: string | null;
  /** "₹136" | "n/a" | "Pick" | "Out of stock" | "Not available" */
  price_label: string;
  cheapest: boolean;
}

export interface CartTableRow {
  idx: number;
  label: string;
  cells: Partial<Record<CartSite, CartCell>>;
}

export interface CartTable {
  /** The ok sites, in card order. */
  columns: CartSite[];
  rows: CartTableRow[];
  totals: Partial<Record<CartSite, string>>;
}

export interface CartCandidate {
  product_id: string;
  name: string;
  size_label: string | null;
  price_label: string;
  packs: number;
}

export interface CartPick {
  idx: number;
  site: CartSite;
  label: string;
  candidates: CartCandidate[];
}

export interface CartSplit {
  worth_it: boolean;
  sites: CartSite[];
  saving: number;
  line: string;
}

export interface CartRestaurant {
  name: string;
  locality: string | null;
}

export interface CartSummary {
  request_id: string;
  mode: CartMode;
  status: "done" | "failed";
  /** "Cheapest cart" or the restaurant's name. */
  title: string;
  /** "7 items" | "3 dishes" */
  count_label: string;
  area: string;
  /** ISO; for food the oldest menu fetch used. */
  priced_at: string | null;
  price_note: string;
  /** Food only: "Offers in the app may lower this." */
  offers_note: string | null;
  restaurant: CartRestaurant | null;
  best_site: CartSite | null;
  cards: CartCard[];
  table: CartTable;
  picks: CartPick[];
  split: CartSplit | null;
}

// ---- the API ------------------------------------------------------------------------------

export type AccessStatus = "ok" | "not_connected" | "needs_login" | "unavailable";
export type AccessMap = Partial<Record<CartSite, AccessStatus>>;

export interface UsualItem {
  text: string;
  qty: number;
  /** How many times it was ordered in the last 90 days. */
  times: number;
}

export interface CheckStarted {
  request_id: string;
  sites: CartSite[];
  cart_checks_left: number;
}

/** Items bought 3+ times in the last 90 days (GET /cart/usual). */
export async function getUsual(): Promise<UsualItem[]> {
  const res = await apiFetch<{ items?: UsualItem[] }>("/cart/usual");
  return Array.isArray(res?.items) ? res.items : [];
}

/** Which apps a check in this mode would price (GET /cart/access). */
export async function getAccess(mode: CartMode): Promise<AccessMap> {
  const res = await apiFetch<{ access?: AccessMap }>(`/cart/access?mode=${mode}`);
  return res?.access ?? {};
}

/**
 * Start a grocery check (POST /cart/checks → 202). Throws ApiError whose `detail` is meant to be
 * shown as is (409 no address / no app / already running, 429 daily limit, 503 paused).
 */
export function startCheck(items: readonly CartItem[]): Promise<CheckStarted> {
  return apiFetch<CheckStarted>("/cart/checks", {
    method: "POST",
    body: JSON.stringify({ items: items.map(({ text, qty }) => ({ text, qty })) }),
  });
}

/** Answer a "Pick one": the server recomputes the summary without using a check. */
export async function setChoice(requestId: string, idx: number, site: string, id: string): Promise<CartSummary> {
  const res = await apiFetch<{ summary: CartSummary }>(`/cart/checks/${encodeURIComponent(requestId)}/choices`, {
    method: "POST",
    body: JSON.stringify({ idx, site, product_id: id }),
  });
  return res.summary;
}

/**
 * Cart checks left today, from Plan 4's GET /me/limits (the same read as `getLimits()` in
 * lib/me.ts, which this screen will share once that lands).
 */
export async function getCartChecksLeft(): Promise<number | null> {
  const res = await apiFetch<{ cart_checks_left?: unknown }>("/me/limits");
  return typeof res?.cart_checks_left === "number" ? res.cart_checks_left : null;
}

// ---- reads under RLS ----------------------------------------------------------------------

export type RequestStatus = "searching" | "done" | "failed";

export interface CartRequest {
  id: string;
  mode: CartMode;
  status: RequestStatus;
  items: CartItem[];
  restaurant?: unknown;
  sites?: CartSite[];
  area?: string;
  choices?: Record<string, Record<string, string>>;
  summary: CartSummary | null;
  error?: string | null;
  created_at: string;
  priced_at?: string | null;
}

export interface CartResult {
  request_id: string;
  site: CartSite;
  status: CardStatus;
  error: string | null;
  priced_at: string | null;
}

/** Never lat/lon: the screen has no use for them. */
export const REQUEST_COLUMNS =
  "id, mode, status, items, restaurant, sites, area, choices, summary, error, created_at, priced_at";
export const RESULT_COLUMNS = "request_id, site, status, error, priced_at";
const RECENT_COLUMNS = "id, mode, status, items, summary, created_at";

function toRequest(row: Record<string, unknown>): CartRequest {
  return {
    ...(row as unknown as CartRequest),
    id: String(row.id),
    mode: row.mode === "food" ? "food" : "grocery",
    status: (String(row.status ?? "searching") as RequestStatus),
    items: itemsFrom(row.items),
    summary: row.summary && typeof row.summary === "object" ? (row.summary as CartSummary) : null,
    created_at: String(row.created_at ?? ""),
  };
}

function toResult(row: Record<string, unknown>): CartResult {
  return {
    request_id: String(row.request_id ?? ""),
    site: String(row.site ?? "") as CartSite,
    status: String(row.status ?? "") as CardStatus,
    error: typeof row.error === "string" ? row.error : null,
    priced_at: typeof row.priced_at === "string" ? row.priced_at : null,
  };
}

export async function getRequest(
  id: string,
  client: SupabaseClient = getSupabaseBrowserClient(),
): Promise<CartRequest | null> {
  const { data, error } = await client.from("cart_requests").select(REQUEST_COLUMNS).eq("id", id).maybeSingle();
  if (error || !data) return null;
  return toRequest(data as Record<string, unknown>);
}

export async function getResults(
  id: string,
  client: SupabaseClient = getSupabaseBrowserClient(),
): Promise<CartResult[]> {
  const { data, error } = await client.from("cart_results").select(RESULT_COLUMNS).eq("request_id", id);
  if (error || !Array.isArray(data)) return [];
  return (data as Record<string, unknown>[]).map(toResult);
}

/** The user's newest checks, groceries and food together. */
export async function recentChecks(
  limit = 3,
  client: SupabaseClient = getSupabaseBrowserClient(),
): Promise<CartRequest[]> {
  const { data, error } = await client
    .from("cart_requests")
    .select(RECENT_COLUMNS)
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error || !Array.isArray(data)) return [];
  return (data as Record<string, unknown>[]).map(toRequest);
}

/** "7 items · Zepto ₹412 · 2m ago" (a check with no prices: "7 items · No prices · 2m ago"). */
export function recentLine(req: CartRequest, now: number = Date.now()): string {
  const s = req.summary;
  const count = s?.count_label ?? plural(req.items.length, "item", "items");
  let middle: string;
  if (req.status === "searching") middle = "Checking prices";
  else {
    const best = s?.best_site ? s.cards?.find((c) => c.site === s.best_site) : undefined;
    middle = best && s?.best_site ? `${siteName(s.best_site)} ${best.total_label}` : "No prices";
  }
  const ago = relativeTime(req.created_at, now);
  return [count, middle, ago].filter(Boolean).join(" · ");
}

// ---- live ---------------------------------------------------------------------------------

export interface CartSnapshot {
  request: CartRequest | null;
  results: CartResult[];
}

let channelSeq = 0;

/**
 * Follow one check: `onChange` gets the request and its per-app results when the channel
 * joins, after every change to either table, and when the page wakes or comes back online
 * (Realtime only delivers changes made while joined). Only the newest read may deliver.
 * Returns an unsubscribe function.
 */
export function watchCartRequest(
  id: string,
  onChange: (snap: CartSnapshot) => void,
  client: SupabaseClient = getSupabaseBrowserClient(),
): () => void {
  let active = true;
  let seq = 0;

  const refetch = () => {
    const mine = ++seq;
    void Promise.all([getRequest(id, client), getResults(id, client)]).then(
      ([request, results]) => {
        if (!active || mine !== seq) return;
        onChange({ request, results });
      },
      () => {}, // retried on the next event, reconnect or wake
    );
  };

  const channel: RealtimeChannel = client
    .channel(`cart-${id}-${++channelSeq}`)
    .on("postgres_changes", { event: "*", schema: "public", table: "cart_requests", filter: `id=eq.${id}` }, () =>
      refetch(),
    )
    .on(
      "postgres_changes",
      { event: "*", schema: "public", table: "cart_results", filter: `request_id=eq.${id}` },
      () => refetch(),
    )
    .subscribe((status) => {
      if (status === "SUBSCRIBED") refetch();
    });

  const onVisible = () => {
    if (document.visibilityState === "visible") refetch();
  };
  if (typeof window !== "undefined") {
    window.addEventListener("online", refetch);
    document.addEventListener("visibilitychange", onVisible);
  }

  return () => {
    active = false;
    if (typeof window !== "undefined") {
      window.removeEventListener("online", refetch);
      document.removeEventListener("visibilitychange", onVisible);
    }
    void client.removeChannel(channel);
  };
}
