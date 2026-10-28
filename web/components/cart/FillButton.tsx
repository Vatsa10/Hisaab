"use client";

import Link from "next/link";
import { buttonClassName } from "@/components/ui/Button";
import type { FillOption } from "@/lib/fill";

export interface FillButtonProps {
  /** The server's state for this app; null until it has loaded. */
  option: FillOption | null;
  appName: string;
  /** Opens the confirm sheet (state ready). */
  onStart: () => void;
  /** Opens the live sheet (filling, filled, review). */
  onView: (fillId: string) => void;
}

/** The Fill button of a cheapest app: its label, state and sublabel come from the server. */
export function FillButton({ option, appName, onStart, onView }: FillButtonProps) {
  const label = option?.label ?? `Fill cart on ${appName}`;
  const sublabel = option?.sublabel ?? null;
  const subId = `fill-sub-${appName.toLowerCase()}`;
  const state = option?.state;

  if ((state === "connect" || state === "needs_login") && option?.href) {
    return (
      <div className="mt-3">
        <Link href={option.href} className={buttonClassName({ variant: "secondary", fullWidth: true })}>
          {label}
        </Link>
        {sublabel ? <p className="mt-1.5 text-center text-caption text-ink-muted">{sublabel}</p> : null}
      </div>
    );
  }

  if (state === "ready") {
    return (
      <div className="mt-3">
        <button
          type="button"
          onClick={onStart}
          aria-describedby={sublabel ? subId : undefined}
          className={buttonClassName({ fullWidth: true })}
        >
          {label}
        </button>
        {sublabel ? (
          <p id={subId} className="mt-1.5 text-center text-caption text-ink-muted">
            {sublabel}
          </p>
        ) : null}
      </div>
    );
  }

  if ((state === "filling" || state === "filled" || state === "review") && option?.fill_id) {
    const fillId = option.fill_id;
    return (
      <div className="mt-3">
        <button
          type="button"
          onClick={() => onView(fillId)}
          className={buttonClassName({ variant: "secondary", fullWidth: true })}
        >
          {label}
        </button>
      </div>
    );
  }

  // unavailable, paused, stale, limit, or not loaded yet: shown, not tappable, with the reason.
  return (
    <div className="mt-3">
      <button
        type="button"
        disabled
        aria-disabled="true"
        aria-describedby={sublabel ? subId : undefined}
        className={buttonClassName({ fullWidth: true })}
      >
        {label}
      </button>
      {sublabel ? (
        <p id={subId} className="mt-1.5 text-center text-caption text-ink-muted">
          {sublabel}
        </p>
      ) : null}
    </div>
  );
}
