// Live job progress and connection status over Supabase Realtime (postgres_changes under RLS).
//
// Realtime only delivers changes that happen while the socket is joined. iOS suspends a PWA's
// socket in the background, and networks drop, so every watcher re-reads the current row(s)
// each time the channel (re)joins, and when the page comes back to the foreground or online.
// Nothing here logs rows: a running job's payload holds the phone number, so it is never read.

import type { RealtimeChannel, SupabaseClient } from "@supabase/supabase-js";
import type { SiteId } from "@/lib/apps";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export type JobStatus = "queued" | "running" | "done" | "failed";

export type JobStage =
  | "otp_needed"
  | "otp_received"
  | "question_needed"
  | "answer_received"
  | "syncing"
  | "synced";

export interface JobProgress {
  stage?: JobStage | string;
  /** The worker's prompt text while an OTP or answer is awaited. */
  question?: string;
  orders_found?: number;
  /** Sum of the stored orders (added by the worker in Plan 3 Task 6; may be absent). */
  amount_found?: number;
  /** When the current prompt was published (ISO), if the worker records it. Written by the worker when it asks for the code. */
  prompt_at?: string;
}

export interface Job {
  id: string;
  site: string;
  type: string;
  status: JobStatus;
  progress: JobProgress;
  /** Failure code, e.g. "otp_timeout", "login_failed", "proxy_unavailable". */
  error: string | null;
  /** When a worker claimed the job (ISO); null while queued. */
  started_at?: string | null;
}

export type ConnectionStatus = "connected" | "needs_login" | "syncing" | "error" | "disconnected";

export interface Connection {
  site: SiteId | string;
  status: ConnectionStatus | string;
  last_synced_at: string | null;
}

/** Columns read from `jobs`. Never `payload` (it carries the phone while a connect runs). */
export const JOB_COLUMNS = "id, site, type, status, progress, error, started_at";
export const CONNECTION_COLUMNS = "site, status, last_synced_at";

const TERMINAL: ReadonlySet<string> = new Set(["done", "failed"]);

export function isTerminal(job: Pick<Job, "status"> | null | undefined): boolean {
  return Boolean(job && TERMINAL.has(job.status));
}

/** Copy only the fields the UI needs out of a row (drops `payload` and anything else). */
function toJob(row: Record<string, unknown>): Job {
  const progress = row.progress && typeof row.progress === "object" ? (row.progress as JobProgress) : {};
  return {
    id: String(row.id),
    site: String(row.site ?? ""),
    type: String(row.type ?? ""),
    status: String(row.status ?? "queued") as JobStatus,
    progress: { ...progress },
    error: typeof row.error === "string" ? row.error : null,
    started_at: typeof row.started_at === "string" ? row.started_at : null,
  };
}

function toConnection(row: Record<string, unknown>): Connection {
  return {
    site: String(row.site ?? ""),
    status: String(row.status ?? ""),
    last_synced_at: typeof row.last_synced_at === "string" ? row.last_synced_at : null,
  };
}

let channelSeq = 0;

/** Re-run `refetch` when the page returns to the foreground or the network comes back. */
function onWake(refetch: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onVisible = () => {
    if (document.visibilityState === "visible") refetch();
  };
  window.addEventListener("online", refetch);
  document.addEventListener("visibilitychange", onVisible);
  return () => {
    window.removeEventListener("online", refetch);
    document.removeEventListener("visibilitychange", onVisible);
  };
}

/**
 * Follow one job: `onChange` gets the job's row now (once the channel joins), on every change,
 * and again after every reconnect. Returns an unsubscribe function.
 *
 * Ordering: a re-read is a snapshot taken when it starts. If a realtime event arrives while it is
 * in flight, the event is newer, so the re-read's answer is dropped (otherwise a slow re-read
 * could roll "otp_needed" back to "queued" and hide the OTP field). Only the newest re-read may
 * deliver, and once the job has finished no later row may move it back to running.
 */
export function watchJob(
  jobId: string,
  onChange: (job: Job) => void,
  client: SupabaseClient = getSupabaseBrowserClient(),
): () => void {
  let active = true;
  let finished = false;
  let events = 0; // realtime events seen so far
  let reads = 0; // re-reads started so far

  const deliver = (row: Record<string, unknown> | null | undefined) => {
    if (!active || !row || String(row.id) !== jobId) return;
    const job = toJob(row);
    if (finished && !isTerminal(job)) return;
    if (isTerminal(job)) finished = true;
    onChange(job);
  };

  const refetch = () => {
    const read = ++reads;
    const eventsAtStart = events;
    void Promise.resolve(
      client.from("jobs").select(JOB_COLUMNS).eq("id", jobId).maybeSingle(),
    ).then(
      ({ data }) => {
        if (read !== reads || events !== eventsAtStart) return; // superseded while in flight
        deliver(data as Record<string, unknown> | null);
      },
      () => {}, // a failed re-read is retried on the next reconnect / wake
    );
  };

  const channel: RealtimeChannel = client
    .channel(`job-${jobId}-${++channelSeq}`)
    .on(
      "postgres_changes",
      { event: "*", schema: "public", table: "jobs", filter: `id=eq.${jobId}` },
      (payload) => {
        events += 1;
        deliver(payload.new as Record<string, unknown>);
      },
    )
    .subscribe((status) => {
      if (status === "SUBSCRIBED") refetch();
    });

  const stopWake = onWake(refetch);
  return () => {
    active = false;
    stopWake();
    void client.removeChannel(channel);
  };
}

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The month of the user's earliest stored order on a site, as "Oct 2025" (null if none). */
export async function firstOrderMonth(
  site: string,
  client: SupabaseClient = getSupabaseBrowserClient(),
): Promise<string | null> {
  try {
    const { data, error } = await client
      .from("orders")
      .select("placed_at")
      .eq("site", site)
      .not("placed_at", "is", null)
      .order("placed_at", { ascending: true })
      .limit(1)
      .maybeSingle();
    const raw = !error && data ? (data as { placed_at?: unknown }).placed_at : null;
    const m = typeof raw === "string" ? /^(\d{4})-(\d{2})/.exec(raw) : null;
    const month = m ? SHORT_MONTHS[Number(m[2]) - 1] : undefined;
    return m && month ? `${month} ${m[1]}` : null;
  } catch {
    return null;
  }
}

/**
 * Follow the signed-in user's connections (RLS limits rows to their own). `onChange` gets the
 * full list on join, on every change and after every reconnect, so a second device sees a new
 * connection appear live. Returns an unsubscribe function.
 */
export function watchConnections(
  onChange: (connections: Connection[]) => void,
  client: SupabaseClient = getSupabaseBrowserClient(),
): () => void {
  let active = true;
  let seq = 0;

  const refetch = () => {
    const mine = ++seq;
    void Promise.resolve(client.from("connections").select(CONNECTION_COLUMNS)).then(
      ({ data, error }) => {
        // Only the newest read wins, so an older, slower response never overwrites a newer one.
        if (!active || error || !Array.isArray(data) || mine !== seq) return;
        onChange((data as Record<string, unknown>[]).map(toConnection));
      },
      () => {},
    );
  };

  const channel: RealtimeChannel = client
    .channel(`connections-${++channelSeq}`)
    .on("postgres_changes", { event: "*", schema: "public", table: "connections" }, () => refetch())
    .subscribe((status) => {
      if (status === "SUBSCRIBED") refetch();
    });

  const stopWake = onWake(refetch);
  return () => {
    active = false;
    stopWake();
    void client.removeChannel(channel);
  };
}
