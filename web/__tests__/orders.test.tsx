import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SupabaseClient } from "@supabase/supabase-js";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OrderDetail, OrderSummary } from "@/lib/queries";

const q = vi.hoisted(() => ({
  getMonthlySpend: vi.fn(),
  getCategorySpend: vi.fn(),
  getTopMerchants: vi.fn(),
  getOrders: vi.fn(),
  getOrder: vi.fn(),
}));
vi.mock("@/lib/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/queries")>()),
  ...q,
}));

import { OrderDetailView } from "@/components/spend/OrderDetailView";
import { OrdersList } from "@/components/spend/OrdersList";

const order = (o: Partial<OrderSummary> & Pick<OrderSummary, "orderId">): OrderSummary => ({
  site: "swiggy",
  subPlatform: null,
  placedAt: "2026-10-03T19:42:00",
  total: 412,
  status: "delivered",
  merchant: "McDonald's",
  ...o,
});

const PAGE_1: OrderSummary[] = [
  order({ orderId: "a1", placedAt: "2026-10-21T13:05:00", merchant: "Behrouz Biryani", total: 649 }),
  order({ orderId: "a2", placedAt: "2026-10-12T09:15:00", site: "swiggy", subPlatform: "instamart", merchant: "Instamart", total: 300 }),
  order({ orderId: "a3", placedAt: "2026-10-03T19:42:00", site: "zomato", merchant: "Domino's", status: "cancelled", total: 520 }),
  order({ orderId: "a4", placedAt: "2026-09-28T20:10:00", site: "zepto", merchant: null, total: 1234.5 }),
];
const PAGE_2: OrderSummary[] = [order({ orderId: "b1", placedAt: "2026-08-14T12:00:00", site: "blinkit", merchant: "Blinkit", total: 88 })];

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrdersList", () => {
  it("groups orders by month with app, merchant (or app name), date and amount", async () => {
    q.getOrders.mockResolvedValueOnce({ orders: PAGE_1, nextCursor: null });
    render(<OrdersList />);

    const oct = await screen.findByRole("region", { name: "October 2026" });
    const sep = screen.getByRole("region", { name: "September 2026" });
    expect(within(oct).getAllByRole("link")).toHaveLength(3);
    expect(within(sep).getAllByRole("link")).toHaveLength(1);

    const biryani = within(oct).getByRole("link", { name: /Behrouz Biryani/ });
    expect(biryani).toHaveAttribute("href", "/orders/swiggy/a1");
    expect(biryani).toHaveTextContent("Swiggy");
    expect(biryani).toHaveTextContent("21 Oct");
    expect(biryani).toHaveTextContent("₹649");

    // No merchant: the app name is the title.
    const zepto = within(sep).getByRole("link");
    expect(zepto.querySelector("[data-title]")).toHaveTextContent("Zepto");
    expect(zepto).toHaveTextContent("₹1,235");
    expect(q.getOrders).toHaveBeenCalledWith({});
  });

  it("shows Instamart orders as Swiggy with an Instamart sub-label", async () => {
    q.getOrders.mockResolvedValueOnce({ orders: PAGE_1, nextCursor: null });
    render(<OrdersList />);
    const link = await screen.findByRole("link", { name: /₹300/ });
    expect(link.querySelector("[data-title]")).toHaveTextContent("Swiggy");
    expect(link).toHaveTextContent("Instamart");
    expect(link.querySelector("[data-app-dot]")).toHaveClass("bg-app-swiggy");
  });

  it("shows a cancelled badge", async () => {
    q.getOrders.mockResolvedValueOnce({ orders: PAGE_1, nextCursor: null });
    render(<OrdersList />);
    const dominos = await screen.findByRole("link", { name: /Domino's/ });
    expect(within(dominos).getByText("Cancelled")).toBeInTheDocument();
    const biryani = screen.getByRole("link", { name: /Behrouz Biryani/ });
    expect(within(biryani).queryByText("Cancelled")).not.toBeInTheDocument();
  });

  it("loads the next page with the cursor and appends it", async () => {
    q.getOrders
      .mockResolvedValueOnce({ orders: PAGE_1, nextCursor: "c1" })
      .mockResolvedValueOnce({ orders: PAGE_2, nextCursor: null });
    render(<OrdersList site="swiggy" />);
    await screen.findByRole("region", { name: "October 2026" });

    await userEvent.click(screen.getByRole("button", { name: "Show older orders" }));
    expect(q.getOrders).toHaveBeenLastCalledWith({ site: "swiggy", cursor: "c1" });
    expect(await screen.findByRole("region", { name: "August 2026" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show older orders" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /₹/ })).toHaveLength(5);
  });

  it("offers a retry when the first page fails", async () => {
    q.getOrders.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ orders: PAGE_1, nextCursor: null });
    render(<OrdersList />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t load/i);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "October 2026" })).toBeInTheDocument();
  });

  it("explains an empty list and links to Accounts", async () => {
    q.getOrders.mockResolvedValueOnce({ orders: [], nextCursor: null });
    render(<OrdersList />);
    expect(await screen.findByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/accounts");
  });
});

describe("OrderDetailView", () => {
  const DETAIL: OrderDetail = {
    ...order({ orderId: "a3", site: "zomato", merchant: "Domino's", status: "cancelled", total: 520 }),
    currency: "INR",
    items: [
      { idx: 0, name: "Farmhouse Pizza", qty: 2, price: 398 },
      { idx: 1, name: "Garlic Bread", qty: 1, price: null },
    ],
  };

  it("lists items with name, qty and the price or —", async () => {
    q.getOrder.mockResolvedValueOnce(DETAIL);
    render(<OrderDetailView site="zomato" orderId="a3" />);

    expect(await screen.findByRole("heading", { level: 1, name: "Domino's" })).toBeInTheDocument();
    expect(q.getOrder).toHaveBeenCalledWith("zomato", "a3");
    expect(screen.getByText("Cancelled")).toBeInTheDocument();

    const items = within(screen.getByRole("region", { name: /Items/ })).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Farmhouse Pizza");
    expect(items[0]).toHaveTextContent("×2");
    expect(items[0]).toHaveTextContent("₹398");
    expect(items[1]).toHaveTextContent("Garlic Bread");
    expect(items[1]).toHaveTextContent("×1");
    expect(items[1]).toHaveTextContent("—");
    expect(screen.getByRole("link", { name: /Orders/ })).toHaveAttribute("href", "/orders");
  });

  it("says when the order does not exist", async () => {
    q.getOrder.mockResolvedValueOnce(null);
    render(<OrderDetailView site="zomato" orderId="nope" />);
    expect(await screen.findByText("Order not found")).toBeInTheDocument();
  });
});

// --- lib/queries against a recording fake of the Supabase query builder ---

type Call = [string, unknown[]];

function fakeClient(result: { data: unknown; error: unknown }) {
  const calls: Call[] = [];
  const builder: Record<string, unknown> = new Proxy(
    {},
    {
      get(_t, prop: string) {
        if (prop === "then") {
          return (resolve: (v: unknown) => void) => resolve(result);
        }
        return (...args: unknown[]) => {
          calls.push([prop, args]);
          return builder;
        };
      },
    },
  );
  const client = { from: (table: string) => (calls.push(["from", [table]]), builder) };
  return { client: client as unknown as SupabaseClient, calls };
}

describe("lib/queries getOrders", () => {
  it("orders newest first, asks for one extra row and returns a cursor when there are more", async () => {
    const { getOrders, ORDERS_PAGE_SIZE } = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
    const rows = Array.from({ length: ORDERS_PAGE_SIZE + 1 }, (_, i) => ({
      site: "swiggy",
      order_id: `o${i}`,
      sub_platform: null,
      placed_at: `2026-10-${String(28 - (i % 27)).padStart(2, "0")}T10:00:00`,
      total_amount: 100,
      status: "delivered",
      merchant: "X",
    }));
    const { client, calls } = fakeClient({ data: rows, error: null });
    const page = await getOrders({ site: "swiggy", month: "2026-10" }, client);

    expect(page.orders).toHaveLength(ORDERS_PAGE_SIZE);
    expect(page.nextCursor).toBeTruthy();
    expect(calls).toContainEqual(["from", ["orders"]]);
    expect(calls).toContainEqual(["eq", ["site", "swiggy"]]);
    expect(calls).toContainEqual(["like", ["placed_at", "2026-10%"]]);
    expect(calls).toContainEqual(["order", ["placed_at", { ascending: false, nullsFirst: false }]]);
    expect(calls).toContainEqual(["limit", [ORDERS_PAGE_SIZE + 1]]);
    expect(calls.some(([m]) => m === "or")).toBe(false);

    // The cursor resumes strictly after the last row shown (keyset, quoted values).
    const next = fakeClient({ data: [], error: null });
    const last = page.orders[page.orders.length - 1]!;
    await getOrders({ cursor: page.nextCursor! }, next.client);
    const or = next.calls.find(([m]) => m === "or");
    expect(or?.[1][0]).toBe(
      `placed_at.lt."${last.placedAt}",placed_at.is.null,` +
        `and(placed_at.eq."${last.placedAt}",site.lt."swiggy"),` +
        `and(placed_at.eq."${last.placedAt}",site.eq."swiggy",order_id.lt."${last.orderId}")`,
    );
  });

  it("returns no cursor on the last page and throws on a query error", async () => {
    const { getOrders } = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
    const done = fakeClient({ data: [], error: null });
    expect(await getOrders({}, done.client)).toEqual({ orders: [], nextCursor: null });
    const bad = fakeClient({ data: null, error: { message: "permission denied" } });
    await expect(getOrders({}, bad.client)).rejects.toThrow();
  });
});

describe("lib/queries getTopMerchants", () => {
  it("ranks this year's merchants by amount, skipping cancelled orders and missing merchants", async () => {
    const { getTopMerchants } = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
    const { client, calls } = fakeClient({
      data: [
        { merchant: "McDonald's", site: "swiggy", total_amount: 300, status: "delivered" },
        { merchant: "McDonald's", site: "swiggy", total_amount: 250, status: null },
        { merchant: "McDonald's", site: "swiggy", total_amount: 999, status: "cancelled" },
        { merchant: "Domino's", site: "zomato", total_amount: 500, status: "delivered" },
        { merchant: null, site: "zepto", total_amount: 5000, status: "delivered" },
      ],
      error: null,
    });
    const top = await getTopMerchants(2026, client);
    expect(top).toEqual([
      { merchant: "McDonald's", site: "swiggy", orders: 2, amount: 550 },
      { merchant: "Domino's", site: "zomato", orders: 1, amount: 500 },
    ]);
    expect(calls).toContainEqual(["gte", ["placed_at", "2026-01-01"]]);
    expect(calls).toContainEqual(["lt", ["placed_at", "2027-01-01"]]);
  });
});

