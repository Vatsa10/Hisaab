import { NextResponse, type NextRequest } from "next/server";
import { authErrorCode } from "@/lib/authErrors";
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
  const failure = (reason?: string) =>
    NextResponse.redirect(new URL(`${LOGIN_PATH}?error=${reason ?? "auth"}`, origin));

  // The sign-up hook's refusal (invite_only / signups_paused) comes back as an error on this URL.
  const refused = authErrorCode(searchParams.get("error_description") ?? searchParams.get("error"));
  if (refused) return failure(refused);

  if (code) {
    try {
      const supabase = await getSupabaseServerClient();
      const { error } = await supabase.auth.exchangeCodeForSession(code);
      if (!error) return NextResponse.redirect(new URL(next, origin));
      return failure(authErrorCode(error.message));
    } catch {
      // fall through to the login page with a generic message
    }
  }
  return failure();
}
