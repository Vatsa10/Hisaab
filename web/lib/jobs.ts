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
  /** Cart fill: the choices offered for the question (the id goes back as the answer). */
  choices?: { id: string; label: string }[];
  /** Cart fill: items handled so far, and in all. */
  done?: number;
  total?: number;
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
export function onWake(refetch: () => void): () => void {
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

  // Inserts and updates of this one job only. Never DELETE (or "*"): Supabase delivers DELETE events
  // to every subscriber, unfiltered, and a job row is never deleted while someone follows it.
  const onRow = (payload: { new: unknown }) => {
    events += 1;
    deliver(payload.new as Record<string, unknown>);
  };
  let channel: RealtimeChannel = client.channel(`job-${jobId}-${++channelSeq}`);
  for (const event of OWN_ROW_EVENTS) {
    channel = channel.on(
      "postgres_changes",
      { event, schema: "public", table: "jobs", filter: `id=eq.${jobId}` },
      onRow,
    );
  }
  channel = channel.subscribe((status) => {
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

/** The only Realtime events per-user watchers subscribe to. */
export const OWN_ROW_EVENTS = ["INSERT", "UPDATE"] as const;

/**
 * Listen for the user's own new and changed rows of a per-user table, filtered to `user_id=eq.<uid>`.
 * Never DELETE: Supabase applies neither RLS nor the filter to DELETE events, so a DELETE
 * subscription would deliver other users' deleted keys. Deletes show on the next re-read (channel
 * rejoin, wake, back online), or with the insert or update that usually comes with them.
 */
export function onOwnRowChanges(
  channel: RealtimeChannel,
  table: string,
  userId: string,
  onEvent: () => void,
): RealtimeChannel {
  let ch = channel;
  for (const event of OWN_ROW_EVENTS) {
    ch = ch.on("postgres_changes", { event, schema: "public", table, filter: `user_id=eq.${userId}` }, () =>
      onEvent(),
    );
  }
  return ch;
}

/**
 * Run `start(userId)` for the signed-in user and return a stop function. With `userId` given it
 * starts at once; otherwise it reads the session first (and starts nothing without one).
 */
export function forUser(
  client: SupabaseClient,
  userId: string | undefined,
  start: (userId: string) => () => void,
): () => void {
  let stop: (() => void) | null = null;
  let active = true;
  // Read the session first, even when the id is known: the Realtime socket must hold the user's JWT
  // before the channel joins. Joined with only the anon key, Supabase says SUBSCRIBED but RLS then
  // hides every row, so no event ever arrives.
  void Promise.resolve(client.auth.getSession()).then(
    async ({ data }) => {
      const session = data.session;
      const uid = userId ?? session?.user?.id;
      if (!uid) return;
      if (session?.access_token) {
        try {
          await client.realtime?.setAuth(session.access_token);
        } catch {
          // The client sets the token itself on the next auth event; carry on.
        }
      }
      if (active) stop = start(uid);
    },
    () => {},
  );
  return () => {
    active = false;
    stop?.();
  };
}

/**
 * Follow the signed-in user's connections (RLS limits reads to their own). `onChange` gets the full
 * list on join, on each of their own inserts and updates (onOwnRowChanges), and after every
 * reconnect or wake, so a second device sees a new connection appear live. Returns an unsubscribe
 * function.
 */
export function watchConnections(
  onChange: (connections: Connection[]) => void,
  client: SupabaseClient = getSupabaseBrowserClient(),
  userId?: string,
): () => void {
  return forUser(client, userId, (uid) => followConnections(onChange, client, uid));
}

function followConnections(
  onChange: (connections: Connection[]) => void,
  client: SupabaseClient,
  userId: string,
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

  const channel: RealtimeChannel = onOwnRowChanges(
    client.channel(`connections-${++channelSeq}`),
    "connections",
    userId,
    refetch,
  ).subscribe((status) => {
      if (status === "SUBSCRIBED") refetch();
    });

  const stopWake = onWake(refetch);
  return () => {
    active = false;
    stopWake();
    void client.removeChannel(channel);
  };
}
