"use client";

import type { ReactNode } from "react";
import type { AppInfo } from "@/lib/apps";
import { cn } from "@/lib/cn";
import type { Connection } from "@/lib/jobs";
import { relativeTime } from "./copy";
import { RefreshIcon, Spinner } from "./icons";

export type RowState = "connect" | "synced" | "syncing" | "needs_login";

/** What a row shows, from the connection row and whether a job for the app is running. */
export function rowState(connection: Connection | undefined, busy = false): RowState {
  if (busy || connection?.status === "syncing") return "syncing";
  if (connection?.status === "needs_login") return "needs_login";
  if (connection?.status === "connected") return "synced";
  // "error" keeps showing the last good sync if there was one; a connect that never worked is
  // simply not connected yet.
  if (connection?.status === "error" && connection.last_synced_at) return "synced";
  return "connect";
}

export interface AppRowProps {
  app: AppInfo;
  connection?: Connection;
  /** A job for this app is queued or running (a refresh, or a connect from this device). */
  busy?: boolean;
  /** Tapping a busy row reopens its connect flow (when there is one to reopen). */
  onOpenJob?: (app: AppInfo) => void;
  onConnect: (app: AppInfo) => void;
  onRefresh: (app: AppInfo) => void;
  refreshing?: boolean;
  /** Manual refreshes left today for this app; unknown (undefined) until the limits load. */
  refreshLeft?: number;
  /** Clock for "Synced 2h ago" (the screen ticks it every minute). */
  now?: number;
}

// The separator starts after the logo, iOS style, and the last row has none.
const INNER = "flex min-h-14 min-w-0 flex-1 items-center gap-3 border-b border-line-soft group-last:border-b-0";
const PRESSABLE = "flex w-full items-center gap-3 pl-4 text-left transition-colors duration-150 active:bg-canvas";

function Logo({ app }: { app: AppInfo }) {
  return <span aria-hidden="true" className={cn("size-8 shrink-0 rounded-tile", app.dotClass)} />;
}

function Name({ children }: { children: ReactNode }) {
  return <span className="min-w-0 flex-1 truncate font-medium">{children}</span>;
}

/** One app on the Accounts screen: logo, name and its state, with the action that state needs. */
export function AppRow({ app, connection, busy, onOpenJob, onConnect, onRefresh, refreshing, refreshLeft, now }: AppRowProps) {
  const state = rowState(connection, busy);

  if (state === "connect" || state === "needs_login") {
    const reconnect = state === "needs_login";
    return (
      <li className="group">
        <button
          type="button"
          onClick={() => onConnect(app)}
          aria-label={reconnect ? `Reconnect ${app.name}, needs login` : `Connect ${app.name}`}
          className={PRESSABLE}
        >
          <Logo app={app} />
          <span className={cn(INNER, "pr-4")}>
            <Name>{app.name}</Name>
            {reconnect ? (
              <span className="flex items-center gap-1 text-label font-semibold text-up">
                <span>Needs login</span>
                <span aria-hidden="true">›</span>
              </span>
            ) : (
              <span className="text-label font-semibold text-accent">Connect ›</span>
            )}
          </span>
        </button>
      </li>
    );
  }

  if (state === "syncing") {
    const body = (
      <>
        <Logo app={app} />
        <span className={cn(INNER, "pr-4")}>
          <Name>{app.name}</Name>
          <span className="flex items-center gap-1.5 text-label text-ink-muted">
            <Spinner className="animate-spin" />
            <span>Syncing…</span>
          </span>
        </span>
      </>
    );
    return (
      <li className="group">
        {onOpenJob ? (
          <button
            type="button"
            onClick={() => onOpenJob(app)}
            aria-label={`${app.name}, syncing. Show progress`}
            className={PRESSABLE}
          >
            {body}
          </button>
        ) : (
          <div className="flex items-center gap-3 pl-4">{body}</div>
        )}
      </li>
    );
  }

  const synced = connection?.last_synced_at
    ? `✓ Synced ${relativeTime(connection.last_synced_at, now)}`
    : "✓ Connected";
  const known = refreshLeft !== undefined;
  const exhausted = known && refreshLeft <= 0;
  const refreshLabel = !known
    ? `Refresh ${app.name} now`
    : exhausted
      ? "No refreshes left today"
      : `Refresh now · ${refreshLeft} left today`;
  return (
    <li className="group flex items-center gap-3 pl-4">
      <Logo app={app} />
      <span className={cn(INNER, "pr-1.5")}>
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium">{app.name}</span>
          {known ? (
            <span className="block text-label text-ink-muted">
              {exhausted ? "No refreshes left today" : `${refreshLeft} refresh${refreshLeft === 1 ? "" : "es"} left today`}
            </span>
          ) : null}
        </span>
        <span className="text-label font-semibold text-accent" suppressHydrationWarning>
          {synced}
        </span>
        <button
          type="button"
          onClick={() => onRefresh(app)}
          disabled={refreshing || exhausted}
          aria-label={refreshLabel}
          title={refreshLabel}
          className="grid size-11 shrink-0 place-items-center rounded-full text-ink-muted transition-colors duration-150 hover:bg-canvas hover:text-ink active:bg-line-soft disabled:opacity-50"
        >
          <RefreshIcon size={20} className={refreshing ? "animate-spin" : undefined} />
        </button>
      </span>
    </li>
  );
}
