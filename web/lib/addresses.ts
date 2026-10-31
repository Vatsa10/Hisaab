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

// --- Same-place matcher: a scored, classic (no LLM) rule set mirrored from server/address_match.py. Display only;
// pricing always uses each app's own address_id. Both sides run tests/fixtures/address_pairs.json.
export const MATCH_THRESHOLD = 0.75;
const NEAR_M = 30;
const FAR_M = 250;
const PIN_VETO_FREE_M = 50;
const W_GEO = 0.45;
const W_TEXT = 0.55;
const PIN_BONUS = 0.15;
const UNIT_BONUS = 0.4;
const ABBREV: Record<string, string> = {
  soc: "society", apt: "apartment", apts: "apartments", flt: "flat", nr: "near", opp: "opposite", rd: "road",
  st: "street", sec: "sector", ph: "phase", blk: "block", bldg: "building", no: "number", hno: "house number",
};
const PLACES = new Set(
  ("india karnataka gujarat maharashtra delhi telangana tamil nadu kerala rajasthan haryana uttar pradesh punjab " +
    "bengal bengaluru bangalore vadodara baroda mumbai pune hyderabad chennai kolkata ahmedabad surat gurgaon " +
    "gurugram noida kochi jaipur").split(" "),
);
const FILLER = new Set("flat house number tower block wing near opposite the".split(" "));
const GROUP_WORDS = new Set(["tower", "block", "wing"]);
const AREA_WORDS = new Set("road street main cross lane sector phase layout nagar area".split(" "));
const PIN_RE = /^[1-9]\d{5}$/;
const NUM_RE = /^\d{1,5}[a-z]?$/;

function tokens(text: string): string[] {
  const s = text.normalize("NFKC").toLowerCase().replace(/[‘’'`´]/g, "");
  const out: string[] = [];
  for (const t of s.replace(/[^0-9a-z]+/g, " ").split(" ").filter(Boolean)) out.push(...(ABBREV[t] ?? t).split(" "));
  return out;
}

export interface ParsedAddress {
  pin: string | null;
  unit: string | null;
  tokens: string[];
  name: string;
}

export function parseAddress(...parts: Array<string | null | undefined>): ParsedAddress {
  const all = tokens(parts.filter(Boolean).join(" "));
  const pin = all.find((t) => PIN_RE.test(t)) ?? null;
  const toks = all.filter((t) => t !== pin);
  let number = -1;
  for (let i = 0; i < toks.length - 1; i++) {
    if ((toks[i] === "flat" || toks[i] === "number") && NUM_RE.test(toks[i + 1]!)) {
      number = i + 1;
      break;
    }
  }
  if (number < 0) number = toks.findIndex((t) => NUM_RE.test(t));
  let tower: string | null = null;
  const used = new Set<number>();
  for (let i = 0; i < toks.length && tower === null; i++) {
    if (!GROUP_WORDS.has(toks[i]!)) continue;
    for (const j of [i + 1, i - 1]) {
      const c = toks[j];
      if (c !== undefined && j >= 0 && c.length <= 2 && !FILLER.has(c) && j !== number) {
        tower = c;
        used.add(j);
        break;
      }
    }
  }
  if (tower === null && number > 0 && /^[a-z]$/.test(toks[number - 1]!)) {
    tower = toks[number - 1]!;
    used.add(number - 1);
  }
  const unit = number >= 0 ? (tower ?? "") + toks[number]! : null;
  if (number >= 0) used.add(number);
  const rest = toks.filter((t, i) => !used.has(i) && !PLACES.has(t) && !FILLER.has(t));
  return { pin, unit, tokens: rest, name: rest.filter((t) => !AREA_WORDS.has(t)).join(" ") };
}

export function levenshteinRatio(a: string, b: string): number {
  if (!a && !b) return 0;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur.push(Math.min(prev[j]! + 1, cur[j - 1]! + 1, prev[j - 1]! + (a[i - 1] !== b[j - 1] ? 1 : 0)));
    }
    prev = cur;
  }
  return 1 - prev[b.length]! / Math.max(a.length, b.length);
}

type PlaceLike = Pick<AppAddress, "line" | "area" | "city" | "lat" | "lon">;

/** How likely two saved addresses are one place; 0 on a veto (different flat, or different PIN beyond 50 m). */
export function placeScore(a: PlaceLike, b: PlaceLike): number {
  const pa = parseAddress(a.line, a.area, a.city);
  const pb = parseAddress(b.line, b.area, b.city);
  const ga = pin(a as AppAddress);
  const gb = pin(b as AppAddress);
  const d = ga && gb ? distanceM(ga, gb) : null;
  if (pa.unit && pb.unit && pa.unit !== pb.unit) return 0;
  if (pa.pin && pb.pin && pa.pin !== pb.pin && !(d !== null && d <= PIN_VETO_FREE_M)) return 0;
  const sa = new Set(pa.tokens);
  const sb = new Set(pb.tokens);
  let text = 0;
  if (sa.size && sb.size) {
    const inter = [...sa].filter((t) => sb.has(t)).length;
    text = 0.5 * (inter / (sa.size + sb.size - inter)) + 0.5 * levenshteinRatio(pa.name, pb.name);
  }
  let total = text;
  if (d !== null) {
    const geo = d <= NEAR_M ? 1 : d >= FAR_M ? 0 : (FAR_M - d) / (FAR_M - NEAR_M);
    total = W_GEO * geo + W_TEXT * text;
  }
  if (pa.pin && pa.pin === pb.pin) total += PIN_BONUS;
  if (pa.unit && pa.unit === pb.unit) total += UNIT_BONUS;
  return total;
}

export function samePlace(a: PlaceLike, b: PlaceLike): boolean {
  return placeScore(a, b) >= MATCH_THRESHOLD;
}

/**
 * Display groups: union-find over pairs that score at least the threshold, best pair first, never two rows of one
 * app in a group. Rows come in display order (defaults first). A group is led by its default address, else its
 * highest-scoring member; the rest keep their order.
 */
export function groupRows(rows: readonly AppAddress[]): AppAddress[][] {
  const n = rows.length;
  const parent = Array.from({ length: n }, (_, i) => i);
  const members = new Map<number, number[]>(rows.map((_, i) => [i, [i]]));
  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]!]!;
      i = parent[i]!;
    }
    return i;
  };
  const total = new Array<number>(n).fill(0);
  const pairs: Array<[number, number, number]> = [];
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const s = placeScore(rows[i]!, rows[j]!);
      if (s >= MATCH_THRESHOLD) pairs.push([s, i, j]);
    }
  }
  pairs.sort((p, q) => q[0] - p[0] || p[1] - q[1] || p[2] - q[2]);
  for (const [s, i, j] of pairs) {
    const ri = find(i);
    const rj = find(j);
    if (ri === rj) continue;
    const sites = new Set(members.get(ri)!.map((k) => rows[k]!.site));
    if (members.get(rj)!.some((k) => sites.has(rows[k]!.site))) continue;
    parent[rj] = ri;
    members.set(ri, [...members.get(ri)!, ...members.get(rj)!]);
    members.delete(rj);
    total[i]! += s;
    total[j]! += s;
  }
  return [...members.values()]
    .map((g) => [...g].sort((x, y) => x - y))
    .sort((x, y) => x[0]! - y[0]!)
    .map((g) => {
      let lead = g.find((k) => rows[k]!.is_default);
      if (lead === undefined) lead = g.reduce((best, k) => (total[k]! > total[best]! ? k : best), g[0]!);
      return [rows[lead]!, ...g.filter((k) => k !== lead).map((k) => rows[k]!)];
    });
}

/**
 * The user's saved addresses as picker options: one per place, default places first.
 *
 * Places are matched by the scored matcher above (flat/tower key, PIN, society name, distance), at most one row
 * per app in a group (two flats saved on one app are two places, however close). The default leads its group.
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
  const groups = groupRows(sorted);
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
