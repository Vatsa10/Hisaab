"use client";

import { useMemo, useState } from "react";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/TextField";
import { QTY_MAX, QTY_MIN, siteName, type FoodSite } from "@/lib/cart";
import type { MenuDish, UsualDish } from "@/lib/food";
import { DishRow } from "./DishRow";

export interface MenuPickerProps {
  menu: MenuDish[];
  usual: UsualDish[];
  /** Picked quantity by row key (`d:{dish_id}` for menu dishes, `u:{text}` for usual dishes not on the menu). */
  qtyOf: (key: string) => number;
  onMenuQty: (dish: MenuDish, qty: number) => void;
  onUsualQty: (dish: UsualDish, qty: number) => void;
  /** Dishes are capped; true once no more can be added. */
  full: boolean;
}

export const menuKey = (d: MenuDish) => `d:${d.dish_id}`;
export const usualKey = (d: UsualDish) => `u:${d.text.trim().toLowerCase()}`;

/** The menu dish a usual dish is, by name; the usual row and the menu row then share one pick. */
export function menuMatch(menu: readonly MenuDish[], usual: UsualDish): MenuDish | undefined {
  const t = usual.text.trim().toLowerCase();
  return menu.find((m) => m.in_stock && m.name.trim().toLowerCase() === t);
}

/** Step 2 body: "Your usual dishes", then the full menu with a filter. */
export function MenuPicker({ menu, usual, qtyOf, onMenuQty, onUsualQty, full }: MenuPickerProps) {
  const [filter, setFilter] = useState("");
  const f = filter.trim().toLowerCase();
  const shown = useMemo(
    () => (f ? menu.filter((d) => `${d.name} ${d.variant ?? ""}`.toLowerCase().includes(f)) : menu),
    [menu, f],
  );

  return (
    <>
      {usual.length ? (
        <section aria-labelledby="usual-dishes" className="mt-5">
          <h2 id="usual-dishes" className="mb-2 pl-1 text-headline">
            Your usual dishes
          </h2>
          <Card padding="none">
            <ul>
              {usual.map((u) => {
                const m = menuMatch(menu, u);
                const key = m ? menuKey(m) : usualKey(u);
                const qty = qtyOf(key);
                return (
                  <DishRow
                    key={usualKey(u)}
                    dish={{
                      key,
                      name: u.text,
                      variant: m?.variant,
                      veg: m?.veg,
                      priceLabel: m?.price_label,
                      meta: `Ordered ${u.times} ${u.times === 1 ? "time" : "times"}`,
                      available: true,
                    }}
                    qty={qty}
                    addLabel={`Add ${u.text} (usual)`}
                    onQty={(q) => {
                      if (!qty && full) return;
                      // The first tap adds the quantity you usually order.
                      const next = !qty && q === 1 ? Math.min(QTY_MAX, Math.max(QTY_MIN, u.qty)) : q;
                      if (m) onMenuQty(m, next);
                      else onUsualQty(u, next);
                    }}
                  />
                );
              })}
            </ul>
          </Card>
        </section>
      ) : null}

      <section aria-labelledby="full-menu" className="mt-6">
        <h2 id="full-menu" className="mb-2 pl-1 text-headline">
          Menu
          <span className="ml-1.5 text-label font-normal text-ink-muted">{menu.length} dishes</span>
        </h2>
        <Card padding="none" className="px-2 py-3">
          <TextField
            label="Filter dishes"
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="e.g. biryani"
            autoComplete="off"
            enterKeyHint="search"
          />
        </Card>
        {full ? <p className="mt-2 px-1 text-label text-ink-muted">That is the most dishes one check can compare.</p> : null}
        {menu.length === 0 ? (
          <p className="mt-3 px-1 text-label text-ink-muted">We couldn&apos;t read this menu. Try another restaurant.</p>
        ) : shown.length ? (
          <Card padding="none" className="mt-3">
            <ul aria-label="Menu">
              {shown.map((d) => (
                <DishRow
                  key={menuKey(d)}
                  dish={{
                    key: menuKey(d),
                    name: d.name,
                    variant: d.variant,
                    veg: d.veg,
                    priceLabel: d.price_label,
                    available: d.in_stock,
                  }}
                  qty={qtyOf(menuKey(d))}
                  onQty={(q) => (!qtyOf(menuKey(d)) && full ? undefined : onMenuQty(d, q))}
                />
              ))}
            </ul>
          </Card>
        ) : (
          <p className="mt-3 px-1 text-label text-ink-muted">No dish matches &ldquo;{filter.trim()}&rdquo;.</p>
        )}
      </section>
    </>
  );
}

/** "Closed on Zomato right now." per closed app. */
export function closedLines(closed: readonly FoodSite[]): string[] {
  return closed.map((s) => `Closed on ${siteName(s)} right now.`);
}
