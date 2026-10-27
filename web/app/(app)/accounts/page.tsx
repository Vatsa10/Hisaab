import type { Metadata } from "next";
import { SignOutButton } from "@/components/auth/SignOutButton";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { APPS } from "@/lib/apps";
import { cn } from "@/lib/cn";
import { getSupabaseServerClient } from "@/lib/supabase/server";

export const metadata: Metadata = { title: "Accounts" };

export default async function AccountsPage() {
  const supabase = await getSupabaseServerClient();
  const { data } = await supabase.auth.getUser();
  const email = data.user?.email;

  return (
    <>
      <PageHeader title="Your apps" subtitle="Read-only. We never order or pay." />

      <Card as="section" aria-label="Delivery apps" padding="none">
        <ul className="divide-y divide-line-soft px-4">
          {APPS.map((app) => (
            <li key={app.id} className="flex min-h-14 items-center gap-3">
              <span aria-hidden="true" className={cn("size-7 shrink-0 rounded-tile", app.dotClass)} />
              <span className="flex-1 font-medium">{app.name}</span>
              <span className="text-label text-ink-muted">Not connected</span>
            </li>
          ))}
        </ul>
      </Card>
      <p className="mt-2 text-label text-ink-muted">
        Instamart orders come in with your Swiggy account.
      </p>

      <Card as="section" aria-label="Your account" className="mt-6">
        <p className="text-label text-ink-muted">Signed in as</p>
        <p className="mt-0.5 break-all font-medium">{email ?? "Unknown account"}</p>
        <div className="mt-4">
          <SignOutButton />
        </div>
      </Card>
    </>
  );
}
