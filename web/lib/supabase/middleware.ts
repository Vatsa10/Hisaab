import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { hasSupabaseEnv, supabaseAnonKey, supabaseUrl } from "@/lib/env";
import { LOGIN_PATH, isPublicPath, safeNextPath } from "@/lib/routes";

function redirect(request: NextRequest, to: string, from: NextResponse): NextResponse {
  const url = request.nextUrl.clone();
  const [pathname, query = ""] = to.split("?", 2);
  url.pathname = pathname ?? "/";
  url.search = query ? `?${query}` : "";
  const res = NextResponse.redirect(url);
  // Carry over any refreshed auth cookies so the session survives the redirect.
  for (const cookie of from.cookies.getAll()) res.cookies.set(cookie);
  return res;
}

/**
 * Refresh the Supabase session cookie on every request and gate the app: signed-out users on
 * an app route go to /login?next=<path>; signed-in users on /login go to their app. Without
 * Supabase env the app routes still redirect to /login (which explains the problem) instead of
 * crashing. Logs nothing: cookies and tokens never reach the console.
 */
export async function updateSession(request: NextRequest): Promise<NextResponse> {
  const { pathname, search } = request.nextUrl;
  const isPublic = isPublicPath(pathname);
  const loginWithNext = `${LOGIN_PATH}?next=${encodeURIComponent(pathname + search)}`;
  let response = NextResponse.next({ request });

  if (!hasSupabaseEnv()) {
    return isPublic ? response : redirect(request, loginWithNext, response);
  }

  const supabase = createServerClient(supabaseUrl(), supabaseAnonKey(), {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(toSet) {
        for (const { name, value } of toSet) request.cookies.set(name, value);
        response = NextResponse.next({ request });
        for (const { name, value, options } of toSet) response.cookies.set(name, value, options);
      },
    },
  });

  // getUser() asks Supabase Auth to validate the token; an unreachable Auth counts as signed out.
  let signedIn = false;
  try {
    const { data } = await supabase.auth.getUser();
    signedIn = Boolean(data.user);
  } catch {
    signedIn = false;
  }

  if (!signedIn && !isPublic) return redirect(request, loginWithNext, response);
  if (signedIn && pathname === LOGIN_PATH) {
    return redirect(request, safeNextPath(request.nextUrl.searchParams.get("next")), response);
  }
  return response;
}
