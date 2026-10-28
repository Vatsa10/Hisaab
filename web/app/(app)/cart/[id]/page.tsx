import type { Metadata } from "next";
import { CartResultView } from "@/components/cart/CartResultView";

export const metadata: Metadata = { title: "Cart" };

export default async function CartResultPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <CartResultView id={id} />;
}
