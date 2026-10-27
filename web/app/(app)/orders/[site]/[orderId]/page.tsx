import type { Metadata } from "next";
import { OrderDetailView } from "@/components/spend/OrderDetailView";

export const metadata: Metadata = { title: "Order" };

/** Decode a route segment once; a malformed escape is kept as written. */
function decode(v: string): string {
  try {
    return decodeURIComponent(v);
  } catch {
    return v;
  }
}

export default async function OrderPage({ params }: { params: Promise<{ site: string; orderId: string }> }) {
  const { site, orderId } = await params;
  return <OrderDetailView site={decode(site)} orderId={decode(orderId)} />;
}
