import type { ReactNode } from "react";
import { Card } from "@/components/ui/Card";

export interface EmptyStateProps {
  title: string;
  children: ReactNode;
  /** Decorative icon (24px, currentColor); shown in a soft green tile. */
  icon?: ReactNode;
  /** Usually a single Button or button-styled Link. */
  action?: ReactNode;
}

/** A card that says what will appear here and how to get it there. */
export function EmptyState({ title, children, icon, action }: EmptyStateProps) {
  return (
    <Card className="px-5 py-6">
      {icon ? (
        <div aria-hidden="true" className="mb-4 grid size-11 place-items-center rounded-control bg-accent-soft text-accent">
          {icon}
        </div>
      ) : null}
      <h2 className="text-headline">{title}</h2>
      <div className="mt-1 max-w-[34ch] text-body text-ink-muted">{children}</div>
      {action ? <div className="mt-5">{action}</div> : null}
    </Card>
  );
}
