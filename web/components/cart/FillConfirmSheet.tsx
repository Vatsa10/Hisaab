"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import { itemsLabel, startFill, type FillOption } from "@/lib/fill";

export interface FillConfirmSheetProps {
  open: boolean;
  requestId: string;
  site: string;
  appName: string;
  option: FillOption | null;
  onClose: () => void;
  /** The fill was queued: show it live. */
  onStarted: (fillId: string) => void;
}

/** What a fill will do, before anything is added. Add-only: it never offers checkout. */
export function FillConfirmSheet({ open, requestId, site, appName, option, onClose, onStarted }: FillConfirmSheetProps) {
  const ref = useRef<SheetHandle>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const preview = option?.preview ?? { add: [], ask: [], skip: [] };
  const n = preview.add.length + preview.ask.length;
  const ask = preview.ask.length;

  async function go() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const started = await startFill(requestId, site);
      setBusy(false);
      onStarted(started.fill_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Couldn't start the fill. Try again.");
      setBusy(false);
    }
  }

  return (
    <Sheet
      ref={ref}
      open={open}
      onClose={() => {
        setError(null);
        onClose();
      }}
      title={`Fill your ${appName} cart?`}
      footer={
        <div className="flex flex-col gap-2">
          <Button fullWidth loading={busy} disabled={n === 0 || option?.state !== "ready"} onClick={go}>
            Add {itemsLabel(n)}
          </Button>
          <Button variant="ghost" fullWidth onClick={() => ref.current?.close()}>
            Cancel
          </Button>
          {option?.sublabel ? <p className="text-center text-caption text-ink-muted">{option.sublabel}</p> : null}
        </div>
      }
    >
      <ul className="space-y-2.5 text-body">
        <li>
          <span className="font-semibold">We&apos;ll add {itemsLabel(n)} to your {appName} cart.</span>
        </li>
        <li className="text-ink-muted">Items already in your cart stay there.</li>
        {ask > 0 ? (
          <li>
            {ask} {ask === 1 ? "item needs" : "items need"} your choice while we fill.
          </li>
        ) : null}
        {preview.skip.length ? <li className="text-ink-muted">Can&apos;t be added: {preview.skip.join(", ")}.</li> : null}
        <li className="text-ink-muted">You pay in {appName}. We never place the order.</li>
      </ul>
      {error ? (
        <p role="alert" className="mt-4 rounded-tile bg-danger-soft px-3 py-2 text-label text-danger">
          {error}
        </p>
      ) : null}
    </Sheet>
  );
}
