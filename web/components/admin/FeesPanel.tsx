"use client";

import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/TextField";
import {
  appName,
  getFees,
  isForbidden,
  problem,
  putFees,
  type FeeRow,
  type FeeRules,
} from "@/lib/admin";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Badge, ListSkeleton, LoadError, SiteMark, useAdminLoad, type PanelProps } from "./parts";

const GROCERY_WAIVES = [
  ["delivery", "Delivery"],
  ["handling", "Handling"],
  ["small_cart", "Small cart"],
  ["surge", "Surge"],
] as const;
const FOOD_WAIVES = [
  ["delivery", "Delivery"],
  ["packaging", "Packaging"],
  ["platform", "Platform fee"],
  ["small_order", "Small order"],
  ["surge", "Surge"],
] as const;

/** Fees tab: one card per site with the admin-maintained fee rules, edited in place. */
export function FeesPanel({ onForbidden }: PanelProps) {
  const fees = useAdminLoad("fees", getFees, onForbidden);

  if (fees.status === "loading") return <ListSkeleton rows={5} label="Fee rules" />;
  if (fees.status === "error") return <LoadError what="fee rules" onRetry={fees.reload} />;
  if (fees.data.length === 0) {
    return (
      <Card>
        <p className="text-body text-ink-muted">No fee rules yet.</p>
      </Card>
    );
  }
  return (
    <ul aria-label="Fee rules" className="flex flex-col gap-3">
      {fees.data.map((row) => (
        <FeeCard key={row.site} row={row} onSaved={fees.reload} onForbidden={onForbidden} />
      ))}
    </ul>
  );
}

// ---- form state ---------------------------------------------------------------------------

interface Form {
  food: boolean;
  /** Grocery: min_subtotal. Food: max_km ("" on the last tier, which has none). */
  tiers: { at: string; fee: string }[];
  handling: string;
  packaging: string;
  platform: string;
  smallOn: boolean;
  smallBelow: string;
  smallFee: string;
  surge: boolean;
  surgeFee: string;
  memberOn: boolean;
  memberName: string;
  waives: string[];
  gstFood: string;
  gstFees: string;
  sourceUrl: string;
  verified: boolean;
}

const text = (v: unknown) => (v == null ? "" : String(v));
const obj = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

function fromRules(rules: FeeRules, verified: boolean): Form {
  const food = rules.kind === "food";
  const raw = rules[food ? "delivery_by_km" : "delivery"];
  const tiers = (Array.isArray(raw) ? raw : []).map((t) => {
    const o = obj(t);
    return { at: text(food ? o.max_km : o.min_subtotal), fee: text(o.fee) };
  });
  const small = obj(rules[food ? "small_order" : "small_cart"]);
  const member = obj(rules.membership);
  const gst = obj(rules.gst);
  return {
    food,
    tiers: tiers.length ? tiers : [{ at: food ? "" : "0", fee: "" }],
    handling: text(rules.handling),
    packaging: text(rules.packaging_per_item),
    platform: text(rules.platform),
    smallOn: Boolean(rules[food ? "small_order" : "small_cart"]),
    smallBelow: text(small.below),
    smallFee: text(small.fee),
    surge: Boolean(obj(rules.surge).possible),
    surgeFee: text(obj(rules.surge).fee),
    memberOn: Boolean(rules.membership),
    memberName: text(member.name),
    waives: Array.isArray(member.waives) ? member.waives.map(String) : [],
    gstFood: text(gst.food_pct),
    gstFees: text(gst.fees_pct),
    sourceUrl: text(rules.source_url),
    verified,
  };
}

/** A number when the box holds one; otherwise the raw text, so the API says what is wrong. */
const num = (s: string): number | string => (s.trim() !== "" && Number.isFinite(Number(s)) ? Number(s) : s.trim());

function toRules(f: Form, base: FeeRules): FeeRules {
  const waivable = f.food ? FOOD_WAIVES : GROCERY_WAIVES;
  const common = {
    surge: f.surge ? { possible: true, ...(f.surgeFee.trim() ? { fee: num(f.surgeFee) } : {}) } : { possible: false },
    membership: f.memberOn
      ? { name: f.memberName.trim(), waives: waivable.map(([k]) => k).filter((k) => f.waives.includes(k)) }
      : null,
    source_url: f.sourceUrl.trim() || null,
    note: base.note ?? null,
  };
  const small = f.smallOn ? { below: num(f.smallBelow), fee: num(f.smallFee) } : null;
  if (f.food) {
    return {
      kind: "food",
      delivery_by_km: f.tiers.map((t, i) => ({ max_km: i === f.tiers.length - 1 ? null : num(t.at), fee: num(t.fee) })),
      packaging_per_item: num(f.packaging),
      platform: num(f.platform),
      small_order: small,
      gst: { food_pct: num(f.gstFood), fees_pct: num(f.gstFees) },
      ...common,
    };
  }
  return {
    ...(base.kind ? { kind: base.kind } : {}),
    delivery: f.tiers.map((t, i) => ({ min_subtotal: i === 0 ? 0 : num(t.at), fee: num(t.fee) })),
    handling: num(f.handling),
    small_cart: small,
    ...common,
  };
}

const rupees = (s: string) => `₹${s.trim() || "0"}`;

function summary(rules: FeeRules): string {
  const f = fromRules(rules, false);
  const first = f.tiers[0]?.fee ?? "0";
  return f.food
    ? `Delivery from ${rupees(first)} · platform ${rupees(f.platform)} · GST ${f.gstFood || 0}% / ${f.gstFees || 0}%`
    : `Delivery from ${rupees(first)} · handling ${rupees(f.handling)}`;
}

// ---- pieces -------------------------------------------------------------------------------

function Num(props: { label: string; value: string; onChange: (v: string) => void; readOnly?: boolean; hint?: ReactNode }) {
  return (
    <TextField
      label={props.label}
      hint={props.hint}
      value={props.value}
      readOnly={props.readOnly}
      inputMode="decimal"
      autoComplete="off"
      onChange={(e) => props.onChange(e.target.value)}
      className={props.readOnly ? "opacity-70" : undefined}
    />
  );
}

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex min-h-11 cursor-pointer items-center gap-3 text-body text-ink">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-5 shrink-0 accent-accent"
      />
      <span>{label}</span>
    </label>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-5 flex flex-col gap-3 border-t border-line-soft pt-4">
      <h4 className="text-label font-semibold text-ink-muted">{title}</h4>
      {children}
    </section>
  );
}

// ---- one site -----------------------------------------------------------------------------

function FeeCard({ row, onSaved, onForbidden }: { row: FeeRow; onSaved: () => void; onForbidden: () => void }) {
  const name = appName(row.site);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(() => fromRules(row.rules, row.verified));
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState<number | null>(null);
  const set = (patch: Partial<Form>) => {
    setSaved(null);
    setForm((f) => ({ ...f, ...patch }));
  };
  const setTier = (i: number, patch: Partial<Form["tiers"][number]>) =>
    set({ tiers: form.tiers.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const addTier = () => {
    const blank = { at: "", fee: "" };
    set({ tiers: form.food ? [...form.tiers.slice(0, -1), blank, form.tiers[form.tiers.length - 1]!] : [...form.tiers, blank] });
  };
  const removeTier = (i: number) => set({ tiers: form.tiers.filter((_, j) => j !== i) });
  const waivable = form.food ? FOOD_WAIVES : GROCERY_WAIVES;
  const unit = form.food ? "km" : "₹";

  async function save() {
    setBusy(true);
    setErrors([]);
    setSaved(null);
    try {
      const res = await putFees(row.site, toRules(form, row.rules), form.verified);
      setSaved(res.version);
      onSaved();
    } catch (e) {
      if (isForbidden(e)) onForbidden();
      else if (e instanceof ApiError && e.status === 422) setErrors(e.detail.split("; ").filter(Boolean));
      else setErrors([problem(e, `Couldn't save ${name}'s fees. Try again.`)]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card as="li" aria-label={`${name} fees`}>
      <div className="flex items-center gap-3">
        <SiteMark site={row.site} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-headline">
            {name}
            <span className="ml-1.5 text-label font-normal text-ink-muted">{form.food ? "food" : "groceries"}</span>
          </h3>
          <p className="text-label text-ink-muted">Version {saved ?? row.version}</p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {row.verified ? <Badge tone="ok">Verified</Badge> : <Badge tone="warn">{`Unverified — check ${name}'s fee page`}</Badge>}
      </div>
      <p className="mt-2 text-label tabular-nums text-ink-muted">{summary(row.rules)}</p>

      <Button
        variant="secondary"
        fullWidth
        className="mt-3"
        aria-expanded={open}
        aria-controls={`fees-form-${row.site}`}
        aria-label={`${open ? "Close" : "Edit"} ${name} fees`}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "Close" : "Edit"}
      </Button>

      {open ? (
        <form
          id={`fees-form-${row.site}`}
          className="mt-4"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <h4 className="text-label font-semibold text-ink-muted">{form.food ? "Delivery by distance" : "Delivery by cart value"}</h4>
          <ul className="mt-3 flex flex-col gap-4">
            {form.tiers.map((t, i) => {
              const last = form.tiers.length - 1 === i;
              const beyond = form.food && last;
              const n = i + 1;
              return (
                <li key={i} className="flex flex-col gap-2">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-label tabular-nums text-ink">
                      {form.food
                        ? beyond
                          ? `Beyond → ${rupees(t.fee)}`
                          : `Up to ${t.at.trim() || "?"} km → ${rupees(t.fee)}`
                        : `From ${rupees(i === 0 ? "0" : t.at)} → ${rupees(t.fee)}`}
                    </p>
                    {form.tiers.length > 1 && i > 0 && !beyond ? (
                      <Button variant="ghost" className="-mr-3" onClick={() => removeTier(i)} aria-label={`Remove tier ${n}`}>
                        Remove
                      </Button>
                    ) : null}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    {beyond ? (
                      <p className="flex min-h-12 items-end pb-3 text-body text-ink-muted">Beyond</p>
                    ) : (
                      <Num
                        label={form.food ? `Tier ${n} up to (${unit})` : `Tier ${n} from (${unit})`}
                        value={!form.food && i === 0 ? "0" : t.at}
                        readOnly={!form.food && i === 0}
                        onChange={(v) => setTier(i, { at: v })}
                      />
                    )}
                    <Num label={beyond ? "Beyond fee (₹)" : `Tier ${n} fee (₹)`} value={t.fee} onChange={(v) => setTier(i, { fee: v })} />
                  </div>
                </li>
              );
            })}
          </ul>
          <Button variant="ghost" className="mt-2 -ml-3" onClick={addTier}>
            Add a tier
          </Button>

          <Section title="Other fees">
            {form.food ? (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Num label="Packaging per dish (₹)" value={form.packaging} onChange={(v) => set({ packaging: v })} />
                  <Num label="Platform fee (₹)" value={form.platform} onChange={(v) => set({ platform: v })} />
                </div>
              </>
            ) : (
              <Num label="Handling fee (₹)" value={form.handling} onChange={(v) => set({ handling: v })} />
            )}
            <Check
              label={form.food ? "Small-order fee applies" : "Small-cart fee applies"}
              checked={form.smallOn}
              onChange={(v) => set({ smallOn: v })}
            />
            {form.smallOn ? (
              <div className="grid grid-cols-2 gap-3">
                <Num
                  label={form.food ? "Small-order below (₹)" : "Small-cart below (₹)"}
                  value={form.smallBelow}
                  onChange={(v) => set({ smallBelow: v })}
                />
                <Num
                  label={form.food ? "Small-order fee (₹)" : "Small-cart fee (₹)"}
                  value={form.smallFee}
                  onChange={(v) => set({ smallFee: v })}
                />
              </div>
            ) : null}
            <Check label="Surge pricing is possible" checked={form.surge} onChange={(v) => set({ surge: v })} />
            {form.surge ? (
              <Num label="Surge fee (₹, optional)" value={form.surgeFee} onChange={(v) => set({ surgeFee: v })} />
            ) : null}
          </Section>

          {form.food ? (
            <Section title="GST">
              <div className="grid grid-cols-2 gap-3">
                <Num label="GST on food (%)" value={form.gstFood} onChange={(v) => set({ gstFood: v })} />
                <Num label="GST on fees (%)" value={form.gstFees} onChange={(v) => set({ gstFees: v })} />
              </div>
            </Section>
          ) : null}

          <Section title="Membership">
            <Check label="Has a membership" checked={form.memberOn} onChange={(v) => set({ memberOn: v })} />
            {form.memberOn ? (
              <>
                <TextField
                  label="Membership name"
                  value={form.memberName}
                  maxLength={40}
                  onChange={(e) => set({ memberName: e.target.value })}
                />
                <fieldset className="flex flex-col">
                  <legend className="mb-1 text-label font-medium text-ink">Fees it waives</legend>
                  {waivable.map(([key, label]) => (
                    <Check
                      key={key}
                      label={label}
                      checked={form.waives.includes(key)}
                      onChange={(v) => set({ waives: v ? [...form.waives, key] : form.waives.filter((w) => w !== key) })}
                    />
                  ))}
                </fieldset>
              </>
            ) : null}
          </Section>

          <Section title="Source">
            <TextField
              label="Source URL"
              type="url"
              inputMode="url"
              autoComplete="off"
              value={form.sourceUrl}
              hint={`Where these numbers come from, e.g. ${name}'s fee page.`}
              onChange={(e) => set({ sourceUrl: e.target.value })}
            />
            <Check label="These match the app today" checked={form.verified} onChange={(v) => set({ verified: v })} />
          </Section>

          {errors.length > 0 ? (
            <div role="alert" className="mt-4 rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
              <p className="font-semibold">Not saved. Fix this and try again:</p>
              <ul className="mt-1 list-disc pl-4">
                {errors.map((m, i) => (
                  <li key={i} className="[overflow-wrap:anywhere]">
                    {m}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {saved != null ? (
            <p role="status" className={cn("mt-4 text-label font-semibold text-accent")}>
              {`Saved. Now version ${saved}.`}
            </p>
          ) : null}

          <Button type="submit" fullWidth className="mt-4" loading={busy} aria-label={`Save ${name} fees`}>
            Save
          </Button>
        </form>
      ) : null}
    </Card>
  );
}
