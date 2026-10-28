import type { Metadata } from "next";
import { NotificationsCard } from "@/components/account/NotificationsCard";
import { SignOutButton } from "@/components/auth/SignOutButton";
import { AccountsScreen, type ConsentRow } from "@/components/connect/AccountsScreen";
import type { Profile } from "@/components/connect/AddressRow";
import { Card } from "@/components/ui/Card";
import { CONNECTION_COLUMNS, JOB_COLUMNS, type Connection, type Job } from "@/lib/jobs";
import { getSupabaseServerClient } from "@/lib/supabase/server";

export const metadata: Metadata = { title: "Accounts" };

/**
 * Accounts: the first paint comes from the server (the user's own rows, through RLS), then
 * AccountsScreen keeps connections and jobs live over Realtime.
 */
export default async function AccountsPage() {
  const supabase = await getSupabaseServerClient();
  const { data } = await supabase.auth.getUser();
  const email = data.user?.email;

  // Each read degrades to "nothing yet" on error; the live subscription fills in the rest.
  const [connections, consents, profile, jobs] = await Promise.all([
    supabase.from("connections").select(CONNECTION_COLUMNS),
    supabase.from("consents").select("site, text_version"),
    supabase.from("profiles").select("address, lat, lon").maybeSingle(),
    supabase
      .from("jobs")
      .select(JOB_COLUMNS)
      .in("status", ["queued", "running"])
      .order("created_at", { ascending: true }),
  ]);

  return (
    <>
      <AccountsScreen
        initialConnections={(connections.data as Connection[] | null) ?? []}
        initialConsents={(consents.data as ConsentRow[] | null) ?? []}
        initialProfile={(profile.data as Profile | null) ?? null}
        initialJobs={(jobs.data as Job[] | null) ?? []}
      />

      <NotificationsCard />

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
