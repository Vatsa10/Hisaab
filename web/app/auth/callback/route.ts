import { NextResponse, type NextRequest } from "next/server";
import { LOGIN_PATH, safeNextPath } from "@/lib/routes";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Landing point for Google OAuth and email-confirmation links (PKCE). Exchanges the one-time
 * `code` for a session cookie and sends the user on to `next` (same-site paths only).
 * Neither the code nor any token is logged.
 */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const next = safeNextPath(searchParams.get("next"));

  if (code) {
    try {
      const supabase = await getSupabaseServerClient();
      const { error } = await supabase.auth.exchangeCodeForSession(code);
      if (!error) return NextResponse.redirect(new URL(next, origin));
    } catch {
      // fall through to the login page with a generic message
    }
  }
  return NextResponse.redirect(new URL(`${LOGIN_PATH}?error=auth`, origin));
}
