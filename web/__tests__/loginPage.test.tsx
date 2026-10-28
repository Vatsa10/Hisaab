import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/env", () => ({ hasSupabaseEnv: () => true }));
vi.mock("@/components/auth/LoginForm", () => ({
  LoginForm: ({ initialError }: { initialError?: string }) => <p role="alert">{initialError}</p>,
}));

import LoginPage from "@/app/(auth)/login/page";

describe("login page", () => {
  it("login page shows the waitlist message on invite_only", async () => {
    render(await LoginPage({ searchParams: Promise.resolve({ error: "invite_only", email: "a@b.co" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "invite-only for now. We've added a@b.co to the waitlist and will email you when a spot opens.",
    );
  });

  it("shows the paused message and keeps the generic one for unknown codes", async () => {
    const a = render(await LoginPage({ searchParams: Promise.resolve({ error: "signups_paused" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-ups are paused until next month");
    a.unmount();
    render(await LoginPage({ searchParams: Promise.resolve({ error: "zzz" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-in didn't finish. Try again.");
  });
});
