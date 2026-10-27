import type { Metadata } from "next";
import { OrdersList } from "@/components/spend/OrdersList";
import { APPS } from "@/lib/apps";

export const metadata: Metadata = { title: "Orders" };

type Search = Record<string, string | string[] | undefined>;

function one(v: string | string[] | undefined): string | undefined {
  return typeof v === "string" ? v : undefined;
}

/** Orders, optionally filtered by `?site=` (a known app) and `?month=YYYY-MM`; anything else is ignored. */
export default async function OrdersPage({ searchParams }: { searchParams: Promise<Search> }) {
  const params = await searchParams;
  const siteParam = one(params.site);
  const monthParam = one(params.month);
  const site = APPS.some((a) => a.id === siteParam) ? siteParam : undefined;
  const month = monthParam && /^\d{4}-(0[1-9]|1[0-2])$/.test(monthParam) ? monthParam : undefined;
  return <OrdersList key={`${site ?? ""}|${month ?? ""}`} site={site} month={month} />;
}
