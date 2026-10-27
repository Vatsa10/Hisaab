"use client";

import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";

/** Only absolute http(s) links are followed; anything else renders as plain text. */
export function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null; // relative or malformed
  }
}

// --- table shaping ---------------------------------------------------------------------------
// Minimal hast shape: the plugin only walks elements and reads or rewrites text.
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

const NUMERIC = /^[+\-−]?\s*₹?\s*[+\-−]?\d[\d,]*(\.\d+)?\s*(%|x|×)?$/i;
/** A rank column: "#", "No.", "Rank", "Sr. No." over 1, 2, 3… */
const RANK_HEADER = /^(#|no\.?|rank|sr\.?(\s*no\.?)?|s\.?\s*no\.?)$/i;
const RANK_VALUE = /^\d{1,3}\.?$/;

function textOf(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

function firstText(node: HastNode): HastNode | undefined {
  if (node.type === "text") return node;
  for (const child of node.children ?? []) {
    const found = firstText(child);
    if (found) return found;
  }
  return undefined;
}

function childElements(node: HastNode, tag: string): HastNode[] {
  return (node.children ?? []).filter((c) => c.type === "element" && c.tagName === tag);
}

function setProps(cell: HastNode, props: Record<string, unknown>, classes: string[] = []): void {
  const current = cell.properties?.className;
  const list = Array.isArray(current) ? current : typeof current === "string" ? current.split(" ") : [];
  cell.properties = { ...cell.properties, ...props, className: [...list, ...classes] };
}

function shapeTable(table: HastNode): void {
  const sections = (table.children ?? []).filter((c) => c.type === "element");
  const rows = sections.flatMap((s) => (s.tagName === "tr" ? [s] : childElements(s, "tr")));
  const cellsOf = (row: HastNode) =>
    (row.children ?? []).filter((c) => c.type === "element" && (c.tagName === "td" || c.tagName === "th"));
  const bodyRows = rows.filter((r) => cellsOf(r).some((c) => c.tagName === "td"));
  const headRow = rows.find((r) => cellsOf(r).every((c) => c.tagName === "th"));
  const width = Math.max(0, ...rows.map((r) => cellsOf(r).length));
  const column = (col: number) =>
    bodyRows.map((r) => textOf(cellsOf(r)[col] ?? { type: "text" }).trim()).filter(Boolean);

  // Number columns line up on the right. An explicit alignment from the markdown (|---:|) wins.
  for (let col = 0; col < width; col++) {
    const values = column(col);
    if (!values.length || !values.every((v) => NUMERIC.test(v))) continue;
    for (const row of rows) {
      const cell = cellsOf(row)[col];
      if (cell && !cell.properties?.align) setProps(cell, { align: "right" });
    }
  }

  // A true minus sign (U+2212) in front of negative numbers: same width as "+", easier to read.
  for (const row of bodyRows) {
    for (const cell of cellsOf(row)) {
      if (!NUMERIC.test(textOf(cell).trim())) continue;
      const text = firstText(cell);
      if (text?.value) text.value = text.value.replace(/^(\s*)-/, "$1−");
    }
  }

  // The first column stays put while the rest scrolls (td/th `first:` classes). When it is only a
  // rank, the name column after it is pinned too, so a scrolled row still says what it is.
  const rankFirst =
    width > 2 &&
    headRow !== undefined &&
    RANK_HEADER.test(textOf(cellsOf(headRow)[0] ?? { type: "text" }).trim()) &&
    column(0).length > 0 &&
    column(0).every((v) => RANK_VALUE.test(v));
  // The pinned label column is capped (see PIN_CAP) and keeps its full text in `title`. When
  // rank and name together would take too much of the card, TableCard pins the name alone (at
  // the left edge) and lets the rank scroll away with the other columns.
  const capped = (cell: HastNode) => ({ dataPinCap: "", title: textOf(cell).trim() || undefined });
  for (const row of rows) {
    const cells = cellsOf(row);
    const [first, second] = cells;
    if (!first) continue;
    if (rankFirst && second) {
      setProps(first, {}, ["w-11", "min-w-11", "max-w-11", "group-data-[pin-name-only]/table:static"]);
      setProps(second, { ...capped(second), dataPinSecond: "" }, [
        "sticky",
        "left-11",
        "z-[1]",
        "group-data-[pin-name-only]/table:left-0",
        second.tagName === "th" ? "bg-canvas" : "bg-surface",
      ]);
    } else {
      setProps(first, capped(first));
    }
  }
}

/** Shapes every table in the answer (see shapeTable). */
function rehypeShapeTables() {
  return (tree: HastNode) => {
    const walk = (node: HastNode) => {
      if (node.type === "element" && node.tagName === "table") shapeTable(node);
      node.children?.forEach(walk);
    };
    walk(tree);
  };
}

const REHYPE_PLUGINS = [rehypeShapeTables] as unknown as NonNullable<Options["rehypePlugins"]>;
const REMARK_PLUGINS: NonNullable<Options["remarkPlugins"]> = [remarkGfm];

// --- table card --------------------------------------------------------------------------------

const EDGE =
  "pointer-events-none absolute inset-y-px w-3 from-ink/[0.08] to-transparent transition-opacity duration-150 ease-out-quart";

/** Pinned columns may take at most this share of the card; past it only the name stays pinned. */
export const MAX_PIN_SHARE = 0.45;

/**
 * The pinned label column is at most 8rem wide (6.5rem of text plus 0.75rem padding each side):
 * long names wrap to two lines, then end in an ellipsis. The cell's `title` keeps the full name,
 * and screen readers still read all of it (line-clamp only hides it visually).
 */
const PIN_CAP = "line-clamp-2 max-w-[6.5rem] whitespace-normal break-words";

/**
 * A table in its own bordered card that scrolls sideways inside the bubble, so the page never
 * does. Soft 12px shadows mark the edges where columns are hidden: at the right while more is to
 * come, and just past the pinned column(s) once scrolled. (They are overlays, not the classic
 * local/scroll background trick, because the opaque pinned column and header row would paint
 * over a background shadow.) When a rank column and the name after it would together cover more
 * than MAX_PIN_SHARE of the card, the name alone is pinned (it is what identifies the row) and
 * the rank scrolls with the rest.
 */
function TableCard({ children }: { children?: ReactNode }) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ start: false, end: false, pin: 0, nameOnly: false });

  const measure = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    const first = el.querySelector<HTMLElement>("tr > :first-child");
    const name = el.querySelector<HTMLElement>("[data-pin-second]");
    // Widths don't depend on stickiness, so this can't flip back and forth.
    const nameOnly =
      first !== null && name !== null && first.offsetWidth + name.offsetWidth > MAX_PIN_SHARE * el.clientWidth;
    // The last pinned column: the name when there is one, else the first column.
    const edge = name ?? first;
    const pin = edge ? Math.round(edge.getBoundingClientRect().right - el.getBoundingClientRect().left) : 1;
    const next = { start: el.scrollLeft > 1, end: el.scrollLeft < max - 1, pin, nameOnly };
    setEdges((prev) =>
      prev.start === next.start && prev.end === next.end && prev.pin === next.pin && prev.nameOnly === next.nameOnly
        ? prev
        : next,
    );
  }, []);

  useLayoutEffect(() => {
    measure();
    const el = scrollerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure]);

  return (
    <div className="relative">
      <div
        ref={scrollerRef}
        onScroll={measure}
        role="region"
        aria-label="Table, scrolls sideways"
        tabIndex={0}
        data-pin-name-only={edges.nameOnly ? "" : undefined}
        className="group/table max-w-full overflow-x-auto overscroll-x-contain rounded-tile border border-line bg-surface"
      >
        <table className="w-full border-separate border-spacing-0 text-label tabular-nums">{children}</table>
      </div>
      <span
        aria-hidden="true"
        data-edge="start"
        style={{ left: edges.pin }}
        className={cn(EDGE, "bg-gradient-to-r", edges.start ? "opacity-100" : "opacity-0")}
      />
      <span
        aria-hidden="true"
        data-edge="end"
        className={cn(EDGE, "right-px rounded-r-tile bg-gradient-to-l", edges.end ? "opacity-100" : "opacity-0")}
      />
    </div>
  );
}

// --- elements --------------------------------------------------------------------------------
// Answers live inside a chat bubble: headings become bold lines (no h1 inside a bubble), images
// show their alt text only (no remote loads), and tables scroll sideways inside their own card.

/** Headings get 16px above them (more than the 10px between paragraphs) to start a section. */
const HEADING_GAP = "[&:not(:first-child)]:mt-4";

function MajorHeading({ children }: { children?: ReactNode }) {
  return <p className={cn("text-headline text-ink", HEADING_GAP)}>{children}</p>;
}

function MinorHeading({ children }: { children?: ReactNode }) {
  return <p className={cn("font-semibold text-ink", HEADING_GAP)}>{children}</p>;
}

const COMPONENTS: Components = {
  p: ({ children }) => <p>{children}</p>,
  h1: MajorHeading,
  h2: MajorHeading,
  h3: MinorHeading,
  h4: MinorHeading,
  h5: MinorHeading,
  h6: MinorHeading,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  a: ({ href, children }) => {
    const safe = safeHref(href);
    if (!safe) return <span>{children}</span>;
    return (
      <a
        href={safe}
        target="_blank"
        rel="noopener noreferrer"
        className="font-medium text-accent underline decoration-accent/40 underline-offset-2"
      >
        {children}
      </a>
    );
  },
  img: ({ alt }) => (alt ? <span>{alt}</span> : null),
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5 marker:text-ink-muted">{children}</ul>,
  ol: ({ children, start }) => (
    <ol start={start} className="list-decimal space-y-1 pl-5 marker:text-ink-muted">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="rounded-tile bg-canvas px-3 py-2 text-ink-muted">{children}</blockquote>
  ),
  hr: () => <hr className="border-line-soft" />,
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded-tile bg-canvas p-3 text-label [&>code]:bg-transparent [&>code]:p-0">
      {children}
    </pre>
  ),
  code: ({ children }) => <code className="rounded bg-canvas px-1 py-0.5 font-mono text-label">{children}</code>,
  table: ({ children }) => <TableCard>{children}</TableCard>,
  th: ({ children, style, className, title, ...rest }) => {
    const pin = pinProps(rest);
    return (
      <th
        scope="col"
        style={style}
        title={title}
        {...pin.attrs}
        className={cn(
          "whitespace-nowrap bg-canvas px-3 py-2 text-left font-medium text-ink-muted",
          "first:sticky first:left-0 first:z-[1] first:bg-canvas",
          className,
        )}
      >
        {pin.capped ? <span className={PIN_CAP}>{children}</span> : children}
      </th>
    );
  },
  td: ({ children, style, className, title, ...rest }) => {
    const pin = pinProps(rest);
    return (
      <td
        style={style}
        title={title}
        {...pin.attrs}
        className={cn(
          "whitespace-nowrap border-t border-line-soft px-3 py-2 text-ink",
          "first:sticky first:left-0 first:z-[1] first:bg-surface",
          className,
        )}
      >
        {pin.capped ? <span className={PIN_CAP}>{children}</span> : children}
      </td>
    );
  },
};

/** The pinning markers rehypeShapeTables put on a cell, as DOM attributes. */
function pinProps(rest: object) {
  const props = rest as Record<string, unknown>;
  const capped = props["data-pin-cap"] !== undefined;
  const second = props["data-pin-second"] !== undefined;
  return {
    capped,
    attrs: { "data-pin-cap": capped ? "" : undefined, "data-pin-second": second ? "" : undefined },
  };
}

/**
 * The analyst's markdown answer. GitHub-flavoured (tables, lists, strikethrough); raw HTML in
 * the answer is dropped, never rendered.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="min-w-0 break-words [&>*+*]:mt-2.5">
      <ReactMarkdown
        skipHtml
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={COMPONENTS}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
