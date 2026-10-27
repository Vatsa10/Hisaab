// Which paths need a signed-in user. Default-deny: everything is protected except the
// sign-in page and the OAuth/email callback.

export const HOME_PATH = "/spend";
export const LOGIN_PATH = "/login";

const PUBLIC_PATHS = [LOGIN_PATH, "/auth"];

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/**
 * A post-login redirect target taken from a query string. Only same-site absolute paths are
 * kept; protocol-relative ("//host"), backslash tricks, full URLs and auth pages fall back home.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return HOME_PATH;
  }
  const path = next.split(/[?#]/, 1)[0] ?? "";
  if (isPublicPath(path)) return HOME_PATH;
  return next;
}
