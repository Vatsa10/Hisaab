import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const auth = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  signUp: vi.fn(),
  signInWithOAuth: vi.fn(),
}));
vi.mock("@/lib/supabase/client", () => ({ getSupabaseBrowserClient: () => ({ auth }) }));

import { LoginForm } from "@/components/auth/LoginForm";

const session = { access_token: "t" };

describe("LoginForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows inline errors and does not call Supabase for an invalid email or short password", async () => {
    render(<LoginForm next="/spend" />);
    await userEvent.type(screen.getByLabelText("Email"), "nope");
    await userEvent.type(screen.getByLabelText("Password"), "123");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/at least 6 characters/)).toBeInTheDocument();
    expect(auth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("signs in with email and password and goes to the next page", async () => {
    auth.signInWithPassword.mockResolvedValue({ data: { session }, error: null });
    render(<LoginForm next="/orders" />);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "secret1");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(auth.signInWithPassword).toHaveBeenCalledWith({ email: "a@b.co", password: "secret1" });
    expect(router.replace).toHaveBeenCalledWith("/orders");
  });

  it("explains a wrong password without echoing it", async () => {
    auth.signInWithPassword.mockResolvedValue({
      data: { session: null },
      error: { message: "Invalid login credentials" },
    });
    render(<LoginForm next="/spend" />);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "secret1");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("don't match");
    expect(alert).not.toHaveTextContent("secret1");
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("creates an account and asks to confirm the email when there is no session yet", async () => {
    auth.signUp.mockResolvedValue({ data: { session: null, user: {} }, error: null });
    render(<LoginForm next="/spend" />);
    await userEvent.click(screen.getByRole("tab", { name: "Create account" }));
    expect(screen.getByRole("tab", { name: "Create account" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "secret1");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(auth.signUp).toHaveBeenCalledWith(
      expect.objectContaining({ email: "a@b.co", password: "secret1" }),
    );
    expect(await screen.findByText(/confirmation link/)).toBeInTheDocument();
  });

  it("starts Google sign-in through Supabase OAuth with the callback route", async () => {
    auth.signInWithOAuth.mockResolvedValue({ data: {}, error: null });
    render(<LoginForm next="/spend" />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(auth.signInWithOAuth).toHaveBeenCalledWith({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback?next=%2Fspend` },
    });
  });

  it("says so when Google is not enabled in Supabase yet", async () => {
    auth.signInWithOAuth.mockResolvedValue({
      data: {},
      error: { message: "Unsupported provider: provider is not enabled" },
    });
    render(<LoginForm next="/spend" />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Google sign-in isn't switched on yet");
  });
});
