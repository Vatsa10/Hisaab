import type { Metadata } from "next";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = { title: "Cart" };

export default function CartPage() {
  return (
    <>
      <PageHeader title="Cheapest cart" />
      <EmptyState
        title="Coming soon"
        icon={
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 4h2.2l2.3 11.1a1 1 0 0 0 1 .8h8.9a1 1 0 0 0 1-.78L20 8H6.1" />
            <circle cx="9.5" cy="19.5" r="1.25" />
            <circle cx="17" cy="19.5" r="1.25" />
          </svg>
        }
      >
        Price your usual grocery list on Zepto, Blinkit and Instamart at once, and see which app is
        cheapest with fees included.
      </EmptyState>
    </>
  );
}
