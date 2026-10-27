import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pathname = vi.hoisted(() => ({ value: "/spend" }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname.value }));

import { TabBar } from "@/components/ui/TabBar";

describe("TabBar", () => {
  beforeEach(() => {
    pathname.value = "/spend";
  });

  it("renders five links in order: Spend, Orders, Cart, Ask, Accounts", () => {
    render(<TabBar />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Spend", "Orders", "Cart", "Ask", "Accounts"]);
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/spend",
      "/orders",
      "/cart",
      "/ask",
      "/accounts",
    ]);
  });

  it("marks only the active tab", () => {
    pathname.value = "/orders";
    render(<TabBar />);
    const active = screen.getByRole("link", { name: "Orders" });
    expect(active).toHaveAttribute("aria-current", "page");
    for (const name of ["Spend", "Cart", "Ask", "Accounts"]) {
      expect(screen.getByRole("link", { name })).not.toHaveAttribute("aria-current");
    }
  });

  it("keeps a tab active on its nested pages", () => {
    pathname.value = "/orders/swiggy/123456";
    render(<TabBar />);
    expect(screen.getByRole("link", { name: "Orders" })).toHaveAttribute("aria-current", "page");
  });

  it("does not treat a path that only shares a prefix as active", () => {
    pathname.value = "/asking";
    render(<TabBar />);
    expect(screen.getByRole("link", { name: "Ask" })).not.toHaveAttribute("aria-current");
  });
});
