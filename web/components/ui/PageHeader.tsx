import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface PageHeaderProps {
  title: string;
  /** Small line above the title, e.g. "October 2026". */
  eyebrow?: ReactNode;
  /** Line under the title, e.g. "Read-only. We never order or pay." */
  subtitle?: ReactNode;
  /** Right-aligned control next to the title (keep it 44px tall). */
  action?: ReactNode;
  className?: string;
}

/** The screen title block from the mockups: muted eyebrow, bold 24px title, optional subtitle. */
export function PageHeader({ title, eyebrow, subtitle, action, className }: PageHeaderProps) {
  return (
    <header className={cn("mb-4 flex items-end gap-3", className)}>
      <div className="min-w-0 flex-1">
        {eyebrow ? <p className="text-label text-ink-muted">{eyebrow}</p> : null}
        <h1 className="text-title">{title}</h1>
        {subtitle ? <p className="mt-0.5 text-label text-ink-muted">{subtitle}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </header>
  );
}
