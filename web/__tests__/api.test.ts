import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  session: { access_token: "access-tok" } as { access_token: string } | null,
}));

vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({
    auth: {
      getSession: async () => ({ data: { session: auth.session }, error: null }),
    },
  }),
}));

import { ApiError, apiFetch } from "@/lib/api";

/** The ApiError a call rejects with (fails the test if it resolves). */
async function failure(call: Promise<unknown>): Promise<ApiError> {
  try {
    await call;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the call to fail");
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiFetch", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    auth.session = { access_token: "access-tok" };
    vi.stubEnv("NEXT_PUBLIC_API_URL", "http://api.test");
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("calls the API with the Supabase access token and returns parsed JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }));
    await expect(apiFetch("/me")).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("http://api.test/me");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer access-tok");
  });

  it("joins the base URL and path with exactly one slash", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "http://api.test/");
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await apiFetch("jobs/1");
    expect(fetchMock.mock.calls[0]![0]).toBe("http://api.test/jobs/1");
  });

  it("sends a JSON content type with a string body and keeps the caller's method", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await apiFetch("/consents", { method: "POST", body: JSON.stringify({ version: "2026-10-27" }) });
    const init = fetchMock.mock.calls[0]![1]!;
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });

  it("throws ApiError with the FastAPI detail on an error status", async () => {
    fetchMock.mockResolvedValue(jsonResponse(412, { detail: "consent required" }));
    const err = await failure(apiFetch("/connections/swiggy/connect", { method: "POST" }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(412);
    expect(err.detail).toBe("consent required");
  });

  it("flattens a validation error list into a readable detail", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ msg: "field required" }, { msg: "too long" }] }),
    );
    const err = await failure(apiFetch("/ask", { method: "POST" }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.detail).toBe("field required; too long");
  });

  it("flattens a list of plain strings and mixed items into a readable detail", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: ["handling must be from 0 to 500", { msg: "too long" }, 7] }),
    );
    const err = await failure(apiFetch("/admin/fees/zepto", { method: "PUT" }));
    expect(err.detail).toBe("handling must be from 0 to 500; too long");
  });

  it("uses the status text when the error body is not JSON", async () => {
    fetchMock.mockResolvedValue(new Response("boom", { status: 502, statusText: "Bad Gateway" }));
    const err = await failure(apiFetch("/me"));
    expect(err.status).toBe(502);
    expect(err.detail).toBe("Bad Gateway");
  });

  it("throws a 401 ApiError without calling the API when signed out", async () => {
    auth.session = null;
    const err = await failure(apiFetch("/me"));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns undefined for 204 No Content", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch("/jobs/1/otp", { method: "POST" })).resolves.toBeUndefined();
  });

  it("turns a network failure into ApiError status 0", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await failure(apiFetch("/me"));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
  });

  it("refuses to run without NEXT_PUBLIC_API_URL", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    await expect(apiFetch("/me")).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
  });
});
