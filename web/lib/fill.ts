// Cart fill: the Fill button state, starting and confirming a fill, and following one live.
// Every user-facing line of a result is built by the server (server/fill_summary.py) and rendered as is.

import type { RealtimeChannel, SupabaseClient } from "@supabase/supabase-js";
import { apiFetch } from "@/lib/api";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export type FillState =
  | "ready"
  | "connect"
  | "needs_login"
  | "unavailable"
  | "paused"
  | "stale"
  | "limit"
  | "filling"
  | "filled"
  | "review";

export interface FillPreview {
  add: string[];
  ask: string[];
  skip: string[];
}

export interface FillOption {
  state: FillState;
  label: string;
  sublabel: string | null;
  href: string | null;
  fill_id: string | null;
  preview: FillPreview | null;
}

export interface FillOptions {
  fills_left: number;
  fills_limit: number;
  sites: Record<string, FillOption>;
}

export interface FillChoice {
  id: string;
  label: string;
}

export interface FillPlanItem {
  idx: number;
  label: string;
  action: "add" | "ask" | "skip";
  product_id: string | null;
  name: string | null;
  packs: number | null;
  price: number | null;
  question: string | null;
  choices: FillChoice[];
  reason: string | null;
}

export type FillItemStatus = "added" | "partial" | "out_of_stock" | "not_added" | "skipped" | "pending";

export interface FillItem {
  idx: number;
  label: string;
  name: string | null;
  product_id: string | null;
  packs: number | null;
  status: FillItemStatus;
  added_qty: number | null;
  skip_reason: "you_skipped" | "no_answer" | "out_of_stock" | "not_found" | null;
}

export interface FillSection {
  key: string;
  heading: string;
  rows: string[];
}

export interface FillSummary {
  status: string;
  title: string;
  tone: "success" | "warning" | "error";
  sections: FillSection[];
  cart_line: string | null;
  existing_line: string | null;
  warning_line: string | null;
  location_line: string | null;
  reprice: { rows: { label: string; old: string; new: string }[]; total_line: string } | null;
  error_line: string | null;
  open: { label: string; url: string } | null;
  pay_note: string;
  confirm_label: string | null;
  counted: boolean;
}

export type FillStatus =
  | "queued"
  | "filling"
  | "done"
  | "partial"
  | "failed"
  | "needs_confirmation"
  | "interrupted";

export interface CartFill {
  id: string;
  request_id: string;
  site: string;
  job_id: string | null;
  status: FillStatus;
  plan: FillPlanItem[];
  items: FillItem[];
  priced_location: { area?: string } | null;
  confirm_location: { area?: string } | null;
  app_location: { area?: string } | null;
  location_selected: boolean;
  error: string | null;
  summary: FillSummary | null;
}

export interface FillStarted {
  fill_id: string;
  job_id: string;
  fills_left: number;
}

export const ACTIVE_STATUSES: ReadonlySet<string> = new Set(["queued", "filling"]);

export function getFillOptions(requestId: string): Promise<FillOptions> {
  return apiFetch<FillOptions>(`/cart/checks/${encodeURIComponent(requestId)}/fill`);
}

export function startFill(requestId: string, site: string): Promise<FillStarted> {
  return apiFetch<FillStarted>(`/cart/checks/${encodeURIComponent(requestId)}/fill`, {
    method: "POST",
    body: JSON.stringify({ site }),
  });
}

export function confirmFill(fillId: string): Promise<FillStarted> {
  return apiFetch<FillStarted>(`/cart/fills/${encodeURIComponent(fillId)}/confirm`, { method: "POST" });
}

export async function answerFill(jobId: string, answer: string): Promise<void> {
  await apiFetch(`/jobs/${encodeURIComponent(jobId)}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

export const FILL_COLUMNS =
  "id, request_id, site, job_id, status, plan, items, priced_location, confirm_location, app_location, " +
  "location_selected, error, summary";

const list = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
const obj = <T,>(v: unknown): T | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as T) : null);

export function toFill(row: Record<string, unknown>): CartFill {
  return {
    id: String(row.id),
    request_id: String(row.request_id ?? ""),
    site: String(row.site ?? ""),
    job_id: typeof row.job_id === "string" ? row.job_id : null,
    status: String(row.status ?? "queued") as FillStatus,
    plan: list<FillPlanItem>(row.plan),
    items: list<FillItem>(row.items),
    priced_location: obj(row.priced_location),
    confirm_location: obj(row.confirm_location),
    app_location: obj(row.app_location),
    location_selected: row.location_selected === true,
    error: typeof row.error === "string" ? row.error : null,
    summary: obj<FillSummary>(row.summary),
  };
}

export async function getFill(
  id: string,
  client: SupabaseClient = getSupabaseBrowserClient(),
): Promise<CartFill | null> {
  const { data, error } = await client.from("cart_fills").select(FILL_COLUMNS).eq("id", id).maybeSingle();
  if (error || !data) return null;
  return toFill(data as unknown as Record<string, unknown>);
}

let channelSeq = 0;

/**
 * Follow one fill: `onChange` gets the row when the channel joins, on every change, and when the page
 * wakes or comes back online. Only the newest read may deliver. Returns an unsubscribe function.
 */
export function watchFill(
  id: string,
  onChange: (fill: CartFill) => void,
  client: SupabaseClient = getSupabaseBrowserClient(),
): () => void {
  let active = true;
  let seq = 0;

  const refetch = () => {
    const mine = ++seq;
    void getFill(id, client).then(
      (fill) => {
        if (active && mine === seq && fill) onChange(fill);
      },
      () => {},
    );
  };

  const channel: RealtimeChannel = client
    .channel(`fill-${id}-${++channelSeq}`)
    .on("postgres_changes", { event: "*", schema: "public", table: "cart_fills", filter: `id=eq.${id}` }, () =>
      refetch(),
    )
    .subscribe((status) => {
      if (status === "SUBSCRIBED") refetch();
    });

  const onVisible = () => {
    if (document.visibilityState === "visible") refetch();
  };
  window.addEventListener("online", refetch);
  document.addEventListener("visibilitychange", onVisible);

  return () => {
    active = false;
    window.removeEventListener("online", refetch);
    document.removeEventListener("visibilitychange", onVisible);
    void client.removeChannel(channel);
  };
}

/** "1 item" / "3 items". */
export const itemsLabel = (n: number) => `${n} ${n === 1 ? "item" : "items"}`;
