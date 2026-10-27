import type { Metadata } from "next";
import Link from "next/link";
import { buttonClassName } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = { title: "Orders" };

export default function OrdersPage() {
  return (
    <>
      <PageHeader title="Orders" />
      <EmptyState
        title="No orders yet"
        icon={
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 3.5h12v17l-3-1.75-3 1.75-3-1.75-3 1.75z" />
            <path d="M9.5 8.5h5M9.5 12.5h5" />
          </svg>
        }
        action={
          <Link href="/accounts" className={buttonClassName({ fullWidth: true })}>
            Connect an app
          </Link>
        }
      >
        Every order from your connected apps shows up here, newest first, with what you bought.
      </EmptyState>
    </>
  );
}
