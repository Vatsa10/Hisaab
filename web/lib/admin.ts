// The admin view's data. Everything comes from the API's /admin/* routes (guarded by
// require_admin on the server) using the signed-in user's own token: the PWA never holds the
// service-role key and never reads admin tables directly. A non-admin gets 403 "admin only".

import { ApiError, apiFetch } from "@/lib/api";
import { APPS } from "@/lib/apps";

export type BudgetLevel = "ok" | "alert" | "paused";

export interface UserCost {
  user_id: string;
  email: string;
  proxy_gb: number;
  ai_tokens: number;
  worker_minutes: number;
  syncs: number;
  cart_checks: number;
  inr: number;
}

export interface Costs {
  /** "YYYY-MM" (IST month) */
  month: string;
  budget_inr: number;
  spent_inr: number;
  fixed_inr: number;
  pct: number;
  level: BudgetLevel | string;
  unit_costs: { proxy_inr_per_gb: number; ai_inr_per_1k_tokens: number; worker_inr_per_minute: number };
  users: UserCost[];
}

export interface SiteHealth {
  site: string;
  kind: string;
  version: number | null;
  learned_at: string | null;
  healthy: boolean;
  paused: boolean;
  health_note: string | null;
  syncs_ok_7d: number;
  syncs_failed_7d: number;
  last_ok_at: string | null;
  /** The calling admin's own connection for this site is connected (needed to learn). */
  admin_connected: boolean;
  /** Why the site is paused; the server supplies it later (P7 T13). Absent is treated as manual. */
  paused_reason?: "auto" | "manual";
  cooldown_until?: string | null;
  backoff_level?: number;
  failures_window?: number;
  jobs_window?: number;
}

export interface Invite {
  email: string;
  invited_at: string;
  accepted_at: string | null;
}

export interface WaitlistEntry {
  email: string;
  joined_at: string;
  invited_at: string | null;
}

export interface Invites {
  invites: Invite[];
  waitlist: WaitlistEntry[];
}

export interface DiffEntry {
  op: "add" | "remove" | "change" | string;
  /** JSON path, e.g. "$.read_post_allowlist[0].url_regex" */
  path: string;
  old?: unknown;
  new?: unknown;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "auto_applied";

export interface Approval {
  id: string;
  site: string;
  kind: string;
  reason: string;
  status: ApprovalStatus | string;
  proposed: Record<string, unknown> | null;
  current: Record<string, unknown> | null;
  diff: DiffEntry[] | null;
  job_id: string | null;
  created_at: string;
  decided_at: string | null;
}

/** One POST the learn job asks to allow (job progress `entries`). */
export interface PostEntry {
  url_regex: string;
  why?: string;
}

export type Decision = "approve" | "reject";

// ---- API --------------------------------------------------------------------------------

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const enc = encodeURIComponent;

export const getCosts = (month?: string) =>
  apiFetch<Costs>(month ? `/admin/costs?month=${enc(month)}` : "/admin/costs");
export const getSites = () => apiFetch<SiteHealth[]>("/admin/sites");
export const pauseSite = (site: string) => apiFetch<void>(`/admin/sites/${enc(site)}/pause`, { method: "POST" });
export const resumeSite = (site: string) => apiFetch<void>(`/admin/sites/${enc(site)}/resume`, { method: "POST" });
export type LearnKind = "orders" | "search" | "food";
export const startLearn = (site: string, kind: LearnKind = "orders") =>
  apiFetch<{ job_id: string }>(`/admin/learn/${enc(site)}${kind === "orders" ? "" : `?kind=${kind}`}`, { method: "POST" });
export const getInvites = () => apiFetch<Invites>("/admin/invites");
export const sendInvite = (email: string) => apiFetch<unknown>("/admin/invites", json({ email }));
export const revokeInvite = (email: string) => apiFetch<void>(`/admin/invites/${enc(email)}`, { method: "DELETE" });
export const getApprovals = (status: "pending" | "all") =>
  apiFetch<Approval[]>(`/admin/approvals?status=${status}`);
export const decideApproval = (id: string, decision: Decision) =>
  apiFetch<{ status: string }>(`/admin/approvals/${enc(id)}/decide`, json({ decision }));

// ---- helpers ----------------------------------------------------------------------------

export const ADMIN_ONLY = "This page is for the admin.";

export function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

/** The API's detail for a known failure, else a generic line. */
export function problem(e: unknown, fallback: string): string {
  if (e instanceof ApiError && (e.status === 0 || (e.status >= 400 && e.status < 500 && e.detail))) return e.detail;
  return fallback;
}

/** "ananya@example.com" -> "a***@example.com": the Costs tab never shows a full address. */
export function maskEmail(email: string): string {
  const at = email.lastIndexOf("@");
  if (at < 1) return "***";
  return `${email[0]}***${email.slice(at)}`;
}

/** "zepto" -> "Zepto" (falls back to capitalising unknown sites). */
export function appName(site: string): string {
  return APPS.find((a) => a.id === site)?.name ?? (site ? site[0]!.toUpperCase() + site.slice(1) : site);
}

/** Tailwind colour class for a site's dot (neutral for unknown sites). */
export function appDot(site: string): string {
  return APPS.find((a) => a.id === site)?.dotClass ?? "bg-line";
}

/** The current calendar month in India (the budget's month), as "YYYY-MM". */
export function currentMonthIST(now: number = Date.now()): string {
  const ist = new Date(now + 330 * 60_000);
  return `${ist.getUTCFullYear()}-${String(ist.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** "2026-10" shifted by `delta` months. */
export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number) as [number, number];
  const i = y * 12 + (m - 1) + delta;
  return `${Math.floor(i / 12)}-${String((i % 12) + 1).padStart(2, "0")}`;
}

/** 0.4213 -> "0.42 GB" */
export const formatGB = (gb: number) => `${(Number.isFinite(gb) ? gb : 0).toFixed(2)} GB`;

/** 81200 -> "81.2k tokens" */
export function formatTokens(tokens: number): string {
  const k = (Number.isFinite(tokens) ? tokens : 0) / 1000;
  return `${k >= 100 ? Math.round(k) : Number(k.toFixed(1))}k tokens`;
}

/** 140.6 -> "141 min"; under one minute keeps a decimal ("0.4 min"). */
export function formatMinutes(min: number): string {
  const m = Number.isFinite(min) ? min : 0;
  return `${m > 0 && m < 1 ? m.toFixed(1) : Math.round(m)} min`;
}

/** Plain-language reason for a pending approval. */
export function reasonLabel(reason: string): string {
  switch (reason) {
    case "post_allowlist":
      return "Asks to allow a new POST request";
    case "account_specific":
      return "Holds account-specific values";
    case "cart":
      return "Cart recipes always need review";
    case "learned":
      return "Learned recipe";
    case "drift_relearn":
      return "Re-learned after the old recipe stopped working";
    default:
      return reason;
  }
}

/** "approved" -> "Approved", "auto_applied" -> "Auto-applied". */
export function statusLabel(status: string): string {
  if (status === "auto_applied") return "Auto-applied";
  return status ? status[0]!.toUpperCase() + status.slice(1) : status;
}

/** The POST entries a post_allowlist approval proposes (`proposed.read_post_allowlist`). */
export function proposedEntries(a: Pick<Approval, "proposed">): PostEntry[] {
  const raw = a.proposed?.read_post_allowlist;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((e): e is Record<string, unknown> => Boolean(e) && typeof e === "object")
    .map((e) => ({ url_regex: String(e.url_regex ?? ""), why: typeof e.why === "string" ? e.why : undefined }));
}

// ---- fee rules (Plan 5) -----------------------------------------------------------------

export type FeeRules = Record<string, unknown>;

/** One site's row of GET /admin/fees. */
export interface FeeRow {
  site: string;
  /** "grocery" or "food"; falls back to rules.kind when the API leaves it out. */
  kind?: string;
  rules: FeeRules | null;
  version: number;
  verified: boolean;
  source?: string;
  updated_at?: string;
}

export const getFees = () => apiFetch<FeeRow[]>("/admin/fees");
export const putFees = (site: string, rules: FeeRules, verified: boolean) =>
  apiFetch<{ version: number }>(`/admin/fees/${enc(site)}`, {
    method: "PUT",
    body: JSON.stringify({ rules, verified }),
  });

/** The groups the Sites tab lists recipes under, and the learn kind each one runs. */
export type SiteGroup = "orders" | "search" | "food" | "other";
export const SITE_GROUPS: { id: SiteGroup; title: string; learnLabel: string }[] = [
  { id: "orders", title: "Order history", learnLabel: "" },
  { id: "search", title: "Grocery prices", learnLabel: "grocery prices" },
  { id: "food", title: "Restaurants & menus", learnLabel: "restaurants and menus" },
  // Anything else (cart, addresses, future kinds): health and pause, never Learn.
  { id: "other", title: "Other", learnLabel: "" },
];

export function groupOf(kind: string): SiteGroup {
  if (!kind || kind === "orders") return "orders";
  if (kind === "search") return "search";
  if (kind === "restaurants" || kind === "menu" || kind === "food") return "food";
  return "other";
}
