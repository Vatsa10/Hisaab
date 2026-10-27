import type { ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";

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

// --- number columns ------------------------------------------------------------------------
// Minimal hast shape: the plugin only walks elements and reads text.
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

const NUMERIC = /^[+\-−]?\s*₹?\s*[+\-−]?\d[\d,]*(\.\d+)?\s*(%|x|×)?$/i;

function textOf(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

function childElements(node: HastNode, tag: string): HastNode[] {
  return (node.children ?? []).filter((c) => c.type === "element" && c.tagName === tag);
}

function alignNumberColumns(table: HastNode): void {
  const sections = (table.children ?? []).filter((c) => c.type === "element");
  const rows = sections.flatMap((s) => (s.tagName === "tr" ? [s] : childElements(s, "tr")));
  const cellsOf = (row: HastNode) =>
    (row.children ?? []).filter((c) => c.type === "element" && (c.tagName === "td" || c.tagName === "th"));
  const bodyRows = rows.filter((r) => cellsOf(r).some((c) => c.tagName === "td"));
  const width = Math.max(0, ...rows.map((r) => cellsOf(r).length));

  for (let col = 0; col < width; col++) {
    const values = bodyRows.map((r) => textOf(cellsOf(r)[col] ?? { type: "text" }).trim()).filter(Boolean);
    if (!values.length || !values.every((v) => NUMERIC.test(v))) continue;
    for (const row of rows) {
      const cell = cellsOf(row)[col];
      // An explicit alignment from the markdown (|---:|) wins.
      if (cell && !cell.properties?.align) cell.properties = { ...cell.properties, align: "right" };
    }
  }
}

/** Right-aligns table columns whose body cells are all amounts or counts. */
function rehypeAlignNumbers() {
  return (tree: HastNode) => {
    const walk = (node: HastNode) => {
      if (node.type === "element" && node.tagName === "table") alignNumberColumns(node);
      node.children?.forEach(walk);
    };
    walk(tree);
  };
}

const REHYPE_PLUGINS = [rehypeAlignNumbers] as unknown as NonNullable<Options["rehypePlugins"]>;
const REMARK_PLUGINS: NonNullable<Options["remarkPlugins"]> = [remarkGfm];

// --- elements --------------------------------------------------------------------------------
// Answers live inside a chat bubble: headings become bold lines (no h1 inside a bubble), images
// show their alt text only (no remote loads), and tables scroll sideways inside their own card so
// the page itself never does.

function Strong({ children }: { children?: ReactNode }) {
  return <p className="font-semibold text-ink">{children}</p>;
}

const COMPONENTS: Components = {
  p: ({ children }) => <p>{children}</p>,
  h1: Strong,
  h2: Strong,
  h3: Strong,
  h4: Strong,
  h5: Strong,
  h6: Strong,
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
  table: ({ children }) => (
    <div
      role="region"
      aria-label="Table, scrolls sideways"
      tabIndex={0}
      className="max-w-full overflow-x-auto overscroll-x-contain rounded-tile border border-line bg-surface"
    >
      <table className="w-full border-collapse text-label tabular-nums">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      scope="col"
      style={style}
      className="whitespace-nowrap bg-canvas px-3 py-2 text-left font-medium text-ink-muted"
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td style={style} className="whitespace-nowrap border-t border-line-soft px-3 py-2 text-ink">
      {children}
    </td>
  ),
};

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
