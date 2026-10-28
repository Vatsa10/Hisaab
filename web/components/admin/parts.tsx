"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/spend/parts";
import { isForbidden } from "@/lib/admin";
import { cn } from "@/lib/cn";

export interface PanelProps {
  /** Called when the API answers 403: the screen swaps to the admin-only message. */
  onForbidden: () => void;
}

export type AdminLoad<T> =
  | { status: "loading"; reload: () => void }
  | { status: "error"; reload: () => void }
  | { status: "ready"; data: T; reload: () => void };

/**
 * Load a panel's data when `key` changes. `reload()` refreshes in place: the last data stays on
 * screen while the new answer is fetched, so an action never flashes the panel back to skeletons.
 * A 403 calls `onForbidden`. An answer for an older key or reload is dropped.
 */
export function useAdminLoad<T>(key: string, load: () => Promise<T>, onForbidden: () => void): AdminLoad<T> {
  const loadRef = useRef(load);
  const forbidRef = useRef(onForbidden);
  useEffect(() => {
    loadRef.current = load;
    forbidRef.current = onForbidden;
  });
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ key: string; data?: T; failed?: boolean }>({ key: "" });

  useEffect(() => {
    let live = true;
    loadRef.current().then(
      (data) => live && setState({ key, data }),
      (e: unknown) => {
        if (!live) return;
        if (isForbidden(e)) forbidRef.current();
        setState((s) => (s.key === key && s.data !== undefined ? s : { key, failed: true }));
      },
    );
    return () => {
      live = false;
    };
  }, [key, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  if (state.key === key && state.data !== undefined) return { status: "ready", data: state.data, reload };
  if (state.key === key && state.failed) return { status: "error", reload };
  return { status: "loading", reload };
}

export type Tone = "ok" | "warn" | "bad" | "neutral";

const TONES: Record<Tone, string> = {
  ok: "bg-accent-soft text-accent-strong",
  warn: "bg-up-soft text-up",
  bad: "bg-danger-soft text-danger",
  neutral: "bg-line-soft text-ink-muted",
};

/** Small status pill. Always carries a word, so colour is never the only signal. */
export function Badge({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-caption font-semibold",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Card of placeholder rows shaped like a list of two-line items. */
export function ListSkeleton({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <Card padding="none" aria-busy="true" aria-label={label}>
      <ul aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <li key={i} className="flex items-center gap-3 border-b border-line-soft px-4 py-3.5 last:border-b-0">
            <div className="min-w-0 flex-1">
              <Skeleton className="h-4 w-40 max-w-full" />
              <Skeleton className="mt-2 h-3 w-56 max-w-full" />
            </div>
            <Skeleton className="h-4 w-12" />
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** "Couldn't load …" with a retry, in a card. */
export function LoadError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <Card className="flex items-center justify-between gap-3">
      <p role="alert" className="text-body text-ink-muted">
        Couldn&apos;t load {what}.
      </p>
      <Button variant="ghost" className="-mr-3 shrink-0" onClick={onRetry}>
        Try again
      </Button>
    </Card>
  );
}

/** Inline error line under an action (announced). */
export function Notice({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p role="alert" className={cn("rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger", className)}>
      {children}
    </p>
  );
}

/** Muted section label above a card, iOS grouped-list style. */
export function SectionLabel({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-2 mt-6 flex items-baseline justify-between gap-3 px-1 first:mt-0">
      <h2 className="text-label font-semibold text-ink-muted">{children}</h2>
      {aside ? <span className="text-label text-ink-muted">{aside}</span> : null}
    </div>
  );
}
