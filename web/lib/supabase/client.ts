import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";
import { supabaseAnonKey, supabaseUrl } from "@/lib/env";

let client: SupabaseClient | undefined;

/**
 * The browser Supabase client (anon key + the user's session cookie). Created on first use so
 * importing this module never needs env. The session lives in cookies managed by @supabase/ssr;
 * nothing else (no phone numbers, no OTPs) is ever written to browser storage.
 */
export function getSupabaseBrowserClient(): SupabaseClient {
  client ??= createBrowserClient(supabaseUrl(), supabaseAnonKey());
  return client;
}
