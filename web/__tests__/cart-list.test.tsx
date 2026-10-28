import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ---- mocks -------------------------------------------------------------------------------

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/cart",
}));

// Supabase reads (recent checks and ?from= prefill) come from these rows, newest first.
const db = vi.hoisted(() => ({ cart_requests: [] as Array<Record<string, unknown>> }));
vi.mock("@/lib/supabase/client", () => {
  function from(table: string) {
    const filters: Array<[string, unknown]> = [];
    let limit: number | undefined;
    const result = () => {
      let rows = table === "cart_requests" ? db.cart_requests : [];
      for (const [col, val] of filters) rows = rows.filter((r) => r[col] === val);
      return limit == null ? rows : rows.slice(0, limit);
    };
    const builder = {
      select: () => builder,
      eq: (col: string, val: unknown) => {
        filters.push([col, val]);
        return builder;
      },
      order: () => builder,
      limit: (n: number) => {
        limit = n;
        return builder;
      },
      maybeSingle: () => Promise.resolve({ data: result()[0] ?? null, error: null }),
      then: (ok: (v: unknown) => unknown, bad?: (e: unknown) => unknown) =>
        Promise.resolve({ data: result(), error: null }).then(ok, bad),
    };
    return builder;
  }
  return { getSupabaseBrowserClient: () => ({ from }) };
});

import { ApiError } from "@/lib/api";
import { ListEditor } from "@/components/cart/ListEditor";

// ---- the stand-in API ---------------------------------------------------------------------

interface Backend {
  access: Record<string, string>;
  usual: Array<{ text: string; qty: number; times: number }>;
  checksLeft: number;
  check: () => unknown;
}

let backend: Backend;

function route(path: string, init?: RequestInit): Promise<unknown> {
  if (path === "/cart/access?mode=grocery") return Promise.resolve({ access: backend.access });
  if (path === "/cart/usual") return Promise.resolve({ items: backend.usual });
  if (path === "/me/limits")
    return Promise.resolve({ cart_checks_left: backend.checksLeft, cart_checks_limit: 10, budget_paused: false });
  if (path === "/cart/checks" && init?.method === "POST") {
    try {
      return Promise.resolve(backend.check());
    } catch (e) {
      return Promise.reject(e);
    }
  }
  return Promise.reject(new ApiError(404, `unexpected ${path}`));
}

const HOME = { address: "12 MG Road, Gorwa, Vadodara, Gujarat 390016, India", lat: 22.33, lon: 73.15 };

beforeEach(() => {
  backend = {
    access: { zepto: "ok", blinkit: "ok", instamart: "not_connected" },
    usual: [],
    checksLeft: 7,
    check: () => ({ request_id: "req-9", sites: ["zepto", "blinkit"], cart_checks_left: 6 }),
  };
  db.cart_requests = [];
  api.apiFetch.mockReset();
  api.apiFetch.mockImplementation(route);
  nav.push.mockReset();
});

function addLine(text: string) {
  const input = screen.getByPlaceholderText("Add an item, e.g. Amul Taaza 1L ×2");
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(input.closest("form")!);
}

function compareButton() {
  return screen.getByRole("button", { name: "Compare prices" });
}

async function ready() {
  await screen.findByText("7 checks left today");
}

describe("the grocery list", () => {
  it("shows the title, subtitle and delivery area", async () => {
    render(<ListEditor profile={HOME} />);
    expect(screen.getByRole("heading", { level: 1, name: "Cart" })).toBeInTheDocument();
    expect(screen.getByText("Compare your prices on Zepto, Blinkit and Instamart.")).toBeInTheDocument();
    expect(screen.getByText("Gorwa, Vadodara")).toBeInTheDocument();
    expect(screen.getByText(/Delivering to/)).toBeInTheDocument();
    await ready();
  });

  it("adds, steps and removes items", async () => {
    render(<ListEditor profile={HOME} />);
    await ready();
    expect(compareButton()).toBeDisabled(); // nothing to compare yet

    addLine("Amul Taaza 1L ×2");
    addLine("Bread");
    const milk = screen.getByRole("listitem", { name: "Amul Taaza 1L" });
    expect(within(milk).getByText("2")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Add an item, e.g. Amul Taaza 1L ×2")).toHaveValue("");

    fireEvent.click(within(milk).getByRole("button", { name: "More Amul Taaza 1L" }));
    expect(within(milk).getByText("3")).toBeInTheDocument();
    fireEvent.click(within(milk).getByRole("button", { name: "Fewer Amul Taaza 1L" }));
    fireEvent.click(within(milk).getByRole("button", { name: "Fewer Amul Taaza 1L" }));
    expect(within(milk).getByText("1")).toBeInTheDocument();
    expect(within(milk).getByRole("button", { name: "Fewer Amul Taaza 1L" })).toBeDisabled();

    // The stepper and remove controls keep 44px hit areas.
    expect(within(milk).getByRole("button", { name: "More Amul Taaza 1L" }).className).toMatch(/size-11/);
    expect(within(milk).getByRole("button", { name: "Remove Amul Taaza 1L" }).className).toMatch(/size-11/);

    fireEvent.click(within(milk).getByRole("button", { name: "Remove Amul Taaza 1L" }));
    expect(screen.queryByRole("listitem", { name: "Amul Taaza 1L" })).not.toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Bread" })).toBeInTheDocument();
    expect(compareButton()).toBeEnabled();
  });

  it("caps the quantity at 20", async () => {
    render(<ListEditor profile={HOME} />);
    await ready();
    addLine("Eggs ×20");
    const eggs = screen.getByRole("listitem", { name: "Eggs" });
    expect(within(eggs).getByRole("button", { name: "More Eggs" })).toBeDisabled();
  });

  it("usual sheet adds checked items", async () => {
    backend.usual = [
      { text: "Amul Taaza 1L", qty: 2, times: 9 },
      { text: "Bread 400g", qty: 1, times: 5 },
      { text: "Curd 400g", qty: 1, times: 3 },
    ];
    render(<ListEditor profile={HOME} />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Start from my usual" }));

    const sheet = await screen.findByRole("dialog");
    await within(sheet).findByText("×9 in 90 days");
    const boxes = within(sheet).getAllByRole("checkbox");
    expect(boxes).toHaveLength(3);
    boxes.forEach((b) => expect(b).toBeChecked());
    expect(within(sheet).getByRole("button", { name: "Add 3 items" })).toBeInTheDocument();

    fireEvent.click(within(sheet).getByRole("checkbox", { name: /Bread 400g/ }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Add 2 items" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const milk = screen.getByRole("listitem", { name: "Amul Taaza 1L" });
    expect(within(milk).getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Curd 400g" })).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "Bread 400g" })).not.toBeInTheDocument();
  });

  it("empty usual shows the hint", async () => {
    render(<ListEditor profile={HOME} />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Start from my usual" }));
    const sheet = await screen.findByRole("dialog");
    expect(
      await within(sheet).findByText("We'll suggest items once you've ordered groceries a few times."),
    ).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: /^Add/ })).not.toBeInTheDocument();
  });

  it("compare posts the list and navigates", async () => {
    render(<ListEditor profile={HOME} />);
    await ready();
    addLine("Amul Taaza 1L ×2");
    addLine("2 x Bread");
    fireEvent.click(compareButton());

    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/cart/req-9"));
    expect(api.apiFetch).toHaveBeenCalledWith("/cart/checks", {
      method: "POST",
      body: JSON.stringify({
        items: [
          { text: "Amul Taaza 1L", qty: 2 },
          { text: "Bread", qty: 2 },
        ],
      }),
    });
  });

  it("429 detail is shown and nothing navigates", async () => {
    backend.check = () => {
      throw new ApiError(429, "You can check 10 carts a day. Try again tomorrow.");
    };
    render(<ListEditor profile={HOME} />);
    await ready();
    addLine("Bread");
    fireEvent.click(compareButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("You can check 10 carts a day. Try again tomorrow.");
    expect(nav.push).not.toHaveBeenCalled();
    expect(compareButton()).toBeEnabled();
  });

  it("no address disables compare with the link", async () => {
    render(<ListEditor profile={{ address: null, lat: null, lon: null }} />);
    await ready();
    addLine("Bread");
    const link = screen.getByRole("link", { name: /Add your delivery address/ });
    expect(link).toHaveAttribute("href", "/accounts");
    expect(compareButton()).toBeDisabled();
  });

  it("no connected grocery app disables compare and links to accounts", async () => {
    backend.access = { zepto: "not_connected", blinkit: "not_connected", instamart: "not_connected" };
    render(<ListEditor profile={HOME} />);
    await ready();
    addLine("Bread");
    await screen.findByText("Connect Blinkit, Zepto or Instamart to compare.");
    expect(compareButton()).toBeDisabled();
    const apps = screen.getByRole("list", { name: "Apps we compare" });
    const links = within(apps).getAllByRole("link");
    expect(links).toHaveLength(3);
    links.forEach((l) => expect(l).toHaveAttribute("href", "/accounts"));
    expect(within(apps).getAllByText("Connect")).toHaveLength(3);
  });

  it("needs_login app shows log in again", async () => {
    backend.access = { zepto: "ok", blinkit: "needs_login", instamart: "not_connected" };
    render(<ListEditor profile={HOME} />);
    await ready();
    const apps = screen.getByRole("list", { name: "Apps we compare" });
    const blinkit = await within(apps).findByRole("link", { name: /Blinkit/ });
    expect(blinkit).toHaveTextContent("Log in again");
    expect(blinkit).toHaveAttribute("href", "/accounts");
    expect(within(apps).getByText("Connected")).toBeInTheDocument();
    expect(within(apps).getByRole("link", { name: /Instamart/ })).toHaveTextContent("Connect");
    addLine("Bread");
    expect(compareButton()).toBeEnabled();
  });

  it("edit list prefills from a request", async () => {
    db.cart_requests = [
      {
        id: "old-1",
        mode: "grocery",
        status: "done",
        items: [
          { text: "Amul Taaza 1L", qty: 2 },
          { text: "Eggs 6", qty: 1 },
        ],
        summary: null,
        created_at: "2026-10-28T09:00:00Z",
      },
    ];
    render(<ListEditor profile={HOME} fromId="old-1" />);
    const milk = await screen.findByRole("listitem", { name: "Amul Taaza 1L" });
    expect(within(milk).getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Eggs 6" })).toBeInTheDocument();
  });

  it("checks left line comes from limits", async () => {
    backend.checksLeft = 3;
    render(<ListEditor profile={HOME} />);
    expect(await screen.findByText("3 checks left today")).toBeInTheDocument();
  });

  it("shows up to three recent checks linking to their results", async () => {
    const summary = {
      count_label: "7 items",
      best_site: "zepto",
      cards: [{ site: "zepto", name: "Zepto", total_label: "₹412" }],
    };
    const at = new Date(Date.now() - 5 * 60_000).toISOString();
    db.cart_requests = ["a", "b", "c", "d"].map((id) => ({
      id,
      mode: "grocery",
      status: "done",
      items: [],
      summary,
      created_at: at,
    }));
    render(<ListEditor profile={HOME} />);
    const recent = await screen.findByRole("region", { name: "Recent checks" });
    const links = within(recent).getAllByRole("link");
    expect(links).toHaveLength(3);
    expect(links[0]).toHaveAttribute("href", "/cart/a");
    expect(links[0]).toHaveTextContent("7 items · Zepto ₹412 · 5m ago");
  });

  it("stops at 30 items", async () => {
    render(<ListEditor profile={HOME} />);
    await ready();
    for (let i = 1; i <= 30; i++) addLine(`Item ${i}`);
    expect(screen.getAllByRole("listitem", { name: /^Item / })).toHaveLength(30);
    expect(screen.getByPlaceholderText("Add an item, e.g. Amul Taaza 1L ×2")).toBeDisabled();
    expect(screen.getByText("You can compare up to 30 items at a time.")).toBeInTheDocument();
  });
});
