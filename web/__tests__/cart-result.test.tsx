import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ---- mocks -------------------------------------------------------------------------------

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

// The user's running jobs, read under RLS.
const jobs = vi.hoisted(() => ({ rows: [] as Array<Record<string, unknown>> }));
vi.mock("@/lib/supabase/client", () => {
  const query = {
    select: () => query,
    in: () => query,
    then: (ok: (r: unknown) => unknown) => Promise.resolve({ data: jobs.rows, error: null }).then(ok),
  };
  return { getSupabaseBrowserClient: () => ({ from: () => query }) };
});

const live = vi.hoisted(() => ({ cb: null as null | ((snap: unknown) => void), unsubs: 0 }));
vi.mock("@/lib/cart", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/cart")>();
  return {
    ...real,
    watchCartRequest: vi.fn((_id: string, cb: (snap: unknown) => void) => {
      live.cb = cb;
      return () => {
        live.unsubs += 1;
      };
    }),
  };
});

import { ApiError } from "@/lib/api";
import type { CartCard, CartRequest, CartResult, CartSummary } from "@/lib/cart";
import { CartResultView } from "@/components/cart/CartResultView";

// ---- fixtures (the shape of server/cart_rank.build_summary) -------------------------------

const NOW = Date.parse("2026-10-28T10:00:00Z");
const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

function card(over: Partial<CartCard>): CartCard {
  return {
    site: "zepto",
    name: "Zepto",
    rank: 1,
    status: "ok",
    cheapest: false,
    subtotal: 397,
    fees: { delivery: 0, handling: 15, total: 15 },
    fees_estimated: false,
    total: 412,
    total_label: "₹412",
    delta_label: null,
    fee_line: null,
    member_note: null,
    fee_hint: null,
    stock_line: null,
    swaps: [],
    to_pick: 0,
    note: null,
    action: null,
    ...over,
  };
}

function grocerySummary(): CartSummary {
  const items = ["Amul Taaza 1L ×2", "Bisleri 1L ×2", "Lay's Classic", "Bread 400g", "Eggs 6", "Curd 400g", "Paneer 200g"];
  return {
    request_id: "r1",
    mode: "grocery",
    status: "done",
    title: "Cheapest cart",
    count_label: "7 items",
    area: "Gorwa, Vadodara",
    priced_at: minutesAgo(2),
    price_note: "Prices include fees we could see.",
    offers_note: null,
    restaurant: null,
    best_site: "zepto",
    cards: [
      card({
        site: "zepto",
        name: "Zepto",
        rank: 1,
        cheapest: true,
        fee_line: "Items ₹397 + delivery ₹0 + handling ₹15",
        member_note: "Zepto Pass applied",
        stock_line: "all 7 in stock",
      }),
      card({
        site: "blinkit",
        name: "Blinkit",
        rank: 2,
        total: 449,
        total_label: "₹449",
        delta_label: "+₹37",
        fees_estimated: true,
        fee_hint: "Free delivery above ₹199",
        stock_line: "all in stock",
      }),
      card({
        site: "instamart",
        name: "Instamart",
        rank: 3,
        total: 461,
        total_label: "₹461",
        delta_label: "+₹49",
        swaps: ["Lay's Classic swapped for Lay's Magic Masala"],
        stock_line: "1 to pick",
        to_pick: 1,
      }),
    ],
    table: {
      columns: ["zepto", "blinkit", "instamart"],
      rows: items.map((label, idx) => ({
        idx,
        label,
        cells: {
          zepto: { status: "matched", name: label, price_label: "₹50", cheapest: idx === 0 },
          blinkit: { status: "matched", name: label, price_label: "₹52", cheapest: idx === 1 },
          instamart:
            idx === 2
              ? { status: "not_found", name: null, price_label: "n/a", cheapest: false }
              : idx === 3
                ? { status: "pick", name: null, price_label: "Pick", cheapest: false }
                : { status: "matched", name: label, price_label: "₹55", cheapest: false },
        },
      })),
      totals: { zepto: "₹412", blinkit: "₹449", instamart: "₹461" },
    },
    picks: [
      {
        idx: 3,
        site: "instamart",
        label: "Bread 400g",
        candidates: [
          { product_id: "p1", name: "Harvest Gold White Bread", size_label: "400 g", price_label: "₹45", packs: 1 },
          { product_id: "p2", name: "Modern Sandwich Bread", size_label: "400 g", price_label: "₹50", packs: 1 },
        ],
      },
    ],
    split: { worth_it: false, sites: ["zepto", "blinkit"], saving: 6, line: "Splitting across apps saves ₹6 more but adds a 2nd delivery fee. Not worth it." },
  };
}

function foodSummary(): CartSummary {
  return {
    request_id: "f1",
    mode: "food",
    status: "done",
    title: "Kailash Parbat",
    count_label: "3 dishes",
    area: "Gorwa, Vadodara",
    priced_at: minutesAgo(1),
    price_note: "Menu prices before offers.",
    offers_note: "Offers in the app may lower this.",
    restaurant: { name: "Kailash Parbat", locality: "Alkapuri" },
    best_site: "swiggy",
    cards: [
      card({ site: "swiggy", name: "Swiggy", rank: 1, cheapest: true, total_label: "₹540", stock_line: "all 3 available" }),
      card({
        site: "zomato",
        name: "Zomato",
        rank: 2,
        status: "closed",
        total: null,
        total_label: "—",
        note: "Closed right now. Opens at 7 pm.",
        fees: null,
        subtotal: null,
      }),
    ],
    table: {
      columns: ["swiggy"],
      rows: ["Pav Bhaji", "Chole Bhature", "Lassi"].map((label, idx) => ({
        idx,
        label,
        cells: { swiggy: { status: "matched", name: label, price_label: "₹180", cheapest: true } },
      })),
      totals: { swiggy: "₹540" },
    },
    picks: [],
    split: null,
  };
}

function request(over: Partial<CartRequest>): CartRequest {
  return {
    id: "r1",
    mode: "grocery",
    status: "done",
    items: [{ text: "Milk", qty: 2 }],
    sites: ["zepto", "blinkit", "instamart"],
    summary: grocerySummary(),
    created_at: minutesAgo(3),
    ...over,
  };
}

function result(site: CartResult["site"], status: CartResult["status"]): CartResult {
  return { request_id: "r1", site, status, error: null, priced_at: null };
}

function show(req: CartRequest | null, results: CartResult[] = []) {
  act(() => live.cb?.({ request: req, results }));
}

function renderView(id = "r1") {
  return render(<CartResultView id={id} />);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
  api.apiFetch.mockReset();
  router.push.mockReset();
  jobs.rows = [];
  live.cb = null;
});

afterEach(() => {
  vi.useRealTimers();
});

// ---- tests -------------------------------------------------------------------------------

describe("ranked results", () => {
  it("cards render in order with cheapest ring and pill", () => {
    renderView();
    show(request({}));
    const cards = screen.getAllByRole("article");
    expect(cards.map((c) => within(c).getByRole("heading").textContent)).toEqual(["Zepto", "Blinkit", "Instamart"]);
    expect(cards[0]).toHaveClass("ring-2", "ring-accent");
    expect(cards[1]).not.toHaveClass("ring-2");
    expect(within(cards[0]!).getByText("Cheapest")).toBeInTheDocument();
    expect(within(cards[0]!).getByText("₹412")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Cheapest cart" })).toBeInTheDocument();
    expect(screen.getByText("7 items · priced live at Gorwa, Vadodara · 2 min ago")).toBeInTheDocument();
    expect(screen.getByText("Prices include fees we could see.")).toBeInTheDocument();
  });

  it("fill button is disabled with coming soon and calls nothing", () => {
    renderView();
    show(request({}));
    const btn = screen.getByRole("button", { name: /Fill cart on Zepto/ });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Coming soon")).toBeInTheDocument();
    fireEvent.click(btn);
    expect(api.apiFetch).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: /Fill cart/ })).toHaveLength(1);
  });

  it("other cards show delta labels", () => {
    renderView();
    show(request({}));
    const cards = screen.getAllByRole("article");
    expect(within(cards[1]!).getByText("+₹37")).toBeInTheDocument();
    expect(within(cards[2]!).getByText("+₹49")).toBeInTheDocument();
  });

  it("swap note, member note and fees estimated render", () => {
    renderView();
    show(request({}));
    const cards = screen.getAllByRole("article");
    expect(within(cards[0]!).getByText("Zepto Pass applied")).toHaveClass("text-accent-strong");
    expect(within(cards[0]!).getByText(/Items ₹397 \+ delivery ₹0/)).toBeInTheDocument();
    expect(within(cards[1]!).getByText("fees estimated")).toBeInTheDocument();
    expect(within(cards[0]!).queryByText("fees estimated")).toBeNull();
    expect(within(cards[1]!).getByText("Free delivery above ₹199")).toBeInTheDocument();
    expect(within(cards[2]!).getByText("Lay's Classic swapped for Lay's Magic Masala")).toBeInTheDocument();
    expect(within(cards[2]!).getByRole("button", { name: /1 to pick/ })).toBeInTheDocument();
  });

  it("not connected card links to accounts", () => {
    const s = grocerySummary();
    s.cards[2] = card({
      site: "instamart",
      name: "Instamart",
      rank: 3,
      status: "not_connected",
      total: null,
      total_label: "—",
      note: "Connect Instamart to compare",
      action: { label: "Connect", href: "/accounts" },
    });
    renderView();
    show(request({ summary: s }));
    const c = screen.getAllByRole("article")[2]!;
    expect(within(c).getByText("Connect Instamart to compare")).toBeInTheDocument();
    expect(within(c).getByRole("link", { name: "Connect" })).toHaveAttribute("href", "/accounts");
    expect(within(c).queryByText("—")).toBeNull();
  });

  it("needs login card shows log in again", () => {
    const s = grocerySummary();
    s.cards[1] = card({
      site: "blinkit",
      name: "Blinkit",
      rank: 2,
      status: "needs_login",
      total: null,
      total_label: "—",
      note: "Blinkit logged you out.",
      action: { label: "Log in again", href: "/accounts?connect=blinkit" },
    });
    renderView();
    show(request({ summary: s }));
    const c = screen.getAllByRole("article")[1]!;
    expect(within(c).getByRole("link", { name: "Log in again" })).toHaveAttribute("href", "/accounts?connect=blinkit");
  });

  it("food cards show offers note and restaurant title", () => {
    renderView("f1");
    show(request({ id: "f1", mode: "food", summary: foodSummary(), sites: ["swiggy", "zomato"] }));
    expect(screen.getByRole("heading", { level: 1, name: "Kailash Parbat" })).toBeInTheDocument();
    expect(screen.getByText("Alkapuri")).toBeInTheDocument();
    const c = screen.getAllByRole("article")[0]!;
    expect(within(c).getByText("Offers in the app may lower this.")).toHaveClass("text-ink-muted");
    expect(within(c).getByRole("button", { name: /Fill cart on Swiggy/ })).toBeDisabled();
  });

  it("closed card shows its note and no total", () => {
    renderView("f1");
    show(request({ id: "f1", mode: "food", summary: foodSummary() }));
    const c = screen.getAllByRole("article")[1]!;
    expect(within(c).getByText("Closed right now. Opens at 7 pm.")).toBeInTheDocument();
    expect(within(c).queryByText("—")).toBeNull();
    expect(within(c).queryByText("Offers in the app may lower this.")).toBeNull();
  });
});

describe("item table", () => {
  it("table toggles with highlighted cells and totals row", () => {
    renderView();
    show(request({}));
    expect(screen.queryByRole("table")).toBeNull();
    const toggle = screen.getByRole("button", { name: "See item prices" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 7 + 1);
    const milk = within(table).getByRole("row", { name: /Amul Taaza/ });
    const cells = within(milk).getAllByRole("cell");
    expect(cells[0]).toHaveClass("text-accent", "font-bold");
    expect(cells[1]).not.toHaveClass("font-bold");
    const lays = within(table).getByRole("row", { name: /Lay's Classic/ });
    expect(within(lays).getByText("n/a")).toHaveClass("text-ink-muted");
    expect(within(table).getByRole("button", { name: /Pick/ })).toBeInTheDocument();
    const totals = within(table).getByRole("row", { name: /Total \+ fees/ });
    expect(within(totals).getByText("₹449")).toBeInTheDocument();
    expect(screen.getByText(/Splitting across apps saves ₹6/)).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("split line hidden for food", () => {
    renderView("f1");
    show(request({ id: "f1", mode: "food", summary: foodSummary() }));
    fireEvent.click(screen.getByRole("button", { name: "See item prices" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.queryByText(/Splitting/)).toBeNull();
    expect(screen.queryByTestId("split-line")).toBeNull();
  });
});

describe("pick one", () => {
  it("pick sheet posts the choice and updates totals", async () => {
    const updated = grocerySummary();
    updated.cards[2] = { ...updated.cards[2]!, total_label: "₹455", delta_label: "+₹43", to_pick: 0 };
    updated.picks = [];
    api.apiFetch.mockRejectedValueOnce(new ApiError(409, "That product is gone. Pick another."));
    api.apiFetch.mockResolvedValueOnce({ summary: updated });

    renderView();
    show(request({}));
    fireEvent.click(screen.getByRole("button", { name: /1 to pick/ }));
    const sheet = await screen.findByRole("dialog", { name: "Which one did you mean?" });
    fireEvent.click(within(sheet).getByRole("radio", { name: /Modern Sandwich Bread/ }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Use this" }));
    expect(await within(sheet).findByText("That product is gone. Pick another.")).toBeInTheDocument();

    fireEvent.click(within(sheet).getByRole("button", { name: "Use this" }));
    await waitFor(() => expect(screen.getAllByRole("article")[2]).toHaveTextContent("₹455"));
    const [path, init] = api.apiFetch.mock.calls[1]!;
    expect(path).toBe("/cart/checks/r1/choices");
    expect(JSON.parse(init.body)).toEqual({ idx: 3, site: "instamart", product_id: "p2" });
    expect(screen.queryByRole("button", { name: /to pick/ })).toBeNull();
  });

  it("a second pick starts with nothing chosen and sends its own product", async () => {
    const curd = {
      idx: 5,
      site: "instamart" as const,
      label: "Curd 400g",
      candidates: [
        { product_id: "c1", name: "Amul Masti Dahi", size_label: "400 g", price_label: "₹36", packs: 1 },
        { product_id: "c2", name: "Nestle a+ Dahi", size_label: "400 g", price_label: "₹40", packs: 1 },
      ],
    };
    const first = grocerySummary();
    first.cards[2] = { ...first.cards[2]!, to_pick: 2, stock_line: "2 to pick" };
    first.picks = [first.picks[0]!, curd];
    const second = grocerySummary();
    second.picks = [curd];
    second.cards[2] = { ...second.cards[2]!, to_pick: 1, total_label: "₹458" };
    const done = grocerySummary();
    done.picks = [];
    done.cards[2] = { ...done.cards[2]!, to_pick: 0, total_label: "₹452" };
    api.apiFetch.mockResolvedValueOnce({ summary: second }).mockResolvedValueOnce({ summary: done });

    renderView();
    show(request({ summary: first }));
    fireEvent.click(screen.getByRole("button", { name: /2 to pick/ }));
    let sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByRole("radio", { name: /Modern Sandwich Bread/ }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Use this" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: /1 to pick/ }));
    sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText(/Curd 400g/)).toBeInTheDocument();
    expect(within(sheet).getAllByRole("radio").every((r) => !(r as HTMLInputElement).checked)).toBe(true);
    expect(within(sheet).getByRole("button", { name: "Use this" })).toBeDisabled();
    fireEvent.click(within(sheet).getByRole("radio", { name: /Nestle a\+ Dahi/ }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Use this" }));
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(2));
    expect(JSON.parse(api.apiFetch.mock.calls[1]![1].body)).toEqual({ idx: 5, site: "instamart", product_id: "c2" });
  });

  it("pick count is not repeated when the stock line already has it", () => {
    const s = grocerySummary();
    s.cards[2] = { ...s.cards[2]!, stock_line: "1 out of stock · 1 to pick", to_pick: 1 };
    renderView();
    show(request({ summary: s }));
    const c = screen.getAllByRole("article")[2]!;
    expect(c.textContent?.match(/to pick/g)).toHaveLength(1);
    expect(within(c).getByText(/1 out of stock/)).toBeInTheDocument();
    expect(within(c).getByRole("button", { name: "1 to pick ›" })).toBeInTheDocument();
  });
});

describe("item table highlights", () => {
  it("totals highlight the cheapest app from the data and show a missing total as n/a", () => {
    const s = grocerySummary();
    s.best_site = "blinkit";
    s.table.columns = ["zepto", "blinkit", "instamart"];
    s.table.totals = { zepto: "₹470", blinkit: "₹449" };
    renderView();
    show(request({ summary: s }));
    fireEvent.click(screen.getByRole("button", { name: "See item prices" }));
    const totals = within(screen.getByRole("table")).getByRole("row", { name: /Total \+ fees/ });
    const [z, b, i] = within(totals).getAllByRole("cell");
    expect(z).not.toHaveClass("text-accent");
    expect(b).toHaveClass("text-accent");
    expect(i).toHaveTextContent("n/a");
    expect(i).toHaveClass("text-ink-muted");
  });

  it("one priced app highlights nothing", () => {
    renderView("f1");
    show(request({ id: "f1", mode: "food", summary: foodSummary() }));
    fireEvent.click(screen.getByRole("button", { name: "See item prices" }));
    const table = screen.getByRole("table");
    for (const cell of within(table).getAllByRole("cell")) {
      expect(cell).not.toHaveClass("text-accent");
      expect(cell).not.toHaveClass("font-bold");
    }
  });
});

describe("states", () => {
  it("searching state shows waiting for sync", async () => {
    jobs.rows = [{ id: "j1", site: "swiggy", type: "sync", status: "running", progress: {}, error: null }];
    renderView();
    show(request({ status: "searching", summary: null, created_at: minutesAgo(0) }), [result("zepto", "ok")]);
    expect(screen.getByText("Checking your prices…")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Zepto");
    expect(rows[0]).toHaveTextContent("Done");
    expect(rows[1]).toHaveTextContent("Checking…");
    expect(await screen.findByText("Waiting for your Instamart sync to finish…")).toBeInTheDocument();
    expect(screen.queryByText(/Taking longer/)).toBeNull();
    act(() => {
      vi.advanceTimersByTime(121_000);
    });
    expect(screen.getByText("Taking longer than usual. You can leave this screen. Your result will be here.")).toBeInTheDocument();
  });

  it("failed request offers try again and says not counted", async () => {
    api.apiFetch.mockResolvedValueOnce({ request_id: "r2", sites: ["zepto"], cart_checks_left: 2 });
    renderView();
    show(request({ status: "failed", summary: null }));
    expect(screen.getByText("We couldn't get prices right now. This check wasn't counted.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/cart/r2"));
    expect(api.apiFetch.mock.calls[0]![0]).toBe("/cart/checks");
  });

  it("stale prices offer check again", () => {
    const s = grocerySummary();
    s.priced_at = minutesAgo(45);
    renderView();
    show(request({ summary: s }));
    expect(screen.getByText("Prices may have changed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    expect(screen.getByText(/· 45 min ago$/)).toBeInTheDocument();
  });

  it("edit list links back with mode and from", () => {
    const { unmount } = renderView();
    show(request({}));
    expect(screen.getByRole("link", { name: "Edit list" })).toHaveAttribute("href", "/cart?from=r1");
    unmount();
    renderView("f1");
    show(request({ id: "f1", mode: "food", summary: foodSummary() }));
    expect(screen.getByRole("link", { name: "Edit list" })).toHaveAttribute("href", "/cart?mode=food&from=f1");
  });
});
