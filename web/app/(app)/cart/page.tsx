import type { Metadata } from "next";
import { FoodFlow } from "@/components/cart/food/FoodFlow";
import { ListEditor } from "@/components/cart/ListEditor";
import { ModeSwitch } from "@/components/cart/ModeSwitch";
import { cartArea } from "@/lib/cart";
import { getSupabaseServerClient } from "@/lib/supabase/server";

export const metadata: Metadata = { title: "Cart" };

interface CartPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * Cart. Groceries is the default; `?mode=food` selects Food. The saved delivery address is read on
 * the server (the user's own profile row, through RLS) and only its short area label goes to the
 * browser: never the full address or the coordinates. The list, app status and checks left load in
 * the browser.
 */
export default async function CartPage({ searchParams }: CartPageProps) {
  const params = await searchParams;
  const from = typeof params.from === "string" && params.from ? params.from : null;
  const food = params.mode === "food";

  if (food) {
    return (
      <>
        <ModeSwitch mode="food" />
        <FoodFlow fromId={from} />
      </>
    );
  }

  const supabase = await getSupabaseServerClient();
  const { data } = await supabase.from("profiles").select("address, lat, lon").maybeSingle();
  const area = cartArea(data as { address: string | null; lat: number | null; lon: number | null } | null);

  return (
    <>
      <ModeSwitch mode="grocery" />
      <ListEditor area={area} fromId={from} />
    </>
  );
}
