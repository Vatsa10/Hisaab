import type { Metadata } from "next";
import { AskChat } from "@/components/ask/AskChat";

export const metadata: Metadata = { title: "Ask" };

export default function AskPage() {
  return <AskChat />;
}
