// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The service worker is a plain script in public/. Run it against a fake worker scope and
// check which requests it takes over: only same-origin navigations and immutable static files,
// never the API or Supabase.

const source = readFileSync(fileURLToPath(new URL("../public/sw.js", import.meta.url)), "utf-8");
const ORIGIN = "https://app.example";

type Handler = (event: unknown) => void;

function makeCaches() {
  const stores = new Map<string, Map<string, Response>>();
  const open = async (name: string) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name)!;
    return {
      match: async (req: { url: string } | string) => store.get(typeof req === "string" ? req : req.url)?.clone(),
      put: async (req: { url: string }, res: Response) => {
        store.set(req.url, res);
      },
      addAll: async () => {},
    };
  };
  return {
    stores,
    api: {
      open,
      keys: async () => [...stores.keys()],
      delete: async (name: string) => stores.delete(name),
      match: async (req: { url: string }) => {
        for (const store of stores.values()) {
          const hit = store.get(req.url);
          if (hit) return hit.clone();
        }
        return undefined;
      },
    },
  };
}

function boot(fetchImpl: typeof fetch) {
  const handlers: Record<string, Handler> = {};
  const caches = makeCaches();
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type: string, fn: Handler) => {
      handlers[type] = fn;
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn() },
  };
  new Function("self", "caches", "fetch", source)(self, caches.api, fetchImpl);
  return { handlers, caches };
}

function fetchEvent(url: string, opts: { mode?: string; method?: string } = {}) {
  const respondWith = vi.fn();
  const waitUntil = vi.fn();
  const request = { url, mode: opts.mode ?? "cors", method: opts.method ?? "GET" };
  return { event: { request, respondWith, waitUntil }, respondWith };
}

describe("service worker", () => {
  let fetchImpl: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchImpl = vi.fn();
  });

  it("never intercepts API or Supabase requests", () => {
    const { handlers } = boot(fetchImpl as unknown as typeof fetch);
    for (const url of [
      "http://localhost:8000/connections/swiggy/connect",
      "https://api.example/ask",
      "http://127.0.0.1:54321/rest/v1/orders?select=*",
      "https://abc.supabase.co/auth/v1/token?grant_type=refresh_token",
      "wss://abc.supabase.co/realtime/v1/websocket",
    ]) {
      const { event, respondWith } = fetchEvent(url);
      handlers.fetch!(event);
      expect(respondWith, url).not.toHaveBeenCalled();
    }
  });

  it("leaves same-origin non-GET requests and the auth callback alone", () => {
    const { handlers } = boot(fetchImpl as unknown as typeof fetch);
    const post = fetchEvent(`${ORIGIN}/spend`, { method: "POST", mode: "navigate" });
    handlers.fetch!(post.event);
    expect(post.respondWith).not.toHaveBeenCalled();
    const cb = fetchEvent(`${ORIGIN}/auth/callback?code=abc`, { mode: "navigate" });
    handlers.fetch!(cb.event);
    expect(cb.respondWith).not.toHaveBeenCalled();
  });

  it("serves navigations network-first and falls back to the cached page offline", async () => {
    fetchImpl.mockResolvedValueOnce(new Response("<p>live</p>", { status: 200 }));
    const { handlers } = boot(fetchImpl as unknown as typeof fetch);

    const online = fetchEvent(`${ORIGIN}/spend`, { mode: "navigate" });
    handlers.fetch!(online.event);
    expect(online.respondWith).toHaveBeenCalledOnce();
    const live = (await online.respondWith.mock.calls[0]![0]) as Response;
    expect(await live.text()).toBe("<p>live</p>");
    await Promise.all(online.event.waitUntil.mock.calls.map((c) => c[0]));

    fetchImpl.mockRejectedValueOnce(new TypeError("offline"));
    const offline = fetchEvent(`${ORIGIN}/spend`, { mode: "navigate" });
    handlers.fetch!(offline.event);
    const cached = (await offline.respondWith.mock.calls[0]![0]) as Response;
    expect(await cached.text()).toBe("<p>live</p>");
  });

  it("shows an offline page when a navigation was never cached", async () => {
    fetchImpl.mockRejectedValueOnce(new TypeError("offline"));
    const { handlers } = boot(fetchImpl as unknown as typeof fetch);
    const nav = fetchEvent(`${ORIGIN}/orders`, { mode: "navigate" });
    handlers.fetch!(nav.event);
    const res = (await nav.respondWith.mock.calls[0]![0]) as Response;
    expect(res.status).toBe(503);
    expect(await res.text()).toContain("offline");
  });
});
