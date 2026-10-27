"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";

export const ANSWER_MAX = 500;

export interface QuestionStepProps {
  /** The question the app asked, as the worker relayed it. */
  question: string;
  /** Sends the answer. Resolves to an error message to show, or null when it was accepted. */
  onSubmit: (answer: string) => Promise<string | null>;
}

/**
 * Some logins ask something besides an OTP (e.g. "Which city do you order from?"). The question
 * is the field's label; the answer is free text up to 500 characters, cleared once sent.
 */
export function QuestionStep({ question, onSubmit }: QuestionStepProps) {
  const id = useId();
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const text = answer.trim();
    if (!text) {
      setError("Type your answer first.");
      return;
    }
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const problem = await onSubmit(text);
    busyRef.current = false;
    setAnswer("");
    setBusy(false);
    if (problem) setError(problem);
  }

  const counterId = `${id}-count`;
  const errorId = `${id}-error`;
  const near = answer.length >= ANSWER_MAX - 50;
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <label htmlFor={id} className="text-headline">
          {question}
        </label>
        <div
          className={cn(
            "rounded-field border bg-surface transition-[border-color,box-shadow] duration-150 focus-within:ring-[3px]",
            error
              ? "border-danger focus-within:ring-danger/25"
              : "border-line-field focus-within:border-accent focus-within:ring-accent/25",
          )}
        >
          <textarea
            id={id}
            value={answer}
            maxLength={ANSWER_MAX}
            rows={3}
            autoFocus
            enterKeyHint="send"
            onChange={(e) => {
              setAnswer(e.target.value.slice(0, ANSWER_MAX));
              if (error) setError(null);
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={[error ? errorId : null, counterId].filter(Boolean).join(" ")}
            className="block min-h-24 w-full resize-none rounded-field bg-transparent px-3.5 py-3 text-field text-ink focus:outline-none"
          />
        </div>
        <div className="flex items-start justify-between gap-3">
          {error ? (
            <p id={errorId} role="alert" className="text-label text-danger">
              {error}
            </p>
          ) : (
            <p className="text-label text-ink-muted">Your answer goes straight to the app.</p>
          )}
          <p
            id={counterId}
            className={cn("shrink-0 text-label tabular-nums", near ? "text-up" : "text-ink-muted")}
          >
            {answer.length}/{ANSWER_MAX}
          </p>
        </div>
      </div>
      <Button type="submit" size="lg" fullWidth loading={busy}>
        Send answer
      </Button>
    </form>
  );
}
