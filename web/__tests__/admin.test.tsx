import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ---- mocks -------------------------------------------------------------------------------

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

import { AdminScreen } from "@/components/admin/AdminScreen";
import { ApprovalsPanel } from "@/components/admin/ApprovalsPanel";
import { CostsPanel } from "@/components/admin/CostsPanel";
import { InvitesPanel } from "@/components/admin/InvitesPanel";
import { RecipeDiff } from "@/components/admin/RecipeDiff";
import { SitesPanel } from "@/components/admin/SitesPanel";
import { ApiError } from "@/lib/api";
import type { Approval, Costs, SiteHealth } from "@/lib/admin";
import type { Job } from "@/lib/jobs";

type Route = (init?: RequestInit) => unknown;

/** Answer apiFetch by "METHOD path" (path without the query string unless the key has one). */
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

function costs(over: Partial<Costs> = {}): Costs {
  return {
    month: "2026-10",
    budget_inr: 5000,
    spent_inr: 4120,
    fixed_inr: 0,
    pct: 82,
    level: "alert",
    unit_costs: { proxy_inr_per_gb: 400, ai_inr_per_1k_tokens: 0.05, worker_inr_per_minute: 0.2 },
    users: [
      { user_id: "u2", email: "kabir@example.com", proxy_gb: 0.4213, ai_tokens: 12_340, worker_minutes: 38, syncs: 14, cart_checks: 2, inr: 176.4 },
      { user_id: "u1", email: "ananya@example.com", proxy_gb: 2.5, ai_tokens: 81_200, worker_minutes: 140.6, syncs: 41, cart_checks: 9, inr: 1032.75 },
    ],
    ...over,
  };
}

function site(over: Partial<SiteHealth> = {}): SiteHealth {
  return {
    site: "zepto",
    kind: "orders",
    version: 4,
    learned_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
    healthy: true,
    paused: false,
    health_note: null,
    syncs_ok_7d: 52,
    syncs_failed_7d: 1,
    last_ok_at: new Date().toISOString(),
    admin_connected: true,
    ...over,
  };
}

beforeEach(() => {
  api.apiFetch.mockReset();
  live.jobs.clear();
});
afterEach(() => vi.useRealTimers());

// ---- tests -------------------------------------------------------------------------------

describe("admin view", () => {
  it("non-admin sees the admin-only message", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(403, "admin only"));
    render(<AdminScreen />);
    expect(await screen.findByText("This page is for the admin.")).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("costs list users by rupees with budget level colour", async () => {
    routes({ "GET /admin/costs": () => costs() });
    const { unmount } = render(<CostsPanel onForbidden={vi.fn()} initialMonth="2026-10" />);

    const list = await screen.findByRole("list", { name: "Cost per user" });
    const rows = within(list).getAllByRole("listitem");
    const emails = rows.map((r) => r.querySelector("[data-email]"));
    expect(emails.map((e) => e?.textContent)).toEqual(["a***@example.com", "k***@example.com"]);
    expect(emails.map((e) => e?.getAttribute("title"))).toEqual(["a***@example.com", "k***@example.com"]);
    expect(list).not.toHaveTextContent("ananya");
    expect(rows[0]).toHaveTextContent("₹1,033");
    expect(rows[0]).toHaveTextContent("2.50 GB");
    expect(rows[0]).toHaveTextContent("81.2k tokens");
    expect(rows[0]).toHaveTextContent("141 min");
    expect(rows[1]).toHaveTextContent("0.42 GB");
    expect(rows[1]).toHaveTextContent("₹176");

    expect(screen.getByText("October 2026")).toBeInTheDocument();
    expect(screen.getByTestId("budget-fill").className).toContain("bg-up");
    expect(screen.getByText(/82%/)).toBeInTheDocument();
    expect(screen.getByText(/₹400 \/ GB proxy · ₹0\.05 \/ 1k AI tokens · ₹0\.20 \/ worker min/)).toBeInTheDocument();
    expect(screen.queryByText("Sign-ups and cart checks paused")).not.toBeInTheDocument();
    unmount();

    routes({ "GET /admin/costs": () => costs({ level: "paused", pct: 104, spent_inr: 5210 }) });
    render(<CostsPanel onForbidden={vi.fn()} initialMonth="2026-10" />);
    expect(await screen.findByText("Sign-ups and cart checks paused")).toBeInTheDocument();
    expect(screen.getByTestId("budget-fill").className).toContain("bg-danger");
  });

  it("learn shows approval_needed entries and approve posts the decision", async () => {
    routes({
      "GET /admin/sites": () => [site()],
      "POST /admin/learn/zepto": () => ({ job_id: "L1" }),
      "POST /admin/approvals/A1/decide": () => ({ status: "approved" }),
    });
    render(<SitesPanel onForbidden={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto" }));
    await waitFor(() => expect(live.jobs.has("L1")).toBe(true));

    const update = (progress: Job["progress"] & Record<string, unknown>) =>
      act(() => live.jobs.get("L1")!({ id: "L1", site: "zepto", type: "learn", status: "running", progress, error: null }));

    update({ stage: "learning" });
    expect(screen.getByText("Learning…")).toBeInTheDocument();

    update({
      stage: "approval_needed",
      approval_id: "A1",
      entries: [{ url_regex: "^https://api\\.zepto\\.test/orders$", why: "order list" }],
    });
    expect(screen.getByText("^https://api\\.zepto\\.test/orders$")).toBeInTheDocument();
    expect(screen.getByText("order list")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls("POST", "/admin/approvals/A1/decide")).toHaveLength(1));
    const [, init] = calls("POST", "/admin/approvals/A1/decide")[0]!;
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ decision: "approve" });
  });

  it("waitlist invite posts the email", async () => {
    let posted: unknown = null;
    routes({
      "GET /admin/invites": () => ({
        invites: [
          { email: "ananya@example.com", invited_at: "2026-10-02T10:00:00Z", accepted_at: "2026-10-03T10:00:00Z" },
          { email: "rohan@example.com", invited_at: "2026-10-20T10:00:00Z", accepted_at: null },
        ],
        waitlist: [{ email: "meera.iyer@example.com", joined_at: "2026-10-21T08:00:00Z", invited_at: null }],
      }),
      "POST /admin/invites": (init) => {
        posted = JSON.parse(String(init?.body));
        return { email: "meera.iyer@example.com" };
      },
    });
    render(<InvitesPanel onForbidden={vi.fn()} />);

    expect(await screen.findByText("Joined")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke invite for rohan@example.com" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke invite for ananya@example.com" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Invite meera.iyer@example.com" }));
    await waitFor(() => expect(posted).toEqual({ email: "meera.iyer@example.com" }));
  });

  it("recipe diff renders add/remove/change", () => {
    render(
      <RecipeDiff
        diff={[
          { op: "add", path: "$.read_post_allowlist[0].url_regex", new: "^https://x\\.test/orders$" },
          { op: "remove", path: "$.headers.Referer", old: "https://x.test/" },
          { op: "change", path: "$.endpoint", old: "/v1/orders", new: "/v2/orders" },
          { op: "change", path: "$.page_size", old: 10, new: 20 },
        ]}
      />,
    );
    const lines = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(lines).toEqual([
      "+ $.read_post_allowlist[0].url_regex: ^https://x\\.test/orders$",
      "− $.headers.Referer",
      "~ $.endpoint: /v1/orders → /v2/orders",
      "~ $.page_size: 10 → 20",
    ]);
  });

  it("learn disabled without admin connection", async () => {
    routes({ "GET /admin/sites": () => [site({ site: "blinkit", admin_connected: false })] });
    render(<SitesPanel onForbidden={vi.fn()} />);
    const learn = await screen.findByRole("button", { name: "Learn Blinkit" });
    expect(learn).toBeDisabled();
    expect(screen.getByText("Connect Blinkit on your own account first")).toBeInTheDocument();
  });
});

function approval(over: Partial<Approval> & { id: string }): Approval {
  return {
    site: "zepto",
    kind: "orders",
    reason: "post_allowlist",
    status: "pending",
    proposed: {},
    current: {},
    diff: [{ op: "change", path: "$.endpoint", old: "/v1", new: "/v2" }],
    job_id: null,
    created_at: "2026-10-20T10:00:00Z",
    decided_at: null,
    ...over,
  };
}

describe("admin view, review round 1", () => {
  it("a 403 that arrives after the first load swaps in the admin-only message", async () => {
    routes({
      "GET /admin/costs": () => costs(),
      "GET /admin/sites": () => {
        throw new ApiError(403, "admin only");
      },
    });
    render(<AdminScreen />);
    expect(await screen.findByRole("list", { name: "Cost per user" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Sites" }));
    expect(await screen.findByText("This page is for the admin.")).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("learn shows the 409 detail from the API", async () => {
    routes({
      "GET /admin/sites": () => [site()],
      "POST /admin/learn/zepto": () => {
        throw new ApiError(409, "A learn for Zepto is already running.");
      },
    });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A learn for Zepto is already running.");
    expect(live.jobs.size).toBe(0);
  });

  it("approval_needed without an approval id waits instead of claiming approval", async () => {
    routes({ "GET /admin/sites": () => [site()], "POST /admin/learn/zepto": () => ({ job_id: "L2" }) });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Learn Zepto" }));
    await waitFor(() => expect(live.jobs.has("L2")).toBe(true));
    act(() =>
      live.jobs.get("L2")!({ id: "L2", site: "zepto", type: "learn", status: "running", progress: { stage: "approval_needed" }, error: null }),
    );
    expect(screen.getByText("Waiting for the approval…")).toBeInTheDocument();
    expect(screen.queryByText(/Approved/)).not.toBeInTheDocument();
  });

  it("pause asks first, says it affects every user, then posts", async () => {
    routes({ "GET /admin/sites": () => [site()], "POST /admin/sites/zepto/pause": () => undefined });
    render(<SitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Pause Zepto" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Pauses Zepto for every user");
    expect(calls("POST", "/admin/sites/zepto/pause")).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("button", { name: "Pause Zepto" }));
    await waitFor(() => expect(calls("POST", "/admin/sites/zepto/pause")).toHaveLength(1));
  });

  it("reject posts the decision and the card can be answered again after a failure", async () => {
    let attempts = 0;
    routes({
      "GET /admin/approvals": () => [approval({ id: "p1" })],
      "POST /admin/approvals/p1/decide": () => {
        attempts += 1;
        if (attempts === 1) throw new ApiError(409, "This approval has expired.");
        return { status: "rejected" };
      },
    });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const card = await screen.findByRole("listitem", { name: /Zepto orders/ });

    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent("This approval has expired.");
    // busy is cleared in finally, so both buttons work again
    expect(within(card).getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(within(card).getByRole("button", { name: "Approve" })).toBeEnabled();

    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls("POST", "/admin/approvals/p1/decide")).toHaveLength(2));
    const bodies = calls("POST", "/admin/approvals/p1/decide").map(([, init]) => JSON.parse(String((init as RequestInit).body)));
    expect(bodies).toEqual([{ decision: "reject" }, { decision: "reject" }]);
  });

  it("revoke asks first, then deletes the invite", async () => {
    routes({
      "GET /admin/invites": () => ({
        invites: [{ email: "rohan@example.com", invited_at: "2026-10-20T10:00:00Z", accepted_at: null }],
        waitlist: [],
      }),
      "DELETE /admin/invites/rohan%40example.com": () => undefined,
    });
    render(<InvitesPanel onForbidden={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke invite for rohan@example.com" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("rohan@example.com won't be able to sign up");
    expect(calls("DELETE", "/admin/invites/rohan%40example.com")).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke invite" }));
    await waitFor(() => expect(calls("DELETE", "/admin/invites/rohan%40example.com")).toHaveLength(1));
  });

  it("history shows only decided rows, newest first, at most 20", async () => {
    const decided = Array.from({ length: 25 }, (_, i) =>
      approval({
        id: `h${i}`,
        status: i % 2 ? "approved" : "rejected",
        // shuffled order: decided_at spread over days, not in array order
        decided_at: new Date(Date.UTC(2026, 9, 1 + ((i * 7) % 25), 12)).toISOString(),
      }),
    );
    routes({
      "GET /admin/approvals?status=pending": () => [approval({ id: "p1" })],
      "GET /admin/approvals?status=all": () => [approval({ id: "p1" }), ...decided],
    });
    render(<ApprovalsPanel onForbidden={vi.fn()} />);
    const list = await screen.findByRole("list", { name: "Recent decisions" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(20);
    expect(within(list).queryByText("Pending")).not.toBeInTheDocument();

    const expected = [...decided]
      .sort((a, b) => Date.parse(b.decided_at!) - Date.parse(a.decided_at!))
      .slice(0, 20)
      .map((a) => a.id);
    expect(rows.map((r) => r.getAttribute("data-id"))).toEqual(expected);
  });
});
