import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SupabaseClient } from "@supabase/supabase-js";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CategorySpendRow, MonthlySpendRow, TopMerchant } from "@/lib/queries";

const q = vi.hoisted(() => ({
  getMonthlySpend: vi.fn(),
  getCategorySpend: vi.fn(),
  getTopMerchants: vi.fn(),
  getSpendToDate: vi.fn(),
  getOrders: vi.fn(),
  getOrder: vi.fn(),
}));
vi.mock("@/lib/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/queries")>()),
  ...q,
}));

import { SpendDashboard } from "@/components/spend/SpendDashboard";
import { buildSpendSummary } from "@/components/spend/summary";

const TODAY = new Date(2026, 9, 27); // 27 October 2026, local time

const row = (month: string, site: string, amount: number, subPlatform: string | null = null, orders = 1): MonthlySpendRow => ({
  month,
  site,
  subPlatform,
  orders,
  amount,
});

// Six months ending October 2026, plus one row outside the window.
const MONTHLY: MonthlySpendRow[] = [
  row("2025-12", "zomato", 999),
  row("2026-05", "swiggy", 2300, null, 6),
  row("2026-06", "zomato", 4600, null, 11),
  row("2026-07", "swiggy", 4800, null, 12),
  row("2026-08", "blinkit", 2400, null, 5),
  row("2026-09", "zomato", 1500, null, 4),
  row("2026-09", "zepto", 1000, null, 3),
  row("2026-10", "swiggy", 802, null, 3),
  row("2026-10", "swiggy", 300, "instamart", 2),
];

const TOP: TopMerchant[] = [{ merchant: "McDonald's", site: "swiggy", orders: 17, amount: 5113 }];

const CATEGORIES: Record<string, CategorySpendRow[]> = {
  "2026-10": [
    { category: "food delivery", amount: 802 },
    { category: "uncategorized", amount: 120 },
    { category: "dairy", amount: 180 },
  ],
  "2026-09": [
    { category: "food delivery", amount: 1500 },
    { category: "snacks", amount: 640 },
  ],
};

/** `toDate`: spend in the same day-of-month window of last month (1 to 27 September). */
function stub(monthly: MonthlySpendRow[] = MONTHLY, top: TopMerchant[] = TOP, toDate = 2500) {
  q.getSpendToDate.mockResolvedValue(toDate);
  q.getMonthlySpend.mockResolvedValue(monthly);
  q.getTopMerchants.mockResolvedValue(top);
  q.getCategorySpend.mockImplementation(async (month: string) => CATEGORIES[month] ?? []);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("SpendDashboard", () => {
  it("renders totals with stub data", async () => {
    stub();
    render(<SpendDashboard today={TODAY} />);

    expect(await screen.findByRole("heading", { level: 1, name: "Spending" })).toBeInTheDocument();
    expect(await screen.findByText("October 2026")).toBeInTheDocument();

    const total = await screen.findByRole("region", { name: "This month so far" });
    expect(within(total).getByText("₹1,102")).toBeInTheDocument();
    // Month to date against the same window of last month (1 to 27 September).
    const change = await within(total).findByText(/56% vs this time in September/);
    expect(change).toHaveTextContent("▼");
    expect(change).toHaveClass("text-accent");
    expect(q.getSpendToDate).toHaveBeenCalledWith("2026-09", 27);

    // Six bars, May to October, with the current month selected.
    const bars = within(total).getAllByRole("button");
    expect(bars.map((b) => b.textContent)).toEqual(["May", "Jun", "Jul", "Aug", "Sep", "Oct"]);
    expect(within(total).getByRole("button", { name: /^October 2026/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(total).getByRole("button", { name: /^September 2026/ })).toHaveAttribute("aria-pressed", "false");

    // Per-app card: all four apps, Swiggy first, Instamart as a Swiggy sub-label.
    const apps = screen.getByRole("region", { name: "By app" });
    const items = within(apps).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("[data-app-name]")?.textContent)).toEqual([
      "Swiggy",
      "Zomato",
      "Zepto",
      "Blinkit",
    ]);
    expect(items[0]).toHaveTextContent("₹1,102");
    expect(items[0]).toHaveTextContent("Instamart ₹300");
    expect(items[1]).toHaveTextContent("₹0");

    // Top this year.
    const top = await screen.findByRole("region", { name: "Top this year" });
    expect(within(top).getByText("McDonald's")).toBeInTheDocument();
    expect(top).toHaveTextContent("₹5,113");
    expect(top).toHaveTextContent("17 orders");
    expect(q.getTopMerchants).toHaveBeenCalledWith(2026);

    // Categories for the current month, largest first, "Other" last.
    const cats = screen.getByRole("region", { name: /By category/ });
    await waitFor(() => expect(within(cats).getAllByRole("listitem")).toHaveLength(3));
    expect(within(cats).getAllByRole("listitem").map((li) => li.querySelector("[data-category]")?.textContent)).toEqual([
      "Food delivery",
      "Dairy",
      "Other",
    ]);
    expect(q.getCategorySpend).toHaveBeenCalledWith("2026-10");
  });

  it("empty spend state is mockup A at zero with one Connect an app link to /accounts", async () => {
    stub([], []);
    render(<SpendDashboard today={TODAY} />);

    const total = await screen.findByRole("region", { name: "This month so far" });
    expect(within(total).getByText("₹0")).toBeInTheDocument();
    expect(within(total).queryByText(/vs September/)).not.toBeInTheDocument();
    expect(within(total).queryAllByRole("button")).toHaveLength(0); // faded bars are not interactive

    const apps = screen.getByRole("region", { name: "By app" });
    const items = within(apps).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    for (const li of items) expect(li).toHaveTextContent("₹0");

    const links = screen.getAllByRole("link", { name: "Connect an app" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/accounts");

    expect(screen.queryByRole("region", { name: "Top this year" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /By category/ })).not.toBeInTheDocument();
    expect(q.getCategorySpend).not.toHaveBeenCalled();
  });

  it("tapping a bar changes the category month", async () => {
    stub();
    render(<SpendDashboard today={TODAY} />);
    const total = await screen.findByRole("region", { name: "This month so far" });
    const cats = screen.getByRole("region", { name: /By category/ });
    await waitFor(() => expect(within(cats).getByText("Dairy")).toBeInTheDocument());

    await userEvent.click(within(total).getByRole("button", { name: /^September 2026/ }));

    expect(q.getCategorySpend).toHaveBeenLastCalledWith("2026-09");
    await waitFor(() => expect(within(cats).getByText("Snacks")).toBeInTheDocument());
    expect(within(cats).queryByText("Dairy")).not.toBeInTheDocument();
    expect(cats).toHaveAccessibleName(/September/);

    // The total card follows the selection; a past month compares whole month with whole month.
    const sept = screen.getByRole("region", { name: "September 2026" });
    expect(within(sept).getByText("₹2,500")).toBeInTheDocument();
    expect(within(sept).getByRole("button", { name: /^September 2026/ })).toHaveAttribute("aria-pressed", "true");
    const change = within(sept).getByText(/4% vs August/);
    expect(change).toHaveTextContent("▲");
    expect(change).not.toHaveTextContent(/this time/);
    expect(q.getSpendToDate).toHaveBeenCalledTimes(1);

    // Only the selected month's label is emphasised, the current month included.
    expect(within(sept).getByText("Sep")).toHaveClass("font-semibold", "text-ink");
    expect(within(sept).getByText("Oct")).toHaveClass("text-ink-muted");
  });

  it("shows ▲ in orange when spending went up, and hides the change when there is nothing to compare", async () => {
    stub([row("2026-09", "zomato", 4000), row("2026-10", "zomato", 1500)], TOP, 1000);
    const { unmount } = render(<SpendDashboard today={TODAY} />);
    const up = await screen.findByText(/50% vs this time in September/);
    expect(up).toHaveTextContent("▲");
    expect(up).toHaveClass("text-up");
    unmount();

    stub([row("2026-10", "zomato", 1500)], TOP, 0);
    render(<SpendDashboard today={TODAY} />);
    const total = await screen.findByRole("region", { name: "This month so far" });
    expect(within(total).getByText("₹1,500")).toBeInTheDocument();
    expect(within(total).queryByText(/vs September/)).not.toBeInTheDocument();
  });

  it("shows the zero state with the last order month when nothing falls in the six months", async () => {
    stub([row("2025-12", "zomato", 999), row("2026-01", "zepto", 120)], []);
    render(<SpendDashboard today={TODAY} />);
    const total = await screen.findByRole("region", { name: "This month so far" });
    expect(within(total).getByText("₹0")).toBeInTheDocument();
    expect(within(total).queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByRole("link", { name: "Connect an app" })).toHaveAttribute("href", "/accounts");
    expect(screen.getByText("Your last order was in January 2026.")).toBeInTheDocument();
  });

  it("shows a retry when loading fails", async () => {
    q.getMonthlySpend.mockRejectedValueOnce(new Error("boom"));
    q.getTopMerchants.mockResolvedValue(TOP);
    q.getCategorySpend.mockResolvedValue([]);
    render(<SpendDashboard today={TODAY} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t load/i);
    q.getMonthlySpend.mockResolvedValueOnce(MONTHLY);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "This month so far" })).toBeInTheDocument();
    expect(q.getMonthlySpend).toHaveBeenCalledTimes(2);
  });
});

describe("buildSpendSummary", () => {
  it("fills the six months ending today, folds Instamart into Swiggy and counts every app", () => {
    const s = buildSpendSummary(MONTHLY, TODAY);
    expect(s.months.map((m) => m.month)).toEqual(["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]);
    expect(s.months.map((m) => m.total)).toEqual([2300, 4600, 4800, 2400, 2500, 1102]);
    const oct = s.months[5]!;
    expect(oct.byApp).toEqual({ swiggy: 1102, zomato: 0, zepto: 0, blinkit: 0 });
    expect(oct.instamart).toBe(300);
    expect(s.hasData).toBe(true);
    expect(s.inWindow).toBe(true);
    expect(s.lastMonth).toBe("2026-10");
  });

  it("knows when every order is older than the six months", () => {
    const s = buildSpendSummary([row("2025-12", "zomato", 999)], TODAY);
    expect(s.hasData).toBe(true);
    expect(s.inWindow).toBe(false);
    expect(s.lastMonth).toBe("2025-12");
    expect(buildSpendSummary([], TODAY)).toMatchObject({ hasData: false, inWindow: false, lastMonth: null });
  });

  it("wraps across a year boundary and fills missing months with zero", () => {
    const s = buildSpendSummary([row("2026-01", "zepto", 50)], new Date(2026, 1, 3));
    expect(s.months.map((m) => m.month)).toEqual(["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]);
    expect(s.months.map((m) => m.total)).toEqual([0, 0, 0, 0, 50, 0]);
  });
});

describe("lib/queries getSpendToDate", () => {
  it("sums the month's non-cancelled orders up to and including the given day", async () => {
    const { getSpendToDate } = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
    const calls: Array<[string, unknown[]]> = [];
    const result = {
      data: [
        { total_amount: 1000, status: "delivered" },
        { total_amount: 250.5, status: null },
        { total_amount: 999, status: "cancelled" },
        { total_amount: 400, status: "refunded" },
      ],
      error: null,
    };
    const builder: Record<string, unknown> = new Proxy(
      {},
      {
        get(_t, prop: string) {
          if (prop === "then") return (resolve: (v: unknown) => void) => resolve(result);
          return (...args: unknown[]) => (calls.push([prop, args]), builder);
        },
      },
    );
    const client = { from: (t: string) => (calls.push(["from", [t]]), builder) } as unknown as SupabaseClient;

    expect(await getSpendToDate("2026-09", 27, client)).toBe(1250.5);
    expect(calls).toContainEqual(["from", ["orders"]]);
    expect(calls).toContainEqual(["gte", ["placed_at", "2026-09-01"]]);
    expect(calls).toContainEqual(["lt", ["placed_at", "2026-09-28"]]);
  });
});
