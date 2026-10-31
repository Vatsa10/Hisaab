// Saved delivery addresses, read from the user's own apps (table app_addresses, own rows only under
// RLS) and kept live over Supabase Realtime, so the addresses an app sync just found appear without a
// reload. Grouping into picker options happens here, in the PWA only: the same place saved on
// several apps (pins within 150 m of the group's lead, or the same address line, at most one row per
// app) is one option. Nothing here logs an address.

import type { RealtimeChannel, SupabaseClient } from "@supabase/supabase-js";
import { apiFetch } from "@/lib/api";
import { forUser, onOwnRowChanges, onWake } from "@/lib/jobs";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export interface AppAddress {
  site: string;
  address_id: string;
  label: string | null;
  line: string | null;
  area: string | null;
  city: string | null;
  lat: number | null;
  lon: number | null;
  is_default: boolean;
}

export interface Choice {
  site: string;
  address_id: string;
}

export interface AddressOption {
  /** Stable key: the first member's site and id. */
  key: string;
  label: string | null;
  line: string | null;
  area: string | null;
  city: string | null;
  is_default: boolean;
  /** The apps that have this place saved, in the Accounts screen's order. */
  sites: string[];
  /** Each app's own saved address for this place; the lead (shown) first. */
  members: Choice[];
  /** Each app's own address line for this place, in the Accounts screen's order (shown when they differ). */
  appLines: Array<{ site: string; line: string | null }>;
  /** What a tap saves: the lead if it has a pin, else the first member with one, else the lead. */
  send: Choice;
}

export const ADDRESS_COLUMNS = "site, address_id, label, line, area, city, lat, lon, is_default";
export const SAME_PLACE_M = 150;
const SITE_ORDER = ["zepto", "blinkit", "swiggy", "zomato"];

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v : null;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Copy only the known fields out of a row. */
export function toAddress(row: Record<string, unknown>): AppAddress {
  return {
    site: String(row.site ?? ""),
    address_id: String(row.address_id ?? ""),
    label: str(row.label),
    line: str(row.line),
    area: str(row.area),
    city: str(row.city),
    lat: num(row.lat),
    lon: num(row.lon),
    is_default: row.is_default === true,
  };
}

export function distanceM(a: [number, number], b: [number, number]): number {
  const rad = (d: number) => (d * Math.PI) / 180;
  const [lat1, lon1, lat2, lon2] = [rad(a[0]), rad(a[1]), rad(b[0]), rad(b[1])];
  const h = Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function normLine(text: string | null): string {
  return (text ?? "").toLowerCase().replace(/[^0-9a-z]+/g, " ").trim().replace(/\s+/g, " ");
}

function pin(a: AppAddress): [number, number] | null {
  return a.lat != null && a.lon != null ? [a.lat, a.lon] : null;
}

export function samePlace(a: AppAddress, b: AppAddress): boolean {
  const pa = pin(a);
  const pb = pin(b);
  if (pa && pb && distanceM(pa, pb) <= SAME_PLACE_M) return true;
  const la = normLine(a.line);
  return la !== "" && la === normLine(b.line);
}

/**
 * The user's saved addresses as picker options: one per place, default places first.
 *
 * Each group has a lead (its first row: defaults are visited first, so a default always leads its
 * group). A row joins a group only if it is the same place as that group's lead (never "same as
 * any member", so groups cannot chain across a street) and the group has no row from its app yet
 * (two flats saved on one app are two places, however close).
 */
export function groupOptions(rows: readonly AppAddress[]): AddressOption[] {
  const rank = (s: string) => {
    const i = SITE_ORDER.indexOf(s);
    return i === -1 ? SITE_ORDER.length : i;
  };
  const sorted = [...rows].sort(
    (a, b) =>
      Number(b.is_default) - Number(a.is_default) ||
      rank(a.site) - rank(b.site) ||
      a.address_id.localeCompare(b.address_id),
  );
  const groups: AppAddress[][] = [];
  for (const r of sorted) {
    const home = groups.find((g) => samePlace(r, g[0]!) && !g.some((m) => m.site === r.site));
    if (home) home.push(r);
    else groups.push([r]);
  }
  return groups.map((g): AddressOption => {
    const lead = g[0]!;
    const send = pin(lead) ? lead : (g.find((m) => pin(m)) ?? lead);
    return {
      key: `${lead.site}:${lead.address_id}`,
      label: lead.label ?? g.find((m) => m.label)?.label ?? null,
      line: lead.line,
      area: lead.area,
      city: lead.city,
      is_default: lead.is_default,
      sites: SITE_ORDER.filter((s) => g.some((m) => m.site === s)),
      members: g.map((m) => ({ site: m.site, address_id: m.address_id })),
      appLines: SITE_ORDER.flatMap((s) => g.filter((m) => m.site === s).map((m) => ({ site: s, line: m.line }))),
      send: { site: send.site, address_id: send.address_id },
    };
  });
}

export function isChosen(option: AddressOption, chosen: Choice | null | undefined): boolean {
  return Boolean(chosen && option.members.some((m) => m.site === chosen.site && m.address_id === chosen.address_id));
}

export interface ChoiceResponse {
  address: string;
  lat: number;
  lon: number;
  display?: string | null;
  chosen: Choice;
}

/** Make this saved place the delivery address (the API copies its text and pin into the profile). */
export function chooseAddress(option: AddressOption): Promise<ChoiceResponse> {
  return apiFetch<ChoiceResponse>("/profile", {
    method: "PUT",
    body: JSON.stringify({ choice: { site: option.send.site, address_id: option.send.address_id } }),
  });
}

/** Ask the server to re-read the saved addresses on the user's connected apps (read-only; once per 2 minutes). */
export function refreshAddresses(): Promise<{ sites: string[] }> {
  return apiFetch<{ sites: string[] }>("/addresses/refresh", { method: "POST" });
}

let channelSeq = 0;

/**
 * Follow the signed-in user's saved addresses: `onChange` gets the full list on join, on each of
 * their own inserts and updates (onOwnRowChanges: never DELETE, which Supabase delivers unfiltered),
 * and when the page wakes or comes back online, which is when deletes show. Without `userId` it is
 * read from the session. Without a browser Supabase client (not configured) it does nothing, and
 * the server-rendered list stays. Returns an unsubscribe function.
 */
export function watchAddresses(
  onChange: (rows: AppAddress[]) => void,
  client?: SupabaseClient,
  userId?: string,
): () => void {
  let db: SupabaseClient;
  try {
    db = client ?? getSupabaseBrowserClient();
  } catch {
    return () => {};
  }
  return forUser(db, userId, (uid) => follow(db, uid, onChange));
}

function follow(db: SupabaseClient, userId: string, onChange: (rows: AppAddress[]) => void): () => void {
  let active = true;
  let seq = 0;

  const refetch = () => {
    const mine = ++seq;
    void Promise.resolve(db.from("app_addresses").select(ADDRESS_COLUMNS)).then(
      ({ data, error }) => {
        if (!active || error || !Array.isArray(data) || mine !== seq) return; // only the newest read wins
        onChange((data as Record<string, unknown>[]).map(toAddress));
      },
      () => {},
    );
  };

  const channel: RealtimeChannel = onOwnRowChanges(
    db.channel(`app-addresses-${++channelSeq}`),
    "app_addresses",
    userId,
    refetch,
  ).subscribe((status) => {
    if (status === "SUBSCRIBED") refetch();
  });

  const stopWake = onWake(refetch);
  return () => {
    active = false;
    stopWake();
    void db.removeChannel(channel);
  };
}
