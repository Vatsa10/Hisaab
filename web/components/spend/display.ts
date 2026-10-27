// Display helpers shared by the Spend and Orders screens: app names and dots, dates from the
// stored `placed_at` text, statuses and categories. Pure string work, no Intl, so the server
// render and the phone always agree.

import { APPS } from "@/lib/apps";
import { NOT_SPENT } from "@/lib/queries";

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

export interface AppLabel {
  /** "Swiggy" (Instamart orders are Swiggy orders). */
  name: string;
  /** "Instamart" for Swiggy Instamart orders, otherwise null. */
  sub: string | null;
  dotClass: string;
}

/** How an order's app shows: Instamart appears as Swiggy with an "Instamart" sub-label. */
export function appLabel(site: string, subPlatform?: string | null): AppLabel {
  const instamart = site === "instamart" || subPlatform === "instamart";
  const id = site === "instamart" ? "swiggy" : site;
  const app = APPS.find((a) => a.id === id);
  return {
    name: app?.name ?? (site ? site.charAt(0).toUpperCase() + site.slice(1) : "Unknown app"),
    sub: instamart ? "Instamart" : null,
    dotClass: app?.dotClass ?? "bg-ink-muted",
  };
}

/**
 * The merchant worth naming, or null. Grocery apps report themselves as the merchant ("Zepto",
 * "Instamart"); that adds nothing next to the app name, so it is dropped.
 */
export function distinctMerchant(merchant: string | null, app: AppLabel): string | null {
  if (!merchant) return null;
  const m = merchant.trim().toLowerCase();
  if (!m || m === app.name.toLowerCase() || (app.sub && m === app.sub.toLowerCase())) return null;
  return merchant.trim();
}

interface Parts {
  y: number;
  m: number;
  d: number;
  hh: number | null;
  mm: number | null;
}

function parts(placedAt: string | null): Parts | null {
  if (!placedAt) return null;
  const x = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(placedAt);
  if (!x) return null;
  const m = Number(x[2]);
  if (m < 1 || m > 12) return null;
  return {
    y: Number(x[1]),
    m,
    d: Number(x[3]),
    hh: x[4] === undefined ? null : Number(x[4]),
    mm: x[5] === undefined ? null : Number(x[5]),
  };
}

/** "YYYY-MM" of an order, or null when its date is unknown. */
export function orderMonth(placedAt: string | null): string | null {
  const p = parts(placedAt);
  return p ? `${p.y}-${String(p.m).padStart(2, "0")}` : null;
}

/** "3 Oct" */
export function shortDate(placedAt: string | null): string {
  const p = parts(placedAt);
  return p ? `${p.d} ${SHORT_MONTHS[p.m - 1]}` : "Date unknown";
}

function clock(hh: number, mm: number): string {
  const h12 = hh % 12 === 0 ? 12 : hh % 12;
  return `${h12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "am" : "pm"}`;
}

/** "Sat, 3 Oct 2026 · 7:42 pm" (time left out when the app gave only a date). */
export function longDateTime(placedAt: string | null): string {
  const p = parts(placedAt);
  if (!p) return "Date unknown";
  const weekday = WEEKDAYS[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()];
  const date = `${weekday}, ${p.d} ${SHORT_MONTHS[p.m - 1]} ${p.y}`;
  return p.hh === null || p.mm === null ? date : `${date} · ${clock(p.hh, p.mm)}`;
}

/** "Cancelled" / "Refunded" / "Failed" for orders that didn't count as spend, otherwise null. */
export function voidedLabel(status: string | null): string | null {
  if (!status || !NOT_SPENT.has(status)) return null;
  return status.charAt(0).toUpperCase() + status.slice(1);
}

/** "delivered" -> "Delivered"; underscores become spaces. */
export function statusLabel(status: string | null): string | null {
  if (!status) return null;
  const s = status.replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : null;
}

/** Category names as people say them; the catch-all reads "Other". */
export function categoryLabel(category: string): string {
  if (!category || category === "uncategorized") return "Other";
  return category.charAt(0).toUpperCase() + category.slice(1);
}

/** Quantities without trailing zeros: 2 -> "2", 0.5 -> "0.5". */
export function qtyLabel(qty: number): string {
  return Number.isInteger(qty) ? String(qty) : String(Math.round(qty * 100) / 100);
}
