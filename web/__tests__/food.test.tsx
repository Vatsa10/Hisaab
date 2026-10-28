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

// Jobs finish at once; the test only cares that the flow waited for them.
const jobs = vi.hoisted(() => ({ watched: [] as string[], status: "done" as "done" | "failed" }));
vi.mock("@/lib/jobs", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/jobs")>();
  return {
    ...real,
    watchJob: (id: string, onChange: (j: unknown) => void) => {
      jobs.watched.push(id);
      setTimeout(() => onChange({ id, site: "swiggy", type: "lookup", status: jobs.status, progress: {}, error: null }), 30);
      return () => {};
    },
  };
});

const db = vi.hoisted(() => ({ cart_requests: [] as Array<Record<string, unknown>> }));
vi.mock("@/lib/supabase/client", () => {
  function from(table: string) {
    const filters: Array<[string, unknown]> = [];
    const rows = () => {
      let r = table === "cart_requests" ? db.cart_requests : [];
      for (const [c, v] of filters) r = r.filter((x) => x[c] === v);
      return r;
    };
    const b = {
      select: () => b,
      eq: (c: string, v: unknown) => {
        filters.push([c, v]);
        return b;
      },
      order: () => b,
      limit: () => b,
      maybeSingle: () => Promise.resolve({ data: rows()[0] ?? null, error: null }),
      then: (ok: (v: unknown) => unknown, bad?: (e: unknown) => unknown) =>
        Promise.resolve({ data: rows(), error: null }).then(ok, bad),
    };
    return b;
  }
  return { getSupabaseBrowserClient: () => ({ from }) };
});

vi.mock("@/lib/supabase/server", () => ({
  getSupabaseServerClient: async () => ({
    from: () => ({ select: () => ({ maybeSingle: async () => ({ data: null }) }) }),
  }),
}));

import { ApiError } from "@/lib/api";
import CartPage from "@/app/(app)/cart/page";
import { FoodFlow } from "@/components/cart/food/FoodFlow";
import { startFoodCheck } from "@/lib/food";

// ---- the stand-in API ---------------------------------------------------------------------

interface Backend {
  access: Record<string, string>;
  usual: Array<{ name: string; sites: string[]; orders: number }>;
  checksLeft: number;
  restaurants: () => unknown;
  menus: () => unknown;
  check: () => unknown;
}

let backend: Backend;

const listing = (site: string, id: string, extra: Record<string, unknown> = {}) => ({
  site,
  restaurant_id: id,
  name: "Dum Pukht",
  locality: "Alkapuri",
  distance_km: 2.1,
  is_open: true,
  ...extra,
});

const SURE_PAIR = {
  key: "swiggy:s1|zomato:z1",
  name: "Dum Pukht",
  locality: "Alkapuri",
  distance_km: 2.1,
  listings: { swiggy: listing("swiggy", "s1"), zomato: listing("zomato", "z1") },
  confidence: "sure",
  note: null,
};
const ASK_PAIR = {
  key: "swiggy:s2|zomato:z2",
  name: "Bhatt Sweets",
  locality: "Fatehgunj",
  distance_km: 3.4,
  listings: {
    swiggy: listing("swiggy", "s2", { name: "Bhatt Sweets", locality: "Fatehgunj", distance_km: 3.4 }),
    zomato: listing("zomato", "z2", { name: "Bhatt Sweets & Snacks", locality: "Sayajigunj", distance_km: 4.1, is_open: false }),
  },
  confidence: "ask",
  note: "Check this is the same restaurant",
};
const SINGLE_PAIR = {
  key: "swiggy:s3",
  name: "Kebab Junction",
  locality: "Manjalpur",
  distance_km: 5,
  listings: { swiggy: listing("swiggy", "s3", { name: "Kebab Junction", locality: "Manjalpur" }), zomato: null },
  confidence: "single",
  note: "Only on Swiggy",
};

const MENU = [
  { dish_id: "d1", name: "Paneer Tikka", variant: "Half", veg: true, price: 220, price_label: "₹220", in_stock: true },
  { dish_id: "d2", name: "Chicken Biryani", variant: null, veg: false, price: 340, price_label: "₹340", in_stock: true },
  { dish_id: "d3", name: "Dal Makhani", variant: "Full", veg: true, price: 260, price_label: "₹260", in_stock: false },
];

function menuReady(extra: Record<string, unknown> = {}) {
  return {
    status: "ready",
    access: {},
    restaurant: {
      name: "Dum Pukht",
      locality: "Alkapuri",
      listings: {
        swiggy: { restaurant_id: "s1", name: "Dum Pukht", locality: "Alkapuri", distance_km: 2.1 },
        zomato: { restaurant_id: "z1", name: "Dum Pukht", locality: "Alkapuri", distance_km: 2.1 },
      },
    },
    menu: MENU,
    usual_dishes: [{ text: "Chicken Biryani", qty: 2, times: 4 }],
    ...extra,
  };
}

function route(path: string, init?: RequestInit): Promise<unknown> {
  if (path === "/cart/access?mode=food") return Promise.resolve({ access: backend.access });
  if (path === "/cart/food/usual") return Promise.resolve({ restaurants: backend.usual });
  if (path === "/me/limits")
    return Promise.resolve({ cart_checks_left: backend.checksLeft, cart_checks_limit: 10, budget_paused: false });
  if (path.startsWith("/cart/food/restaurants?q=")) return wrap(backend.restaurants);
  if (path === "/cart/food/menus" && init?.method === "POST") return wrap(backend.menus);
  if (path === "/cart/food/checks" && init?.method === "POST") return wrap(backend.check);
  return Promise.reject(new ApiError(404, `unexpected ${path}`));
}

function wrap(fn: () => unknown): Promise<unknown> {
  try {
    return Promise.resolve(fn());
  } catch (e) {
    return Promise.reject(e);
  }
}

function calls(path: string) {
  return api.apiFetch.mock.calls.filter((c) => String(c[0]).startsWith(path));
}

function body(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit).body));
}

beforeEach(() => {
  backend = {
    access: { swiggy: "ok", zomato: "ok" },
    usual: [{ name: "Dum Pukht", sites: ["swiggy", "zomato"], orders: 5 }],
    checksLeft: 7,
    restaurants: () => ({ status: "ready", results: [SURE_PAIR, ASK_PAIR, SINGLE_PAIR] }),
    menus: () => menuReady(),
    check: () => ({ request_id: "req-f1", sites: ["swiggy", "zomato"], cart_checks_left: 6 }),
  };
  db.cart_requests = [];
  jobs.watched = [];
  jobs.status = "done";
  nav.push.mockReset();
  api.apiFetch.mockReset();
  api.apiFetch.mockImplementation(route);
});

async function search(q: string) {
  fireEvent.change(await screen.findByLabelText("Search a restaurant"), { target: { value: q } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

// A result card, not the same-named row in "Restaurants you order from".
async function pairButton(name: RegExp) {
  return within(await screen.findByRole("region", { name: "Results" })).findByRole("button", { name });
}

async function openMenu() {
  render(<FoodFlow />);
  await search("dum");
  fireEvent.click(await pairButton(/Dum Pukht/));
  await screen.findByRole("heading", { name: "Dum Pukht" });
}

// ---- tests ---------------------------------------------------------------------------------

describe("food mode", () => {
  it("mode switch shows food flow and keeps groceries default", async () => {
    const grocery = render(await CartPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("navigation", { name: "Cart mode" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Groceries" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Food" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("heading", { name: /Your list/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Search a restaurant")).not.toBeInTheDocument();
    grocery.unmount();

    render(await CartPage({ searchParams: Promise.resolve({ mode: "food" }) }));
    expect(screen.getByRole("link", { name: "Food" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByLabelText("Search a restaurant")).toBeInTheDocument();
  });

  it("no connected food app disables the flow", async () => {
    backend.access = { swiggy: "not_connected", zomato: "needs_login" };
    render(<FoodFlow />);
    expect(await screen.findByText("Connect Swiggy or Zomato to compare.")).toBeInTheDocument();
    expect(screen.getByLabelText("Search a restaurant")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
    expect(screen.getByRole("link", { name: /Log in again/ })).toHaveAttribute("href", "/accounts");
  });

  it("usual restaurant tap searches by name", async () => {
    render(<FoodFlow />);
    const usual = await screen.findByRole("button", { name: /Dum Pukht.*5 orders/ });
    fireEvent.click(usual);
    await waitFor(() => expect(calls("/cart/food/restaurants")).toHaveLength(1));
    expect(calls("/cart/food/restaurants")[0]![0]).toBe("/cart/food/restaurants?q=Dum%20Pukht");
    expect(screen.getByLabelText("Search a restaurant")).toHaveValue("Dum Pukht");
  });

  it("does not search per keystroke", async () => {
    render(<FoodFlow />);
    const field = await screen.findByLabelText("Search a restaurant");
    fireEvent.change(field, { target: { value: "k" } });
    fireEvent.change(field, { target: { value: "ke" } });
    fireEvent.change(field, { target: { value: "keb" } });
    expect(calls("/cart/food/restaurants")).toHaveLength(0);
    fireEvent.submit(field.closest("form")!);
    await waitFor(() => expect(calls("/cart/food/restaurants")).toHaveLength(1));
  });

  it("searching waits for jobs then shows pairs", async () => {
    let asked = 0;
    backend.restaurants = () =>
      ++asked === 1
        ? { status: "searching", job_ids: ["j1", "j2"], access: {} }
        : { status: "ready", results: [SURE_PAIR] };
    render(<FoodFlow />);
    await search("dum");
    expect(await screen.findByText("Searching Swiggy and Zomato…")).toBeInTheDocument();
    expect(await pairButton(/Dum Pukht/)).toBeInTheDocument();
    expect(jobs.watched).toEqual(["j1", "j2"]);
    expect(calls("/cart/food/restaurants")).toHaveLength(2);
    expect(screen.queryByText("Searching Swiggy and Zomato…")).not.toBeInTheDocument();
  });

  it("ask pair opens the sheet and records the answer", async () => {
    render(<FoodFlow />);
    await search("bhatt");
    fireEvent.click(await screen.findByRole("button", { name: /Bhatt Sweets/ }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText("Bhatt Sweets & Snacks")).toBeInTheDocument();
    expect(within(sheet).getByText(/Sayajigunj/)).toBeInTheDocument();
    expect(within(sheet).getByText("Closed")).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "Yes, same restaurant" }));
    await waitFor(() => expect(calls("/cart/food/menus")).toHaveLength(1));
    expect(body(calls("/cart/food/menus")[0]!)).toEqual({ name: "Bhatt Sweets", swiggy_id: "s2", zomato_id: "z2" });

    // "No, different" keeps only the tapped app and records the other as declined.
    fireEvent.click(await screen.findByRole("button", { name: "Change restaurant" }));
    fireEvent.click(await screen.findByRole("button", { name: /Bhatt Sweets/ }));
    const again = await screen.findByRole("dialog");
    fireEvent.click(within(again).getByRole("button", { name: /Zomato listing/ }));
    fireEvent.click(within(again).getByRole("button", { name: "No, different — use Zomato" }));
    await waitFor(() => expect(calls("/cart/food/menus")).toHaveLength(2));
    expect(body(calls("/cart/food/menus")[1]!)).toEqual({ name: "Bhatt Sweets & Snacks", zomato_id: "z2" });
  });

  it("only on one app note renders", async () => {
    render(<FoodFlow />);
    await search("kebab");
    const card = await screen.findByRole("button", { name: /Kebab Junction/ });
    expect(within(card).getByText("Only on Swiggy")).toBeInTheDocument();
    expect(within(card).getByText(/Manjalpur/)).toBeInTheDocument();
    expect(within(card).getByText(/5 km/)).toBeInTheDocument();
    fireEvent.click(card);
    await waitFor(() => expect(calls("/cart/food/menus")).toHaveLength(1));
    expect(body(calls("/cart/food/menus")[0]!)).toEqual({ name: "Kebab Junction", swiggy_id: "s3" });
  });

  it("menu shows veg dots, variants and not available", async () => {
    await openMenu();
    const paneer = screen.getByRole("listitem", { name: /Paneer Tikka/ });
    expect(within(paneer).getByText("Half")).toBeInTheDocument();
    expect(within(paneer).getByText("₹220")).toBeInTheDocument();
    expect(within(paneer).getByLabelText("Vegetarian")).toBeInTheDocument();
    const menuList = within(screen.getByRole("list", { name: "Menu" }));
    expect(within(menuList.getByRole("listitem", { name: /Chicken Biryani/ })).getByLabelText("Non-vegetarian")).toBeInTheDocument();
    const dal = menuList.getByRole("listitem", { name: /Dal Makhani/ });
    expect(within(dal).getByText("Not available")).toBeInTheDocument();
    expect(within(dal).queryByRole("button", { name: /Add/ })).not.toBeInTheDocument();
    // The filter narrows the menu.
    fireEvent.change(screen.getByLabelText("Filter dishes"), { target: { value: "tikka" } });
    expect(within(screen.getByRole("list", { name: "Menu" })).queryByRole("listitem", { name: /Chicken Biryani/ })).not.toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: /Paneer Tikka/ })).toBeInTheDocument();
  });

  it("closed on one app is shown in the header", async () => {
    backend.menus = () =>
      menuReady({
        restaurant: {
          name: "Dum Pukht",
          locality: "Alkapuri",
          listings: {
            swiggy: { restaurant_id: "s1", name: "Dum Pukht", locality: "Alkapuri", distance_km: 2.1, is_open: true },
            zomato: { restaurant_id: "z1", name: "Dum Pukht", locality: "Alkapuri", distance_km: 2.1, is_open: false },
          },
        },
      });
    await openMenu();
    expect(screen.getByText("Closed on Zomato right now.")).toBeInTheDocument();
    expect(screen.queryByText(/Closed on Swiggy/)).not.toBeInTheDocument();
  });

  it("compare posts restaurant and dishes then navigates", async () => {
    await openMenu();
    expect(screen.getByRole("button", { name: /Compare/ })).toBeDisabled();
    expect(screen.getByText("7 checks left today")).toBeInTheDocument();

    // A usual dish, a menu dish with a variant, then a bigger quantity.
    fireEvent.click(screen.getByRole("button", { name: "Add Chicken Biryani (usual)" }));
    fireEvent.click(screen.getByRole("button", { name: "Add Paneer Tikka" }));
    fireEvent.click(screen.getByRole("button", { name: "More Paneer Tikka" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare 2 dishes" }));

    await waitFor(() => expect(calls("/cart/food/checks")).toHaveLength(1));
    expect(body(calls("/cart/food/checks")[0]!)).toEqual({
      restaurant: { name: "Dum Pukht", swiggy_id: "s1", zomato_id: "z1", confirmed: false, declined: [] },
      items: [
        { text: "Chicken Biryani", qty: 2, veg: false },
        { text: "Paneer Tikka", qty: 2, variant: "Half", veg: true },
      ],
    });
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/cart/req-f1"));
  });

  it("clips dish text to 80 and variant to 60 before posting", async () => {
    const long = "Chef Special Slow Cooked Dum Pukht Hyderabadi Chicken Biryani With Extra Raita And Salan Combo";
    expect(long.length).toBeGreaterThan(80);
    await startFoodCheck({
      restaurant: { name: "Dum Pukht", swiggy_id: "s1", confirmed: false, declined: [] },
      items: [
        { text: long, qty: 1, variant: "Regular (Serves 1-2)" },
        { text: "Naan", qty: 1, variant: "v".repeat(75) },
      ],
    });
    const items = body(calls("/cart/food/checks")[0]!).items;
    expect(items[0].text).toHaveLength(80);
    expect(items[0].text).toBe(long.slice(0, 80));
    expect(items[0].variant).toBe("Regular (Serves 1-2)");
    expect(items[1].variant).toBeUndefined();
  });

  it("menu failure offers Search again", async () => {
    backend.menus = () => {
      throw new ApiError(409, "A lookup is already running. Try again in a moment.");
    };
    render(<FoodFlow />);
    await search("dum");
    fireEvent.click(await pairButton(/Dum Pukht/));
    expect(await screen.findByRole("alert")).toHaveTextContent("A lookup is already running.");
    fireEvent.click(screen.getByRole("button", { name: "Search again" }));
    expect(await screen.findByLabelText("Search a restaurant")).toBeInTheDocument();
  });

  it("lookup limit detail is shown", async () => {
    backend.restaurants = () => {
      throw new ApiError(429, "You've used today's lookups. Try again tomorrow.");
    };
    render(<FoodFlow />);
    await search("dum");
    expect(await screen.findByRole("alert")).toHaveTextContent("You've used today's lookups. Try again tomorrow.");
  });

  it("check start 409 detail is shown and keeps the dishes", async () => {
    backend.check = () => {
      throw new ApiError(409, "A cart check is already running.");
    };
    await openMenu();
    fireEvent.click(screen.getByRole("button", { name: "Add Paneer Tikka" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare 1 dish" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A cart check is already running.");
    expect(screen.getByRole("button", { name: "More Paneer Tikka" })).toBeInTheDocument();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("?from restores the restaurant and dishes", async () => {
    db.cart_requests = [
      {
        id: "old-1",
        mode: "food",
        status: "done",
        items: [{ text: "Paneer Tikka", qty: 3, variant: "Half", veg: true }],
        restaurant: { name: "Dum Pukht", swiggy_id: "s1", zomato_id: "z1", confirmed: true, declined: [] },
        created_at: "2026-10-27T10:00:00Z",
      },
    ];
    render(<FoodFlow fromId="old-1" />);
    await screen.findByRole("heading", { name: "Dum Pukht" });
    expect(body(calls("/cart/food/menus")[0]!)).toEqual({ name: "Dum Pukht", swiggy_id: "s1", zomato_id: "z1" });
    expect(await screen.findByRole("button", { name: "More Paneer Tikka" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Compare 1 dish" })).toBeEnabled();
  });
});
