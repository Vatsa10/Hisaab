"use client";

import { useEffect, useState } from "react";
import { relativeTime } from "@/components/connect/copy";
import { Spinner } from "@/components/connect/icons";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  appName,
  connectionName,
  decideApproval,
  hasRiskyEntry,
  toPermissionEntry,
  getSites,
  isForbidden,
  pauseSite,
  problem,
  resumeSite,
  SITE_GROUPS,
  groupOf,
  startLearn,
  type Decision,
  type SiteGroup,
  type PostEntry,
  type SiteHealth,
} from "@/lib/admin";
import { isTerminal, watchJob, type Job } from "@/lib/jobs";
import { NEVER_LINE, PermissionEntry, RiskWarning } from "./CartPermissions";
import {
  Badge,
  ConfirmSheet,
  ListSkeleton,
  LoadError,
  Notice,
  SectionLabel,
  SiteMark,
  useAdminLoad,
  type PanelProps,
} from "./parts";

/** Sites tab: each site's recipe and health, with Learn and Pause/Resume. */
export function SitesPanel({ onForbidden }: PanelProps) {
  const sites = useAdminLoad("sites", getSites, onForbidden);

  if (sites.status === "loading") return <ListSkeleton rows={4} label="Sites" />;
  if (sites.status === "error") return <LoadError what="sites" onRetry={sites.reload} />;
  if (sites.data.length === 0) {
    return (
      <Card>
        <p className="text-body text-ink-muted">No site recipes yet.</p>
      </Card>
    );
  }

  // Rows are grouped by what they learn. Pause acts on a whole site, so only the first row
  // of a site carries it; every row has its own Learn for its kind.
  const pausable = new Set<string>();
  const sections = SITE_GROUPS.map((g) => {
    const seenInGroup = new Set<string>();
    const rows = sites.data.filter((s) => {
      if (groupOf(s.kind) !== g.id) return false;
      // Restaurants and menus are learned together (kind food), so they share one row per site.
      if (g.id === "food" && seenInGroup.has(s.site)) return false;
      seenInGroup.add(s.site);
      return true;
    });
    return { ...g, rows };
  }).filter((g) => g.rows.length > 0);

  return (
    <div aria-label="Sites">
      {sections.map((g) => (
        <section key={g.id} aria-labelledby={`sites-group-${g.id}`} className="mt-6 first:mt-0">
          <SectionLabel>
            <span id={`sites-group-${g.id}`}>{g.title}</span>
          </SectionLabel>
          <ul aria-label={g.title} className="flex flex-col gap-3">
            {g.rows.map((s) => {
              const pause = g.id !== "cart" && !pausable.has(s.site);
              if (g.id !== "cart") pausable.add(s.site);
              return (
                <SiteCard
                  key={`${s.site}:${s.kind}`}
                  site={s}
                  group={g.id}
                  learnLabel={g.learnLabel}
                  pause={pause}
                  onChanged={sites.reload}
                  onForbidden={onForbidden}
                />
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** Matches the server's ERROR_BUDGET_WINDOW_H default. */
const WINDOW_HOURS = 24;

function hhmm(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function healthBadge(s: SiteHealth) {
  if (s.paused) return <Badge tone="bad">Paused</Badge>;
  if (!s.healthy) return <Badge tone="warn">Unhealthy</Badge>;
  return <Badge tone="ok">Healthy</Badge>;
}

interface SiteCardProps {
  site: SiteHealth;
  group: SiteGroup;
  /** Words after the app name in the Learn button's label ("" for order history). */
  learnLabel: string;
  /** This card carries Pause/Resume (the first row of a site). */
  pause: boolean;
  onChanged: () => void;
  onForbidden: () => void;
}

function SiteCard({ site: s, group, learnLabel, pause, onChanged, onForbidden }: SiteCardProps) {
  const name = appName(s.site);
  const [busy, setBusy] = useState<"learn" | "pause" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const now = Date.now();
  const cooling = Boolean(s.cooldown_until) && new Date(s.cooldown_until!).getTime() > now;
  const failures = s.failures_window ?? 0;
  const jobs = s.jobs_window ?? 0;

  useEffect(() => {
    if (!jobId) return;
    return watchJob(jobId, setJob);
  }, [jobId]);

  const [confirmPause, setConfirmPause] = useState(false);
  const learning = Boolean(jobId) && !isTerminal(job);
  const finished = Boolean(job && isTerminal(job));
  useEffect(() => {
    if (finished) onChanged();
  }, [finished, onChanged]);

  async function act(kind: "learn" | "pause", run: () => Promise<unknown>, fallback: string) {
    setBusy(kind);
    setError(null);
    try {
      await run();
    } catch (e) {
      if (isForbidden(e)) onForbidden();
      else setError(problem(e, fallback));
    } finally {
      setBusy(null);
    }
  }

  const learn = () =>
    act(
      "learn",
      async () => {
        const res = await startLearn(s.site, group === "other" ? "orders" : group);
        setJob(null);
        setJobId(res.job_id);
      },
      `Couldn't start learning ${name}.`,
    );

  const resume = () =>
    act(
      "pause",
      async () => {
        await resumeSite(s.site);
        onChanged();
      },
      `Couldn't resume ${name}.`,
    );

  const label = learnLabel ? `${name} ${learnLabel}` : name;
  const hintId = `learn-hint-${s.site}-${group}`;
  const canLearn = group !== "other";
  const cart = group === "cart";
  const conn = connectionName(s.site);

  return (
    <Card as="li" aria-label={label}>
      <div className="flex items-center gap-3">
        <SiteMark site={s.site} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-headline">
            {name}
          </h3>
          <p className="text-label text-ink-muted" suppressHydrationWarning>
            {s.version != null ? `Recipe v${s.version}` : "No recipe yet"}
            {s.learned_at ? ` · learned ${relativeTime(s.learned_at, now)}` : ""}
          </p>
        </div>
        {cart && s.pending_approval ? <Badge tone="warn">Waiting for approval</Badge> : healthBadge(s)}
      </div>

      {cooling ? (
        <Badge tone="warn" className="mt-3">
          Cooling down until {hhmm(s.cooldown_until!)} · level {s.backoff_level ?? 0}
        </Badge>
      ) : null}

      {s.p50_ms != null ? (
        <p className="mt-3 text-label tabular-nums text-ink-muted" data-testid="site-speed">
          Speed <span className="text-ink">{(s.p50_ms / 1000).toFixed(1)}s</span> typical ·{" "}
          <span className="text-ink">{((s.p95_ms ?? s.p50_ms) / 1000).toFixed(1)}s</span> slowest
          {s.slow_jobs_window ? <> · {s.slow_jobs_window} over budget</> : null}
        </p>
      ) : null}

      {cart ? null : (
      <p className="mt-3 text-label tabular-nums text-ink-muted" suppressHydrationWarning>
        <span className="text-ink">{s.syncs_ok_7d}</span> ok ·{" "}
        <span className={s.syncs_failed_7d > 0 ? "font-semibold text-up" : "text-ink"}>{s.syncs_failed_7d}</span> failed
        in 7 days
        {s.last_ok_at ? ` · last ok ${relativeTime(s.last_ok_at, now)}` : ""}
      </p>
      )}
      {jobs > 0 ? (
        <p
          className={`mt-1 text-label tabular-nums ${
            failures / jobs >= 0.5 ? "font-semibold text-up" : "text-ink-muted"
          }`}
        >
          Failures ({WINDOW_HOURS} h): {failures} of {jobs} ({Math.round((failures / jobs) * 100)}%)
        </p>
      ) : null}
      {s.paused ? (
        <p className="mt-2 text-label text-danger">
          {s.paused_reason === "auto"
            ? "Paused automatically after repeated failures. Resume once the site is back."
            : "Paused. Resume once the site is back."}
        </p>
      ) : null}
      {s.health_note ? (
        <p className={`mt-2 text-label ${s.paused ? "text-danger" : s.healthy ? "text-ink-muted" : "text-up"}`}>
          {s.health_note}
        </p>
      ) : null}

      <div className={`mt-3 grid gap-2 ${pause && canLearn ? "grid-cols-2" : "grid-cols-1"}`}>
        {canLearn ? (
          <Button
            variant="secondary"
            onClick={learn}
            loading={busy === "learn"}
            disabled={!s.admin_connected || learning}
            aria-label={cart ? `Learn ${name} cart` : `Learn ${label}`}
            aria-describedby={!s.admin_connected ? hintId : undefined}
          >
            Learn
          </Button>
        ) : null}
        {pause ? (
          <Button
            variant="secondary"
            onClick={s.paused ? resume : () => setConfirmPause(true)}
            loading={busy === "pause"}
            aria-label={s.paused ? `Resume ${name}` : `Pause ${name}`}
          >
            {s.paused ? "Resume" : "Pause"}
          </Button>
        ) : null}
      </div>
      {canLearn && !s.admin_connected ? (
        <p id={hintId} className="mt-2 text-label text-ink-muted">
          Connect {conn} on your own account first
        </p>
      ) : null}

      {jobId ? <LearnProgress job={job} site={s.site} cart={cart} onForbidden={onForbidden} /> : null}
      {error ? <Notice className="mt-3">{error}</Notice> : null}

      <ConfirmSheet
        open={confirmPause}
        title={`Pause ${name}?`}
        description={`Pauses ${name} for every user. Scheduled syncs and Refresh now stop until you resume it.`}
        confirmLabel={`Pause ${name}`}
        onConfirm={async () => {
          await pauseSite(s.site);
          onChanged();
        }}
        onClose={() => setConfirmPause(false)}
        onForbidden={onForbidden}
      />
    </Card>
  );
}

// ---- learn progress ---------------------------------------------------------------------

function entriesOf(job: Job): PostEntry[] {
  const raw = (job.progress as Record<string, unknown>).entries;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((e): e is Record<string, unknown> => Boolean(e) && typeof e === "object")
    .map((e) => toPermissionEntry(e, typeof e.op === "string" ? e.op : undefined));
}

const CART_WAITING = "Waiting for your approval in Approvals";

/** A finished cart learn: its note (what the probe added), then where the approval is. */
function cartLearnedLine(job: Job): string {
  const p = job.progress as Record<string, unknown>;
  const note = typeof p.note === "string" ? p.note : "";
  return note ? `${note} ${CART_WAITING}` : CART_WAITING;
}

function learnedLine(job: Job, cart = false): string {
  const p = job.progress as Record<string, unknown>;
  if (cart && (p.status === "pending" || p.stage === "learned")) return cartLearnedLine(job);
  if (p.status === "pending") return "Learned. Waiting for your review in Approvals.";
  if (typeof p.version === "number") return `Recipe updated to v${p.version}.`;
  return "Learn finished.";
}

function Status({ children, spinning = false, tone = "muted" }: { children: string; spinning?: boolean; tone?: "muted" | "ok" | "bad" }) {
  const color = tone === "ok" ? "text-accent" : tone === "bad" ? "text-danger" : "text-ink-muted";
  return (
    <p role="status" className={`mt-3 flex items-center gap-1.5 text-label ${color}`}>
      {spinning ? <Spinner className="animate-spin" /> : null}
      <span>{children}</span>
    </p>
  );
}

/** What a running learn job is doing; asks for approval inline when the job waits on one. */
function LearnProgress({
  job,
  site,
  cart = false,
  onForbidden,
}: {
  job: Job | null;
  site: string;
  cart?: boolean;
  onForbidden: () => void;
}) {
  const [decided, setDecided] = useState<{ id: string; decision: Decision } | null>(null);
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!job || job.status === "queued") return <Status spinning>Waiting for a worker…</Status>;
  if (job.status === "failed") return <Status tone="bad">{`Learn failed${job.error ? ` (${job.error})` : ""}.`}</Status>;
  if (job.status === "done") return <Status tone="ok">{learnedLine(job, cart)}</Status>;

  const stage = job.progress.stage;
  const approvalId = String((job.progress as Record<string, unknown>).approval_id ?? "");

  if (stage === "approval_needed" && approvalId && decided?.id !== approvalId) {
    const entries = entriesOf(job);
    const decide = async (decision: Decision) => {
      setBusy(decision);
      setError(null);
      try {
        await decideApproval(approvalId, decision);
        setDecided({ id: approvalId, decision });
      } catch (e) {
        if (isForbidden(e)) onForbidden();
        else setError(problem(e, "Couldn't send your decision. Try again."));
      } finally {
        setBusy(null);
      }
    };
    return (
      <div className="mt-3 rounded-control bg-up-soft p-3">
        <p className="text-label font-semibold text-ink">
          {cart
            ? `Allow ${entries.length === 1 ? "this request" : `these ${entries.length} requests`} on your cart?`
            : `Allow ${entries.length === 1 ? "this POST request" : `these ${entries.length} POST requests`}?`}
        </p>
        <p className="text-label text-ink-muted">The job waits for you, then gives up after a few minutes.</p>
        <ul aria-label="Requested POST requests" className="mt-2 flex flex-col gap-1.5">
          {entries.map((e, i) =>
            cart ? (
              <PermissionEntry key={`${e.url_regex}-${i}`} entry={e} />
            ) : (
              <li key={`${e.url_regex}-${i}`} className="rounded-tile bg-surface px-2.5 py-1.5">
                <code className="block font-mono text-label text-ink [overflow-wrap:anywhere]">{e.url_regex}</code>
                {e.why ? <span className="mt-0.5 block text-label text-ink-muted">{e.why}</span> : null}
              </li>
            ),
          )}
        </ul>
        {cart ? (
          <>
            <p className="mt-2 text-label font-medium text-ink">{NEVER_LINE(appName(site))}</p>
            {hasRiskyEntry(entries) ? <RiskWarning /> : null}
          </>
        ) : null}
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button onClick={() => decide("approve")} loading={busy === "approve"} disabled={busy !== null}>
            Approve
          </Button>
          <Button variant="danger" onClick={() => decide("reject")} loading={busy === "reject"} disabled={busy !== null}>
            Reject
          </Button>
        </div>
        {error ? <Notice className="mt-2">{error}</Notice> : null}
      </div>
    );
  }

  if (stage === "approval_needed") {
    // No approval row yet, or one this screen has already answered.
    if (!approvalId || decided?.id !== approvalId) return <Status spinning>Waiting for the approval…</Status>;
    return (
      <Status spinning>{decided.decision === "reject" ? "Rejected. Finishing without it…" : "Approved. Finishing…"}</Status>
    );
  }
  if (stage === "approval_received") return <Status spinning>Approved. Finishing…</Status>;
  if (stage === "learned") return <Status spinning>{learnedLine(job, cart)}</Status>;
  return <Status spinning>Learning…</Status>;
}
