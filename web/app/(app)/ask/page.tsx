import type { Metadata } from "next";
import { AskChat } from "@/components/ask/AskChat";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = { title: "Ask" };

export default function AskPage() {
  return (
    <>
      <PageHeader eyebrow="Spending analyst" title="Ask" />
      <AskChat />
    </>
  );
}
