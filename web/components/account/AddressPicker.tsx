"use client";

import { useRef, useState, type KeyboardEvent } from "react";
import { CheckIcon, Spinner } from "@/components/connect/icons";
import { ApiError } from "@/lib/api";
import { APPS } from "@/lib/apps";
import { chooseAddress, isChosen, refreshAddresses, normLine, type AddressOption, type Choice, type ChoiceResponse } from "@/lib/addresses";
import { cn } from "@/lib/cn";

export interface AddressPickerProps {
  options: readonly AddressOption[];
  /** The saved address the profile uses now, if it came from an app. */
  chosen: Choice | null | undefined;
  /** Called with the API's answer once a choice is saved. */
  onChosen: (res: ChoiceResponse) => void;
  onTypeInstead: () => void;
}

function chooseProblem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return "That address changed in the app. Pick another, or type one.";
    if (e.status === 503) return "Address lookup is down right now. Try again in a minute.";
    if (e.status === 401) return "Please sign in again.";
    if (e.status === 0) return e.detail;
  }
  return "Couldn't use that address. Try again.";
}

function refreshProblem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return "Just refreshed. Try again in a couple of minutes.";
    if (e.status === 409) return "Connect an app first.";
    if (e.status === 401) return "Please sign in again.";
    if (e.status === 0) return e.detail;
  }
  return "Couldn't refresh. Try again.";
}

const appOf = (site: string) => APPS.find((a) => a.id === site);

function placeOf(o: AddressOption): string | null {
  const parts = [o.area, o.city].filter((p): p is string => Boolean(p));
  return parts.length ? parts.join(", ") : null;
}

/**
 * The user's own saved addresses from their apps, one option per place. Tapping one makes it the
 * delivery address straight away (PUT /profile {choice}). Each option names the apps that have it,
 * with their colour dots, so it is clear which apps can price there.
 */
export function AddressPicker({ options, chosen, onChosen, onTypeInstead }: AddressPickerProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  const radios = useRef<Array<HTMLButtonElement | null>>([]);
  // Roving tab stop: the checked option, else the first.
  const checkedIndex = options.findIndex((o) => isChosen(o, chosen));
  const tabStop = checkedIndex === -1 ? 0 : checkedIndex;

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    let next: number | undefined;
    if (step !== undefined) next = (index + step + options.length) % options.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = options.length - 1;
    if (next === undefined) return;
    e.preventDefault();
    radios.current[next]?.focus(); // moving focus does not save: Space or Enter chooses
  }

  async function choose(option: AddressOption) {
    if (busy) return;
    setBusy(option.key);
    setError(null);
    try {
      onChosen(await chooseAddress(option));
    } catch (e) {
      setError(chooseProblem(e));
    } finally {
      setBusy(null);
    }
  }

  async function refresh() {
    if (refreshing) return;
    setRefreshing(true);
    setError(null);
    setRefreshNote(null);
    try {
      await refreshAddresses();
      setRefreshNote("Checking your apps. New addresses appear here in a moment.");
    } catch (e) {
      setError(refreshProblem(e));
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => void refresh()}
        disabled={refreshing || busy !== null}
        aria-busy={refreshing || undefined}
        className="mb-3 flex min-h-11 w-full items-center justify-center gap-2 rounded-control text-body font-semibold text-accent transition-colors duration-150 hover:bg-accent/5 active:bg-accent/10 disabled:opacity-50"
      >
        {refreshing ? <Spinner className="motion-safe:animate-spin" /> : null}
        Refresh addresses
      </button>
      {refreshNote ? (
        <p role="status" className="mb-3 text-center text-label text-ink-muted">
          {refreshNote}
        </p>
      ) : null}
      <div role="radiogroup" aria-label="Your saved addresses" className="flex flex-col gap-2.5">
        {options.map((o, i) => {
          const checked = isChosen(o, chosen);
          const saving = busy === o.key;
          const apps = o.sites.map(appOf).filter((a) => a !== undefined);
          const place = placeOf(o);
          return (
              <button
                key={o.key}
                ref={(el) => {
                  radios.current[i] = el;
                }}
                type="button"
                role="radio"
                tabIndex={i === tabStop ? 0 : -1}
                onKeyDown={(e) => onKeyDown(e, i)}
                aria-checked={checked}
                aria-busy={saving || undefined}
                disabled={busy !== null && !saving}
                onClick={() => void choose(o)}
                className={cn(
                  "flex w-full items-start gap-3 rounded-control bg-surface p-3.5 text-left",
                  "transition-[box-shadow,background-color,transform] duration-150 ease-out-quart active:scale-[0.99]",
                  "disabled:opacity-60 aria-busy:cursor-progress",
                  checked ? "bg-accent-soft/70 ring-2 ring-inset ring-accent" : "ring-1 ring-inset ring-line hover:bg-canvas",
                )}
              >
                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline gap-2">
                    <span className="text-headline">{o.label ?? "Saved address"}</span>
                    {o.is_default ? <span className="text-label text-ink-muted">Default</span> : null}
                  </span>
                  {o.line ? <span className="mt-0.5 block break-words text-body">{o.line}</span> : null}
                  {place ? <span className="mt-0.5 block text-label text-ink-muted">{place}</span> : null}
                  <span className="mt-2 flex items-center gap-1.5 text-label text-ink-muted">
                    <span aria-hidden="true" className="flex items-center gap-1">
                      {apps.map((a) => (
                        <span key={a.id} data-app-dot className={cn("size-2.5 rounded-full", a.dotClass)} />
                      ))}
                    </span>
                    <span>{apps.map((a) => a.name).join(", ")}</span>
                  </span>
                  {o.appLines
                    .filter((l) => l.line && normLine(l.line) !== normLine(o.line))
                    .map((l) => (
                      <span key={l.site} className="mt-1 block break-words text-label text-ink-muted">
                        {appOf(l.site)?.name ?? l.site}: {l.line}
                      </span>
                    ))}
                </span>
                <span
                  aria-hidden="true"
                  className={cn(
                    "mt-0.5 grid size-6 shrink-0 place-items-center rounded-full transition-colors duration-150",
                    checked ? "bg-accent text-white" : "ring-[1.5px] ring-inset ring-line-field text-accent",
                  )}
                >
                  {saving ? (
                    <Spinner className={cn("motion-safe:animate-spin", checked && "text-white")} />
                  ) : checked ? (
                    <CheckIcon size={16} strokeWidth={2.25} />
                  ) : null}
                </span>
              </button>
          );
        })}
      </div>

      {error ? (
        <p role="alert" className="mt-3 rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
          {error}
        </p>
      ) : null}

      <button
        type="button"
        onClick={onTypeInstead}
        disabled={busy !== null}
        className="mt-3 min-h-11 w-full rounded-control text-body font-semibold text-accent transition-colors duration-150 hover:bg-accent/5 active:bg-accent/10 disabled:opacity-50"
      >
        Enter an area instead
      </button>
    </div>
  );
}
