const GENERIC = "Sign-in didn't finish. Try again.";

/** Plain-language sign-in problems, keyed by the `error` code the login page and callback pass around. */
export function authErrorMessage(code: string, email?: string): string {
  if (code === "invite_only") {
    const who = email ? email : "you";
    return `Hisaab is invite-only for now. We've added ${who} to the waitlist and will email you when a spot opens.`;
  }
  if (code === "signups_paused") return "Sign-ups are paused until next month. Please try again then.";
  return GENERIC;
}

/** Picks the known code out of a Supabase error text (the sign-up hook raises it as the message). */
export function authErrorCode(text: string | null | undefined): string | undefined {
  const t = (text ?? "").toLowerCase();
  if (t.includes("invite_only")) return "invite_only";
  if (t.includes("signups_paused")) return "signups_paused";
  return undefined;
}
