"use client";

import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { cn } from "@/lib/cn";
import { useKeyboardInset } from "./useKeyboardInset";

export const MAX_QUESTION_LENGTH = 1000;

/** Injected only while typing: the tab bar would otherwise float over the keyboard. */
const HIDE_TAB_BAR_CSS = 'nav[aria-label="Main"]{display:none}';

export interface ComposerProps {
  /** Send a question; returns false when it was not accepted (e.g. one is already running). */
  onSend: (question: string) => boolean;
  /** A question is being answered: the send button waits, typing does not. */
  busy: boolean;
}

/**
 * The message box pinned to the bottom of the Ask screen. It sits on top of the tab bar; while
 * the user types, the tab bar is hidden and the box rides just above the iOS keyboard (via
 * visualViewport, moved with a transform so nothing reflows). Return sends, Shift+Return adds a
 * line. An in-flow spacer as tall as the bar keeps the last message clear of it.
 */
export function Composer({ onSend, busy }: ComposerProps) {
  const [text, setText] = useState("");
  const [typing, setTyping] = useState(false);
  const [barHeight, setBarHeight] = useState(0);
  const inset = useKeyboardInset();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const counterId = useId();
  const inputId = useId();

  const length = text.length;
  const canSend = !busy && text.trim().length > 0 && length <= MAX_QUESTION_LENGTH;

  // Grow the textarea with its content, up to the max-height in its classes.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  // Keep the spacer exactly as tall as the fixed bar.
  useLayoutEffect(() => {
    const bar = barRef.current;
    if (!bar) return;
    const measure = () => setBarHeight(bar.offsetHeight);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(bar);
    return () => ro.disconnect();
  }, []);

  function submit() {
    if (!canSend) return;
    if (onSend(text.trim())) {
      setText("");
      // Drop the keyboard so the answer has the screen.
      inputRef.current?.blur();
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    submit();
  }

  const lifted = typing && inset > 0;

  return (
    <>
      <div aria-hidden="true" style={{ height: barHeight }} />
      {typing ? <style>{HIDE_TAB_BAR_CSS}</style> : null}
      <div
        ref={barRef}
        data-ask-composer=""
        style={lifted ? { transform: `translateY(-${inset}px)` } : undefined}
        className={cn(
          "px-safe fixed inset-x-0 z-20 mx-auto max-w-[480px] bg-canvas pt-2",
          "transition-transform duration-200 ease-out-quart motion-reduce:transition-none",
          // Messages fade out as they slide under the bar instead of being cut off.
          "before:pointer-events-none before:absolute before:inset-x-0 before:-top-4 before:h-4",
          "before:bg-gradient-to-t before:from-canvas before:to-transparent",
          typing
            ? cn("bottom-0", lifted ? "pb-1" : "pb-[max(0.25rem,env(safe-area-inset-bottom))]")
            : "bottom-[calc(theme(spacing.tabbar)+env(safe-area-inset-bottom))] pb-1",
        )}
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          className={cn(
            "flex items-end gap-1.5 rounded-card bg-surface p-1.5 pl-3.5 shadow-card ring-1 ring-inset ring-line",
            "transition-shadow duration-150 focus-within:ring-2 focus-within:ring-accent",
          )}
        >
          <label htmlFor={inputId} className="sr-only">
            Ask a question about your spending
          </label>
          <textarea
            ref={inputRef}
            id={inputId}
            rows={1}
            value={text}
            maxLength={MAX_QUESTION_LENGTH}
            enterKeyHint="send"
            autoComplete="off"
            placeholder="Ask about your spending"
            aria-describedby={counterId}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            onFocus={() => setTyping(true)}
            onBlur={() => setTyping(false)}
            className={cn(
              "max-h-36 min-h-11 min-w-0 flex-1 resize-none self-center bg-transparent py-2.5 text-field text-ink",
              "placeholder:text-ink-muted focus:outline-none focus-visible:outline-none",
            )}
          />
          <button
            type="submit"
            aria-label="Send"
            disabled={!canSend}
            // Keep focus in the textarea, so the bar doesn't drop under the finger mid-tap.
            onPointerDown={(e) => e.preventDefault()}
            onMouseDown={(e) => e.preventDefault()}
            className={cn(
              "grid size-11 shrink-0 place-items-center rounded-tile bg-accent text-white",
              "transition-[background-color,transform,opacity] duration-150 ease-out-quart",
              "hover:bg-accent-pressed active:scale-[0.96] active:bg-accent-pressed",
              "disabled:bg-line disabled:text-ink-muted",
            )}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M12 19V5M5.5 11.5 12 5l6.5 6.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </form>
        <p id={counterId} className="h-5 px-1 pt-1 text-right text-caption tabular-nums text-ink-muted">
          {length > 0 ? (
            <span className={length >= MAX_QUESTION_LENGTH ? "text-danger" : undefined}>
              {length}/{MAX_QUESTION_LENGTH}
            </span>
          ) : null}
        </p>
      </div>
    </>
  );
}
