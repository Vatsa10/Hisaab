"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import { countOrders, disconnectApp } from "@/lib/account";
import type { AppInfo } from "@/lib/apps";

export interface DisconnectSheetProps {
  app: AppInfo;
  onClose: () => void;
  /** The app was disconnected; `ordersDeleted` is what the API reported. */
  onDone: (app: AppInfo, ordersDeleted: number) => void;
}

function problem(e: unknown): string {
  if (e instanceof ApiError && (e.status === 503 || e.status === 409 || e.status === 0) && e.detail) return e.detail;
  return "Couldn't disconnect. Try again in a bit.";
}

/** Confirm, then delete one app's session and orders. Cool-down details are shown as the API sent them. */
export function DisconnectSheet({ app, onClose, onDone }: DisconnectSheetProps) {
  const [orders, setOrders] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sheetRef = useRef<SheetHandle>(null);
  const deleted = useRef<number | null>(null);

  useEffect(() => {
    let live = true;
    countOrders(app.id)
      .then((n) => live && setOrders(n))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [app.id]);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const res = await disconnectApp(app.id);
      deleted.current = res.orders_deleted;
      onDone(app, res.orders_deleted);
      sheetRef.current?.close();
    } catch (e) {
      setBusy(false);
      setError(problem(e));
    }
  }

  const stored =
    orders === null
      ? `the ${app.name} orders we stored`
      : `the ${orders} ${app.name} ${orders === 1 ? "order" : "orders"} we stored`;

  return (
    <Sheet
      ref={sheetRef}
      open
      title={`Disconnect ${app.name}?`}
      onClose={onClose}
      footer={
        <div className="flex flex-col gap-2">
          {error ? (
            <p role="alert" className="text-label text-danger">
              {error}
            </p>
          ) : null}
          <Button variant="danger" size="lg" fullWidth loading={busy} onClick={confirm}>
            Disconnect {app.name}
          </Button>
          <Button variant="ghost" fullWidth disabled={busy} onClick={() => sheetRef.current?.close()}>
            Cancel
          </Button>
        </div>
      }
    >
      <p className="text-body">
        We&apos;ll delete your {app.name} sign-in session and {stored}. Your {app.name} account itself isn&apos;t
        touched. You can connect again any time.
        {app.id === "swiggy" ? " Instamart orders go too, because they come through your Swiggy sign-in." : ""}
      </p>
    </Sheet>
  );
}
