"use client";

import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/api";
import { answerFill, type FillChoice } from "@/lib/fill";

export const QUESTION_SECONDS = 180;

export interface FillQuestionProps {
  jobId: string;
  question: string;
  choices: FillChoice[];
  /** When the question was asked (ISO), if the worker recorded it; otherwise it counts from mount. */
  askedAt?: string;
}

function secondsLeft(askedAt: string | undefined, mountedAt: number): number {
  const t = askedAt ? Date.parse(askedAt) : NaN;
  const start = Number.isFinite(t) ? t : mountedAt;
  return Math.max(0, QUESTION_SECONDS - Math.floor((Date.now() - start) / 1000));
}

/** One item the app has several answers for: the user picks, or skips it. Unanswered it skips after 180s. */
export function FillQuestion({ jobId, question, choices, askedAt }: FillQuestionProps) {
  const name = useId();
  const [mountedAt] = useState(() => Date.now());
  const [picked, setPicked] = useState<string | null>(null);
  const [sending, setSending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [left, setLeft] = useState(() => secondsLeft(askedAt, mountedAt));

  useEffect(() => {
    const t = setInterval(() => setLeft(secondsLeft(askedAt, mountedAt)), 1000);
    return () => clearInterval(t);
  }, [askedAt, mountedAt]);

  const expired = left <= 0;
  const locked = expired || sending !== null;

  async function send(answer: string) {
    if (locked) return;
    setSending(answer);
    setError(null);
    try {
      await answerFill(jobId, answer);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Couldn't send that. Try again.");
      setSending(null);
    }
  }

  return (
    <section aria-label="Your choice" className="rounded-card bg-canvas p-4">
      <p className="text-body font-semibold">{question}</p>
      <div role="radiogroup" aria-label={question} className="mt-3 flex flex-col gap-1.5">
        {choices.map((c) => (
          <label
            key={c.id}
            className={cn(
              "flex min-h-11 cursor-pointer items-center gap-3 rounded-control bg-surface px-3 py-2 text-body ring-1 ring-inset ring-line",
              picked === c.id && "ring-2 ring-accent",
              locked && "pointer-events-none opacity-60",
            )}
          >
            <input
              type="radio"
              name={name}
              value={c.id}
              checked={picked === c.id}
              disabled={locked}
              onChange={() => setPicked(c.id)}
              className="size-4 shrink-0 accent-accent"
            />
            <span className="min-w-0 flex-1 break-words">{c.label}</span>
          </label>
        ))}
      </div>
      {error ? (
        <p role="alert" className="mt-2 text-label text-danger">
          {error}
        </p>
      ) : null}
      <div className="mt-3 flex items-center gap-2">
        <Button className="flex-1" disabled={!picked || locked} loading={sending !== null && sending !== "skip"} onClick={() => picked && send(picked)}>
          Add this
        </Button>
        <Button variant="secondary" className="flex-1" disabled={locked} onClick={() => send("skip")}>
          Skip item
        </Button>
      </div>
      <p role="timer" className="mt-2 text-center text-caption tabular-nums text-ink-muted">
        {expired ? "Skipping…" : `${left}s left`}
      </p>
    </section>
  );
}
