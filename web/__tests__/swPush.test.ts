// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

const source = readFileSync(fileURLToPath(new URL("../public/sw.js", import.meta.url)), "utf-8");
const ORIGIN = "https://app.example";

type Handler = (event: unknown) => void;

function boot(clientList: Array<{ url: string; focus: () => Promise<unknown>; navigate: (u: string) => Promise<unknown> }> = []) {
  const handlers: Record<string, Handler> = {};
  const openWindow = vi.fn<(u: string) => Promise<undefined>>(async () => undefined);
  const showNotification = vi.fn<(t: string, o: unknown) => Promise<undefined>>(async () => undefined);
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (t: string, fn: Handler) => {
      handlers[t] = fn;
    },
    skipWaiting: vi.fn(),
    registration: { showNotification },
    clients: { claim: vi.fn(), matchAll: vi.fn(async () => clientList), openWindow },
  };
  new Function("self", "caches", "fetch", source)(self, {}, vi.fn());
  return { handlers, showNotification, openWindow };
}

const pushEvent = (data: unknown) => {
  const waitUntil = vi.fn();
  return { event: { data: { json: () => data }, waitUntil }, waitUntil };
};

describe("service worker push", () => {
  it("shows the notification with the icon and target url", async () => {
    const { handlers, showNotification } = boot();
    const { event, waitUntil } = pushEvent({ title: "Zepto synced", body: "12 new orders", url: "/orders" });
    handlers.push!(event);
    await waitUntil.mock.calls[0]![0];
    expect(showNotification).toHaveBeenCalledWith("Zepto synced", {
      body: "12 new orders",
      icon: "/icons/192.png",
      data: { url: "/orders" },
    });
  });

  it("click focuses and navigates an open client", async () => {
    const client = { url: `${ORIGIN}/spend`, focus: vi.fn(async () => client), navigate: vi.fn(async () => undefined) };
    const { handlers, openWindow } = boot([client]);
    const close = vi.fn();
    const waitUntil = vi.fn();
    handlers.notificationclick!({ notification: { close, data: { url: "/accounts" } }, waitUntil });
    await waitUntil.mock.calls[0]![0];
    expect(close).toHaveBeenCalled();
    expect(client.focus).toHaveBeenCalled();
    expect(client.navigate).toHaveBeenCalledWith(`${ORIGIN}/accounts`);
    expect(openWindow).not.toHaveBeenCalled();
  });

  it("click opens a window when none is open, and refuses off-origin urls", async () => {
    const { handlers, openWindow } = boot([]);
    const waitUntil = vi.fn();
    handlers.notificationclick!({ notification: { close: vi.fn(), data: { url: "/accounts" } }, waitUntil });
    await waitUntil.mock.calls[0]![0];
    expect(openWindow).toHaveBeenCalledWith(`${ORIGIN}/accounts`);

    for (const bad of ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)", undefined]) {
      openWindow.mockClear();
      const w = vi.fn();
      handlers.notificationclick!({ notification: { close: vi.fn(), data: { url: bad } }, waitUntil: w });
      await w.mock.calls[0]![0];
      expect(openWindow).toHaveBeenCalledWith(`${ORIGIN}/`);
    }
  });
});
