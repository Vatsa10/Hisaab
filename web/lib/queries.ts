// Read queries for the Spend and Orders screens. Every query runs through the Supabase client
// with the user's own session (anon key + RLS), so each user only ever sees their own rows.
// Each function takes an optional client: the browser client by default, or the server client
// (lib/supabase/server.ts) from a Server Component. Nothing here logs rows or errors.

import type { SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export interface MonthlySpendRow {
  /** "YYYY-MM" */
  month: string;
  site: string;
  subPlatform: string | null;
  orders: number;
  amount: number;
}

export interface CategorySpendRow {
  category: string;
  amount: number;
}

export interface TopMerchant {
  merchant: string;
  site: string;
  orders: number;
  amount: number;
}

export interface OrderSummary {
  site: string;
  orderId: string;
  subPlatform: string | null;
  /** As stored, e.g. "2026-10-03T19:42:00"; null when the app didn't say. */
  placedAt: string | null;
  total: number | null;
  status: string | null;
  merchant: string | null;
}

export interface OrderItem {
  idx: number;
  name: string | null;
  qty: number | null;
  price: number | null;
}

export interface OrderDetail extends OrderSummary {
  currency: string | null;
  items: OrderItem[];
}

export interface OrdersQuery {
  site?: string;
  /** "YYYY-MM" */
  month?: string;
  /** Opaque value from a previous page's `nextCursor`. */
  cursor?: string;
}

export interface OrdersPage {
  orders: OrderSummary[];
  /** Pass back as `cursor` for the next page; null on the last page. */
  nextCursor: string | null;
}

export const ORDERS_PAGE_SIZE = 30;

/** Statuses that never count as spend (same rule as the views and report.py). */
export const NOT_SPENT = new Set(["cancelled", "refunded", "failed"]);

const ORDER_COLUMNS = "site, order_id, sub_platform, placed_at, total_amount, status, merchant";
/** PostgREST's default max-rows on Supabase; larger reads are paged. */
const READ_CHUNK = 1000;

function client(c?: SupabaseClient): SupabaseClient {
  return c ?? getSupabaseBrowserClient();
}

/** Query failures carry no row data or SQL, only a generic message for the retry UI. */
export class QueryError extends Error {
  constructor(what: string) {
    super(`Couldn't load ${what}.`);
    this.name = "QueryError";
  }
}

function num(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

function numOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v !== "" ? v : null;
}

/** Spend per month, app and sub-platform (cancelled/refunded/failed orders excluded). */
export async function getMonthlySpend(c?: SupabaseClient): Promise<MonthlySpendRow[]> {
  const { data, error } = await client(c)
    .from("monthly_spend")
    .select("month, site, sub_platform, orders, amount")
    .order("month", { ascending: true });
  if (error) throw new QueryError("your spending");
  return (data ?? []).map((r: Record<string, unknown>) => ({
    month: String(r.month ?? ""),
    site: String(r.site ?? ""),
    subPlatform: str(r.sub_platform),
    orders: num(r.orders),
    amount: num(r.amount),
  }));
}

/** Item-level spend by category for one month ("YYYY-MM"), largest first. */
export async function getCategorySpend(month: string, c?: SupabaseClient): Promise<CategorySpendRow[]> {
  const { data, error } = await client(c)
    .from("category_spend")
    .select("category, amount")
    .eq("month", month)
    .order("amount", { ascending: false });
  if (error) throw new QueryError("categories");
  return (data ?? []).map((r: Record<string, unknown>) => ({
    category: String(r.category ?? "uncategorized"),
    amount: num(r.amount),
  }));
}

/**
 * Restaurants and stores ranked by spend in one calendar year, largest first. merchant_spend has
 * no date dimension, so this sums the year's own orders (cancelled ones and orders without a
 * merchant left out), reading in chunks so a busy year is never cut short by max-rows.
 */
export async function getTopMerchants(year: number, c?: SupabaseClient): Promise<TopMerchant[]> {
  const db = client(c);
  const totals = new Map<string, TopMerchant>();
  for (let from = 0; ; from += READ_CHUNK) {
    const { data, error } = await db
      .from("orders")
      .select("merchant, site, total_amount, status")
      .gte("placed_at", `${year}-01-01`)
      .lt("placed_at", `${year + 1}-01-01`)
      .not("merchant", "is", null)
      .order("site", { ascending: true })
      .order("order_id", { ascending: true })
      .range(from, from + READ_CHUNK - 1);
    if (error) throw new QueryError("your top places");
    const rows = (data ?? []) as Record<string, unknown>[];
    for (const r of rows) {
      const merchant = str(r.merchant);
      const status = str(r.status);
      if (!merchant || (status && NOT_SPENT.has(status))) continue;
      const site = String(r.site ?? "");
      const key = `${site}\u0000${merchant}`;
      const t = totals.get(key) ?? { merchant, site, orders: 0, amount: 0 };
      t.orders += 1;
      t.amount = Math.round((t.amount + num(r.total_amount)) * 100) / 100;
      totals.set(key, t);
    }
    if (rows.length < READ_CHUNK) break;
  }
  return [...totals.values()].sort((a, b) => b.amount - a.amount || b.orders - a.orders);
}

/**
 * Spend in `month` ("YYYY-MM") from the 1st up to and including day `dayOfMonth`, for a fair
 * month-to-date comparison ("vs this time in September"). Same rule as the views: cancelled,
 * refunded and failed orders don't count. A day past the month's end simply covers the whole month.
 */
export async function getSpendToDate(month: string, dayOfMonth: number, c?: SupabaseClient): Promise<number> {
  const db = client(c);
  const end = `${month}-${String(Math.max(1, Math.floor(dayOfMonth)) + 1).padStart(2, "0")}`;
  let total = 0;
  for (let from = 0; ; from += READ_CHUNK) {
    const { data, error } = await db
      .from("orders")
      .select("total_amount, status")
      .gte("placed_at", `${month}-01`)
      .lt("placed_at", end)
      .order("site", { ascending: true })
      .order("order_id", { ascending: true })
      .range(from, from + READ_CHUNK - 1);
    if (error) throw new QueryError("last month's spending");
    const rows = (data ?? []) as Record<string, unknown>[];
    for (const r of rows) {
      const status = str(r.status);
      if (status && NOT_SPENT.has(status)) continue;
      total += num(r.total_amount);
    }
    if (rows.length < READ_CHUNK) break;
  }
  return Math.round(total * 100) / 100;
}

// --- Orders: keyset pagination, newest first --------------------------------------------------

type CursorKey = [placedAt: string | null, site: string, orderId: string];

function encodeCursor(o: OrderSummary): string {
  return JSON.stringify([o.placedAt, o.site, o.orderId] satisfies CursorKey);
}

function decodeCursor(cursor: string): CursorKey | null {
  try {
    const v: unknown = JSON.parse(cursor);
    if (
      Array.isArray(v) &&
      v.length === 3 &&
      (v[0] === null || typeof v[0] === "string") &&
      typeof v[1] === "string" &&
      typeof v[2] === "string"
    ) {
      return v as CursorKey;
    }
  } catch {
    // fall through
  }
  return null;
}

/** A PostgREST filter value in double quotes, so commas, dots, colons and brackets are literal. */
function quote(v: string): string {
  return `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function toSummary(r: Record<string, unknown>): OrderSummary {
  return {
    site: String(r.site ?? ""),
    orderId: String(r.order_id ?? ""),
    subPlatform: str(r.sub_platform),
    placedAt: str(r.placed_at),
    total: numOrNull(r.total_amount),
    status: str(r.status),
    merchant: str(r.merchant),
  };
}

/**
 * One page of orders, newest first (orders without a date last), optionally for one app and/or
 * month. Keyset pagination on (placed_at desc, site desc, order_id desc): each page starts
 * strictly after the previous page's last row, so it stays fast and never repeats or skips a
 * row when new orders arrive. Fetches one extra row to know whether another page exists.
 */
export async function getOrders(query: OrdersQuery = {}, c?: SupabaseClient): Promise<OrdersPage> {
  let req = client(c).from("orders").select(ORDER_COLUMNS);
  if (query.site) req = req.eq("site", query.site);
  if (query.month && /^\d{4}-\d{2}$/.test(query.month)) req = req.like("placed_at", `${query.month}%`);

  const after = query.cursor ? decodeCursor(query.cursor) : null;
  if (after) {
    const [p, s, o] = after;
    if (p === null) {
      req = req.is("placed_at", null).or(`site.lt.${quote(s)},and(site.eq.${quote(s)},order_id.lt.${quote(o)})`);
    } else {
      req = req.or(
        `placed_at.lt.${quote(p)},placed_at.is.null,` +
          `and(placed_at.eq.${quote(p)},site.lt.${quote(s)}),` +
          `and(placed_at.eq.${quote(p)},site.eq.${quote(s)},order_id.lt.${quote(o)})`,
      );
    }
  }

  const { data, error } = await req
    .order("placed_at", { ascending: false, nullsFirst: false })
    .order("site", { ascending: false })
    .order("order_id", { ascending: false })
    .limit(ORDERS_PAGE_SIZE + 1);
  if (error) throw new QueryError("your orders");

  const rows = ((data ?? []) as Record<string, unknown>[]).map(toSummary);
  const more = rows.length > ORDERS_PAGE_SIZE;
  const orders = more ? rows.slice(0, ORDERS_PAGE_SIZE) : rows;
  const last = orders[orders.length - 1];
  return { orders, nextCursor: more && last ? encodeCursor(last) : null };
}

/** One order with its items in their original order; null when it doesn't exist (or isn't yours). */
export async function getOrder(site: string, orderId: string, c?: SupabaseClient): Promise<OrderDetail | null> {
  const { data, error } = await client(c)
    .from("orders")
    .select(`${ORDER_COLUMNS}, currency, order_items(idx, name, qty, price)`)
    .eq("site", site)
    .eq("order_id", orderId)
    .maybeSingle();
  if (error) throw new QueryError("this order");
  if (!data) return null;
  const r = data as Record<string, unknown>;
  const items = (Array.isArray(r.order_items) ? (r.order_items as Record<string, unknown>[]) : [])
    .map((i) => ({ idx: num(i.idx), name: str(i.name), qty: numOrNull(i.qty), price: numOrNull(i.price) }))
    .sort((a, b) => a.idx - b.idx);
  return { ...toSummary(r), currency: str(r.currency), items };
}
