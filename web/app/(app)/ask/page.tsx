import type { Metadata } from "next";
import Link from "next/link";
import { buttonClassName } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = { title: "Ask" };

export default function AskPage() {
  return (
    <>
      <PageHeader title="Ask" />
      <EmptyState
        title="Ask about your spending"
        icon={
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4.2A7.5 7.5 0 1 1 20 11.5z" />
          </svg>
        }
        action={
          <Link href="/accounts" className={buttonClassName({ variant: "secondary", fullWidth: true })}>
            Connect an app first
          </Link>
        }
      >
        Questions like &ldquo;How much did I spend on food in July?&rdquo; get answered from your own
        orders once they are synced.
      </EmptyState>
    </>
  );
}
