"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useState, type FormEvent } from "react";
import { PinIcon } from "@/components/connect/icons";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { TextField } from "@/components/ui/TextField";
import { ApiError } from "@/lib/api";
import {
  CART_MAX_ITEMS,
  GROCERY_SITES,
  ITEM_TEXT_MAX,
  addItem,
  areaLabel,
  checksLeftLine,
  getAccess,
  getCartChecksLeft,
  getRequest,
  parseLine,
  recentChecks,
  removeAt,
  setQty,
  startCheck,
  type AccessMap,
  type CartItem,
  type CartRequest,
} from "@/lib/cart";
import { AppAccessRow } from "./AppAccessRow";
import { ItemRow } from "./ItemRow";
import { RecentChecks } from "./RecentChecks";
import { UsualSheet } from "./UsualSheet";

export interface CartProfile {
  address: string | null;
  lat: number | null;
  lon: number | null;
}

export interface ListEditorProps {
  /** The saved delivery address (profiles row), or null when there is none. */
  profile: CartProfile | null;
  /** `?from=<request id>`: start from that check's list ("Edit list"). */
  fromId?: string | null;
}

function problem(e: unknown): string {
  if (e instanceof ApiError && e.detail) return e.detail;
  return "Couldn't start the check. Try again.";
}

/**
 * The /cart screen in Groceries mode: where to deliver, which apps can be priced, the list, and
 * "Compare prices". A check starts on the server (202) and its result lives at /cart/{id}.
 */
export function ListEditor({ profile, fromId }: ListEditorProps) {
  const router = useRouter();
  const formId = useId();
  const listHeadingId = useId();

  const [items, setItems] = useState<CartItem[]>([]);
  const [draft, setDraft] = useState("");
  const [access, setAccess] = useState<AccessMap | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [checksLeft, setChecksLeft] = useState<number | null>(null);
  const [recent, setRecent] = useState<CartRequest[]>([]);
  const [usualOpen, setUsualOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let live = true;
    getAccess("grocery").then(
      (a) => live && setAccess(a),
      () => live && setAccessFailed(true),
    );
    getCartChecksLeft().then(
      (n) => live && setChecksLeft(n),
      () => {},
    );
    recentChecks(3).then(
      (rows) => {
        if (!live) return;
        setRecent(rows);
        setNow(Date.now());
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, []);

  // "Edit list" from a result: start from that check's items, unless the user already began typing.
  useEffect(() => {
    if (!fromId) return;
    let live = true;
    getRequest(fromId).then(
      (req) => {
        if (!live || !req || req.mode !== "grocery" || !req.items.length) return;
        setItems((cur) => (cur.length ? cur : req.items.slice(0, CART_MAX_ITEMS)));
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [fromId]);

  const hasAddress = Boolean(profile?.address) && profile?.lat != null && profile?.lon != null;
  // The server refuses a check with no app it could price (409); apps that are connected but
  // briefly unavailable are its call (503), so they do not block here.
  const noApp =
    access != null && !GROCERY_SITES.some((s) => access[s] === "ok" || access[s] === "unavailable");
  const outOfChecks = checksLeft === 0;
  const full = items.length >= CART_MAX_ITEMS;
  const canCompare = hasAddress && !noApp && !outOfChecks && items.length > 0;

  function edit(next: CartItem[]) {
    setItems(next);
    if (error) setError(null);
  }

  function add(e: FormEvent) {
    e.preventDefault();
    const item = parseLine(draft);
    if (!item.text) return;
    edit(addItem(items, item));
    setDraft("");
  }

  function addMany(list: CartItem[]) {
    edit(list.reduce((acc, it) => addItem(acc, it), items));
  }

  async function compare() {
    if (!canCompare || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await startCheck(items);
      setChecksLeft(res.cart_checks_left);
      router.push(`/cart/${encodeURIComponent(res.request_id)}`);
    } catch (err) {
      setError(problem(err));
      setBusy(false);
    }
  }

  const count = items.length;

  return (
    <>
      <PageHeader title="Cart" subtitle="Compare your prices on Zepto, Blinkit and Instamart." />

      <Card as="section" aria-label="Where and which apps" padding="none">
        {hasAddress ? (
          <Link
            href="/accounts"
            className="flex items-center gap-3 rounded-t-card border-b border-line-soft p-4 transition-colors duration-150 active:bg-canvas"
          >
            <span aria-hidden="true" className="grid size-8 shrink-0 place-items-center rounded-tile bg-accent-soft text-accent">
              <PinIcon size={18} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-label text-ink-muted">Delivering to</span>
              <span className="block truncate font-medium">{areaLabel(profile?.address)}</span>
            </span>
            <span className="shrink-0 text-label font-semibold text-accent">Change</span>
          </Link>
        ) : (
          <Link
            href="/accounts"
            className="flex items-center gap-3 rounded-t-card border-b border-line-soft p-4 transition-colors duration-150 active:bg-canvas"
          >
            <span aria-hidden="true" className="grid size-8 shrink-0 place-items-center rounded-tile bg-up-soft text-up">
              <PinIcon size={18} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block font-medium">Add your delivery address</span>
              <span className="block text-label text-ink-muted">Prices depend on where you order to.</span>
            </span>
            <span aria-hidden="true" className="shrink-0 font-semibold text-accent">
              ›
            </span>
          </Link>
        )}
        <AppAccessRow access={access} failed={accessFailed} />
        {noApp ? (
          <p className="border-t border-line-soft px-4 py-3 text-label text-ink">
            Connect Blinkit, Zepto or Instamart to compare.
          </p>
        ) : null}
      </Card>

      <section aria-labelledby={listHeadingId} className="mt-6">
        <div className="mb-2 flex items-center justify-between gap-3 pl-1">
          <h2 id={listHeadingId} className="text-headline">
            Your list
            {count ? <span className="ml-1.5 text-label font-normal text-ink-muted">{count === 1 ? "1 item" : `${count} items`}</span> : null}
          </h2>
          <Button variant="ghost" className="-mr-2 shrink-0 px-3" onClick={() => setUsualOpen(true)}>
            Start from my usual
          </Button>
        </div>

        <Card padding="none">
          <form id={formId} onSubmit={add} noValidate className="p-3">
            <TextField
              label="New item"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Add an item, e.g. Amul Taaza 1L ×2"
              maxLength={ITEM_TEXT_MAX + 6}
              enterKeyHint="done"
              autoComplete="off"
              autoCapitalize="sentences"
              disabled={full}
              hint={full ? `You can compare up to ${CART_MAX_ITEMS} items at a time.` : undefined}
              trailing={
                <button
                  type="submit"
                  aria-label="Add item"
                  disabled={full || !draft.trim()}
                  className="mr-0.5 grid size-11 shrink-0 place-items-center rounded-tile text-accent transition-[background-color,opacity] duration-150 active:bg-accent/10 disabled:opacity-40"
                >
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
                    <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.75" />
                    <path d="M12 8v8M8 12h8" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
                  </svg>
                </button>
              }
            />
          </form>
          {count ? (
            <ul aria-label="Items" className="border-t border-line-soft">
              {items.map((item, i) => (
                <ItemRow
                  key={`${item.text}-${i}`}
                  item={item}
                  onQty={(q) => edit(setQty(items, i, q))}
                  onRemove={() => edit(removeAt(items, i))}
                />
              ))}
            </ul>
          ) : (
            <p className="border-t border-line-soft px-4 py-4 text-label text-ink-muted">
              Add what you buy, one item at a time. Write ×2 for two.
            </p>
          )}
        </Card>
      </section>

      {/* Pinned just above the tab bar, so Compare stays in reach however long the list gets. */}
      <div className="sticky bottom-[calc(theme(spacing.tabbar)+env(safe-area-inset-bottom))] z-10 -mx-4 mt-3 bg-canvas px-4 pb-3 pt-3">
        <Button size="lg" fullWidth disabled={!canCompare} loading={busy} onClick={compare}>
          Compare prices
        </Button>
        {error ? (
          <p role="alert" className="mt-3 text-center text-label text-danger">
            {error}
          </p>
        ) : null}
        {checksLeft != null ? (
          <p className="mt-2 text-center text-label text-ink-muted">{checksLeftLine(checksLeft)}</p>
        ) : null}
      </div>

      <RecentChecks checks={recent} now={now} />

      <UsualSheet open={usualOpen} onClose={() => setUsualOpen(false)} onAdd={addMany} />
    </>
  );
}
