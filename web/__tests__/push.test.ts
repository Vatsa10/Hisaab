import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), apiFetch: api.apiFetch }));

import { ApiError } from "@/lib/api";
import { disablePush, enablePush, pushState, urlB64ToUint8Array } from "@/lib/push";

const IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";
const subJson = { endpoint: "https://push.example/e1", keys: { p256dh: "pk", auth: "ak" } };

function setup(
  opts: { ua?: string; standalone?: boolean; permission?: string; requested?: string; existing?: boolean; push?: boolean } = {},
) {
  const subscribe = vi.fn<(o: unknown) => Promise<{ toJSON: () => typeof subJson; endpoint: string }>>(async () => ({ toJSON: () => subJson, endpoint: subJson.endpoint }));
  const unsubscribe = vi.fn(async () => true);
  const existing = { toJSON: () => subJson, endpoint: subJson.endpoint, unsubscribe };
  const getSubscription = vi.fn(async () => (opts.existing ? existing : null));
  vi.stubGlobal("navigator", {
    userAgent: opts.ua ?? "Mozilla/5.0 Chrome",
    standalone: opts.standalone,
    serviceWorker: { ready: Promise.resolve({ pushManager: { subscribe, getSubscription } }) },
  });
  const requestPermission = vi.fn(async () => opts.requested ?? "granted");
  if (opts.push !== false) vi.stubGlobal("PushManager", function PushManager() {});
  else vi.stubGlobal("PushManager", undefined);
  vi.stubGlobal("Notification", { permission: opts.permission ?? "default", requestPermission });
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: Boolean(opts.standalone) && q.includes("standalone") }));
  return { subscribe, unsubscribe, requestPermission };
}

describe("push", () => {
  beforeEach(() => {
    api.apiFetch.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("enablePush subscribes with the VAPID key and posts it", async () => {
    const { subscribe, requestPermission } = setup();
    api.apiFetch.mockResolvedValueOnce({ key: "AQAB" }).mockResolvedValueOnce(undefined);
    expect(await enablePush()).toBe("on");
    expect(requestPermission).toHaveBeenCalledOnce();
    const arg = subscribe.mock.calls[0]![0] as { userVisibleOnly: boolean; applicationServerKey: Uint8Array };
    expect(arg.userVisibleOnly).toBe(true);
    expect(Array.from(arg.applicationServerKey)).toEqual([1, 0, 1]);
    const [path, init] = api.apiFetch.mock.calls[1]!;
    expect(path).toBe("/push/subscribe");
    expect(JSON.parse(init.body)).toEqual(subJson);
  });

  it("needs_install on iOS when not standalone", async () => {
    const { requestPermission } = setup({ ua: IOS, standalone: false, push: false });
    expect(await pushState()).toBe("needs_install");
    expect(await enablePush()).toBe("needs_install");
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("denied permission returns denied and posts nothing", async () => {
    setup({ requested: "denied" });
    expect(await enablePush()).toBe("denied");
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("reports state from permission and subscription", async () => {
    setup({ permission: "denied" });
    expect(await pushState()).toBe("denied");
    setup({ existing: true });
    expect(await pushState()).toBe("on");
    setup();
    expect(await pushState()).toBe("off");
    setup({ push: false });
    expect(await pushState()).toBe("unsupported");
  });

  it("unsupported when the server has no VAPID key", async () => {
    setup();
    api.apiFetch.mockRejectedValueOnce(new ApiError(404, "not configured"));
    expect(await enablePush()).toBe("unsupported");
  });

  it("disablePush unsubscribes and tells the server", async () => {
    const { unsubscribe } = setup({ existing: true });
    api.apiFetch.mockResolvedValueOnce(undefined);
    await disablePush();
    expect(unsubscribe).toHaveBeenCalled();
    const [path, init] = api.apiFetch.mock.calls[0]!;
    expect(path).toBe("/push/unsubscribe");
    expect(JSON.parse(init.body)).toEqual({ endpoint: subJson.endpoint });
  });

  it("decodes url-safe base64", () => {
    expect(Array.from(urlB64ToUint8Array("AQAB"))).toEqual([1, 0, 1]);
    expect(Array.from(urlB64ToUint8Array("-_8"))).toEqual([251, 255]);
  });
});
