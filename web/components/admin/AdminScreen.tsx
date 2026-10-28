"use client";

import { useCallback, useState, type KeyboardEvent } from "react";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { ADMIN_ONLY } from "@/lib/admin";
import { cn } from "@/lib/cn";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { CostsPanel } from "./CostsPanel";
import { InvitesPanel } from "./InvitesPanel";
import { SitesPanel } from "./SitesPanel";

const TABS = [
  { id: "costs", label: "Costs" },
  { id: "sites", label: "Sites" },
  { id: "invites", label: "Invites" },
  { id: "approvals", label: "Approvals" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/**
 * The admin view: a segmented control over Costs · Sites · Invites · Approvals. Every panel reads
 * /admin/* with the admin's own token; the first 403 from any of them swaps the whole screen for
 * the admin-only message (the server is the gate, this only explains it).
 */
export function AdminScreen({ initialTab = "costs" }: { initialTab?: TabId }) {
  const [tab, setTab] = useState<TabId>(initialTab);
  const [forbidden, setForbidden] = useState(false);
  const onForbidden = useCallback(() => setForbidden(true), []);

  if (forbidden) {
    return (
      <>
        <PageHeader title="Admin" />
        <EmptyState
          title={ADMIN_ONLY}
          icon={
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="5" y="10.5" width="14" height="10" rx="2.5" />
              <path d="M8.5 10.5V7.75a3.5 3.5 0 0 1 7 0v2.75" />
            </svg>
          }
        >
          Your account can&apos;t open it. Your spending and apps are on the other tabs.
        </EmptyState>
      </>
    );
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = TABS.findIndex((t) => t.id === tab);
    const next = TABS[(i + step + TABS.length) % TABS.length]!.id;
    setTab(next);
    document.getElementById(`admin-tab-${next}`)?.focus();
  }

  return (
    <>
      <PageHeader title="Admin" subtitle="Costs, site health, invites and recipe approvals." />
      <div
        role="tablist"
        aria-label="Admin sections"
        onKeyDown={onKeyDown}
        className="mb-4 grid grid-cols-4 gap-1 rounded-control bg-line-soft p-1"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            id={`admin-tab-${t.id}`}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            aria-controls="admin-panel"
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            className={cn(
              "min-h-11 min-w-0 rounded-tile px-1 text-label font-semibold transition-[background-color,color,box-shadow] duration-150",
              tab === t.id ? "bg-surface text-ink shadow-card" : "text-ink-muted hover:text-ink",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div id="admin-panel" role="tabpanel" aria-labelledby={`admin-tab-${tab}`}>
        {tab === "costs" ? <CostsPanel onForbidden={onForbidden} /> : null}
        {tab === "sites" ? <SitesPanel onForbidden={onForbidden} /> : null}
        {tab === "invites" ? <InvitesPanel onForbidden={onForbidden} /> : null}
        {tab === "approvals" ? <ApprovalsPanel onForbidden={onForbidden} /> : null}
      </div>
    </>
  );
}
