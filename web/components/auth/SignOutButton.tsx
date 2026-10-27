"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/** Signs out, drops cached pages (they may show this user's data) and returns to /login. */
export function SignOutButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function signOut() {
    setBusy(true);
    try {
      await getSupabaseBrowserClient().auth.signOut();
    } catch {
      // Even if Supabase is unreachable the local session cookie is cleared; carry on.
    }
    if ("caches" in window) {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith("pages-")).map((k) => caches.delete(k)));
    }
    router.replace("/login");
    router.refresh();
  }

  return (
    <Button variant="danger" fullWidth loading={busy} onClick={signOut}>
      Sign out
    </Button>
  );
}
