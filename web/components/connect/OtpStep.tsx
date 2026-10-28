"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { clock, OTP_WINDOW_S } from "./copy";

export interface OtpStepProps {
  /** When the worker asked for the code (ms); the resend timer counts its 180 s window from here. */
  startedAt: number;
  /** Sends the code. Resolves to an error message to show, or null when it was accepted. */
  onSubmit: (otp: string) => Promise<string | null>;
  /** Starts a fresh connect for the same app and phone. */
  onResend: () => void;
  resending?: boolean;
}

/**
 * One 4 to 8 digit field, filled by hand, by paste or by iOS's "From Messages" suggestion
 * (autocomplete="one-time-code"). It submits itself at 6 digits (other lengths use Verify). The code is cleared from state
 * right after it is sent and is never stored or logged.
 */
export function OtpStep({ startedAt, onSubmit, onResend, resending = false }: OtpStepProps) {
  const id = useId();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const busyRef = useRef(false);
  const mounted = useRef(true);

  const remaining = OTP_WINDOW_S - (now - startedAt) / 1000;
  const expired = remaining <= 0;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setNow(Date.now());
    if (expired) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [startedAt, expired]);

  async function send(otp: string) {
    if (busyRef.current || otp.length < 4) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const problem = await onSubmit(otp);
    busyRef.current = false;
    if (!mounted.current) return;
    setCode("");
    setBusy(false);
    if (problem) setError(problem);
  }

  function onChange(raw: string) {
    const digits = raw.replace(/\D/g, "").slice(0, 8);
    setCode(digits);
    if (error) setError(null);
    if (digits.length === 6) void send(digits);
  }

  function onFormSubmit(e: FormEvent) {
    e.preventDefault();
    void send(code);
  }

  const errorId = `${id}-error`;
  return (
    <form onSubmit={onFormSubmit} noValidate className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-label font-medium">
          Verification code
        </label>
        <input
          id={id}
          name="otp"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="\d{4,8}"
          maxLength={8}
          enterKeyHint="done"
          autoFocus
          spellCheck={false}
          value={code}
          onChange={(e) => onChange(e.target.value)}
          disabled={busy}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          placeholder="••••••"
          className="h-16 w-full rounded-field border border-line-field bg-surface pl-[0.5em] text-center text-figure tabular-nums tracking-[0.5em] text-ink transition-[border-color,box-shadow] duration-150 placeholder:text-line focus:border-accent focus:outline-none focus:ring-[3px] focus:ring-accent/25 disabled:opacity-60 aria-[invalid=true]:border-danger aria-[invalid=true]:ring-danger/25"
        />
        {error ? (
          <p id={errorId} role="alert" className="text-label text-danger">
            {error}
          </p>
        ) : null}
      </div>

      <Button type="submit" size="lg" fullWidth loading={busy} disabled={code.length < 4}>
        Verify
      </Button>

      <div className="flex min-h-11 items-center justify-center" aria-live="polite">
        {expired ? (
          <Button variant="ghost" onClick={onResend} loading={resending}>
            Send a new code
          </Button>
        ) : (
          <p className="text-label tabular-nums text-ink-muted">Resend in {clock(remaining)}</p>
        )}
      </div>
    </form>
  );
}
