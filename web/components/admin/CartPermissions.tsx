import {
  appName,
  cartPermissions,
  hasRiskyEntry,
  opLabel,
  parseRegexUrl,
  type Approval,
  type PostEntry,
} from "@/lib/admin";

export const NEVER_LINE = (app: string) =>
  `Approving lets the worker add items to users' carts on ${app}. It can never check out, pay or change addresses.`;

/** One permitted request: its op label, host and path, the operation, why, and the raw regex. */
export function PermissionEntry({ entry: e }: { entry: PostEntry }) {
  const url = parseRegexUrl(e.url_regex);
  return (
    <li className="rounded-tile bg-surface px-2.5 py-1.5">
      <span className="block text-label font-semibold text-ink">{opLabel(e.op)}</span>
      {url ? (
        <span className="mt-0.5 block text-label text-ink [overflow-wrap:anywhere]">
          Host: {url.host} · Path: {url.path}
        </span>
      ) : null}
      {e.operation ? <span className="block text-label text-ink [overflow-wrap:anywhere]">Operation: {e.operation}</span> : null}
      {e.why ? <span className="mt-0.5 block text-label text-ink-muted">{e.why}</span> : null}
      <code className="mt-1 block font-mono text-[11px] leading-snug text-ink-muted [overflow-wrap:anywhere]">{e.url_regex}</code>
    </li>
  );
}

export function RiskWarning() {
  return (
    <p className="mt-2 rounded-tile bg-danger-soft px-2.5 py-1.5 text-label font-semibold text-danger">
      Warning: a request mentions checkout, payment, address or order. Reject unless you are sure it only changes the cart.
    </p>
  );
}

function Group({ entries, label }: { entries: PostEntry[]; label: string }) {
  if (entries.length === 0) return null;
  return (
    <ul aria-label={label} className="flex flex-col gap-1.5">
      {entries.map((e, i) => (
        <PermissionEntry key={`${e.url_regex}-${i}`} entry={e} />
      ))}
    </ul>
  );
}

/**
 * What approving a cart recipe lets the worker do: the cart, location and read requests it may
 * send, how quantity is set, and the line that says what it can never do.
 */
export function CartPermissions({ approval: a }: { approval: Pick<Approval, "proposed" | "site" | "current"> }) {
  const perms = cartPermissions(a);
  const all = [...perms.cart, ...perms.location, ...perms.read];
  return (
    <section aria-label="Cart permissions" className="rounded-control bg-up-soft p-3">
      <h4 className="text-label font-semibold text-ink">Cart permissions</h4>
      <p className="mt-1 text-label font-medium text-ink">{NEVER_LINE(appName(a.site))}</p>
      {hasRiskyEntry(all) ? <RiskWarning /> : null}
      <div className="mt-2 flex flex-col gap-1.5">
        <Group entries={perms.cart} label="Cart requests" />
        <Group entries={perms.location} label="Location requests" />
        <Group entries={perms.read} label="Read requests" />
      </div>
      {perms.qty ? <p className="mt-2 text-label font-medium text-ink">{perms.qty}</p> : null}
    </section>
  );
}
