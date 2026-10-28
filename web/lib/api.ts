import { apiUrl } from "@/lib/env";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

/** An error from the FastAPI service. `status` 0 means the request never got a response. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function joinUrl(base: string, path: string): string {
  return `${base.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}

/** FastAPI sends {"detail": "..."} or, for validation errors, {"detail": [{"msg": "..."}]}. */
async function readDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) =>
          typeof d === "string" ? d : d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : "",
        )
        .filter(Boolean);
      if (msgs.length) return msgs.join("; ");
    }
  } catch {
    // not JSON; fall through
  }
  return res.statusText || `HTTP ${res.status}`;
}

/**
 * Call the expense-agent API as the signed-in user. Attaches `Authorization: Bearer <access
 * token>` from the current Supabase session, sends JSON when given a string body, and returns
 * the parsed JSON (undefined for 204). Throws ApiError on any failure. Never logs the token or
 * the request body (it may hold a phone number or an OTP).
 */
export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const url = joinUrl(apiUrl(), path);
  const { data } = await getSupabaseBrowserClient().auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new ApiError(401, "not signed in");

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (typeof init.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  let res: Response;
  try {
    res = await fetch(url, { ...init, headers, credentials: "omit", cache: "no-store" });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }

  if (!res.ok) throw new ApiError(res.status, await readDetail(res));
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
