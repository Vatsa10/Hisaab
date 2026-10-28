import { describe, expect, it } from "vitest";
import { authErrorMessage } from "@/lib/authErrors";

describe("authErrorMessage", () => {
  it("maps invite_only and signups_paused", () => {
    expect(authErrorMessage("invite_only", "a@b.co")).toBe(
      "Expense Agent is invite-only for now. We've added a@b.co to the waitlist and will email you when a spot opens.",
    );
    expect(authErrorMessage("invite_only")).toBe(
      "Expense Agent is invite-only for now. We've added you to the waitlist and will email you when a spot opens.",
    );
    expect(authErrorMessage("signups_paused")).toBe("Sign-ups are paused until next month. Please try again then.");
  });
  it("falls back to the generic sign-in error", () => {
    expect(authErrorMessage("auth")).toBe("Sign-in didn't finish. Try again.");
    expect(authErrorMessage("whatever")).toBe("Sign-in didn't finish. Try again.");
  });
});
