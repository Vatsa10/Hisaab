"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { TextField } from "@/components/ui/TextField";
import { ApiError } from "@/lib/api";
import { deleteAccount } from "@/lib/account";

function problem(e: unknown): string {
  if (e instanceof ApiError && (e.status === 503 || e.status === 0) && e.detail) return e.detail;
  return "Couldn't delete your account. Try again in a bit.";
}

/** Type DELETE to enable the button; on success the page leaves for /login?deleted=1. */
export function DeleteAccountSheet({ onClose }: { onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sheetRef = useRef<SheetHandle>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await deleteAccount();
    } catch (e) {
      setBusy(false);
      setError(problem(e));
    }
  }

  return (
    <Sheet
      ref={sheetRef}
      open
      title="Delete your account?"
      onClose={onClose}
      dismissible={!busy}
      footer={
        <div className="flex flex-col gap-2">
          {error ? (
            <p role="alert" className="text-label text-danger">
              {error}
            </p>
          ) : null}
          <Button
            variant="destructive"
            size="lg"
            fullWidth
            loading={busy}
            disabled={typed !== "DELETE"}
            onClick={confirm}
          >
            Delete my account
          </Button>
          <Button variant="ghost" fullWidth disabled={busy} onClick={() => sheetRef.current?.close()}>
            Cancel
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-body">
          This deletes everything we hold for you: app sessions, orders, saved addresses, cart and price-check data,
          notifications, push subscriptions and your sign-in. It can&apos;t be undone. Your delivery-app accounts aren&apos;t touched.
        </p>
        <TextField
          label="Type DELETE to confirm"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoCapitalize="characters"
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          disabled={busy}
        />
      </div>
    </Sheet>
  );
}
