import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const live = vi.hoisted(() => ({ push: null as null | ((c: unknown[]) => void) }));
vi.mock("@/lib/jobs", async (orig) => ({
  ...(await orig<typeof import("@/lib/jobs")>()),
  watchConnections: vi.fn((cb: (c: unknown[]) => void) => {
    live.push = cb;
    return () => {
      live.push = null;
    };
  }),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...r }: { href: string; children: ReactNode }) => (
    <a href={href} {...r}>
      {children}
    </a>
  ),
}));

import { ReloginBanner } from "@/components/account/ReloginBanner";

const conn = (site: string, status: string) => ({ site, status, last_synced_at: null });

describe("ReloginBanner", () => {
  beforeEach(() => {
    live.push = null;
  });

  it("banner lists logged-out apps and links to reconnect", () => {
    render(<ReloginBanner />);
    act(() => live.push!([conn("zepto", "needs_login"), conn("blinkit", "connected"), conn("swiggy", "needs_login")]));
    expect(screen.getByText("Zepto and Swiggy logged you out.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reconnect" })).toHaveAttribute("href", "/accounts?reconnect=zepto");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("banner lists three apps with commas and and", () => {
    render(<ReloginBanner />);
    act(() => live.push!([conn("zepto", "needs_login"), conn("blinkit", "needs_login"), conn("swiggy", "needs_login")]));
    expect(screen.getByText("Zepto, Blinkit and Swiggy logged you out.")).toBeInTheDocument();
  });

  it("banner names a single app", () => {
    render(<ReloginBanner />);
    act(() => live.push!([conn("zomato", "needs_login")]));
    expect(screen.getByText("Zomato logged you out.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("banner hidden when all connected", () => {
    const { container } = render(<ReloginBanner />);
    act(() => live.push!([conn("zepto", "connected"), conn("blinkit", "syncing")]));
    expect(container).toBeEmptyDOMElement();
  });

  it("banner updates live", () => {
    render(<ReloginBanner />);
    act(() => live.push!([conn("zepto", "connected")]));
    expect(screen.queryByText(/logged you out/)).toBeNull();
    act(() => live.push!([conn("zepto", "needs_login")]));
    expect(screen.getByText("Zepto logged you out.")).toBeInTheDocument();
    act(() => live.push!([conn("zepto", "connected")]));
    expect(screen.queryByText(/logged you out/)).toBeNull();
  });
});
