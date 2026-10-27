"use client";

import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import type { AppInfo } from "@/lib/apps";
import { cn } from "@/lib/cn";
import { formatPhone } from "./copy";
import { LockIcon } from "./icons";

/** Keep the 10 national digits: drops spaces, dashes, a pasted +91 / 91 / 0 prefix. */
export function phoneDigits(raw: string): string {
  let d = raw.replace(/\D/g, "");
  if (d.length > 10 && d.startsWith("91")) d = d.slice(2);
  else if (d.length > 10 && d.startsWith("0")) d = d.slice(1);
  return d.slice(0, 10);
}

export interface PhoneStepProps {
  app: AppInfo;
  /** Starts the connect. Resolves to an error message to show, or null when it worked. */
  onSubmit: (digits: string) => Promise<string | null>;
}

/**
 * The phone number for the app account: a fixed +91 prefix and 10 digits. The number lives
 * only in this component's state (and the flow's memory for a resend); it is never stored.
 */
export function PhoneStep({ app, onSubmit }: PhoneStepProps) {
  const id = useId();
  const [digits, setDigits] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!/^[6-9]\d{9}$/.test(digits)) {
      setError(`Enter the 10-digit mobile number on your ${app.name} account.`);
      return;
    }
    setError(null);
    setBusy(true);
    const problem = await onSubmit(digits);
    // On success this step unmounts; on a problem the number stays so it can be corrected.
    if (problem) {
      setBusy(false);
      setError(problem);
    }
  }

  const errorId = `${id}-error`;
  const noteId = `${id}-note`;
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-label font-medium">
          Phone number
        </label>
        <div
          className={cn(
            "flex min-h-14 items-center rounded-field border bg-surface transition-[border-color,box-shadow] duration-150 focus-within:ring-[3px]",
            error
              ? "border-danger focus-within:ring-danger/25"
              : "border-line-field focus-within:border-accent focus-within:ring-accent/25",
          )}
        >
          <span className="flex h-8 items-center border-r border-line pl-3.5 pr-3 text-field font-medium tabular-nums text-ink-muted">
            +91
          </span>
          <input
            id={id}
            type="tel"
            inputMode="numeric"
            autoComplete="tel-national"
            enterKeyHint="send"
            placeholder="98765 43210"
            value={formatPhone(digits)}
            onChange={(e) => {
              setDigits(phoneDigits(e.target.value));
              if (error) setError(null);
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={[error ? errorId : null, noteId].filter(Boolean).join(" ")}
            className="h-14 min-w-0 flex-1 rounded-field bg-transparent px-3 text-headline font-medium tabular-nums tracking-wide text-ink placeholder:font-normal placeholder:text-ink-muted/60 focus:outline-none"
          />
        </div>
        {error ? (
          <p id={errorId} role="alert" className="text-label text-danger">
            {error}
          </p>
        ) : null}
      </div>

      <Button type="submit" size="lg" fullWidth loading={busy}>
        Send OTP
      </Button>

      <p
        id={noteId}
        className="flex items-start gap-2.5 rounded-field bg-accent-soft px-3.5 py-3 text-label text-accent-strong"
      >
        <LockIcon size={16} className="mt-px shrink-0" />
        <span>{app.name} sends the code. We keep only an encrypted session — never your OTP.</span>
      </p>
    </form>
  );
}
