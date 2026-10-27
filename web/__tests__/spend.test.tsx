import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CategorySpendRow, MonthlySpendRow, TopMerchant } from "@/lib/queries";

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

function stub(monthly: MonthlySpendRow[] = MONTHLY, top: TopMerchant[] = TOP) {
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
    const change = within(total).getByText(/56% vs September/);
    expect(change).toHaveTextContent("▼");
    expect(change).toHaveClass("text-accent");

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

    // The total card follows the selection.
    const sept = screen.getByRole("region", { name: "September 2026" });
    expect(within(sept).getByText("₹2,500")).toBeInTheDocument();
    expect(within(sept).getByRole("button", { name: /^September 2026/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows ▲ in orange when spending went up, and hides the change when there is nothing to compare", async () => {
    stub([row("2026-09", "zomato", 1000), row("2026-10", "zomato", 1500)]);
    const { unmount } = render(<SpendDashboard today={TODAY} />);
    const up = await screen.findByText(/50% vs September/);
    expect(up).toHaveTextContent("▲");
    expect(up).toHaveClass("text-up");
    unmount();

    stub([row("2026-10", "zomato", 1500)]);
    render(<SpendDashboard today={TODAY} />);
    const total = await screen.findByRole("region", { name: "This month so far" });
    expect(within(total).getByText("₹1,500")).toBeInTheDocument();
    expect(within(total).queryByText(/vs September/)).not.toBeInTheDocument();
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
  });

  it("wraps across a year boundary and fills missing months with zero", () => {
    const s = buildSpendSummary([row("2026-01", "zepto", 50)], new Date(2026, 1, 3));
    expect(s.months.map((m) => m.month)).toEqual(["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]);
    expect(s.months.map((m) => m.total)).toEqual([0, 0, 0, 0, 50, 0]);
  });
});
