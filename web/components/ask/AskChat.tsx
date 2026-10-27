"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { ApiError, apiFetch } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatINR, monthLabel } from "@/lib/format";
import styles from "./ask.module.css";
import { Composer } from "./Composer";
import { Markdown } from "./Markdown";
import { useMonthSpend, type MonthSpend } from "./useMonthSpend";

/** Suggested first questions, sent exactly as shown. */
export const SUGGESTIONS = [
  "Where did my money go this month?",
  "Food vs groceries",
  "Biggest month this year",
  "Top restaurants",
] as const;

/** After this long, "Thinking…" admits the answer is taking a while. */
export const SLOW_AFTER_MS = 8000;

type Turn =
  | { id: number; question: string; state: "pending" }
  | { id: number; question: string; state: "answered"; answer: string }
  | { id: number; question: string; state: "failed"; error: string };

/** What the person sees when /ask fails. The API's detail text is never shown or logged. */
export function askErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 502:
        return "The analyst is busy — try again";
      case 503:
        return "Ask isn't set up yet";
      case 0:
        return "Can't reach the server. Check your connection and try again.";
      case 401:
        return "Your session has ended. Sign in again to keep asking.";
      case 422:
        return "That question couldn't be read. Try rephrasing it.";
      case 429:
        return "That's a lot of questions at once. Wait a moment, then try again.";
    }
  }
  return "Something went wrong. Try again.";
}

const NO_ANSWER = "I couldn't find an answer to that. Try asking it another way.";

/** "₹1,102 spent in October · 5 orders" (mockup C's context line). */
export function monthSpendLine({ month, amount, orders }: MonthSpend): string {
  const name = monthLabel(month).split(" ")[0];
  return `${formatINR(amount)} spent in ${name} · ${orders} ${orders === 1 ? "order" : "orders"}`;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const BUBBLE = "rounded-card px-3.5 py-2.5 text-body";
const THEIRS = cn(BUBBLE, "min-w-0 max-w-full self-start rounded-bl bg-surface text-ink shadow-card");
const MINE = cn(BUBBLE, "ml-10 self-end whitespace-pre-wrap break-words rounded-br bg-accent text-white");

/**
 * The Ask screen: chat with the spending analyst. Each question goes to POST /ask and the
 * markdown answer is shown under it. The conversation lives in this component's state only: it
 * is never written to browser storage, and an unanswered request is aborted when the screen is
 * left. The column is at least one screen tall (less the shell's padding and the bars), so the
 * empty state's intro and chips sit just above the composer, where the thumb is.
 */
export function AskChat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const spend = useMonthSpend();
  const nextId = useRef(0);
  const busyRef = useRef(false);
  const turnRefs = useRef(new Map<number, HTMLLIElement>());
  const scrollTo = useRef<number | null>(null);
  const controllers = useRef(new Set<AbortController>());
  const busy = turns.some((t) => t.state === "pending");

  useEffect(() => {
    const inFlight = controllers.current;
    return () => {
      for (const c of inFlight) c.abort();
      inFlight.clear();
    };
  }, []);

  const run = useCallback(async (id: number, question: string) => {
    const controller = new AbortController();
    controllers.current.add(controller);
    busyRef.current = true;
    let next: Turn;
    try {
      const res = await apiFetch<{ answer?: unknown } | undefined>("/ask", {
        method: "POST",
        body: JSON.stringify({ question }),
        signal: controller.signal,
      });
      const answer = typeof res?.answer === "string" && res.answer.trim() ? res.answer : NO_ANSWER;
      next = { id, question, state: "answered", answer };
    } catch (err) {
      next = { id, question, state: "failed", error: askErrorMessage(err) };
    } finally {
      controllers.current.delete(controller);
    }
    if (controller.signal.aborted) return; // the screen is gone
    busyRef.current = false;
    scrollTo.current = id; // the turn just grew: bring it (or the end of the page) into view
    setTurns((ts) => ts.map((t) => (t.id === id ? next : t)));
  }, []);

  const ask = useCallback(
    (raw: string): boolean => {
      const question = raw.trim();
      if (!question || busyRef.current) return false;
      const id = ++nextId.current;
      scrollTo.current = id;
      setTurns((ts) => [...ts, { id, question, state: "pending" }]);
      void run(id, question);
      return true;
    },
    [run],
  );

  const retry = useCallback(
    (turn: Turn) => {
      if (busyRef.current) return;
      scrollTo.current = turn.id;
      setTurns((ts) => ts.map((t) => (t.id === turn.id ? { id: t.id, question: t.question, state: "pending" } : t)));
      void run(turn.id, turn.question);
    },
    [run],
  );

  // Bring a new question to the top of the screen, so its answer grows down into view; again
  // when the answer or error lands. A short last turn just scrolls the page to its end.
  useEffect(() => {
    const id = scrollTo.current;
    if (id === null) return;
    scrollTo.current = null;
    turnRefs.current.get(id)?.scrollIntoView?.({
      block: "start",
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  }, [turns]);

  return (
    <div className="flex min-h-[calc(100dvh_-_env(safe-area-inset-top)_-_1rem_-_theme(spacing.tabbar)_-_env(safe-area-inset-bottom)_-_1.5rem)] flex-col">
      <PageHeader
        eyebrow="Spending analyst"
        title="Ask"
        subtitle={spend ? <span className="animate-fade-in tabular-nums">{monthSpendLine(spend)}</span> : undefined}
      />

      {turns.length === 0 ? (
        <section aria-labelledby="ask-intro" className="mt-auto flex flex-col pt-6">
          <p id="ask-intro" className={cn(THEIRS, "mr-8")}>
            Ask me about your orders: what you spent, where and when. I answer from your own synced
            orders and nothing else.
          </p>
          <h2 className="mb-2 mt-6 px-1 text-label font-medium text-ink-muted">Try asking</h2>
          <ul className="flex flex-wrap gap-2">
            {SUGGESTIONS.map((q) => (
              <li key={q}>
                <button
                  type="button"
                  onClick={() => ask(q)}
                  className={cn(
                    "min-h-11 rounded-full bg-surface px-4 text-label font-medium text-ink shadow-card ring-1 ring-inset ring-line",
                    "transition-[background-color,transform] duration-150 ease-out-quart",
                    "hover:bg-accent-soft active:scale-[0.97] active:bg-accent-soft",
                  )}
                >
                  {q}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* The one live region: new questions, "Thinking…", answers and errors are announced
          politely from here, so nothing inside carries its own status/alert role. */}
      <ol
        role="log"
        aria-label="Conversation"
        aria-live="polite"
        className={cn("flex flex-col gap-4", turns.length === 0 && "sr-only")}
      >
        {turns.map((turn) => (
          <li
            key={turn.id}
            ref={(el) => {
              if (el) turnRefs.current.set(turn.id, el);
              else turnRefs.current.delete(turn.id);
            }}
            className="flex scroll-mt-[calc(env(safe-area-inset-top)+1rem)] flex-col gap-2"
          >
            <p className={MINE}>{turn.question}</p>
            {turn.state === "pending" ? <Thinking /> : null}
            {turn.state === "answered" ? (
              <div className={THEIRS}>
                <Markdown>{turn.answer}</Markdown>
              </div>
            ) : null}
            {turn.state === "failed" ? (
              <AskError message={turn.error} disabled={busy} onRetry={() => retry(turn)} />
            ) : null}
          </li>
        ))}
      </ol>

      <Composer onSend={ask} busy={busy} />
    </div>
  );
}

function Thinking() {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setSlow(true), SLOW_AFTER_MS);
    return () => clearTimeout(timer);
  }, []);

  return (
    <div data-ask-thinking="" className={cn(THEIRS, "flex items-center gap-2.5 text-ink-muted")}>
      <span aria-hidden="true" className="flex items-center gap-1">
        {[0, 1, 2].map((i) => (
          <span key={i} className={cn("size-1.5 rounded-full bg-accent", styles.dot)} />
        ))}
      </span>
      <span>{slow ? "Still working on it…" : "Thinking…"}</span>
    </div>
  );
}

function AskError({ message, disabled, onRetry }: { message: string; disabled: boolean; onRetry: () => void }) {
  return (
    <div data-ask-error="" className={cn(THEIRS, "flex flex-col items-start gap-3")}>
      <p className="flex items-start gap-2">
        <svg
          className="mt-0.5 shrink-0 text-danger"
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v5.25M12 16.25v.01" />
        </svg>
        <span>{message}</span>
      </p>
      <button
        type="button"
        onClick={onRetry}
        disabled={disabled}
        className={cn(
          "inline-flex min-h-11 items-center gap-1.5 rounded-control px-3.5 text-label font-semibold text-accent ring-1 ring-inset ring-line",
          "transition-[background-color,transform] duration-150 ease-out-quart",
          "hover:bg-accent-soft active:scale-[0.97] disabled:opacity-50",
        )}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 12a8 8 0 0 1 13.66-5.66L20 8.5M20 4v4.5h-4.5M20 12a8 8 0 0 1-13.66 5.66L4 15.5M4 20v-4.5h4.5" />
        </svg>
        Try again
      </button>
    </div>
  );
}
