import type { CSSProperties, ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { cn } from "@/lib/cn";

/**
 * A grey placeholder block in the `line` neutral (#e3e5e8), dark enough to read on white cards.
 * Size it with classes; it pulses gently (reduced motion: still).
 */
export function Skeleton({ className, style }: { className?: string; style?: CSSProperties }) {
  return <span aria-hidden="true" style={style} className={cn("block animate-pulse rounded-tile bg-line", className)} />;
}

/** 8px app colour dot. */
export function AppDot({ className }: { className: string }) {
  return <span aria-hidden="true" data-app-dot="" className={cn("inline-block size-2 shrink-0 rounded-full", className)} />;
}

/** Small red pill for orders that didn't count as spend. */
export function VoidBadge({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex shrink-0 items-center rounded-full bg-danger-soft px-2 py-0.5 text-caption font-semibold text-danger">
      {children}
    </span>
  );
}

/** Right-pointing chevron that tells a row opens something. */
export function Chevron({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("shrink-0 text-line-field", className)}
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

/** A failed load: says what failed and offers one retry button. */
export function LoadError({ what, onRetry, className }: { what: string; onRetry: () => void; className?: string }) {
  return (
    <Card className={className}>
      <div role="alert">
        <p className="text-headline">Couldn&apos;t load {what}</p>
        <p className="mt-1 text-body text-ink-muted">Check your connection, then try again.</p>
      </div>
      <Button variant="secondary" className="mt-4" onClick={onRetry}>
        Try again
      </Button>
    </Card>
  );
}
