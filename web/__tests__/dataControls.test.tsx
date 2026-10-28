import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));

const auth = vi.hoisted(() => ({ signOut: vi.fn(async () => ({})), count: 43 }));
vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({
    auth: { signOut: auth.signOut },
    from: () => ({
      select: () => ({ in: async () => ({ count: auth.count, error: null }) }),
    }),
  }),
}));
const push = vi.hoisted(() => ({
  disablePush: vi.fn(async () => undefined),
  unsubscribePushLocal: vi.fn(async () => undefined),
}));
vi.mock("@/lib/push", () => ({ disablePush: push.disablePush, unsubscribePushLocal: push.unsubscribePushLocal, pushState: vi.fn(async () => "unsupported") }));
vi.mock("@/lib/pwa", () => ({ clearPageCache: vi.fn(async () => undefined) }));
vi.mock("@/lib/me", () => ({
  getMe: vi.fn(async () => ({ is_admin: false })),
  getLimits: vi.fn(async () => {
    throw new Error("n/a");
  }),
}));
vi.mock("@/lib/jobs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/jobs")>()),
  watchJob: vi.fn(() => () => {}),
  watchConnections: vi.fn(() => () => {}),
  firstOrderMonth: vi.fn(async () => null),
}));

import { DataCard } from "@/components/account/DataCard";
import { AccountsScreen } from "@/components/connect/AccountsScreen";
import { CONSENT_VERSION } from "@/components/connect/copy";
import { ApiError } from "@/lib/api";
import { APPS } from "@/lib/apps";

const zepto = APPS[0]!;
const swiggy = APPS[2]!;

beforeEach(() => {
  api.apiFetch.mockReset();
  auth.signOut.mockClear();
  push.disablePush.mockClear();
  push.unsubscribePushLocal.mockClear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("disconnect", () => {
  it("disconnect sheet shows the order count and the Instamart note for Swiggy", async () => {
    render(<DataCard connected={[zepto, swiggy]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Swiggy/ }));
    const dialog = await screen.findByRole("dialog", { name: "Disconnect Swiggy?" });
    expect(await within(dialog).findByText(/the 43 Swiggy orders we stored/)).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Your Swiggy account itself isn't touched. You can connect again any time.");
    expect(dialog).toHaveTextContent("Instamart orders go too, because they come through your Swiggy sign-in.");
    expect(within(dialog).getByRole("button", { name: "Disconnect Swiggy" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("other apps get no Instamart note", async () => {
    render(<DataCard connected={[zepto]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Zepto/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).not.toHaveTextContent("Instamart");
  });

  it("disconnect posts and shows the toast", async () => {
    api.apiFetch.mockResolvedValueOnce({ site: "zepto", orders_deleted: 12 });
    render(<DataCard connected={[zepto]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Zepto/ }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect Zepto" }));
    expect(api.apiFetch).toHaveBeenCalledWith("/connections/zepto/disconnect", { method: "POST" });
    expect(await screen.findByText("Zepto disconnected. 12 orders deleted.")).toBeInTheDocument();
  });

  it("shows a cool-down detail verbatim", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(503, "Try again in 4 minutes."));
    render(<DataCard connected={[zepto]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Zepto/ }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect Zepto" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Try again in 4 minutes.");
  });
});

describe("delete account", () => {
  it("delete button stays disabled until DELETE is typed", async () => {
    render(<DataCard connected={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete your account?" });
    const button = within(dialog).getByRole("button", { name: "Delete my account" });
    const field = within(dialog).getByLabelText("Type DELETE to confirm");
    expect(button).toBeDisabled();
    await userEvent.type(field, "delete");
    expect(button).toBeDisabled();
    await userEvent.clear(field);
    await userEvent.type(field, "DELETE");
    expect(button).toBeEnabled();
    await userEvent.type(field, "x");
    expect(button).toBeDisabled();
  });

  it("delete retries a 503 then signs out to /login?deleted=1", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });
    api.apiFetch
      .mockRejectedValueOnce(new ApiError(503, "busy"))
      .mockRejectedValueOnce(new ApiError(503, "busy"))
      .mockResolvedValueOnce({ deleted: true });
    const { deleteAccount } = await import("@/lib/account");
    vi.useFakeTimers();
    const done = deleteAccount();
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(2000);
    await done;
    expect(api.apiFetch).toHaveBeenCalledTimes(3);
    expect(api.apiFetch).toHaveBeenLastCalledWith("/account", { method: "DELETE", body: JSON.stringify({ confirm: "DELETE" }) });
    expect(push.unsubscribePushLocal).toHaveBeenCalled();
    expect(auth.signOut).toHaveBeenCalled();
    expect(assign).toHaveBeenCalledWith("/login?deleted=1");
  });

  it("gives up after three retries and surfaces the detail", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(503, "Try again in 2 minutes."));
    const { deleteAccount } = await import("@/lib/account");
    vi.useFakeTimers();
    const done = deleteAccount().catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(6000);
    const err = await done;
    expect(err).toBeInstanceOf(ApiError);
    expect(api.apiFetch).toHaveBeenCalledTimes(4);
    expect(auth.signOut).not.toHaveBeenCalled();
  });
});

describe("round 1 fixes", () => {
  it("offers Disconnect for every app with a connection row, whatever its status", () => {
    render(
      <AccountsScreen
        initialConnections={[
          { site: "zepto", status: "needs_login", last_synced_at: null },
          { site: "blinkit", status: "syncing", last_synced_at: null },
          { site: "swiggy", status: "error", last_synced_at: null },
          { site: "zomato", status: "connected", last_synced_at: "2026-10-27T10:00:00Z" },
        ]}
        initialConsents={[]}
        initialProfile={null}
        initialJobs={[]}
      />,
    );
    for (const n of ["Zepto", "Blinkit", "Swiggy", "Zomato"]) {
      expect(screen.getByRole("button", { name: `Disconnect ${n}` })).toBeInTheDocument();
    }
  });

  it("offers no Disconnect for an app with no connection row", () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    expect(screen.queryByRole("button", { name: /^Disconnect/ })).toBeNull();
  });

  it("delete sheet ignores Escape while the delete is running", async () => {
    api.apiFetch.mockReturnValue(new Promise(() => {}));
    render(<DataCard connected={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Type DELETE to confirm"), "DELETE");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete my account" }));
    await userEvent.keyboard("{Escape}");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300));
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("disconnect sheet ignores Escape while the disconnect is running", async () => {
    api.apiFetch.mockReturnValue(new Promise(() => {}));
    render(<DataCard connected={[zepto]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Zepto/ }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect Zepto" }));
    await userEvent.keyboard("{Escape}");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300));
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows a delete failure as its own alert, not on the field", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(500, "boom"));
    render(<DataCard connected={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
    const dialog = await screen.findByRole("dialog");
    const field = within(dialog).getByLabelText("Type DELETE to confirm");
    await userEvent.type(field, "DELETE");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete my account" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Couldn't delete your account");
    expect(field).not.toHaveAttribute("aria-invalid");
  });

  it("erase copy names everything the delete removes", async () => {
    render(<DataCard connected={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "app sessions, orders, saved addresses, cart and price-check data, notifications, push subscriptions and your sign-in",
    );
  });

  it("disconnect copy says what stays", async () => {
    render(<DataCard connected={[zepto]} />);
    await userEvent.click(screen.getByRole("button", { name: /Disconnect Zepto/ }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Your saved addresses and price checks stay until you delete your account.",
    );
  });

  it("delete unsubscribes push locally only", async () => {
    vi.stubGlobal("location", { assign: vi.fn() });
    api.apiFetch.mockResolvedValue({ deleted: true });
    const { deleteAccount } = await import("@/lib/account");
    await deleteAccount();
    expect(push.unsubscribePushLocal).toHaveBeenCalled();
    expect(push.disablePush).not.toHaveBeenCalled();
  });
});

describe("reconnect", () => {
  it("reconnect query opens the phone step", async () => {
    render(
      <AccountsScreen
        initialConnections={[{ site: "zepto", status: "needs_login", last_synced_at: null }]}
        initialConsents={[{ site: "zepto", text_version: CONSENT_VERSION }]}
        initialProfile={null}
        initialJobs={[]}
        reconnect="zepto"
      />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Connect Zepto" });
    expect(within(dialog).getByLabelText("Phone number")).toBeInTheDocument();
    await act(async () => {});
  });

  it("reconnect without stored consent opens the consent sheet", async () => {
    render(
      <AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} reconnect="zepto" />,
    );
    expect(await screen.findByRole("dialog", { name: "Before you connect Zepto" })).toBeInTheDocument();
  });
});
