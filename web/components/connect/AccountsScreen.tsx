"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ApiError, apiFetch } from "@/lib/api";
import { APPS, type AppInfo, type SiteId } from "@/lib/apps";
import { isTerminal, watchConnections, watchJob, type Connection, type Job } from "@/lib/jobs";
import { AddressRow, type Profile } from "./AddressRow";
import { AppRow, rowState } from "./AppRow";
import { ConnectFlow } from "./ConnectFlow";
import { CONSENT_VERSION } from "./copy";

export interface ConsentRow {
  site: string;
  text_version: string;
}

export interface AccountsScreenProps {
  initialConnections: Connection[];
  initialConsents: ConsentRow[];
  initialProfile: Profile | null;
  /** The user's queued or running jobs when the page was rendered. */
  initialJobs: Job[];
}

function refreshProblem(e: unknown, app: AppInfo): string {
  if (e instanceof ApiError) {
    if (e.status === 409) return `${app.name} is already syncing.`;
    if (e.status === 404) return `Connect ${app.name} first.`;
    if (e.status === 429) return `You've used today's refreshes for ${app.name}. It syncs on its own once a day.`;
    if (e.status === 0) return e.detail;
  }
  return `Couldn't refresh ${app.name}. Try again in a bit.`;
}

const byId = (id: string): AppInfo | undefined => APPS.find((a) => a.id === id);

/**
 * The Accounts screen: one row per delivery app with its live state, the per-app connect flow,
 * "Refresh now" for connected apps, and the delivery address. Connection rows update live over
 * Realtime, so a connect started on another device shows up here without a reload.
 */
export function AccountsScreen({ initialConnections, initialConsents, initialProfile, initialJobs }: AccountsScreenProps) {
  const [connections, setConnections] = useState<Connection[]>(initialConnections);
  const [consented, setConsented] = useState<ReadonlySet<string>>(
    () => new Set(initialConsents.filter((c) => c.text_version === CONSENT_VERSION).map((c) => c.site)),
  );
  const [profile, setProfile] = useState<Profile | null>(initialProfile);
  // The latest job per app that this screen is following (refreshes, connects, ones found at load).
  const [jobs, setJobs] = useState<Record<string, Job>>(() =>
    Object.fromEntries(initialJobs.filter((j) => !isTerminal(j)).map((j) => [j.site, j])),
  );
  const [flow, setFlow] = useState<{ site: SiteId; jobId?: string } | null>(null);
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => watchConnections(setConnections), []);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);

  // Follow every unfinished job until it ends, one channel each. Channels are diffed: a job that
  // appears gets a channel, one that finishes loses its own, and the others are left alone.
  const watching = useRef(new Map<string, () => void>());
  const runningIds = Object.values(jobs)
    .filter((j) => !isTerminal(j))
    .map((j) => j.id)
    .sort()
    .join(",");
  useEffect(() => {
    const want = new Set(runningIds ? runningIds.split(",") : []);
    const subs = watching.current;
    for (const [id, stop] of subs) {
      if (!want.has(id)) {
        stop();
        subs.delete(id);
      }
    }
    for (const id of want) {
      if (subs.has(id)) continue;
      subs.set(
        id,
        watchJob(id, (job) => {
          setJobs((all) => (all[job.site]?.id === job.id ? { ...all, [job.site]: job } : all));
          if (job.type === "sync" && job.status === "failed" && job.error !== "needs_login") {
            const app = byId(job.site);
            if (app) setNotice(`${app.name} didn't refresh. Try again in a bit.`);
          }
        }),
      );
    }
  }, [runningIds]);
  useEffect(() => {
    const subs = watching.current;
    return () => {
      for (const stop of subs.values()) stop();
      subs.clear();
    };
  }, []);

  const connectionFor = useMemo(() => {
    const map = new Map<string, Connection>();
    for (const c of connections) map.set(c.site, c);
    return map;
  }, [connections]);

  const isBusy = (site: string) => {
    const job = jobs[site];
    return Boolean(job && !isTerminal(job));
  };

  function track(app: AppInfo, id: string, type: "connect" | "sync") {
    setJobs((all) => ({ ...all, [app.id]: { id, site: app.id, type, status: "queued", progress: {}, error: null } }));
  }

  function openConnect(app: AppInfo) {
    setNotice(null);
    setFlow({ site: app.id });
  }

  function openJob(app: AppInfo) {
    const job = jobs[app.id];
    if (job && job.type === "connect" && !isTerminal(job)) setFlow({ site: app.id, jobId: job.id });
  }

  async function refresh(app: AppInfo) {
    setRefreshing(app.id);
    setNotice(null);
    try {
      const res = await apiFetch<{ job_id: string }>(`/connections/${app.id}/refresh`, { method: "POST" });
      track(app, res.job_id, "sync");
    } catch (e) {
      setNotice(refreshProblem(e, app));
    } finally {
      setRefreshing(null);
    }
  }

  /** The next app in list order (wrapping) that is neither connected nor syncing. */
  function nextAfter(site: string): AppInfo | undefined {
    const start = APPS.findIndex((a) => a.id === site);
    for (let i = 1; i < APPS.length; i++) {
      const app = APPS[(start + i) % APPS.length]!;
      const state = rowState(connectionFor.get(app.id), isBusy(app.id));
      if (state === "connect" || state === "needs_login") return app;
    }
    return undefined;
  }

  const flowApp = flow ? byId(flow.site) : undefined;

  return (
    <>
      <PageHeader title="Your apps" subtitle="Read-only. We never order or pay." />

      <Card as="section" aria-label="Delivery apps" padding="none">
        <ul>
          {APPS.map((app) => {
            const job = jobs[app.id];
            return (
              <AppRow
                key={app.id}
                app={app}
                connection={connectionFor.get(app.id)}
                busy={isBusy(app.id)}
                onOpenJob={job?.type === "connect" && !isTerminal(job) ? openJob : undefined}
                onConnect={openConnect}
                onRefresh={refresh}
                refreshing={refreshing === app.id}
                now={now}
              />
            );
          })}
        </ul>
      </Card>
      <p className="mt-2 text-label text-ink-muted">Instamart orders come in with your Swiggy account.</p>
      {notice ? (
        <p role="alert" className="mt-3 rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
          {notice}
        </p>
      ) : null}

      <AddressRow profile={profile} onSaved={setProfile} />

      {flow && flowApp ? (
        <ConnectFlow
          app={flowApp}
          consented={consented.has(flowApp.id)}
          onConsented={(app) => setConsented((s) => new Set(s).add(app.id))}
          attachJobId={flow.jobId}
          onJobStarted={(app, id) => track(app, id, "connect")}
          onClose={() => setFlow(null)}
          onConnectNext={(app) => setFlow({ site: app.id })}
          nextApp={nextAfter(flowApp.id)}
        />
      ) : null}
    </>
  );
}
