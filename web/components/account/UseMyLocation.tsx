"use client";

import { useState } from "react";
import { Spinner, PinIcon } from "@/components/connect/icons";
import { ApiError, apiFetch } from "@/lib/api";
import { APPS } from "@/lib/apps";
import type { ChoiceResponse } from "@/lib/addresses";

export interface NearMatch {
  site: string;
  address_id: string;
  label: string | null;
  line: string | null;
  area: string | null;
  city: string | null;
  distance_m: number;
}

export interface PinSaved {
  address: string;
  lat: number;
  lon: number;
}

export function distanceLabel(m: number): string {
  return m < 1000 ? `${Math.max(10, Math.round(m / 10) * 10)} m` : `${(m / 1000).toFixed(1)} km`;
}

const appName = (site: string) => APPS.find((a) => a.id === site)?.name ?? site;

function placeName(m: NearMatch): string {
  return m.label || [m.line, m.area].filter(Boolean).join(", ") || "Saved address";
}

function locateProblem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 401) return "Please sign in again.";
    if (e.status === 0) return e.detail;
  }
  return "Couldn't match your location. Try again, or type the address.";
}

function gpsProblem(e: GeolocationPositionError | Error): string {
  if ("code" in e && e.code === 1) {
    return "Location is off for this site. Allow it in your browser settings, or type the address.";
  }
  return "Couldn't get your location. Try again, or type the address.";
}

type State =
  | { kind: "idle" }
  | { kind: "locating" }
  | { kind: "saving" }
  | { kind: "result"; lat: number; lon: number; matches: NearMatch[] }
  | { kind: "error"; message: string };

export interface UseMyLocationProps {
  onChosen: (res: ChoiceResponse) => void;
  onPinSaved: (res: PinSaved) => void;
}

/**
 * "Use my current location": ask the device where it is, match that to the user's own saved address on each
 * app (nearest first), and let them pick one, or keep the exact spot. The position is sent for the lookup and
 * only stored if they choose the exact spot.
 */
export function UseMyLocation({ onChosen, onPinSaved }: UseMyLocationProps) {
  const [state, setState] = useState<State>({ kind: "idle" });

  function locate() {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setState({ kind: "error", message: "This browser can't share your location. Type the address instead." });
      return;
    }
    setState({ kind: "locating" });
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const { latitude: lat, longitude: lon } = pos.coords;
        try {
          const res = await apiFetch<{ matches: NearMatch[] }>("/profile/locate", {
            method: "POST",
            body: JSON.stringify({ lat, lon }),
          });
          setState({ kind: "result", lat, lon, matches: res.matches });
        } catch (e) {
          setState({ kind: "error", message: locateProblem(e) });
        }
      },
      (err) => setState({ kind: "error", message: gpsProblem(err) }),
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 },
    );
  }

  async function pickMatch(m: NearMatch) {
    setState({ kind: "saving" });
    try {
      onChosen(
        await apiFetch<ChoiceResponse>("/profile", {
          method: "PUT",
          body: JSON.stringify({ choice: { site: m.site, address_id: m.address_id } }),
        }),
      );
    } catch (e) {
      setState({ kind: "error", message: locateProblem(e) });
    }
  }

  async function pickSpot(lat: number, lon: number) {
    setState({ kind: "saving" });
    try {
      onPinSaved(
        await apiFetch<PinSaved>("/profile", { method: "PUT", body: JSON.stringify({ pin: { lat, lon } }) }),
      );
    } catch (e) {
      setState({ kind: "error", message: locateProblem(e) });
    }
  }

  if (state.kind === "idle" || state.kind === "error") {
    return (
      <div className="mb-4">
        <button
          type="button"
          onClick={locate}
          className="flex min-h-12 w-full items-center justify-center gap-2 rounded-control bg-accent-soft px-4 text-body font-semibold text-accent transition-colors duration-150 active:bg-accent/20"
        >
          <PinIcon size={18} />
          Use my current location
        </button>
        {state.kind === "error" ? (
          <p role="alert" className="mt-2 text-label text-danger">
            {state.message}
          </p>
        ) : null}
      </div>
    );
  }

  if (state.kind === "locating" || state.kind === "saving") {
    return (
      <p role="status" className="mb-4 flex items-center gap-2.5 rounded-field bg-canvas px-3.5 py-3 text-body text-ink-muted">
        <Spinner className="size-4" />
        {state.kind === "locating" ? "Finding where you are…" : "Saving…"}
      </p>
    );
  }

  return (
    <div className="mb-4 flex flex-col gap-2" role="group" aria-label="Where you are">
      <p className="text-label font-semibold text-ink-muted">
        {state.matches.length > 0 ? "You are near" : "None of your saved addresses are close"}
      </p>
      {state.matches.map((m) => (
        <button
          key={`${m.site}:${m.address_id}`}
          type="button"
          onClick={() => pickMatch(m)}
          className="flex min-h-14 w-full items-center gap-3 rounded-control bg-surface px-4 py-2 text-left ring-1 ring-inset ring-line transition-colors duration-150 active:bg-canvas"
        >
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium">{placeName(m)}</span>
            <span className="block text-label text-ink-muted">
              {appName(m.site)} · {distanceLabel(m.distance_m)} away
            </span>
          </span>
          <span className="shrink-0 text-label font-semibold text-accent">Use this</span>
        </button>
      ))}
      <button
        type="button"
        onClick={() => pickSpot(state.lat, state.lon)}
        className="min-h-11 w-full rounded-control text-body font-semibold text-accent transition-colors duration-150 hover:bg-accent/5 active:bg-accent/10"
      >
        Use this exact spot
      </button>
    </div>
  );
}
