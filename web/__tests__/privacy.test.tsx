import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/env", () => ({ hasSupabaseEnv: () => true }));
vi.mock("@/components/auth/LoginForm", () => ({
  LoginForm: ({ initialNotice }: { initialNotice?: string }) =>
    initialNotice ? <p role="status">{initialNotice}</p> : null,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: vi.fn(async () => undefined),
}));

import LoginPage from "@/app/(auth)/login/page";
import PrivacyPage from "@/app/privacy/page";
import { ConsentBody } from "@/components/connect/ConsentSheet";
import { APPS } from "@/lib/apps";
import { isPublicPath } from "@/lib/routes";

describe("privacy page", () => {
  it("privacy page renders all seven sections and says we never touch payment", () => {
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByText("Updated 27 October 2026")).toBeInTheDocument();
    const names = [
      "What we do",
      "What we read",
      "What we store",
      "Filling a cart",
      "Who sees it",
      "The risk",
      "Your controls",
    ];
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(names);
    expect(screen.getByText(/We never touch payment: we can't see or use your cards, UPI or wallets/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back/ })).toHaveAttribute("href", "/");
  });

  it("privacy is reachable signed out", () => {
    expect(isPublicPath("/privacy")).toBe(true);
    expect(isPublicPath("/privacyx")).toBe(false);
    expect(isPublicPath("/accounts")).toBe(false);
  });

  it("consent sheet links to privacy", () => {
    render(<ConsentBody app={APPS[0]!} />);
    expect(screen.getByRole("link", { name: "How we handle your data" })).toHaveAttribute("href", "/privacy");
  });

  it("login links to privacy", async () => {
    render(await LoginPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("link", { name: "Privacy" })).toHaveAttribute("href", "/privacy");
  });

  it("login shows the deleted message", async () => {
    render(await LoginPage({ searchParams: Promise.resolve({ deleted: "1" }) }));
    expect(screen.getByRole("status")).toHaveTextContent("Your account and all its data are deleted.");
  });
});
