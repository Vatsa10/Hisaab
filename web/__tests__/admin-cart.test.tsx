import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

const live = vi.hoisted(() => ({ jobs: new Map<string, (job: unknown) => void>() }));
vi.mock("@/lib/jobs", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/jobs")>();
  return {
    ...real,
    watchJob: vi.fn((id: string, cb: (job: unknown) => void) => {
      live.jobs.set(id, cb);
      return () => live.jobs.delete(id);
    }),
  };
});

import { ApprovalsPanel } from "@/components/admin/ApprovalsPanel";
import { CostsPanel } from "@/components/admin/CostsPanel";
import { SitesPanel } from "@/components/admin/SitesPanel";
import { ApiError } from "@/lib/api";
import type { Approval, Costs, SiteHealth } from "@/lib/admin";

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

function cartRow(over: Partial<SiteHealth> = {}): SiteHealth {
  return {
    site: "zepto",
    kind: "cart",
    version: null,
    learned_at: null,
    healthy: true,
    paused: false,
    health_note: null,
    syncs_ok_7d: 0,
    syncs_failed_7d: 0,
    last_ok_at: null,
    admin_connected: true,
    pending_approval: false,
    ...over,
  };
}

beforeEach(() => {
  api.apiFetch.mockReset();
  live.jobs.clear();
});

describe("admin cart learning", () => {
  it("cart rows learn only with a connection", async () => {
    routes({
      "GET /admin/sites": () => [
        cartRow({ site: "zepto" }),
        cartRow({ site: "instamart", admin_connected: false }),
        cartRow({ site: "blinkit", admin_connected: false }),
      ],
      "POST /admin/learn/zepto": () => ({ job_id: "C1" }),
    });
    render(<SitesPanel onForbidden={vi.fn()} />);

    const group = await screen.findByRole("list", { name: "Cart filling" });
    expect(screen.getByText("Cart filling")).toBeInTheDocument();
    expect(within(group).queryByRole("button", { name: /Pause/ })).not.toBeInTheDocument();

    expect(within(group).getByRole("button", { name: "Learn Instamart cart" })).toBeDisabled();
    expect(within(group).getByRole("button", { name: "Learn Blinkit cart" })).toBeDisabled();
    expect(screen.getByText("Connect Swiggy on your own account first")).toBeInTheDocument();
    expect(screen.getByText("Connect Blinkit on your own account first")).toBeInTheDocument();

    fireEvent.click(within(group).getByRole("button", { name: "Learn Zepto cart" }));
    await waitFor(() => expect(calls("POST", "/admin/learn/zepto?kind=cart")).toHaveLength(1));
  });

  it("learn shows the 409 detail verbatim for a cart row", async () => {
    routes({
      "GET /admin/sites": () => [cartRow()],
      "POST /admin/learn/zepto": () => {
        throw new ApiError(409, "Learn Zepto prices (search) first.");
      },
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto cart" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Learn Zepto prices (search) first.");
  });

  it("cart learn approval lists op labels and approves", async () => {
    routes({
      "GET /admin/sites": () => [cartRow()],
      "POST /admin/learn/zepto": () => ({ job_id: "C2" }),
      "POST /admin/approvals/A9/decide": () => ({ status: "approved" }),
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto cart" }));
    await waitFor(() => expect(live.jobs.has("C2")).toBe(true));

    const update = (progress: Record<string, unknown>, status = "running") =>
      act(() => live.jobs.get("C2")!({ id: "C2", site: "zepto", type: "learn", status, progress, error: null }));

    update({ stage: "learning" });
    expect(screen.getByText("Learning…")).toBeInTheDocument();

    update({
      stage: "approval_needed",
      approval_id: "A9",
      entries: [
        { url_regex: "^https://api\\.zepto\\.test/cart/add$", why: "add item", op: "cart" },
        { url_regex: "^https://api\\.zepto\\.test/location/select$", why: "pick store", op: "location" },
        { url_regex: "^https://api\\.zepto\\.test/cart/view$", why: "read back", op: "read" },
      ],
    });
    expect(screen.getByText("Add to cart")).toBeInTheDocument();
    expect(screen.getByText("Select location")).toBeInTheDocument();
    expect(screen.getByText("Read cart")).toBeInTheDocument();
    expect(screen.getByText("^https://api\\.zepto\\.test/cart/add$")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls("POST", "/admin/approvals/A9/decide")).toHaveLength(1));
    const [, init] = calls("POST", "/admin/approvals/A9/decide")[0]!;
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ decision: "approve" });

    update(
      { stage: "learned", status: "pending", approval_id: "A10", note: "Learning added 2 × Amul Butter to your Zepto cart. Remove it in Zepto if you don't want it." },
      "done",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Learning added 2 × Amul Butter to your Zepto cart.");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for your approval in Approvals");
  });

  it("pending badge on cart row", async () => {
    routes({
      "GET /admin/sites": () => [cartRow({ site: "zepto", pending_approval: true }), cartRow({ site: "blinkit" })],
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    const zepto = await screen.findByRole("listitem", { name: "Zepto cart filling" });
    expect(within(zepto).getByText("Waiting for approval")).toBeInTheDocument();
    const blinkit = screen.getByRole("listitem", { name: "Blinkit cart filling" });
    expect(within(blinkit).queryByText("Waiting for approval")).not.toBeInTheDocument();
  });
});

describe("admin cart approvals and costs", () => {
  it("cart approval shows permissions and the never-checkout line", async () => {
    const a: Approval = {
      id: "c1",
      site: "instamart",
      kind: "cart",
      reason: "cart",
      status: "pending",
      proposed: {
        add: { qty_mode: "set" },
        cart_allowlist: [{ url_regex: "^https://x\\.test/api/cart/add$", why: "add item", op: "cart" }],
        location_allowlist: [{ url_regex: "^https://x\\.test/api/location$", why: "pick the store", op: "location" }],
      },
      current: null,
      diff: [{ op: "add", path: "$.cart_allowlist[0].url_regex", new: "^https://x\\.test/api/cart/add$" }],
      job_id: null,
      created_at: "2026-10-20T10:00:00Z",
      decided_at: null,
    };
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);

    const card = await screen.findByRole("listitem", { name: /Instamart cart/ });
    const perms = within(card).getByRole("region", { name: "Cart permissions" });
    expect(within(perms).getByRole("heading", { name: "Cart permissions" })).toBeInTheDocument();
    expect(perms).toHaveTextContent(
      "Approving lets the worker add items to users' carts on Instamart. It can never check out, pay or change addresses.",
    );
    expect(perms).toHaveTextContent("^https://x\\.test/api/cart/add$");
    expect(perms).toHaveTextContent("add item");
    expect(perms).toHaveTextContent("^https://x\\.test/api/location$");
    expect(perms).toHaveTextContent("pick the store");
    expect(perms).toHaveTextContent("Quantity: set");
    // permissions sit above the diff
    expect(perms.compareDocumentPosition(within(card).getByRole("list", { name: "Recipe changes" }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("quantity increment reads as add", async () => {
    const a = {
      id: "c2", site: "zepto", kind: "cart", reason: "cart", status: "pending",
      proposed: { add: { qty_mode: "increment" }, cart_allowlist: [], location_allowlist: [] },
      current: null, diff: [], job_id: null, created_at: "2026-10-20T10:00:00Z", decided_at: null,
    } satisfies Approval;
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    expect(await screen.findByText("Quantity: add")).toBeInTheDocument();
  });

  it("costs show fills", async () => {
    const c: Costs = {
      month: "2026-10",
      budget_inr: 5000,
      spent_inr: 100,
      fixed_inr: 0,
      pct: 2,
      level: "ok",
      unit_costs: { proxy_inr_per_gb: 400, ai_inr_per_1k_tokens: 0.05, worker_inr_per_minute: 0.2 },
      users: [
        { user_id: "u1", email: "ananya@example.com", proxy_gb: 1, ai_tokens: 1000, worker_minutes: 5, syncs: 4, cart_checks: 3, cart_fills: 2, inr: 60 },
        { user_id: "u2", email: "kabir@example.com", proxy_gb: 0.1, ai_tokens: 100, worker_minutes: 1, syncs: 1, cart_checks: 1, cart_fills: 1, inr: 10 },
      ],
    };
    routes({ "GET /admin/costs": () => c });
    render(<CostsPanel onForbidden={vi.fn()} initialMonth="2026-10" />);
    const rows = within(await screen.findByRole("list", { name: "Cost per user" })).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("2 fills");
    expect(rows[1]).toHaveTextContent("1 fill");
    expect(rows[1]).not.toHaveTextContent("1 fills");
  });
});

function cartApproval(proposed: Record<string, unknown>, over: Partial<Approval> = {}): Approval {
  return {
    id: "c9",
    site: "zepto",
    kind: "cart",
    reason: "cart",
    status: "pending",
    proposed,
    current: null,
    diff: [],
    job_id: null,
    created_at: "2026-10-20T10:00:00Z",
    decided_at: null,
    ...over,
  };
}

describe("cart permissions, review round 1", () => {
  it("lists read_post_allowlist as Read cart, minus entries already allowed", async () => {
    const a = cartApproval(
      {
        add: { qty_mode: "set" },
        cart_allowlist: [],
        location_allowlist: [],
        read_post_allowlist: [
          { url_regex: "^https://api\\.zepto\\.test/cart/view$", why: "reads the cart back" },
          { url_regex: "^https://api\\.zepto\\.test/search$", why: "search" },
        ],
      },
      { current: { read_post_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/search$" }] } },
    );
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    const group = within(perms).getByRole("list", { name: "Read requests" });
    expect(within(group).getByText("Read cart")).toBeInTheDocument();
    expect(group).toHaveTextContent("reads the cart back");
    expect(group).not.toHaveTextContent("/search");
  });

  it("strips the op prefix from why so the label shows once", async () => {
    const a = cartApproval({
      cart_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/cart/add$", why: "CART WRITE (adds to the cart): adds one pack", op: "cart" }],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(perms).toHaveTextContent("adds one pack");
    expect(perms).not.toHaveTextContent("CART WRITE");
    expect(within(perms).getAllByText("Add to cart")).toHaveLength(1);
  });

  it("shows host and path parsed from the regex, the raw regex, and the operation", async () => {
    const a = cartApproval({
      cart_allowlist: [
        { url_regex: "^https://api\\.zepto\\.test/graphql$", why: "adds", op: "cart", operation: "AddToCart" },
      ],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(perms).toHaveTextContent("Host: api.zepto.test · Path: /graphql");
    expect(within(perms).getByText("^https://api\\.zepto\\.test/graphql$")).toBeInTheDocument();
    expect(perms).toHaveTextContent("Operation: AddToCart");
  });

  it("an entry with no op still renders with a neutral label", async () => {
    const a = cartApproval({ read_post_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/x$" }] });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(perms).toHaveTextContent("Host: api.zepto.test · Path: /x");
  });

  it("warns when a regex mentions checkout, pay, address or order", async () => {
    const a = cartApproval({
      cart_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/checkout/add$", why: "adds", op: "cart" }],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(within(perms).getByText(/mentions checkout, payment, address or order/)).toBeInTheDocument();
  });

  it("no warning for a plain cart regex, and the safety sentence is medium weight ink", async () => {
    const a = cartApproval({
      cart_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/cart/add$", why: "adds", op: "cart" }],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(within(perms).queryByText(/mentions checkout/)).not.toBeInTheDocument();
    const line = within(perms).getByText(/It can never check out, pay or change addresses/);
    expect(line.className).toContain("text-ink");
    expect(line.className).toContain("font-medium");
  });

  it("whole words only: payload, reorder and border do not warn", async () => {
    const a = cartApproval({
      cart_allowlist: [
        { url_regex: "^https://api\\.zepto\\.test/payload/reorder/border$", why: "adds", op: "cart" },
      ],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(within(perms).queryByText(/mentions checkout/)).not.toBeInTheDocument();
  });

  it("location entries are not flagged for address", async () => {
    const a = cartApproval({
      location_allowlist: [
        { url_regex: "^https://api\\.zepto\\.test/address/select$", why: "picks the store", op: "location" },
      ],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(within(perms).queryByText(/mentions checkout/)).not.toBeInTheDocument();
  });

  it("place-order and payment in a cart entry still warn", async () => {
    const a = cartApproval({
      cart_allowlist: [{ url_regex: "^https://api\\.zepto\\.test/place-order$", why: "adds", op: "cart" }],
    });
    routes({ "GET /admin/approvals": () => [a] });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const perms = await screen.findByRole("region", { name: "Cart permissions" });
    expect(within(perms).getByText(/mentions checkout/)).toBeInTheDocument();
  });

  it("cart rows hide the 7-day sync line", async () => {
    routes({ "GET /admin/sites": () => [cartRow({ syncs_ok_7d: 3 })] });
    render(<SitesPanel onForbidden={vi.fn()} />);
    const row = await screen.findByRole("listitem", { name: "Zepto cart filling" });
    expect(within(row).queryByText(/failed/)).not.toBeInTheDocument();
  });

  it("the learn approval repeats the never-checkout line, quantity mode, host/path and no double label", async () => {
    routes({
      "GET /admin/sites": () => [cartRow()],
      "POST /admin/learn/zepto": () => ({ job_id: "C7" }),
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto cart" }));
    await waitFor(() => expect(live.jobs.has("C7")).toBe(true));
    act(() =>
      live.jobs.get("C7")!({
        id: "C7",
        site: "zepto",
        type: "learn",
        status: "running",
        progress: {
          stage: "approval_needed",
          approval_id: "A7",
          entries: [
            { url_regex: "^https://api\\.zepto\\.test/cart/add$", why: "CART WRITE (adds to the cart): adds a pack", op: "cart" },
          ],
        },
        error: null,
      }),
    );
    expect(screen.getByText(/It can never check out, pay or change addresses/)).toHaveTextContent(
      "Approving lets the worker add items to users' carts on Zepto.",
    );
    expect(screen.queryByText(/Quantity:/)).not.toBeInTheDocument();
    expect(screen.getByText(/Host: api\.zepto\.test · Path: \/cart\/add/)).toBeInTheDocument();
    expect(screen.queryByText(/CART WRITE/)).not.toBeInTheDocument();
    expect(screen.getByText("adds a pack")).toBeInTheDocument();
  });
});
