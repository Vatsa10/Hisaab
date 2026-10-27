"use client";

import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Sheet } from "@/components/ui/Sheet";
import { TextField } from "@/components/ui/TextField";
import { ApiError, apiFetch } from "@/lib/api";
import { PinIcon } from "./icons";
import { useKeyboardInset } from "./useKeyboardInset";

export interface Profile {
  address: string | null;
  lat: number | null;
  lon: number | null;
}

/** "22.3119° N, 73.1723° E" */
export function formatPlace(lat: number, lon: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(4)}° ${ns}, ${Math.abs(lon).toFixed(4)}° ${ew}`;
}

function saveProblem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return "We couldn't find that address. Add the area and city, like “Alkapuri, Vadodara”.";
    if (e.status === 503) return "Address lookup is down right now. Try again in a minute.";
    if (e.status === 0) return e.detail;
  }
  return "Couldn't save the address. Try again.";
}

export interface AddressRowProps {
  profile: Profile | null;
  onSaved: (profile: Profile) => void;
}

/**
 * The delivery address (used to price carts for the right area). Tapping it opens a sheet to
 * edit it; saving geocodes it on the server (PUT /profile) and shows the place it resolved to.
 */
export function AddressRow({ profile, onSaved }: AddressRowProps) {
  const formId = useId();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useKeyboardInset(open);

  const address = profile?.address ?? null;
  const placed = profile && profile.lat != null && profile.lon != null;

  function edit() {
    setDraft(address ?? "");
    setError(null);
    setOpen(true);
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    const value = draft.trim();
    if (value.length < 5 || value.length > 200) {
      setError("Enter your area and city (5 to 200 characters).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch<{ address: string; lat: number; lon: number }>("/profile", {
        method: "PUT",
        body: JSON.stringify({ address: value }),
      });
      onSaved({ address: res.address, lat: res.lat, lon: res.lon });
      setOpen(false);
    } catch (err) {
      setError(saveProblem(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Card as="section" aria-label="Delivery address" padding="none" className="mt-6">
        <button
          type="button"
          onClick={edit}
          className="flex w-full items-start gap-3 rounded-card p-4 text-left transition-colors duration-150 active:bg-canvas"
        >
          <span
            aria-hidden="true"
            className="grid size-8 shrink-0 place-items-center rounded-tile bg-accent-soft text-accent"
          >
            <PinIcon size={18} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-label text-ink-muted">Delivery address</span>
            <span className="mt-0.5 block break-words font-medium">
              {address ?? "Add the address you order to"}
            </span>
            {placed ? (
              <span className="mt-0.5 block text-label tabular-nums text-ink-muted">
                Located at {formatPlace(profile.lat!, profile.lon!)}
              </span>
            ) : null}
          </span>
          <span className="shrink-0 pt-4 text-label font-semibold text-accent">{address ? "Edit" : "Add"}</span>
        </button>
      </Card>

      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title="Delivery address"
        description="We price carts for this area. Your apps keep their own saved addresses."
        className="mb-[var(--kb-inset,0px)]"
        footer={
          <Button type="submit" form={formId} size="lg" fullWidth loading={busy}>
            Save address
          </Button>
        }
      >
        <form id={formId} onSubmit={save} noValidate>
          <TextField
            label="Address"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              if (error) setError(null);
            }}
            autoComplete="street-address"
            enterKeyHint="done"
            maxLength={200}
            placeholder="Society or street, area, city"
            hint={error ? undefined : "Society or street, area and city is enough."}
            error={error ?? undefined}
          />
        </form>
      </Sheet>
    </>
  );
}
