import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});
vi.mock("@/lib/jobs", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/jobs")>();
  return { ...real, watchJob: vi.fn(() => () => {}) };
});

import { FeesPanel } from "@/components/admin/FeesPanel";
import { SitesPanel } from "@/components/admin/SitesPanel";
import { ApiError } from "@/lib/api";
import type { FeeRow, SiteHealth } from "@/lib/admin";

type Route = (init?: RequestInit) => unknown;

function routes(table: Record<string, Route>) {
  api.apiFetch.mockImplementation(async (path: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    const handler = table[`${method} ${path}`] ?? table[`${method} ${path.split("?")[0]}`];
    if (!handler) throw new ApiError(404, `no route ${method} ${path}`);
    return handler(init);
  });
}

function calls(method: string, path: string) {
  return api.apiFetch.mock.calls.filter(
    ([p, init]) => p === path && ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase() === method,
  );
}

const ZEPTO: FeeRow = {
  site: "zepto",
  version: 3,
  verified: false,
  rules: {
    delivery: [
      { min_subtotal: 0, fee: 30 },
      { min_subtotal: 99, fee: 0 },
    ],
    handling: 15,
    small_cart: { below: 99, fee: 25 },
    surge: { possible: true },
    membership: { name: "Zepto Pass", waives: ["delivery", "small_cart"] },
    source_url: null,
    note: "Seed values; verify against the app before launch.",
  },
};

const SWIGGY: FeeRow = {
  site: "swiggy",
  version: 2,
  verified: true,
  rules: {
    kind: "food",
    delivery_by_km: [
      { max_km: 3, fee: 30 },
      { max_km: 7, fee: 45 },
      { max_km: null, fee: 65 },
    ],
    packaging_per_item: 10,
    platform: 20,
    small_order: { below: 149, fee: 20 },
    surge: { possible: true },
    gst: { food_pct: 5, fees_pct: 18 },
    membership: { name: "Swiggy One", waives: ["delivery"] },
    source_url: "https://example.com/swiggy-fees",
    note: null,
  },
};

function site(over: Partial<SiteHealth> = {}): SiteHealth {
  return {
    site: "zepto",
    kind: "orders",
    version: 4,
    learned_at: null,
    healthy: true,
    paused: false,
    health_note: null,
    syncs_ok_7d: 5,
    syncs_failed_7d: 0,
    last_ok_at: null,
    admin_connected: true,
    ...over,
  };
}

const body = (n: number, path: string) => JSON.parse(String((calls("PUT", path)[n]![1] as RequestInit).body));

beforeEach(() => {
  api.apiFetch.mockReset();
});

describe("sites tab, search and food learning", () => {
  it("learn disabled without admin connection", async () => {
    routes({ "GET /admin/sites": () => [site({ site: "blinkit", kind: "search", admin_connected: false })] });
    render(<SitesPanel onForbidden={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Grocery prices" })).toBeInTheDocument();
    const learn = await screen.findByRole("button", { name: "Learn Blinkit grocery prices" });
    expect(learn).toBeDisabled();
    expect(screen.getByText("Connect Blinkit on your own account first")).toBeInTheDocument();
  });

  it("restaurants and menus row learns kind food", async () => {
    routes({
      "GET /admin/sites": () => [
        site({ site: "zepto" }),
        site({ site: "swiggy", kind: "restaurants" }),
        site({ site: "swiggy", kind: "menu" }),
      ],
      "POST /admin/learn/swiggy": () => ({ job_id: "J1" }),
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Order history" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Restaurants & menus" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Learn Swiggy restaurants and menus" }));
    await waitFor(() => expect(calls("POST", "/admin/learn/swiggy?kind=food")).toHaveLength(1));
  });
});

describe("sites tab, other kinds", () => {
  it("unknown kinds show under Other with pause but no Learn", async () => {
    routes({ "GET /admin/sites": () => [site({ site: "zepto", kind: "addresses" }), site({ site: "swiggy", kind: "future_kind" })] });
    render(<SitesPanel onForbidden={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Other" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Pause Zepto" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Learn/ })).not.toBeInTheDocument();
  });
});

describe("fees panel", () => {
  async function open(rows: FeeRow[], name: string) {
    routes({ "GET /admin/fees": () => rows });
    render(<FeesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name }));
  }

  it("unverified badge shows", async () => {
    routes({ "GET /admin/fees": () => [ZEPTO, SWIGGY] });
    render(<FeesPanel onForbidden={vi.fn()} />);
    expect(await screen.findByText("Unverified: check Zepto's fee page")).toBeInTheDocument();
    expect(screen.getByText("Verified")).toBeInTheDocument();
  });

  it("a row with null rules still renders, and row.kind decides food", async () => {
    routes({
      "GET /admin/fees": () => [
        { site: "zepto", version: 1, verified: false, rules: null },
        { site: "zomato", kind: "food", version: 1, verified: false, rules: null },
      ],
    });
    render(<FeesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Zomato fees" }));
    expect(screen.getByLabelText("GST on food (%)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit Zepto fees" }));
    expect(screen.getByLabelText("Handling fee (₹)")).toBeInTheDocument();
  });

  it("food tier 1 can be removed while another non-Beyond tier remains", async () => {
    await open([SWIGGY], "Edit Swiggy fees");
    fireEvent.click(screen.getByRole("button", { name: "Remove tier 1" }));
    expect(screen.getByLabelText("Tier 1 up to (km)")).toHaveValue("7");
    expect(screen.queryByRole("button", { name: /^Remove tier/ })).not.toBeInTheDocument();
  });

  it("first tier minimum is fixed at zero", async () => {
    await open([ZEPTO], "Edit Zepto fees");
    const first = screen.getByLabelText("Tier 1 from (₹)") as HTMLInputElement;
    expect(first.value).toBe("0");
    expect(first).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Tier 2 from (₹)")).not.toHaveAttribute("readonly");
    expect(first).toHaveAttribute("inputmode", "decimal");
  });

  it("grocery fees save", async () => {
    routes({ "GET /admin/fees": () => [ZEPTO], "PUT /admin/fees/zepto": () => ({ version: 4 }) });
    render(<FeesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Zepto fees" }));

    fireEvent.change(screen.getByLabelText("Tier 2 from (₹)"), { target: { value: "149" } });
    fireEvent.change(screen.getByLabelText("Handling fee (₹)"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Handling" }));
    fireEvent.change(screen.getByLabelText("Source URL"), { target: { value: "https://zepto.test/fees" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "These match the app today" }));
    fireEvent.click(screen.getByRole("button", { name: "Save Zepto fees" }));

    await waitFor(() => expect(calls("PUT", "/admin/fees/zepto")).toHaveLength(1));
    expect(body(0, "/admin/fees/zepto")).toEqual({
      verified: true,
      rules: {
        delivery: [
          { min_subtotal: 0, fee: 30 },
          { min_subtotal: 149, fee: 0 },
        ],
        handling: 12,
        small_cart: { below: 99, fee: 25 },
        surge: { possible: true },
        membership: { name: "Zepto Pass", waives: ["delivery", "handling", "small_cart"] },
        source_url: "https://zepto.test/fees",
        note: "Seed values; verify against the app before launch.",
      },
    });
    expect(await screen.findByText("Saved. Now version 4.")).toBeInTheDocument();
  });

  it("food fees save with gst", async () => {
    routes({ "GET /admin/fees": () => [SWIGGY], "PUT /admin/fees/swiggy": () => ({ version: 3 }) });
    render(<FeesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Swiggy fees" }));

    expect(screen.getByText("Up to 3 km → ₹30")).toBeInTheDocument();
    expect(screen.getByText("Beyond → ₹65")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("GST on food (%)"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("GST on fees (%)"), { target: { value: "18" } });
    fireEvent.change(screen.getByLabelText("Packaging per dish (₹)"), { target: { value: "12.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Swiggy fees" }));

    await waitFor(() => expect(calls("PUT", "/admin/fees/swiggy")).toHaveLength(1));
    expect(body(0, "/admin/fees/swiggy")).toEqual({
      verified: true,
      rules: { ...SWIGGY.rules, packaging_per_item: 12.5 },
    });
    expect(await screen.findByText("Saved. Now version 3.")).toBeInTheDocument();
  });

  it("422 errors are listed", async () => {
    routes({
      "GET /admin/fees": () => [ZEPTO],
      "PUT /admin/fees/zepto": () => {
        throw new ApiError(422, "handling must be from 0 to 500; delivery[1].min_subtotal must be above the previous tier");
      },
    });
    render(<FeesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Zepto fees" }));
    fireEvent.click(screen.getByRole("button", { name: "Save Zepto fees" }));

    const alert = await screen.findByRole("alert");
    const items = within(alert).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([
      "handling must be from 0 to 500",
      "delivery[1].min_subtotal must be above the previous tier",
    ]);
    expect(screen.queryByText(/^Saved/)).not.toBeInTheDocument();
  });
});
