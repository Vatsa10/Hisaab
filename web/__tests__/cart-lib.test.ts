import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

import {
  CART_MAX_ITEMS,
  addItem,
  areaLabel,
  checksLeftLine,
  getAccess,
  getCartChecksLeft,
  getUsual,
  parseLine,
  recentChecks,
  recentLine,
  setChoice,
  startCheck,
  watchCartRequest,
  type CartItem,
  type CartSummary,
} from "@/lib/cart";

beforeEach(() => {
  api.apiFetch.mockReset();
});

describe("parseLine", () => {
  it("parseLine handles ×2, x 2 and 2 x", () => {
    expect(parseLine("Amul Taaza 1L ×2")).toEqual({ text: "Amul Taaza 1L", qty: 2 });
    expect(parseLine("Bisleri 1L x 2")).toEqual({ text: "Bisleri 1L", qty: 2 });
    expect(parseLine("Eggs 6 * 3")).toEqual({ text: "Eggs 6", qty: 3 });
    expect(parseLine("2 x Bread")).toEqual({ text: "Bread", qty: 2 });
    expect(parseLine("3× Curd 400g")).toEqual({ text: "Curd 400g", qty: 3 });
    expect(parseLine("Paneer 200g")).toEqual({ text: "Paneer 200g", qty: 1 });
  });

  it("keeps words that end in x and numbers that are sizes", () => {
    expect(parseLine("Surf Excel Matic 1kg")).toEqual({ text: "Surf Excel Matic 1kg", qty: 1 });
    expect(parseLine("Kellogg's Chocos Box")).toEqual({ text: "Kellogg's Chocos Box", qty: 1 });
    expect(parseLine("  Bread   400g  ")).toEqual({ text: "Bread 400g", qty: 1 });
  });

  it("clamps the quantity to 1..20", () => {
    expect(parseLine("Milk ×50").qty).toBe(20);
    expect(parseLine("Milk ×0").qty).toBe(1);
  });
});

describe("list helpers", () => {
  it("adds a repeated item to the existing line, capped at 20", () => {
    const list: CartItem[] = [{ text: "Bread", qty: 19 }];
    expect(addItem(list, { text: "bread", qty: 3 })).toEqual([{ text: "Bread", qty: 20 }]);
  });

  it("never grows past the item limit", () => {
    const full = Array.from({ length: CART_MAX_ITEMS }, (_, i) => ({ text: `Item ${i}`, qty: 1 }));
    expect(addItem(full, { text: "One more", qty: 1 })).toBe(full);
    expect(CART_MAX_ITEMS).toBe(30);
  });

  it("area label drops India and pincodes", () => {
    // The same cases as server/cart_rank.area_label, so the screen and the result header agree.
    expect(areaLabel("12 MG Road, Gorwa, Vadodara, Gujarat 390016, India")).toBe("Gorwa, Vadodara");
    expect(areaLabel("Gorwa, Vadodara, Gujarat, 390016")).toBe("Gorwa, Vadodara");
    expect(areaLabel("Vadodara, Gujarat 390016, India")).toBe("Vadodara");
    expect(areaLabel("Gorwa")).toBe("Gorwa");
    expect(areaLabel(" 22.33 , 73.15 ")).toBe("your address");
    expect(areaLabel("India")).toBe("your address");
    expect(areaLabel("")).toBe("your address");
    expect(areaLabel(null)).toBe("your address");
  });

  it("checks left line", () => {
    expect(checksLeftLine(7)).toBe("7 checks left today");
    expect(checksLeftLine(1)).toBe("1 check left today");
  });
});

describe("API calls", () => {
  it("startCheck posts only text and qty", async () => {
    api.apiFetch.mockResolvedValueOnce({ request_id: "r1", sites: ["zepto"], cart_checks_left: 8 });
    const res = await startCheck([{ text: "Bread", qty: 2, variant: "x" }]);
    expect(api.apiFetch).toHaveBeenCalledWith("/cart/checks", {
      method: "POST",
      body: JSON.stringify({ items: [{ text: "Bread", qty: 2 }] }),
    });
    expect(res.request_id).toBe("r1");
  });

  it("getUsual, getAccess, setChoice and the limits read", async () => {
    api.apiFetch.mockResolvedValueOnce({ items: [{ text: "Bread", qty: 1, times: 4 }] });
    expect(await getUsual()).toEqual([{ text: "Bread", qty: 1, times: 4 }]);
    expect(api.apiFetch).toHaveBeenLastCalledWith("/cart/usual");

    api.apiFetch.mockResolvedValueOnce({ access: { zepto: "ok", blinkit: "needs_login" } });
    expect(await getAccess("grocery")).toEqual({ zepto: "ok", blinkit: "needs_login" });
    expect(api.apiFetch).toHaveBeenLastCalledWith("/cart/access?mode=grocery");

    api.apiFetch.mockResolvedValueOnce({ summary: { request_id: "r1" } });
    expect(await setChoice("r1", 2, "zepto", "p9")).toEqual({ request_id: "r1" });
    expect(api.apiFetch).toHaveBeenLastCalledWith("/cart/checks/r1/choices", {
      method: "POST",
      body: JSON.stringify({ idx: 2, site: "zepto", product_id: "p9" }),
    });

    api.apiFetch.mockResolvedValueOnce({ cart_checks_left: 6, cart_checks_limit: 10, budget_paused: false });
    expect(await getCartChecksLeft()).toBe(6);
    expect(api.apiFetch).toHaveBeenLastCalledWith("/me/limits");
  });
});

// ---- a tiny Supabase client fake ----------------------------------------------------------

type Handler = (payload: unknown) => void;
interface Sub {
  filter: { event: string; schema: string; table: string; filter?: string };
  handler: Handler;
}

function fakeClient(rows: Record<string, unknown[]>) {
  const subs: Sub[] = [];
  const removed: unknown[] = [];
  const queries: Array<{ table: string; filters: Array<[string, unknown]>; limit?: number }> = [];
  let statusCb: ((s: string) => void) | undefined;

  const channel = {
    on(_type: string, filter: Sub["filter"], handler: Handler) {
      subs.push({ filter, handler });
      return channel;
    },
    subscribe(cb: (s: string) => void) {
      statusCb = cb;
      return channel;
    },
  };

  function from(table: string) {
    const q = { table, filters: [] as Array<[string, unknown]>, limit: undefined as number | undefined };
    queries.push(q);
    const result = () => {
      let data = rows[table] ?? [];
      for (const [col, val] of q.filters) data = data.filter((r) => (r as Record<string, unknown>)[col] === val);
      if (q.limit != null) data = data.slice(0, q.limit);
      return data;
    };
    const builder = {
      select: () => builder,
      eq: (col: string, val: unknown) => {
        q.filters.push([col, val]);
        return builder;
      },
      order: () => builder,
      limit: (n: number) => {
        q.limit = n;
        return builder;
      },
      maybeSingle: () => Promise.resolve({ data: result()[0] ?? null, error: null }),
      then: (ok: (v: unknown) => unknown, bad?: (e: unknown) => unknown) =>
        Promise.resolve({ data: result(), error: null }).then(ok, bad),
    };
    return builder;
  }

  const client = {
    from,
    channel: vi.fn(() => channel),
    removeChannel: vi.fn((c: unknown) => {
      removed.push(c);
      return Promise.resolve("ok");
    }),
  };
  return { client, subs, removed, queries, join: () => statusCb?.("SUBSCRIBED") };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("watchCartRequest", () => {
  it("watchCartRequest subscribes to both tables and unsubscribes", async () => {
    const fake = fakeClient({
      cart_requests: [{ id: "r1", mode: "grocery", status: "searching", items: [], created_at: "2026-10-28T10:00:00Z" }],
      cart_results: [{ request_id: "r1", site: "zepto", status: "ok" }],
    });
    const onChange = vi.fn();
    const stop = watchCartRequest("r1", onChange, fake.client as never);

    expect(fake.subs.map((s) => [s.filter.table, s.filter.filter])).toEqual([
      ["cart_requests", "id=eq.r1"],
      ["cart_results", "request_id=eq.r1"],
    ]);

    fake.join();
    await flush();
    expect(onChange).toHaveBeenLastCalledWith({
      request: expect.objectContaining({ id: "r1", status: "searching" }),
      results: [expect.objectContaining({ site: "zepto", status: "ok" })],
    });

    // A realtime event triggers a fresh read of both.
    onChange.mockClear();
    fake.subs[1]!.handler({ new: { request_id: "r1", site: "blinkit" } });
    await flush();
    expect(onChange).toHaveBeenCalledTimes(1);

    stop();
    expect(fake.client.removeChannel).toHaveBeenCalledTimes(1);
    onChange.mockClear();
    fake.subs[0]!.handler({ new: { id: "r1" } });
    await flush();
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("recent checks", () => {
  const summary = {
    count_label: "7 items",
    best_site: "zepto",
    cards: [{ site: "zepto", name: "Zepto", total_label: "₹412" }],
  } as unknown as CartSummary;

  it("reads the newest checks of both modes", async () => {
    const fake = fakeClient({
      cart_requests: [
        { id: "a", mode: "grocery", status: "done", items: [], summary, created_at: "2026-10-28T10:00:00Z" },
        { id: "b", mode: "food", status: "done", items: [], summary, created_at: "2026-10-27T10:00:00Z" },
        { id: "c", mode: "grocery", status: "failed", items: [], summary: null, created_at: "2026-10-26T10:00:00Z" },
        { id: "d", mode: "grocery", status: "done", items: [], summary, created_at: "2026-10-25T10:00:00Z" },
      ],
    });
    const rows = await recentChecks(undefined, fake.client as never);
    expect(rows.map((r) => r.id)).toEqual(["a", "b", "c"]);
  });

  it("formats a row as count · best app total · ago", () => {
    const now = Date.parse("2026-10-28T10:02:00Z");
    expect(
      recentLine({ id: "a", mode: "grocery", status: "done", items: [], summary, created_at: "2026-10-28T10:00:00Z" }, now),
    ).toBe("7 items · Zepto ₹412 · 2m ago");
    expect(
      recentLine(
        { id: "c", mode: "grocery", status: "failed", items: [{ text: "A", qty: 1 }], summary: null, created_at: "2026-10-28T10:00:00Z" },
        now,
      ),
    ).toBe("1 item · No prices · 2m ago");
  });
});
