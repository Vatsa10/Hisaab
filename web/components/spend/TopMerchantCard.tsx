import { useId } from "react";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import type { TopMerchant } from "@/lib/queries";
import { appLabel } from "./display";
import { AppDot } from "./parts";

/** "Top this year": the restaurant or store with the most spend, its amount and order count. */
export function TopMerchantCard({ top }: { top: TopMerchant }) {
  const headingId = useId();
  const app = appLabel(top.site);
  return (
    <Card as="section" aria-labelledby={headingId}>
      <h2 id={headingId} className="text-label text-ink-muted">
        Top this year
      </h2>
      <div className="mt-1.5 flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-body font-semibold">{top.merchant}</p>
          <p className="mt-0.5 flex items-center gap-1.5 text-label text-ink-muted">
            <AppDot className={app.dotClass} />
            {app.name}
          </p>
        </div>
        <p className="shrink-0 text-right">
          <Money amount={top.amount} className="block text-body font-semibold" />
          <span className="block text-label text-ink-muted">
            {top.orders} {top.orders === 1 ? "order" : "orders"}
          </span>
        </p>
      </div>
    </Card>
  );
}
