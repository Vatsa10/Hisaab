"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { getUsual, type CartItem, type UsualItem } from "@/lib/cart";

export interface UsualSheetProps {
  open: boolean;
  onClose: () => void;
  onAdd: (items: CartItem[]) => void;
}

type Load = { state: "loading" } | { state: "error" } | { state: "ready"; items: UsualItem[] };

/**
 * "Start from my usual": the groceries bought 3+ times in the last 90 days, all ticked. The
 * ticked ones are added to the list in one go.
 */
export function UsualSheet({ open, onClose, onAdd }: UsualSheetProps) {
  const sheetRef = useRef<SheetHandle>(null);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [checked, setChecked] = useState<boolean[]>([]);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoad({ state: "loading" });
    getUsual().then(
      (items) => {
        if (!live) return;
        setLoad({ state: "ready", items });
        setChecked(items.map(() => true));
      },
      () => {
        if (live) setLoad({ state: "error" });
      },
    );
    return () => {
      live = false;
    };
  }, [open, attempt]);

  const items = load.state === "ready" ? load.items : [];
  const n = checked.filter(Boolean).length;

  function add() {
    onAdd(items.filter((_, i) => checked[i]).map(({ text, qty }) => ({ text, qty })));
    if (sheetRef.current) sheetRef.current.close();
    else onClose();
  }

  return (
    <Sheet
      ref={sheetRef}
      open={open}
      onClose={onClose}
      title="Your usual"
      description="Groceries you bought 3 or more times in the last 90 days."
      footer={
        items.length ? (
          <Button size="lg" fullWidth disabled={n === 0} onClick={add}>
            {n === 1 ? "Add 1 item" : `Add ${n} items`}
          </Button>
        ) : undefined
      }
    >
      {load.state === "loading" ? (
        <ul aria-label="Loading your usual items" aria-busy="true" className="-mx-1">
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="flex min-h-14 items-center gap-3 px-1">
              <span className="size-5 rounded-[6px] bg-line-soft" />
              <span className="flex-1">
                <span className="block h-3.5 w-2/3 animate-pulse rounded-full bg-line-soft" />
                <span className="mt-2 block h-3 w-1/3 animate-pulse rounded-full bg-line-soft" />
              </span>
            </li>
          ))}
        </ul>
      ) : load.state === "error" ? (
        <div className="py-2">
          <p role="alert" className="text-body text-ink-muted">
            Couldn&apos;t load your usual items.
          </p>
          <Button variant="secondary" className="mt-4" onClick={() => setAttempt((a) => a + 1)}>
            Try again
          </Button>
        </div>
      ) : items.length === 0 ? (
        <p className="py-2 text-body text-ink-muted">We&apos;ll suggest items once you&apos;ve ordered groceries a few times.</p>
      ) : (
        <ul className="-mx-1">
          {items.map((item, i) => (
            <li key={`${item.text}-${i}`} className="border-b border-line-soft last:border-b-0">
              <label className="flex min-h-14 cursor-pointer items-center gap-3 rounded-control px-1 py-2 active:bg-canvas">
                <input
                  type="checkbox"
                  checked={checked[i] ?? false}
                  onChange={(e) => setChecked((c) => c.map((v, j) => (j === i ? e.target.checked : v)))}
                  className="size-5 shrink-0 accent-accent"
                />
                <span className="min-w-0 flex-1">
                  <span className="block break-words font-medium">
                    {item.text}
                    {item.qty > 1 ? <span className="font-normal text-ink-muted"> ×{item.qty}</span> : null}
                  </span>
                  <span className="block text-label text-ink-muted">×{item.times} in 90 days</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </Sheet>
  );
}
