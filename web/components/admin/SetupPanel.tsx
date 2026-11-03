"use client";

import { useEffect, useState } from "react";
import { Spinner } from "@/components/connect/icons";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  getJourney,
  problem,
  startSetup,
  startSetupAll,
  stopSetup,
  type Journey,
  type JourneyStep,
} from "@/lib/admin";
import { Badge, ListSkeleton, LoadError, Notice, SectionLabel, SiteMark, useAdminLoad, type PanelProps } from "./parts";

const POLL_MS = 4000;

function stepWords(step: JourneyStep | null): string {
  if (!step) return "All set";
  if (step.status === "running") return `${step.label}...`;
  if (step.status === "waiting_approval") return `${step.label}: needs your OK`;
  return step.label;
}

function StateBadge({ j }: { j: Journey }) {
  if (j.run?.status === "running") return <Badge tone="warn">Setting up</Badge>;
  if (j.run?.status === "paused") return <Badge tone="bad">Paused</Badge>;
  if (j.done === j.total) return <Badge tone="ok">Ready</Badge>;
  return null;
}

/** One-tap setup: each app's progress, what comes next, and a single button that does the rest. */
export function SetupPanel({ onForbidden }: PanelProps) {
  const journey = useAdminLoad("journey", getJourney, onForbidden);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { reload } = journey;
  const anyRunning = journey.status === "ready" && journey.data.some((j) => j.run?.status === "running");

  useEffect(() => {
    if (!anyRunning) return;
    const t = setInterval(reload, POLL_MS);
    return () => clearInterval(t);
  }, [anyRunning, reload]);

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await fn();
      reload();
    } catch (e) {
      setError(problem(e, "That did not work. Try again."));
    } finally {
      setBusy(null);
    }
  }

  if (journey.status === "loading") return <ListSkeleton rows={2} label="Setup" />;
  if (journey.status === "error") return <LoadError what="setup" onRetry={journey.reload} />;

  const allReady = journey.data.every((j) => j.done === j.total);
  return (
    <section aria-label="Set up your apps" className="mb-8">
      <div className="flex items-center justify-between gap-3">
        <SectionLabel>Set up your apps</SectionLabel>
        {allReady ? null : (
          <Button
            size="md"
            variant="secondary"
            aria-busy={busy === "all"}
            disabled={busy !== null}
            onClick={() => act("all", startSetupAll)}
          >
            Set up all apps
          </Button>
        )}
      </div>
      {error ? <Notice className="mt-3">{error}</Notice> : null}
      <ul className="mt-3 flex flex-col gap-3">
        {journey.data.map((j) => {
          const running = j.run?.status === "running";
          const message = j.run?.status === "paused" || j.run?.status === "done" ? j.run.message : null;
          return (
            <li key={j.site}>
              <Card className="flex flex-col gap-3">
                <div className="flex items-center gap-3">
                  <SiteMark site={j.site} />
                  <div className="min-w-0 flex-1">
                    <p className="text-headline">{j.name}</p>
                    <p className="text-label tabular-nums text-ink-muted">
                      {j.done} of {j.total} done{j.next ? ` · Next: ${stepWords(j.next)}` : ""}
                    </p>
                  </div>
                  <StateBadge j={j} />
                </div>
                <div
                  role="progressbar"
                  aria-label={`${j.name} setup`}
                  aria-valuemin={0}
                  aria-valuemax={j.total}
                  aria-valuenow={j.done}
                  className="h-1.5 w-full overflow-hidden rounded-full bg-line-soft"
                >
                  <div className="h-full rounded-full bg-accent" style={{ width: `${(j.done / j.total) * 100}%` }} />
                </div>
                {message ? <p className="text-body text-ink-muted">{message}</p> : null}
                {running && j.run?.message ? (
                  <p className="flex items-center gap-2 text-body text-ink-muted" role="status">
                    <Spinner className="size-4" /> {j.run.message}
                  </p>
                ) : null}
                {j.done === j.total ? null : (
                  <div className="flex flex-wrap gap-2">
                    {running ? (
                      <Button variant="ghost" disabled={busy !== null} onClick={() => act(j.site, () => stopSetup(j.site))}>
                        Stop
                      </Button>
                    ) : (
                      <Button
                        aria-busy={busy === j.site}
                        disabled={busy !== null}
                        onClick={() => act(j.site, () => startSetup(j.site))}
                      >
                        {j.run?.status === "paused" ? `Continue ${j.name}` : `Set up ${j.name}`}
                      </Button>
                    )}
                  </div>
                )}
              </Card>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
