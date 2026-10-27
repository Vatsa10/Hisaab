// Which paths need a signed-in user. Default-deny: everything is protected except the
// sign-in page and the OAuth/email callback.

export const HOME_PATH = "/spend";
export const LOGIN_PATH = "/login";

const PUBLIC_PATHS = [LOGIN_PATH, "/auth"];

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

// Whitespace and control characters (URL parsers silently drop tab, CR and LF, which turns
// "/\t/evil.example" into "//evil.example"), plus slashes and backslashes that only appear
// once percent-decoded.
const UNSAFE_RAW = /[\s\u0000-\u001f\u007f\\]/;
const UNSAFE_DECODED = /[\s\u0000-\u001f\u007f\\]|^\/\/|\/\//;
const SENTINEL_ORIGIN = "http://next.invalid";

function decodes(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

/**
 * A post-login redirect target taken from a query string. The value is resolved as a URL
 * against a sentinel origin and kept only if it stays on that origin; the result is the
 * normalised pathname + search + hash. Anything with whitespace/control characters (raw or
 * percent-encoded), backslashes, encoded "//", a full URL, or an auth page falls back home.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || UNSAFE_RAW.test(next)) {
    return HOME_PATH;
  }
  let url: URL;
  try {
    url = new URL(next, SENTINEL_ORIGIN);
  } catch {
    return HOME_PATH;
  }
  if (url.origin !== SENTINEL_ORIGIN) return HOME_PATH;

  const decodedPath = decodes(url.pathname);
  if (decodedPath === null || UNSAFE_DECODED.test(decodedPath)) return HOME_PATH;
  if (isPublicPath(url.pathname)) return HOME_PATH;
  return `${url.pathname}${url.search}${url.hash}`;
}
