"use client";

import { useCallback, useState } from "react";
import { relativeTime } from "@/components/connect/copy";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  appName,
  decideApproval,
  getApprovals,
  isForbidden,
  problem,
  proposedEntries,
  reasonLabel,
  statusLabel,
  type Approval,
  type Decision,
  type DiffEntry,
} from "@/lib/admin";
import { RecipeDiff } from "./RecipeDiff";
import {
  Badge,
  ListSkeleton,
  LoadError,
  Notice,
  SectionLabel,
  SiteMark,
  useAdminLoad,
  type PanelProps,
  type Tone,
} from "./parts";

const HISTORY = 20;

/** The change set to show: the stored diff, or (for a bare POST request) the entries it adds. */
function changesOf(a: Approval): DiffEntry[] {
  if (a.diff && a.diff.length > 0) return a.diff;
  return proposedEntries(a).map((e, i) => ({
    op: "add",
    path: `$.read_post_allowlist[${i}].url_regex`,
    new: e.url_regex,
  }));
}

function statusTone(status: string): Tone {
  if (status === "approved" || status === "auto_applied") return "ok";
  if (status === "rejected") return "bad";
  return "neutral";
}

function Title({ a }: { a: Approval }) {
  return (
    <p className="flex min-w-0 items-baseline gap-2">
      <span className="truncate font-semibold">{appName(a.site)}</span>
      <span className="text-label text-ink-muted">{a.kind}</span>
    </p>
  );
}

/** Approvals tab: pending recipe changes to approve or reject, then the latest decisions. */
export function ApprovalsPanel({ onForbidden }: PanelProps) {
  const pending = useAdminLoad("pending", () => getApprovals("pending"), onForbidden);
  const all = useAdminLoad("all", () => getApprovals("all"), onForbidden);
  const reloadPending = pending.reload;
  const reloadAll = all.reload;
  const reload = useCallback(() => {
    reloadPending();
    reloadAll();
  }, [reloadPending, reloadAll]);

  const history =
    all.status === "ready"
      ? all.data
          .filter((a) => a.status !== "pending")
          .sort((x, y) => Date.parse(y.decided_at ?? y.created_at) - Date.parse(x.decided_at ?? x.created_at))
          .slice(0, HISTORY)
      : [];

  return (
    <div>
      <SectionLabel aside={pending.status === "ready" && pending.data.length ? pending.data.length : undefined}>
        Waiting for you
      </SectionLabel>
      {pending.status === "loading" ? (
        <ListSkeleton rows={2} label="Pending approvals" />
      ) : pending.status === "error" ? (
        <LoadError what="approvals" onRetry={pending.reload} />
      ) : pending.data.length === 0 ? (
        <Card>
          <p className="text-body text-ink-muted">Nothing to review. Recipe changes that need you show up here.</p>
        </Card>
      ) : (
        <ul aria-label="Pending approvals" className="flex flex-col gap-3">
          {pending.data.map((a) => (
            <PendingCard key={a.id} approval={a} onDecided={reload} onForbidden={onForbidden} />
          ))}
        </ul>
      )}

      <SectionLabel>Recent decisions</SectionLabel>
      {all.status === "loading" ? (
        <ListSkeleton rows={3} label="Recent decisions" />
      ) : all.status === "error" ? (
        <LoadError what="decisions" onRetry={all.reload} />
      ) : history.length === 0 ? (
        <Card>
          <p className="text-body text-ink-muted">No decisions yet.</p>
        </Card>
      ) : (
        <Card padding="none">
          <ul aria-label="Recent decisions">
            {history.map((a) => (
              <li key={a.id} data-id={a.id} className="flex items-center gap-3 border-b border-line-soft px-4 py-3 last:border-b-0">
                <SiteMark site={a.site} />
                <div className="min-w-0 flex-1">
                  <Title a={a} />
                  <p className="mt-0.5 text-label text-ink-muted" suppressHydrationWarning>
                    {reasonLabel(a.reason)}
                    {a.decided_at ? ` · ${relativeTime(a.decided_at)}` : ""}
                  </p>
                </div>
                <Badge tone={statusTone(a.status)}>{statusLabel(a.status)}</Badge>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function PendingCard({
  approval: a,
  onDecided,
  onForbidden,
}: {
  approval: Approval;
  onDecided: () => void;
  onForbidden: () => void;
}) {
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: Decision) {
    setBusy(decision);
    setError(null);
    try {
      await decideApproval(a.id, decision);
      onDecided();
    } catch (e) {
      if (isForbidden(e)) onForbidden();
      else setError(problem(e, "Couldn't save your decision. Try again."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card as="li" aria-label={`${appName(a.site)} ${a.kind}, ${reasonLabel(a.reason)}`}>
      <div className="flex items-center gap-3">
        <SiteMark site={a.site} />
        <div className="min-w-0 flex-1">
          <p className="flex items-baseline gap-2">
            <span className="truncate text-headline">{appName(a.site)}</span>
            <span className="text-label text-ink-muted">{a.kind}</span>
          </p>
          <p className="text-label text-ink-muted" suppressHydrationWarning>
            {relativeTime(a.created_at)}
          </p>
        </div>
      </div>
      <p className="mt-3 text-label font-medium text-ink">{reasonLabel(a.reason)}</p>
      <RecipeDiff diff={changesOf(a)} className="mt-3" />
      <div className="mt-4 grid grid-cols-2 gap-2">
        <Button onClick={() => decide("approve")} loading={busy === "approve"} disabled={busy !== null}>
          Approve
        </Button>
        <Button variant="danger" onClick={() => decide("reject")} loading={busy === "reject"} disabled={busy !== null}>
          Reject
        </Button>
      </div>
      {error ? <Notice className="mt-3">{error}</Notice> : null}
    </Card>
  );
}
