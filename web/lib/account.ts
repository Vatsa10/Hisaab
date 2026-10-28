import { ApiError, apiFetch } from "@/lib/api";
import { unsubscribePushLocal } from "@/lib/push";
import { clearPageCache } from "@/lib/pwa";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/** How often and how long to wait when the API says "not yet" (503 cool-down). */
export const DELETE_RETRIES = 3;
export const DELETE_RETRY_MS = 2000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** POST /connections/{site}/disconnect: deletes the app's session and its stored orders. */
export async function disconnectApp(site: string): Promise<{ orders_deleted: number }> {
  const res = await apiFetch<{ site: string; orders_deleted: number }>(`/connections/${site}/disconnect`, {
    method: "POST",
  });
  return { orders_deleted: res?.orders_deleted ?? 0 };
}

/** How many orders are stored for an app (Swiggy includes its Instamart orders); null if unknown. */
export async function countOrders(site: string): Promise<number | null> {
  try {
    const sites = site === "swiggy" ? ["swiggy", "instamart"] : [site];
    const { count, error } = await getSupabaseBrowserClient()
      .from("orders")
      .select("id", { count: "exact", head: true })
      .in("site", sites);
    return error || typeof count !== "number" ? null : count;
  } catch {
    return null;
  }
}

/**
 * DELETE /account. A 503 (a sync is winding down, or the cool-down) is retried up to 3 times, 2 s
 * apart; if it still fails the ApiError goes to the caller to show. On success: turn push off,
 * sign out, drop cached pages and go to /login?deleted=1.
 */
export async function deleteAccount(): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      await apiFetch("/account", { method: "DELETE", body: JSON.stringify({ confirm: "DELETE" }) });
      break;
    } catch (e) {
      if (e instanceof ApiError && e.status === 503 && attempt < DELETE_RETRIES) {
        await sleep(DELETE_RETRY_MS);
        continue;
      }
      throw e;
    }
  }
  try {
    await unsubscribePushLocal();
  } catch {
    // the account is gone; a stale subscription can't be reached anyway
  }
  try {
    await getSupabaseBrowserClient().auth.signOut();
  } catch {
    // the session cookie is dead with the user; carry on
  }
  await clearPageCache();
  window.location.assign("/login?deleted=1");
}
