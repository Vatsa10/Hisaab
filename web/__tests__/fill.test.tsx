import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

const live = vi.hoisted(() => ({
  fill: null as null | ((f: unknown) => void),
  job: null as null | ((j: unknown) => void),
}));
vi.mock("@/lib/supabase/client", () => ({ getSupabaseBrowserClient: () => ({}) }));
vi.mock("@/lib/fill", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/fill")>();
  return {
    ...real,
    watchFill: vi.fn((_id: string, cb: (f: unknown) => void) => {
      live.fill = cb;
      return () => {};
    }),
  };
});
vi.mock("@/lib/jobs", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/jobs")>();
  return {
    ...real,
    watchJob: vi.fn((_id: string, cb: (j: unknown) => void) => {
      live.job = cb;
      return () => {};
    }),
  };
});

import { ApiError } from "@/lib/api";
import type { CartFill, FillItem, FillOption, FillSummary } from "@/lib/fill";
import { FillButton } from "@/components/cart/FillButton";
import { FillConfirmSheet } from "@/components/cart/FillConfirmSheet";
import { FillSheet } from "@/components/cart/FillSheet";

// ---- fixtures ---------------------------------------------------------------------------

function option(over: Partial<FillOption> = {}): FillOption {
  return {
    state: "ready",
    label: "Fill cart on Zepto",
    sublabel: "2 fills left today",
    href: null,
    fill_id: null,
    preview: { add: ["Milk", "Bread", "Eggs"], ask: ["Chips"], skip: ["Saffron"] },
    ...over,
  };
}

function item(idx: number, over: Partial<FillItem> = {}): FillItem {
  return {
    idx,
    label: `Thing ${idx}`,
    name: `Thing ${idx}`,
    product_id: `p${idx}`,
    packs: 1,
    status: "pending",
    added_qty: null,
    skip_reason: null,
    ...over,
  };
}

function summary(over: Partial<FillSummary> = {}): FillSummary {
  return {
    status: "done",
    title: "Added 2 items to your Zepto cart",
    tone: "success",
    sections: [
      { key: "added", heading: "Added", rows: ["Milk ×2", "Bread"] },
      { key: "skipped", heading: "Skipped", rows: ["Saffron (you skipped it)"] },
    ],
    cart_line: "Zepto cart total ₹412 · items ₹397 + fees ₹15",
    existing_line: "Includes 1 item that was already in your cart.",
    warning_line: null,
    location_line: "We set Zepto's delivery location to Gorwa, Vadodara.",
    reprice: null,
    error_line: null,
    open: { label: "Open Zepto to pay", url: "https://www.zepto.com/" },
    pay_note: "We never place the order or pay. Review your cart in Zepto and pay there.",
    confirm_label: null,
    counted: true,
    ...over,
  };
}

function fill(over: Partial<CartFill> = {}): CartFill {
  return {
    id: "f1",
    request_id: "r1",
    site: "zepto",
    job_id: "j1",
    status: "filling",
    plan: [],
    items: [item(0), item(1), item(2)],
    priced_location: { area: "Gorwa, Vadodara" },
    confirm_location: null,
    app_location: null,
    location_selected: false,
    error: null,
    summary: null,
    ...over,
  };
}

const pushFill = (f: CartFill) => act(() => live.fill?.(f));
const pushJob = (progress: Record<string, unknown>, status = "running") =>
  act(() => live.job?.({ id: "j1", site: "zepto", type: "fill_cart", status, progress, error: null }));

function renderSheet(handlers: Partial<{ onClose: () => void; onSwitch: (id: string) => void; onChanged: () => void }> = {}) {
  return render(
    <FillSheet
      fillId="f1"
      onClose={handlers.onClose ?? vi.fn()}
      onSwitch={handlers.onSwitch ?? vi.fn()}
      onChanged={handlers.onChanged ?? vi.fn()}
    />,
  );
}

beforeEach(() => {
  api.apiFetch.mockReset();
  live.fill = null;
  live.job = null;
});

afterEach(() => {
  vi.useRealTimers();
});

// ---- tests ------------------------------------------------------------------------------

describe("fill button", () => {
  it("renders every server state", () => {
    const start = vi.fn();
    const view = vi.fn();
    const { rerender } = render(<FillButton option={option()} appName="Zepto" onStart={start} onView={view} />);
    fireEvent.click(screen.getByRole("button", { name: "Fill cart on Zepto" }));
    expect(start).toHaveBeenCalled();
    expect(screen.getByText("2 fills left today")).toBeInTheDocument();

    rerender(<FillButton option={option({ state: "needs_login", label: "Reconnect Zepto to fill", sublabel: null, href: "/accounts" })} appName="Zepto" onStart={start} onView={view} />);
    expect(screen.getByRole("link", { name: "Reconnect Zepto to fill" })).toHaveAttribute("href", "/accounts");

    rerender(<FillButton option={option({ state: "paused", sublabel: "Paused until 9 am" })} appName="Zepto" onStart={start} onView={view} />);
    const off = screen.getByRole("button", { name: "Fill cart on Zepto" });
    expect(off).toBeDisabled();
    expect(off).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Paused until 9 am")).toBeInTheDocument();

    rerender(<FillButton option={option({ state: "filled", label: "See what was added", fill_id: "f9" })} appName="Zepto" onStart={start} onView={view} />);
    fireEvent.click(screen.getByRole("button", { name: "See what was added" }));
    expect(view).toHaveBeenCalledWith("f9");
  });
});

describe("confirm sheet", () => {
  function renderConfirm(onStarted = vi.fn()) {
    render(
      <FillConfirmSheet open requestId="r1" site="zepto" appName="Zepto" option={option()} onClose={vi.fn()} onStarted={onStarted} />,
    );
    return onStarted;
  }

  it("confirm sheet shows counts and keeps-your-cart copy", () => {
    renderConfirm();
    expect(screen.getByRole("heading", { name: "Fill your Zepto cart?" })).toBeInTheDocument();
    expect(screen.getByText("We'll add 4 items to your Zepto cart.")).toBeInTheDocument();
    expect(screen.getByText("Items already in your cart stay there.")).toBeInTheDocument();
    expect(screen.getByText("1 item needs your choice while we fill.")).toBeInTheDocument();
    expect(screen.getByText("Can't be added: Saffron.")).toBeInTheDocument();
    expect(screen.getByText("You pay in Zepto. We never place the order.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add 4 items" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByText("2 fills left today")).toBeInTheDocument();
  });

  it("confirm posts and opens the live sheet", async () => {
    api.apiFetch.mockResolvedValue({ fill_id: "f1", job_id: "j1", fills_left: 1 });
    const started = renderConfirm();
    fireEvent.click(screen.getByRole("button", { name: "Add 4 items" }));
    await waitFor(() => expect(started).toHaveBeenCalledWith("f1"));
    expect(api.apiFetch).toHaveBeenCalledWith("/cart/checks/r1/fill", {
      method: "POST",
      body: JSON.stringify({ site: "zepto" }),
    });
  });

  it("409 detail is shown in the confirm sheet", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(409, "A fill is already running for this check."));
    const started = renderConfirm();
    fireEvent.click(screen.getByRole("button", { name: "Add 4 items" }));
    expect(await screen.findByText("A fill is already running for this check.")).toBeInTheDocument();
    expect(started).not.toHaveBeenCalled();
  });
});

describe("live sheet", () => {
  it("progress rows follow realtime item updates", () => {
    renderSheet();
    pushFill(fill());
    pushJob({ stage: "adding", done: 0, total: 3 });
    expect(screen.getByRole("heading", { name: "Filling your Zepto cart…" })).toBeInTheDocument();
    expect(screen.getByText("Adding items · 0 of 3")).toBeInTheDocument();
    expect(screen.getByText("Adding…")).toBeInTheDocument();
    expect(screen.getAllByText("Waiting")).toHaveLength(2);
    expect(screen.getByText("You can leave this screen. We'll notify you when it's done.")).toBeInTheDocument();

    pushFill(fill({ items: [item(0, { status: "added", added_qty: 2, packs: 2 }), item(1, { status: "partial", packs: 3, added_qty: 1 }), item(2, { status: "out_of_stock" })] }));
    pushJob({ stage: "adding", done: 3, total: 3 });
    expect(screen.getByText("Added ×2")).toBeInTheDocument();
    expect(screen.getByText("Only 1 of 3 added")).toBeInTheDocument();
    expect(screen.getByText("Out of stock")).toBeInTheDocument();
    expect(screen.getByText("Adding items · 3 of 3")).toBeInTheDocument();
  });

  it("stage lines name the app and area", () => {
    renderSheet();
    pushFill(fill({ confirm_location: { area: "Alkapuri" } }));
    pushJob({ stage: "checking_location" });
    expect(screen.getByText("Checking Zepto's delivery location…")).toBeInTheDocument();
    pushJob({ stage: "selecting_location" });
    expect(screen.getByText("Setting Zepto's location to Alkapuri…")).toBeInTheDocument();
    pushJob({ stage: "repricing" });
    expect(screen.getByText("Checking prices at Zepto's location…")).toBeInTheDocument();
  });

  const ask = { stage: "question_needed", question: "Which Chips?", choices: [{ id: "a1", label: "Lay's Classic 52g" }, { id: "a2", label: "Bingo Mad Angles" }], done: 1, total: 3 };

  it("question shows choices and answers with the choice id", async () => {
    api.apiFetch.mockResolvedValue({});
    renderSheet();
    pushFill(fill());
    pushJob(ask);
    expect(screen.getByText("Which Chips?")).toBeInTheDocument();
    const add = screen.getByRole("button", { name: "Add this" });
    expect(add).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /Bingo Mad Angles/ }));
    expect(add).toBeEnabled();
    expect(screen.getByRole("timer")).toHaveTextContent("180s left");
    fireEvent.click(add);
    await waitFor(() =>
      expect(api.apiFetch).toHaveBeenCalledWith("/jobs/j1/answer", { method: "POST", body: JSON.stringify({ answer: "a2" }) }),
    );
  });

  it("skip item answers skip", async () => {
    api.apiFetch.mockResolvedValue({});
    renderSheet();
    pushFill(fill());
    pushJob(ask);
    fireEvent.click(screen.getByRole("button", { name: "Skip item" }));
    await waitFor(() =>
      expect(api.apiFetch).toHaveBeenCalledWith("/jobs/j1/answer", { method: "POST", body: JSON.stringify({ answer: "skip" }) }),
    );
  });

  it("question counts down and expires", () => {
    vi.useFakeTimers({ now: Date.parse("2026-10-28T10:00:00Z") });
    renderSheet();
    pushFill(fill());
    pushJob(ask);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByRole("timer")).toHaveTextContent("120s left");
    act(() => {
      vi.advanceTimersByTime(121_000);
    });
    expect(screen.getByRole("timer")).toHaveTextContent("Skipped — no answer");
    expect(screen.getByRole("button", { name: "Skip item" })).toBeDisabled();
  });

  it("result renders sections, cart line and open link", () => {
    renderSheet();
    pushFill(fill({ status: "done", summary: summary() }));
    expect(screen.getByRole("heading", { name: "Added 2 items to your Zepto cart" })).toHaveClass("text-accent");
    const added = screen.getByRole("region", { name: "Added" });
    expect(within(added).getByText("Milk ×2")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Skipped" })).toBeInTheDocument();
    expect(screen.getByText("Zepto cart total ₹412 · items ₹397 + fees ₹15")).toBeInTheDocument();
    expect(screen.getByText("Includes 1 item that was already in your cart.")).toBeInTheDocument();
    expect(screen.getByText("We set Zepto's delivery location to Gorwa, Vadodara.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Zepto to pay" })).toHaveAttribute("href", "https://www.zepto.com/");
    expect(screen.getByText(/We never place the order or pay/)).toHaveClass("text-ink-muted");
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
  });

  it("open link never mentions checkout and has rel noopener", () => {
    renderSheet();
    pushFill(fill({ status: "done", summary: summary() }));
    const link = screen.getByRole("link", { name: "Open Zepto to pay" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(document.body.textContent ?? "").not.toMatch(/checkout|check out|place order|pay now/i);
  });

  it("check-the-app failures show warning tone and the server line", () => {
    renderSheet();
    pushFill(
      fill({
        status: "failed",
        error: "cart_changed",
        summary: summary({
          title: "Your Zepto cart may be half filled",
          tone: "warning",
          sections: [],
          error_line: "Your Zepto cart changed while we were adding, so we stopped. Some items may already be in your cart. Check your Zepto cart before filling again.",
        }),
      }),
    );
    expect(screen.getByRole("heading", { name: "Your Zepto cart may be half filled" })).toHaveClass("text-up");
    expect(screen.getByText(/Check your Zepto cart before filling again/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Reconnect" })).toBeNull();
  });

  it("reprice notice confirms a pinned fill", async () => {
    api.apiFetch.mockResolvedValue({ fill_id: "f2", job_id: "j2", fills_left: 1 });
    const onSwitch = vi.fn();
    renderSheet({ onSwitch });
    pushFill(
      fill({
        status: "needs_confirmation",
        summary: summary({
          status: "needs_confirmation",
          title: "Zepto is set to a different location",
          tone: "warning",
          sections: [],
          open: null,
          location_line: "Zepto delivers to Alkapuri right now, not Gorwa, Vadodara.",
          reprice: { rows: [{ label: "Milk", old: "₹56", new: "₹58" }], total_line: "Items there: ₹58 instead of ₹56" },
          confirm_label: "Fill at these prices",
        }),
      }),
    );
    expect(screen.getByText("Zepto delivers to Alkapuri right now, not Gorwa, Vadodara.")).toBeInTheDocument();
    expect(screen.getByText("Items there: ₹58 instead of ₹56")).toBeInTheDocument();
    expect(screen.getByText(/₹56 →/)).toBeInTheDocument();
    expect(screen.getByText("Or change the location in Zepto, then check again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not now" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fill at these prices" }));
    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith("f2"));
    expect(api.apiFetch).toHaveBeenCalledWith("/cart/fills/f1/confirm", { method: "POST" });
  });

  it("needs_login failure offers reconnect", () => {
    renderSheet();
    pushFill(
      fill({
        status: "failed",
        error: "needs_login",
        summary: summary({
          status: "failed",
          title: "Couldn't fill your Zepto cart",
          tone: "error",
          sections: [],
          open: null,
          error_line: "Zepto logged you out. Reconnect it, then try again. Nothing was added.",
        }),
      }),
    );
    expect(screen.getByRole("heading", { name: "Couldn't fill your Zepto cart" })).toHaveClass("text-[#b91c1c]");
    expect(screen.getByRole("link", { name: "Reconnect" })).toHaveAttribute("href", "/accounts");
    expect(screen.queryByRole("link", { name: /Open/ })).toBeNull();
  });

  it("reports status changes so the buttons reload", () => {
    const onChanged = vi.fn();
    renderSheet({ onChanged });
    pushFill(fill());
    pushFill(fill({ items: [item(0, { status: "added" }), item(1), item(2)] }));
    expect(onChanged).toHaveBeenCalledTimes(1);
    pushFill(fill({ status: "done", summary: summary() }));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });
});
