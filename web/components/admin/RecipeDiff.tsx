import type { DiffEntry } from "@/lib/admin";
import { cn } from "@/lib/cn";

function show(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === undefined) return "";
  return JSON.stringify(v);
}

const OPS: Record<string, { sign: string; tone: string; label: string }> = {
  add: { sign: "+", tone: "text-accent-strong bg-accent-soft", label: "Added" },
  remove: { sign: "−", tone: "text-danger bg-danger-soft", label: "Removed" },
  change: { sign: "~", tone: "text-up bg-up-soft", label: "Changed" },
};

/** One line of text per change: `+ path: value`, `− path`, `~ path: old → new`. */
export function diffLine(d: DiffEntry): string {
  if (d.op === "add") return `+ ${d.path}: ${show(d.new)}`;
  if (d.op === "remove") return `− ${d.path}`;
  return `~ ${d.path}: ${show(d.old)} → ${show(d.new)}`;
}

/**
 * A recipe change set, one row per path, in monospace so regexes and paths read exactly. Each row
 * is tinted by its operation and starts with its sign, so the meaning never rests on colour.
 * Long values wrap (anywhere) rather than scroll the page sideways.
 */
export function RecipeDiff({ diff, className }: { diff: DiffEntry[]; className?: string }) {
  if (diff.length === 0) return <p className={cn("text-label text-ink-muted", className)}>No changes to the recipe.</p>;
  return (
    <ul aria-label="Recipe changes" className={cn("flex flex-col gap-1", className)}>
      {diff.map((d, i) => {
        const op = OPS[d.op] ?? OPS.change!;
        return (
          <li
            key={`${d.path}-${i}`}
            title={op.label}
            className={cn("rounded-tile px-2.5 py-1.5 font-mono text-label [overflow-wrap:anywhere]", op.tone)}
          >
            {diffLine(d)}
          </li>
        );
      })}
    </ul>
  );
}
