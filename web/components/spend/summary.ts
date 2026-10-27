import type { SiteId } from "@/lib/apps";
import type { MonthlySpendRow } from "@/lib/queries";

/** Per-app amounts. Instamart is counted inside Swiggy (its orders come through Swiggy). */
export type AppAmounts = Record<SiteId, number>;

export interface MonthPoint {
  /** "YYYY-MM" */
  month: string;
  total: number;
  orders: number;
  byApp: AppAmounts;
  /** The Instamart share of `byApp.swiggy`. */
  instamart: number;
}

export interface SpendSummary {
  /** The six months ending with the current one, oldest first. */
  months: MonthPoint[];
  /** Total per "YYYY-MM" across every month with spend (for the change vs the month before). */
  monthTotals: Record<string, number>;
  /** True when the user has any spend at all (in any month). */
  hasData: boolean;
  /** True when some of that spend falls in the six chart months. */
  inWindow: boolean;
  /** The latest "YYYY-MM" with spend, or null when there is none. */
  lastMonth: string | null;
}

export const CHART_MONTHS = 6;

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** "YYYY-MM" for a date, in the phone's own time zone. */
export function monthKey(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
}

/** "2026-10" -> "Oct". */
export function shortMonth(month: string): string {
  return SHORT_MONTHS[Number(month.slice(5, 7)) - 1] ?? month;
}

function shiftMonth(month: string, by: number): string {
  const y = Number(month.slice(0, 4));
  const m = Number(month.slice(5, 7)) - 1 + by;
  const year = y + Math.floor(m / 12);
  const mm = ((m % 12) + 12) % 12;
  return `${year}-${pad2(mm + 1)}`;
}

/** The month before "YYYY-MM". */
export function previousMonth(month: string): string {
  return shiftMonth(month, -1);
}

function emptyApps(): AppAmounts {
  return { swiggy: 0, zomato: 0, zepto: 0, blinkit: 0 };
}

function appOf(site: string): { app: SiteId | null; instamart: boolean } {
  if (site === "instamart") return { app: "swiggy", instamart: true };
  if (site === "swiggy" || site === "zomato" || site === "zepto" || site === "blinkit") {
    return { app: site, instamart: false };
  }
  return { app: null, instamart: false };
}

const round2 = (n: number) => Math.round(n * 100) / 100;

/** Fold monthly_spend rows into the six chart months ending at `today`'s month. */
export function buildSpendSummary(rows: readonly MonthlySpendRow[], today: Date): SpendSummary {
  const current = monthKey(today);
  const months: MonthPoint[] = Array.from({ length: CHART_MONTHS }, (_, i) => ({
    month: shiftMonth(current, i - (CHART_MONTHS - 1)),
    total: 0,
    orders: 0,
    byApp: emptyApps(),
    instamart: 0,
  }));
  const index = new Map(months.map((m, i) => [m.month, i]));

  let inWindow = false;
  let lastMonth: string | null = null;
  const monthTotals: Record<string, number> = {};
  for (const r of rows) {
    const active = r.amount !== 0 || r.orders > 0;
    if (active && (lastMonth === null || r.month > lastMonth)) lastMonth = r.month;
    monthTotals[r.month] = round2((monthTotals[r.month] ?? 0) + r.amount);
    const i = index.get(r.month);
    if (i === undefined) continue;
    if (active) inWindow = true;
    const point = months[i]!;
    point.total = round2(point.total + r.amount);
    point.orders += r.orders;
    const { app, instamart } = appOf(r.site);
    if (!app) continue;
    point.byApp[app] = round2(point.byApp[app] + r.amount);
    if (instamart || r.subPlatform === "instamart") point.instamart = round2(point.instamart + r.amount);
  }
  return { months, monthTotals, hasData: lastMonth !== null, inWindow, lastMonth };
}
