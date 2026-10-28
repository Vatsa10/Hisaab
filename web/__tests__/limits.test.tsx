import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), apiFetch: api.apiFetch }));
const me = vi.hoisted(() => ({ getMe: vi.fn(), getLimits: vi.fn() }));
vi.mock("@/lib/me", () => me);
const push = vi.hoisted(() => ({ pushState: vi.fn(), enablePush: vi.fn(), disablePush: vi.fn() }));
vi.mock("@/lib/push", () => push);
vi.mock("@/lib/jobs", async (orig) => ({
  ...(await orig<typeof import("@/lib/jobs")>()),
  watchJob: vi.fn(() => () => {}),
  watchConnections: vi.fn(() => () => {}),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...r }: { href: string; children: ReactNode }) => (
    <a href={href} {...r}>
      {children}
    </a>
  ),
}));

import { ApiError } from "@/lib/api";
import { NotificationsCard } from "@/components/account/NotificationsCard";
import { AccountsScreen } from "@/components/connect/AccountsScreen";
import { AppRow } from "@/components/connect/AppRow";
import { APPS } from "@/lib/apps";

const zepto = APPS[0]!;
const synced = { site: "zepto", status: "connected" as const, last_synced_at: new Date().toISOString() };
const limits = (left: number) => ({
  refresh_left: { zepto: left },
  refresh_limit: 3,
  cart_checks_left: 10,
  cart_checks_limit: 10,
  budget_paused: false,
  resets_at: "2026-10-29T00:00:00Z",
});
const screenProps = { initialConnections: [synced], initialConsents: [], initialProfile: null, initialJobs: [] };

describe("refresh limits", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    me.getMe.mockResolvedValue({ user_id: "u", email: "a@b.co", is_admin: false });
    me.getLimits.mockResolvedValue(limits(2));
  });

  it("refresh shows remaining and disables at zero", () => {
    const noop = vi.fn();
    const { rerender } = render(<AppRow app={zepto} connection={synced} refreshLeft={2} onConnect={noop} onRefresh={noop} />);
    expect(screen.getByRole("button", { name: "Refresh now · 2 left today" })).toBeEnabled();
    rerender(<AppRow app={zepto} connection={synced} refreshLeft={0} onConnect={noop} onRefresh={noop} />);
    expect(screen.getByRole("button", { name: "No refreshes left today" })).toBeDisabled();
  });

  it("loads the remaining count into the screen", async () => {
    render(<AccountsScreen {...screenProps} />);
    expect(await screen.findByRole("button", { name: "Refresh now · 2 left today" })).toBeEnabled();
  });

  it("429 detail is shown", async () => {
    render(<AccountsScreen {...screenProps} />);
    api.apiFetch.mockRejectedValueOnce(new ApiError(429, "Daily refresh limit reached for Zepto. Resets at midnight IST."));
    await userEvent.click(await screen.findByRole("button", { name: /Refresh now/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Daily refresh limit reached for Zepto. Resets at midnight IST.");
  });

  it("needs_login row has no refresh", () => {
    const noop = vi.fn();
    render(<AppRow app={zepto} connection={{ ...synced, status: "needs_login" }} refreshLeft={2} onConnect={noop} onRefresh={noop} />);
    expect(screen.queryByRole("button", { name: /refresh/i })).toBeNull();
  });

  it("admin link only for admins", async () => {
    const { unmount } = render(<AccountsScreen {...screenProps} />);
    await screen.findByRole("button", { name: /Refresh now/ });
    await waitFor(() => expect(me.getMe).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /Admin/ })).toBeNull();
    unmount();
    me.getMe.mockResolvedValue({ user_id: "u", email: "a@b.co", is_admin: true });
    render(<AccountsScreen {...screenProps} />);
    expect(await screen.findByRole("link", { name: /Admin/ })).toHaveAttribute("href", "/admin");
  });
});

describe("NotificationsCard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("off offers to turn on, and turning on shows on", async () => {
    push.pushState.mockResolvedValue("off");
    push.enablePush.mockResolvedValue("on");
    render(<NotificationsCard />);
    expect(screen.getByText("Get notified when a sync finishes or an app logs you out.")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }));
    expect(await screen.findByText("Notifications on")).toBeInTheDocument();
    push.disablePush.mockResolvedValue(undefined);
    await userEvent.click(screen.getByRole("button", { name: "Turn off" }));
    expect(await screen.findByRole("button", { name: "Turn on notifications" })).toBeInTheDocument();
  });

  it("denied and needs_install copy", async () => {
    push.pushState.mockResolvedValue("denied");
    const a = render(<NotificationsCard />);
    expect(await screen.findByText("Notifications are off in Settings. We'll email you instead.")).toBeInTheDocument();
    a.unmount();
    push.pushState.mockResolvedValue("needs_install");
    render(<NotificationsCard />);
    expect(
      await screen.findByText(
        "Add Expense Agent to your Home Screen (Share → Add to Home Screen) to get notifications. We'll email you meanwhile.",
      ),
    ).toBeInTheDocument();
  });
});
