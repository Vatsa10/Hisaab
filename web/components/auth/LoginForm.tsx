"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import { cn } from "@/lib/cn";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

type Mode = "signin" | "signup";

interface FieldErrors {
  email?: string;
  password?: string;
}

const MIN_PASSWORD = 6;

/** Turn Supabase auth errors into plain sentences. Never echoes the password or logs anything. */
function friendlyError(message: string | undefined, mode: Mode): string {
  const m = (message ?? "").toLowerCase();
  if (m.includes("invalid login credentials")) return "That email and password don't match. Try again, or create an account.";
  if (m.includes("already registered") || m.includes("already exists")) return "There's already an account with this email. Sign in instead.";
  if (m.includes("email not confirmed")) return "Confirm your email first: open the link we sent you, then sign in.";
  if (m.includes("provider is not enabled") || m.includes("unsupported provider")) return "Google sign-in isn't switched on yet. Use your email for now.";
  if (m.includes("password")) return `Use a password of at least ${MIN_PASSWORD} characters.`;
  if (m.includes("rate limit") || m.includes("too many")) return "Too many attempts. Wait a minute and try again.";
  if (m.includes("not set")) return "Sign-in isn't configured on this server yet.";
  if (m.includes("fetch") || m.includes("network")) return "Can't reach the sign-in service. Check your connection and try again.";
  return mode === "signup" ? "Couldn't create your account. Try again." : "Couldn't sign you in. Try again.";
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  );
}

export interface LoginFormProps {
  /** Where to go after signing in (already validated by safeNextPath). */
  next: string;
  /** Message from a failed OAuth / email-link callback. */
  initialError?: string;
}

export function LoginForm({ next, initialError }: LoginFormProps) {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | undefined>(initialError);
  const [notice, setNotice] = useState<string | undefined>();
  const [busy, setBusy] = useState<"email" | "google" | null>(null);

  function callbackUrl(): string {
    return `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`;
  }

  function switchMode(m: Mode) {
    setMode(m);
    setFieldErrors({});
    setFormError(undefined);
    setNotice(undefined);
  }

  function validate(): FieldErrors {
    const errors: FieldErrors = {};
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) errors.email = "Enter an email address like name@example.com.";
    if (password.length < MIN_PASSWORD) errors.password = `Use at least ${MIN_PASSWORD} characters.`;
    return errors;
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setFormError(undefined);
    setNotice(undefined);
    const errors = validate();
    setFieldErrors(errors);
    if (errors.email || errors.password) {
      document.getElementById(errors.email ? "email" : "password")?.focus();
      return;
    }

    setBusy("email");
    try {
      const auth = getSupabaseBrowserClient().auth;
      const creds = { email: email.trim(), password };
      const { data, error } =
        mode === "signin"
          ? await auth.signInWithPassword(creds)
          : await auth.signUp({ ...creds, options: { emailRedirectTo: callbackUrl() } });
      if (error) {
        setFormError(friendlyError(error.message, mode));
        return;
      }
      if (!data.session) {
        // Sign-up with email confirmation switched on: no session until the link is opened.
        setNotice(`Check ${creds.email} for a confirmation link, then come back and sign in.`);
        setMode("signin");
        return;
      }
      router.replace(next);
      router.refresh();
    } catch (err) {
      setFormError(friendlyError(err instanceof Error ? err.message : undefined, mode));
    } finally {
      setBusy(null);
    }
  }

  async function onGoogle() {
    setFormError(undefined);
    setNotice(undefined);
    setBusy("google");
    try {
      const { error } = await getSupabaseBrowserClient().auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: callbackUrl() },
      });
      // On success the browser is already leaving for Google.
      if (error) {
        setFormError(friendlyError(error.message, mode));
        setBusy(null);
      }
    } catch (err) {
      setFormError(friendlyError(err instanceof Error ? err.message : undefined, mode));
      setBusy(null);
    }
  }

  const signup = mode === "signup";

  return (
    <div className="mt-8">
      <div
        role="tablist"
        aria-label="Account"
        className="grid grid-cols-2 gap-1 rounded-control bg-line-soft p-1"
        onKeyDown={(e) => {
          if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
          const m: Mode = mode === "signin" ? "signup" : "signin";
          switchMode(m);
          document.getElementById(`tab-${m}`)?.focus();
        }}
      >
        {(["signin", "signup"] as const).map((m) => (
          <button
            key={m}
            id={`tab-${m}`}
            type="button"
            role="tab"
            aria-selected={mode === m}
            aria-controls="auth-panel"
            tabIndex={mode === m ? 0 : -1}
            onClick={() => switchMode(m)}
            className={cn(
              "min-h-10 rounded-[9px] text-label font-semibold transition-[background-color,color,box-shadow] duration-150",
              mode === m ? "bg-surface text-ink shadow-card" : "text-ink-muted hover:text-ink",
            )}
          >
            {m === "signin" ? "Sign in" : "Create account"}
          </button>
        ))}
      </div>

      <form
        id="auth-panel"
        role="tabpanel"
        aria-labelledby={`tab-${mode}`}
        noValidate
        onSubmit={onSubmit}
        className="mt-5 flex flex-col gap-4"
      >
        <TextField
          id="email"
          label="Email"
          type="email"
          inputMode="email"
          autoComplete="email"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={fieldErrors.email}
        />
        <TextField
          id="password"
          label="Password"
          type={showPassword ? "text" : "password"}
          autoComplete={signup ? "new-password" : "current-password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldErrors.password}
          hint={signup && !fieldErrors.password ? `At least ${MIN_PASSWORD} characters.` : undefined}
          trailing={
            <button
              type="button"
              onClick={() => setShowPassword((s) => !s)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              className="mr-1 min-h-11 shrink-0 rounded-[8px] px-3 text-label font-semibold text-accent active:opacity-60"
            >
              {showPassword ? "Hide" : "Show"}
            </button>
          }
        />

        <div aria-live="polite" className="empty:hidden">
          {formError ? (
            <p role="alert" className="rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
              {formError}
            </p>
          ) : notice ? (
            <p className="rounded-field bg-accent-soft px-3.5 py-2.5 text-label text-accent-strong">{notice}</p>
          ) : null}
        </div>

        <Button type="submit" size="lg" fullWidth loading={busy === "email"} disabled={busy === "google"}>
          {signup ? "Create account" : "Sign in"}
        </Button>
      </form>

      <div className="my-5 flex items-center gap-3 text-label text-ink-muted" aria-hidden="true">
        <span className="h-px flex-1 bg-line" />
        or
        <span className="h-px flex-1 bg-line" />
      </div>

      <Button
        variant="secondary"
        size="lg"
        fullWidth
        className="text-ink ring-line-field hover:bg-canvas"
        icon={<GoogleMark />}
        loading={busy === "google"}
        disabled={busy === "email"}
        onClick={onGoogle}
      >
        Continue with Google
      </Button>
    </div>
  );
}
