"use client";

import Link from "next/link";
import { Button, buttonClassName } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import type { FillSummary } from "@/lib/fill";

const TONES: Record<FillSummary["tone"], string> = {
  success: "text-accent",
  warning: "text-up",
  error: "text-[#b91c1c]",
};

export interface FillResultProps {
  summary: FillSummary;
  /** The fill's failure code; "needs_login" adds a Reconnect link. */
  error: string | null;
  onDone: () => void;
}

type LineTone = "muted" | "ink" | "warning" | "error";
interface Line {
  text: string;
  tone: LineTone;
}

const LINE_TONES: Record<LineTone, string> = {
  muted: "text-ink-muted",
  ink: "text-ink",
  warning: "font-medium text-up",
  error: "font-medium text-danger",
};

function errorTone(tone: FillSummary["tone"]): LineTone {
  return tone === "success" ? "ink" : tone;
}

/** What the fill did, as the server worded it. Hands off to the app: there is no checkout here. */
export function FillResult({ summary, error, onDone }: FillResultProps) {
  const problem: Line | null = summary.error_line ? { text: summary.error_line, tone: errorTone(summary.tone) } : null;
  const lead = problem && summary.tone !== "success" ? problem : null;
  const notes: Line[] = [];
  if (summary.cart_line) notes.push({ text: summary.cart_line, tone: "muted" });
  if (summary.existing_line) notes.push({ text: summary.existing_line, tone: "muted" });
  if (summary.warning_line) notes.push({ text: summary.warning_line, tone: "warning" });
  if (summary.location_line) notes.push({ text: summary.location_line, tone: "muted" });
  if (problem && !lead) notes.push(problem);
  return (
    <div>
      <h3 className={cn("text-headline", TONES[summary.tone])}>{summary.title}</h3>
      {lead ? <p className={cn("mt-1.5 text-body", LINE_TONES[lead.tone])}>{lead.text}</p> : null}

      {summary.sections.map((s) => (
        <section key={s.key} className="mt-4" aria-label={s.heading}>
          <h4 className="text-label font-semibold text-ink-muted">{s.heading}</h4>
          <ul className="mt-1 divide-y divide-line-soft">
            {s.rows.map((row, i) => (
              <li key={`${row}-${i}`} className="py-2 text-body break-words">
                {row}
              </li>
            ))}
          </ul>
        </section>
      ))}

      <div className="mt-4 space-y-1.5 text-label">
        {notes.map((l, i) => (
          <p key={i} className={LINE_TONES[l.tone]}>
            {l.text}
          </p>
        ))}
      </div>

      <div className="mt-5 flex flex-col gap-2">
        {summary.open ? (
          <a
            href={summary.open.url}
            target="_blank"
            rel="noopener noreferrer"
            className={buttonClassName({ fullWidth: true })}
          >
            {summary.open.label}
          </a>
        ) : null}
        <p className="text-center text-caption text-ink-muted">{summary.pay_note}</p>
        {error === "needs_login" ? (
          <Link href="/accounts" className={buttonClassName({ variant: "secondary", fullWidth: true })}>
            Reconnect
          </Link>
        ) : null}
        <Button variant={summary.open ? "ghost" : "secondary"} fullWidth onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}
