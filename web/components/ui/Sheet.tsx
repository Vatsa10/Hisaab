"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";

export interface SheetProps {
  open: boolean;
  /** Called once the sheet has finished animating out; set `open` to false here. */
  onClose: () => void;
  title: string;
  /** Optional line under the title. */
  description?: ReactNode;
  children: ReactNode;
  /** Pinned under the scrollable body, e.g. the sheet's primary action. */
  footer?: ReactNode;
  className?: string;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Exit animation length (matches `animate-sheet-out` / `animate-fade-out`). */
export const SHEET_EXIT_MS = 200;
/** Drag the handle down this far (px), or flick faster than DISMISS_VELOCITY, to dismiss. */
const DISMISS_DISTANCE = 80;
const DISMISS_VELOCITY = 0.5; // px per ms
const EASE_IN = "cubic-bezier(0.5, 0, 0.75, 0)";
const EASE_OUT = "cubic-bezier(0.25, 1, 0.5, 1)";

const noopSubscribe = () => () => {};

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

type Closing = false | "animate" | "drag";

/**
 * iOS-style bottom sheet: a modal dialog that slides up over a dimmed page. Escape, the close
 * button, a tap on the backdrop, or dragging the handle down (past ~80px, or a quick flick)
 * dismiss it: the sheet slides out in 200ms and then calls onClose (at once when reduced motion
 * is preferred). Focus moves into the sheet, is kept there, and returns to the opener.
 */
export function Sheet({ open, onClose, title, description, children, footer, className }: SheetProps) {
  const isClient = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const titleId = useId();
  const descId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const [closing, setClosing] = useState<Closing>(false);
  const closingRef = useRef<Closing>(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const dragRef = useRef<{ startY: number; startT: number; dy: number } | null>(null);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const finish = useCallback(() => {
    closingRef.current = false;
    setClosing(false);
    const panel = panelRef.current;
    if (panel) {
      panel.style.transition = "";
      panel.style.transform = "";
    }
    onCloseRef.current();
  }, []);

  const requestClose = useCallback(
    (how: Exclude<Closing, false> = "animate") => {
      if (closingRef.current) return;
      if (prefersReducedMotion()) {
        finish();
        return;
      }
      closingRef.current = how;
      setClosing(how);
      timerRef.current = setTimeout(finish, SHEET_EXIT_MS);
    },
    [finish],
  );

  useEffect(() => {
    if (!open || !isClient) return;
    const opener = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    panel?.focus();

    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        requestClose();
        return;
      }
      if (e.key !== "Tab" || !panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        e.preventDefault();
        return;
      }
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = overflow;
      opener?.focus?.();
    };
  }, [open, isClient, requestClose]);

  // Drag-to-dismiss on the handle. The panel follows the finger directly (no re-renders).
  function onHandleDown(e: ReactPointerEvent<HTMLDivElement>) {
    if (closingRef.current) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    dragRef.current = { startY: e.clientY, startT: Date.now(), dy: 0 };
    if (panelRef.current) panelRef.current.style.transition = "none";
  }

  function onHandleMove(e: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || !panelRef.current) return;
    drag.dy = Math.max(0, e.clientY - drag.startY);
    panelRef.current.style.transform = `translateY(${drag.dy}px)`;
  }

  function onHandleUp(e: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    dragRef.current = null;
    const panel = panelRef.current;
    if (!drag || !panel) return;
    const dy = Math.max(0, e.clientY - drag.startY);
    const velocity = dy / Math.max(1, Date.now() - drag.startT);
    if (dy > DISMISS_DISTANCE || (velocity > DISMISS_VELOCITY && dy > 10)) {
      panel.style.transition = `transform ${SHEET_EXIT_MS}ms ${EASE_IN}`;
      panel.style.transform = "translateY(100%)";
      requestClose("drag");
    } else {
      panel.style.transition = `transform ${SHEET_EXIT_MS}ms ${EASE_OUT}`;
      panel.style.transform = "translateY(0)";
    }
  }

  if (!open || !isClient) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center">
      <div
        aria-hidden="true"
        className={cn("absolute inset-0 bg-ink/40", closing ? "animate-fade-out" : "animate-fade-in")}
        onClick={() => requestClose()}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        data-state={closing ? "closing" : "open"}
        tabIndex={-1}
        className={cn(
          "relative flex max-h-[90dvh] w-full max-w-[480px] flex-col",
          "rounded-t-sheet bg-surface shadow-sheet focus:outline-none",
          closing === "animate" ? "animate-sheet-out" : closing === "drag" ? "" : "animate-sheet-in",
          className,
        )}
      >
        <div
          data-sheet-handle=""
          aria-hidden="true"
          onPointerDown={onHandleDown}
          onPointerMove={onHandleMove}
          onPointerUp={onHandleUp}
          onPointerCancel={onHandleUp}
          className="flex h-6 shrink-0 cursor-grab touch-none items-end justify-center active:cursor-grabbing"
        >
          <span className="h-1 w-9 rounded-full bg-line" />
        </div>
        <div className="flex items-start gap-2 pl-5 pr-1.5">
          <div className="min-w-0 flex-1 pt-2.5">
            <h2 id={titleId} className="text-headline">
              {title}
            </h2>
            {description ? (
              <p id={descId} className="mt-0.5 text-label text-ink-muted">
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={() => requestClose()}
            className="grid size-11 shrink-0 place-items-center rounded-full text-ink-muted transition-colors hover:bg-canvas active:bg-line-soft"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-4 pt-3">{children}</div>
        {footer ? (
          <div className="border-t border-line-soft px-5 pb-[max(1rem,env(safe-area-inset-bottom))] pt-3">
            {footer}
          </div>
        ) : (
          <div className="pb-safe" />
        )}
      </div>
    </div>,
    document.body,
  );
}
