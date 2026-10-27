// Words used by the Accounts screen and the connect flow, kept in one place so tests and
// screens agree on them exactly.

/** Version of the consent text below. Bump it whenever the text changes (the API checks it). */
export const CONSENT_VERSION = "2026-10-27";

/** The consent text (version 2026-10-27), one sentence per point. */
export function CONSENT_POINTS(appName: string): readonly string[] {
  return [
    `We sign in to ${appName} for you from our servers, with an automated browser.`,
    `Automated access could get your ${appName} account flagged or temporarily locked.`,
    "We only read your order history and prices. We never place an order or pay.",
    "A cart is filled only when you tap to fill it.",
  ];
}

/** How long the worker waits for an OTP or an answer (server setting otp_timeout_s). */
export const OTP_WINDOW_S = 180;

export const COPY = {
  expired: "Code expired — try again",
  badCode: "That code didn't work",
  ourSide: "Something went wrong on our side — try again",
  needsLogin: "You were signed out — sign in again",
} as const;

/** The message for a failed job's error code. Unknown codes are treated as our fault. */
export function failureMessage(error: string | null | undefined): string {
  switch (error) {
    case "otp_timeout":
    case "question_timeout":
      return COPY.expired;
    case "login_failed":
      return COPY.badCode;
    case "needs_login":
      return COPY.needsLogin;
    default:
      return COPY.ourSide;
  }
}

/** "just now", "5m ago", "2h ago", "3d ago", "12 Oct". */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  const date = new Date(then);
  const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][
    date.getMonth()
  ];
  return `${date.getDate()} ${month}`;
}

/** "0:24" */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** "98765 43210" from up to 10 digits. */
export function formatPhone(digits: string): string {
  return digits.length > 5 ? `${digits.slice(0, 5)} ${digits.slice(5)}` : digits;
}

/** "+91 98765 4····": enough to recognise the number, not to read it off a shoulder. */
export function maskPhone(digits: string): string {
  return `+91 ${digits.slice(0, 5)} ${digits.slice(5, 6)}····`;
}
