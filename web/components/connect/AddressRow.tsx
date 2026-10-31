"use client";

import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { AddressPicker } from "@/components/account/AddressPicker";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Sheet, type SheetHandle } from "@/components/ui/Sheet";
import { TextField } from "@/components/ui/TextField";
import { groupOptions, watchAddresses, type AppAddress, type Choice, type ChoiceResponse } from "@/lib/addresses";
import { ApiError, apiFetch } from "@/lib/api";
import { PinIcon } from "./icons";
import { useKeyboardInset } from "./useKeyboardInset";

export interface Profile {
  address: string | null;
  lat: number | null;
  lon: number | null;
  /** A short name for where the geocoder put the pin, when the API gives one. */
  place?: string | null;
  /** The app's saved address this came from; null or absent for a typed address. */
  chosen?: Choice | null;
}

/** The first two parts of a geocoder display name: "Alkapuri, Vadodara, Gujarat, …" -> "Alkapuri, Vadodara". */
export function shortPlace(display: unknown): string | null {
  if (typeof display !== "string") return null;
  const parts = display.split(",").map((p) => p.trim()).filter(Boolean);
  return parts.length ? parts.slice(0, 2).join(", ") : null;
}

/** The line under the address: the resolved place, or a plain confirmation that it was found. */
export function placeLine(profile: Profile | null): string | null {
  if (!profile || profile.lat == null || profile.lon == null) return null;
  return profile.place ? `Pinned near ${profile.place}` : "✓ Found on the map";
}

function saveProblem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return "We couldn't find that address. Add the area and city, like “Alkapuri, Vadodara”.";
    if (e.status === 503) return "Address lookup is down right now. Try again in a minute.";
    if (e.status === 401) return "Please sign in again.";
    if (e.status === 0) return e.detail;
  }
  return "Couldn't save the address. Try again.";
}

interface ProfileResponse {
  address: string;
  lat: number;
  lon: number;
  place?: unknown;
  display?: unknown;
}

export interface AddressRowProps {
  profile: Profile | null;
  onSaved: (profile: Profile) => void;
  /** The user's saved app addresses when the page was rendered; kept live over Realtime. */
  initialAddresses?: AppAddress[];
  /** The signed-in user's id, for the Realtime filter (read from the session when absent). */
  userId?: string;
}

/**
 * The delivery address (used to price carts for the right area). Tapping it opens a sheet with the
 * user's own saved addresses from their apps (AddressPicker); choosing one saves it at once. Typing
 * an address stays as the fallback: saving geocodes it on the server (PUT /profile) and confirms
 * where it was found. With no saved addresses yet the sheet opens on the typed form, and switches
 * to the list by itself when an app's addresses arrive (unless the user has started typing).
 */
export function AddressRow({ profile, onSaved, initialAddresses, userId }: AddressRowProps) {
  const formId = useId();
  const sheetRef = useRef<SheetHandle>(null);
  const [open, setOpen] = useState(false);
  const [typing, setTyping] = useState(false);
  const [rows, setRows] = useState<AppAddress[]>(() => initialAddresses ?? []);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useKeyboardInset(open);
  useEffect(() => watchAddresses(setRows, undefined, userId), [userId]);

  const options = useMemo(() => groupOptions(rows), [rows]);
  const picking = options.length > 0 && !typing;
  const address = profile?.address ?? null;
  const placed = placeLine(profile);

  function edit() {
    setDraft(address ?? "");
    setError(null);
    setTyping(false);
    setOpen(true);
  }

  function closeSheet() {
    // Slide the sheet out like its close button does.
    if (sheetRef.current) sheetRef.current.close();
    else setOpen(false);
  }

  function chosen(res: ChoiceResponse) {
    onSaved({
      address: res.address,
      lat: res.lat,
      lon: res.lon,
      place: typeof res.display === "string" ? res.display : null,
      chosen: res.chosen,
    });
    closeSheet();
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
      const res = await apiFetch<ProfileResponse>("/profile", {
        method: "PUT",
        body: JSON.stringify({ address: value }),
      });
      onSaved({
        address: res.address,
        lat: res.lat,
        lon: res.lon,
        place: typeof res.place === "string" ? res.place : shortPlace(res.display),
        chosen: null,
      });
      closeSheet();
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
            {placed ? <span className="mt-0.5 block text-label text-accent">{placed}</span> : null}
          </span>
          <span className="shrink-0 pt-4 text-label font-semibold text-accent">{address ? "Edit" : "Add"}</span>
        </button>
      </Card>

      <Sheet
        ref={sheetRef}
        open={open}
        onClose={() => setOpen(false)}
        title="Delivery address"
        description={
          picking
            ? "We price each app at its own saved address."
            : "We price carts for this area. Your apps keep their own saved addresses."
        }
        className="mb-[var(--kb-inset,0px)] !max-h-[calc(90dvh-var(--kb-inset,0px))]"
        footer={
          picking ? undefined : (
            <Button type="submit" form={formId} size="lg" fullWidth loading={busy}>
              Save address
            </Button>
          )
        }
      >
        {picking ? (
          <AddressPicker
            options={options}
            chosen={profile?.chosen}
            onChosen={chosen}
            onTypeInstead={() => setTyping(true)}
          />
        ) : (
        <form id={formId} onSubmit={save} noValidate>
          {options.length === 0 ? (
            <p className="mb-4 flex items-start gap-2.5 rounded-field bg-canvas px-3.5 py-3 text-label text-ink-muted">
              <PinIcon size={16} className="mt-px shrink-0 text-accent" />
              <span>
                <span className="block font-semibold text-ink">Connect an app and we&apos;ll show its saved addresses here</span>
                Or type the address you order to.
              </span>
            </p>
          ) : null}
          <TextField
            label="Address"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setTyping(true); // addresses arriving now never pull the form away mid-word
              if (error) setError(null);
            }}
            autoComplete="street-address"
            enterKeyHint="done"
            maxLength={200}
            placeholder="Society or street, area, city"
            hint={error ? undefined : "Society or street, area and city is enough."}
            error={error ?? undefined}
          />
          {options.length > 0 ? (
            <button
              type="button"
              onClick={() => setTyping(false)}
              className="mt-3 min-h-11 w-full rounded-control text-body font-semibold text-accent transition-colors duration-150 hover:bg-accent/5 active:bg-accent/10"
            >
              Use a saved address
            </button>
          ) : null}
        </form>
        )}
      </Sheet>
    </>
  );
}
