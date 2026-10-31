import { cn } from "@/lib/cn";
import { siteName, type CartCell, type CartSite, type CartTable } from "@/lib/cart";

export interface ItemTableProps {
  table: CartTable;
  /** The cheapest app (summary.best_site); its total is the one highlighted. */
  cheapestSite: CartSite | null;
  onPick: (idx: number, site: CartSite) => void;
}

const MUTED = new Set(["n/a", "Out of stock", "Not available"]);

/** Instamart is too wide for a 4-column table on small phones: "Insta" under 360px. */
function ColumnName({ site }: { site: CartSite }) {
  if (site !== "instamart") return <>{siteName(site)}</>;
  return (
    <>
      <span aria-hidden="true" className="min-[360px]:hidden">
        Insta
      </span>
      <span className="sr-only min-[360px]:not-sr-only">Instamart</span>
    </>
  );
}

function Cell({ cell, highlight, onPick }: { cell: CartCell | undefined; highlight: boolean; onPick: () => void }) {
  if (!cell) return <td className="px-1 py-2 text-right text-ink-muted">n/a</td>;
  if (cell.status === "pick" || cell.price_label === "Pick") {
    return (
      <td className="py-0.5 pl-1 text-right">
        <button
          type="button"
          onClick={onPick}
          className="-mr-1 min-h-11 min-w-11 rounded-tile px-1 font-semibold text-accent hover:bg-accent/5 active:bg-accent/10"
        >
          Pick
        </button>
      </td>
    );
  }
  return (
    <td
      className={cn(
        "px-1 py-2 text-right tabular-nums",
        highlight && cell.cheapest && "font-bold text-accent",
        MUTED.has(cell.price_label) && "text-ink-muted",
      )}
    >
      {cell.price_label}
    </td>
  );
}

/** Every item's price on every app (mockup B); the cheapest price per row is green and bold. */
export function ItemTable({ table, cheapestSite, onPick }: ItemTableProps) {
  const cols = table.columns;
  // With one priced app there is nothing to compare: highlighting would mark every cell.
  const compare = cols.length > 1;
  return (
    <div className="rounded-card bg-surface px-3 py-2 shadow-card">
      <table className="w-full table-fixed border-collapse text-label">
        <colgroup>
          <col />
          {cols.map((c) => (
            <col key={c} className="w-[4.25rem] min-[400px]:w-[5rem]" />
          ))}
        </colgroup>
        <thead>
          <tr className="text-caption text-ink-muted">
            <th scope="col" className="py-1.5 pr-1 text-left font-semibold">
              Item
            </th>
            {cols.map((c) => (
              <th key={c} scope="col" className="px-1 py-1.5 text-right font-semibold">
                <ColumnName site={c} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.idx} className="border-t border-line-soft">
              <th scope="row" className="break-words py-2 pr-1 text-left font-normal">
                {row.label}
              </th>
              {cols.map((c) => (
                <Cell key={c} cell={row.cells[c]} highlight={compare} onPick={() => onPick(row.idx, c)} />
              ))}
            </tr>
          ))}
          <tr className="border-t-2 border-line font-bold">
            <th scope="row" className="py-2 pr-1 text-left">
              Total + fees
            </th>
            {cols.map((c) => {
              const total = table.totals[c];
              return (
                <td
                  key={c}
                  className={cn(
                    "px-1 py-2 text-right tabular-nums",
                    !total ? "font-normal text-ink-muted" : compare && c === cheapestSite && "text-accent",
                  )}
                >
                  {total ?? "n/a"}
                </td>
              );
            })}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
