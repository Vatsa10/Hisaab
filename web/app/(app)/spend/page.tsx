import type { Metadata } from "next";
import { SpendDashboard } from "@/components/spend/SpendDashboard";

export const metadata: Metadata = { title: "Spending" };

export default function SpendPage() {
  return <SpendDashboard />;
}
