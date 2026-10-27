"use client";

import { useEffect, useState } from "react";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";

export interface MonthSpend {
  /** "YYYY-MM" */
  month: string;
  amount: number;
  orders: number;
}

/** The device's current month as "YYYY-MM" (the format of monthly_spend.month). */
export function currentMonth(now: Date = new Date()): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/**
 * This month's spend and order count across every app, from the `monthly_spend` view (RLS
 * returns the signed-in user's rows only). Null while loading, on any error, or when the month
 * has no orders yet, so the caller simply shows nothing.
 */
export function useMonthSpend(): MonthSpend | null {
  const [spend, setSpend] = useState<MonthSpend | null>(null);

  useEffect(() => {
    let alive = true;
    const month = currentMonth();
    (async () => {
      try {
        const { data, error } = await getSupabaseBrowserClient()
          .from("monthly_spend")
          .select("orders, amount")
          .eq("month", month);
        if (!alive || error || !Array.isArray(data)) return;
        let orders = 0;
        let amount = 0;
        for (const row of data as Array<{ orders?: unknown; amount?: unknown }>) {
          orders += Number(row.orders) || 0;
          amount += Number(row.amount) || 0;
        }
        if (orders > 0) setSpend({ month, amount, orders });
      } catch {
        // No context line: the chat works without it.
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return spend;
}
