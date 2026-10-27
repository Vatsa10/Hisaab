import type { Metadata } from "next";
import Link from "next/link";
import { buttonClassName } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = { title: "Spending" };

export default function SpendPage() {
  return (
    <>
      <PageHeader eyebrow="This month" title="Spending" />
      <EmptyState
        title="No spending yet"
        icon={
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round">
            <path d="M5 20v-7M12 20V5M19 20v-10" />
          </svg>
        }
        action={
          <Link href="/accounts" className={buttonClassName({ fullWidth: true })}>
            Connect an app
          </Link>
        }
      >
        Connect Swiggy, Zomato, Zepto or Blinkit and your orders appear here, month by month.
      </EmptyState>
    </>
  );
}
