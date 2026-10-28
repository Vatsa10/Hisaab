"use client";

import { useEffect, useRef, useState } from "react";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { cn } from "@/lib/cn";
import { ACTIVE_STATUSES, watchFill, type CartFill, type FillItem, type FillPlanItem } from "@/lib/fill";
import { watchJob, type Job } from "@/lib/jobs";
import { siteName } from "@/lib/cart";
import { FillQuestion } from "./FillQuestion";
import { FillResult } from "./FillResult";
import { RepriceNotice } from "./RepriceNotice";

export interface FillSheetProps {
  /** The fill to follow; null keeps the sheet closed. */
  fillId: string | null;
  onClose: () => void;
  /** A confirmed reprice started a new fill: follow that one. */
  onSwitch: (fillId: string) => void;
  /** The fill row changed (new status): the Fill buttons should reload. */
  onChanged: () => void;
}

type RowState = { text: string; tone: "done" | "wait" | "warn" | "active" };

function rowState(item: FillItem, adding: boolean, firstPending: boolean): RowState {
  switch (item.status) {
    case "added": {
      const n = item.added_qty ?? item.packs ?? 1;
      return { text: n > 1 ? `Added ×${n}` : "Added", tone: "done" };
    }
    case "partial":
      return { text: `Only ${item.added_qty ?? 0} of ${item.packs ?? 1} added`, tone: "warn" };
    case "out_of_stock":
      return { text: "Out of stock", tone: "warn" };
    case "not_added":
      return { text: "Not added", tone: "warn" };
    case "skipped":
      return { text: "Skipped", tone: "wait" };
    default:
      return adding && firstPending ? { text: "Adding…", tone: "active" } : { text: "Waiting", tone: "wait" };
  }
}

function fromPlan(p: FillPlanItem): FillItem {
  return {
    idx: p.idx,
    label: p.label,
    name: p.name,
    product_id: p.product_id,
    packs: p.packs,
    status: p.action === "skip" ? "skipped" : "pending",
    added_qty: null,
    skip_reason: null,
  };
}

function stageLine(fill: CartFill, job: Job | null, app: string): string {
  const stage = job?.progress.stage;
  const area = fill.confirm_location?.area ?? fill.priced_location?.area ?? "your area";
  if (stage === "selecting_location") return `Setting ${app}'s location to ${area}…`;
  if (stage === "repricing") return `Checking prices at ${app}'s location…`;
  if (stage === "adding" || stage === "question_needed" || stage === "answer_received") {
    const total = job?.progress.total ?? fill.items.length;
    const done = job?.progress.done ?? fill.items.filter((i) => i.status !== "pending").length;
    return `Adding items · ${done} of ${total}`;
  }
  return `Checking ${app}'s delivery location…`;
}

function Check({ tone }: { tone: RowState["tone"] }) {
  if (tone === "done")
    return (
      <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" className="shrink-0 text-accent">
        <path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  if (tone === "warn")
    return (
      <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" className="shrink-0 text-up">
        <path d="M12 7v6M12 17h.01" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" />
      </svg>
    );
  return (
    <span
      aria-hidden="true"
      className={cn(
        "mx-[2px] size-3.5 shrink-0 rounded-full border-2",
        tone === "active" ? "animate-pulse border-accent motion-reduce:animate-none" : "border-line",
      )}
    />
  );
}

export function fillTitle(fill: CartFill | null): string {
  if (!fill) return "Your cart";
  const app = siteName(fill.site);
  if (ACTIVE_STATUSES.has(fill.status)) return `Filling your ${app} cart…`;
  if (fill.status === "needs_confirmation" && fill.summary) return `${app} prices changed`;
  return `Your ${app} cart`;
}

/** The sheet's content for one fill row (and its job, while it runs). Pure: no fetching. */
export function FillBody({
  fill,
  job,
  onSwitch,
  onDone,
}: {
  fill: CartFill | null;
  job: Job | null;
  onSwitch: (fillId: string) => void;
  onDone: () => void;
}) {
  const app = fill ? siteName(fill.site) : "";
  const summary = fill?.summary ?? null;
  const active = fill ? ACTIVE_STATUSES.has(fill.status) : false;
  const reprice = fill?.status === "needs_confirmation" && summary;
  const finished = fill !== null && !active && fill.status !== "needs_confirmation";
  let body;
  if (!fill) {
    body = (
      <div aria-busy="true" aria-label="Loading" className="space-y-3 py-1">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-5 animate-pulse rounded-tile bg-line-soft motion-reduce:animate-none" />
        ))}
      </div>
    );
  } else if (active) {
    const items = fill.items.length ? fill.items : fill.plan.map(fromPlan);
    const adding = job?.progress.stage === "adding" || job?.progress.stage === "answer_received";
    const firstPending = items.find((i) => i.status === "pending")?.idx;
    const asking = job?.progress.stage === "question_needed" && job.progress.question && job.id;
    body = (
      <div>
        <p role="status" className="text-body font-medium">
          {stageLine(fill, job, app)}
        </p>
        <ul className="mt-2 divide-y divide-line-soft">
          {items.map((item) => {
            const st = rowState(item, adding, item.idx === firstPending);
            return (
              <li key={item.idx} className="flex min-h-11 items-center gap-3 py-2 text-body">
                <Check tone={st.tone} />
                <span className="min-w-0 flex-1 break-words">{item.name || item.label}</span>
                <span
                  className={cn(
                    "shrink-0 text-label",
                    st.tone === "warn" ? "text-up" : st.tone === "done" ? "text-accent-strong" : "text-ink-muted",
                  )}
                >
                  {st.text}
                </span>
              </li>
            );
          })}
        </ul>
        {asking ? (
          <div className="mt-3">
            <FillQuestion
              key={`${job.id}-${job.progress.question}-${job.progress.prompt_at ?? ""}`}
              jobId={job.id}
              question={job.progress.question ?? ""}
              choices={job.progress.choices ?? []}
              askedAt={job.progress.prompt_at}
            />
          </div>
        ) : null}
        <p className="mt-4 text-center text-caption text-ink-muted">
          You can leave this screen. We&apos;ll notify you when it&apos;s done.
        </p>
      </div>
    );
  } else if (reprice && summary) {
    body = (
      <RepriceNotice
        fill={fill}
        summary={summary}
        appName={app}
        onConfirmed={onSwitch}
        onNotNow={onDone}
      />
    );
  } else if (finished && summary) {
    body = <FillResult summary={summary} error={fill.error} onDone={onDone} />;
  } else {
    body = (
      <div aria-busy="true" aria-label="Loading" className="space-y-3 py-1">
        <div className="h-5 animate-pulse rounded-tile bg-line-soft motion-reduce:animate-none" />
        <div className="h-5 w-2/3 animate-pulse rounded-tile bg-line-soft motion-reduce:animate-none" />
      </div>
    );
  }

  return body;
}

/** One fill, live: progress rows while it runs, a question when the app has several answers, then the result. */
export function FillSheet({ fillId, onClose, onSwitch, onChanged }: FillSheetProps) {
  const ref = useRef<SheetHandle>(null);
  const [fill, setFill] = useState<CartFill | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const changedRef = useRef(onChanged);
  const lastStatus = useRef<string | null>(null);
  const jobId = fill?.job_id ?? null;
  const active = fill ? ACTIVE_STATUSES.has(fill.status) : false;

  useEffect(() => {
    changedRef.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    setFill(null);
    setJob(null);
    lastStatus.current = null;
    if (!fillId) return;
    return watchFill(fillId, (f) => {
      setFill(f);
      if (lastStatus.current !== f.status) {
        lastStatus.current = f.status;
        changedRef.current();
      }
    });
  }, [fillId]);

  useEffect(() => {
    setJob(null);
    if (!jobId || !active) return;
    return watchJob(jobId, setJob);
  }, [jobId, active]);

  if (!fillId) return null;

  return (
    <Sheet ref={ref} open title={fillTitle(fill)} onClose={onClose}>
      <FillBody fill={fill} job={job} onSwitch={onSwitch} onDone={() => ref.current?.close()} />
    </Sheet>
  );
}
