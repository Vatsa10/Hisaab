import { afterEach, describe, expect, it, vi } from "vitest";
import { clearPageCache } from "@/lib/pwa";

describe("clearPageCache", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("deletes only the service worker's page caches", async () => {
    const deleted: string[] = [];
    vi.stubGlobal("caches", {
      keys: async () => ["pages-v1", "static-v1", "pages-v0"],
      delete: async (k: string) => {
        deleted.push(k);
        return true;
      },
    });
    await clearPageCache();
    expect(deleted.sort()).toEqual(["pages-v0", "pages-v1"]);
  });

  it("does nothing (and does not throw) without the Cache API", async () => {
    vi.stubGlobal("caches", undefined);
    await expect(clearPageCache()).resolves.toBeUndefined();
  });

  it("swallows Cache API errors so sign-in and sign-out still finish", async () => {
    vi.stubGlobal("caches", {
      keys: async () => {
        throw new Error("SecurityError");
      },
    });
    await expect(clearPageCache()).resolves.toBeUndefined();
  });
});
