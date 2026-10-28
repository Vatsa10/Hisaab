"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { setChoice, siteName, type CartMode, type CartPick, type CartSummary } from "@/lib/cart";

export interface PickSheetProps {
  requestId: string;
  mode: CartMode;
  pick: CartPick | null;
  onClose: () => void;
  /** The recomputed summary after the choice was saved. */
  onChosen: (summary: CartSummary) => void;
}

/** "Which one did you mean?": the server found several close matches and never guesses. */
export function PickSheet({ requestId, mode, pick, onClose, onChosen }: PickSheetProps) {
  const sheet = useRef<SheetHandle>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (!pick || !chosen || saving) return;
    setSaving(true);
    setError(null);
    try {
      const summary = await setChoice(requestId, pick.idx, pick.site, chosen);
      onChosen(summary);
      sheet.current?.close();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Couldn't save that. Try again.");
    } finally {
      setSaving(false);
    }
  }

  const title = mode === "food" ? "Which dish did you mean?" : "Which one did you mean?";

  return (
    <Sheet
      ref={sheet}
      open={pick !== null}
      onClose={onClose}
      title={title}
      description={pick ? `“${pick.label}” on ${siteName(pick.site)}` : undefined}
      footer={
        <>
          {error ? (
            <p role="alert" className="mb-2 text-label text-danger">
              {error}
            </p>
          ) : null}
          <Button fullWidth size="lg" onClick={save} loading={saving} disabled={!chosen}>
            Use this
          </Button>
        </>
      }
    >
      <fieldset>
        <legend className="sr-only">{title}</legend>
        <ul className="divide-y divide-line-soft">
          {pick?.candidates.map((c) => {
            const on = chosen === c.product_id;
            return (
              <li key={c.product_id}>
                <label
                  className={cn(
                    "flex min-h-14 cursor-pointer items-center gap-3 py-2.5",
                    on && "text-ink",
                  )}
                >
                  <input
                    type="radio"
                    name="pick"
                    value={c.product_id}
                    checked={on}
                    onChange={() => setChosen(c.product_id)}
                    className="size-5 shrink-0 accent-accent"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-body">{c.name}</span>
                    {c.size_label || c.packs > 1 ? (
                      <span className="block text-label text-ink-muted">
                        {[c.size_label, c.packs > 1 ? `${c.packs} packs` : null].filter(Boolean).join(" · ")}
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 text-body font-semibold tabular-nums">{c.price_label}</span>
                </label>
              </li>
            );
          })}
        </ul>
      </fieldset>
    </Sheet>
  );
}
