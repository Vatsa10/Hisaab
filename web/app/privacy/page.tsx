import type { Metadata } from "next";
import { BackLink } from "@/components/account/BackLink";
import { Card } from "@/components/ui/Card";

export const metadata: Metadata = { title: "Privacy" };

const PRIVACY_SECTIONS: ReadonlyArray<{ heading: string; body: string }> = [
  {
    heading: "What we do",
    body: "Hisaab reads your order history and the prices of groceries you ask about. We never touch payment: we can't see or use your cards, UPI or wallets, and we never place an order.",
  },
  {
    heading: "What we read",
    body: "Your past orders on the apps you connect (Zepto, Blinkit, Swiggy with Instamart, Zomato): date, shop or restaurant, items, amounts and status. For price checks, live prices at your delivery area.",
  },
  {
    heading: "What we store",
    body: "Your orders, your delivery address and its map position, cart and price-check data, saved app addresses (read from the apps), notifications and push subscriptions, and an encrypted copy of each app's sign-in session so you don't need a new code every day. We never store the one-time codes you type. Your phone number is kept only while a sign-in is running. We keep your data until you delete it.",
  },
  {
    heading: "Filling a cart",
    body: 'Only when you tap "Fill cart", we add the items you chose to that app\'s cart. We never empty your cart, check out, pay, apply coupons or change addresses. You pay in the app yourself.',
  },
  {
    heading: "Who sees it",
    body: "Only you. We never sell your data. The admin view shows cost and usage per account, not your orders. Answers to your questions are written by Claude (Anthropic). We send it only the order and spend data needed to answer, never your app logins, phone or OTPs.",
  },
  {
    heading: "The risk",
    body: "Delivery apps may flag or briefly lock an account that is used automatically. We keep requests slow and back off when an app pushes back, but we can't rule it out.",
  },
  {
    heading: "Your controls",
    body: "Disconnect an app on the Accounts tab to delete its session and orders. Delete your account to remove everything, including your sign-in. Deletion is immediate; server logs keep only an anonymous id and expire within 30 days.",
  },
];

/** Public, static: readable signed out. */
export default function PrivacyPage() {
  return (
    <main className="px-safe mx-auto w-full max-w-[560px] pb-[calc(env(safe-area-inset-bottom)+2rem)] pt-[calc(env(safe-area-inset-top)+0.5rem)]">
      <BackLink />
      <header className="mb-5 mt-2">
        <h1 className="text-title">Privacy</h1>
        <p className="mt-0.5 text-label text-ink-muted">Updated 27 October 2026</p>
      </header>

      <div className="flex flex-col gap-3">
        {PRIVACY_SECTIONS.map((s, i) => (
          <Card as="section" key={s.heading} aria-labelledby={`p-${i}`}>
            <h2 id={`p-${i}`} className="text-headline">
              {s.heading}
            </h2>
            <p className="mt-1.5 max-w-[65ch] text-body text-ink-muted">{s.body}</p>
          </Card>
        ))}
      </div>
    </main>
  );
}
