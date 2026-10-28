import type { Metadata } from "next";
import { ListEditor, type CartProfile } from "@/components/cart/ListEditor";
import { getSupabaseServerClient } from "@/lib/supabase/server";

export const metadata: Metadata = { title: "Cart" };

interface CartPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * Cart (Groceries): the saved delivery address comes from the server (the user's own profile
 * row, through RLS); the list, app status and checks left load in the browser.
 */
export default async function CartPage({ searchParams }: CartPageProps) {
  const params = await searchParams;
  const from = typeof params.from === "string" && params.from ? params.from : null;

  const supabase = await getSupabaseServerClient();
  const { data } = await supabase.from("profiles").select("address, lat, lon").maybeSingle();

  return <ListEditor profile={(data as CartProfile | null) ?? null} fromId={from} />;
}
