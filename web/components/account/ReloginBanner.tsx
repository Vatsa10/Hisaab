"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { appName } from "@/lib/admin";
import { watchConnections, type Connection } from "@/lib/jobs";

/** "Zepto logged you out." / "Zepto and Blinkit logged you out." / "A, B and C logged you out." */
function message(names: string[]): string {
  const list = names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `${list} logged you out.`;
}

/**
 * Notice under the safe-area top on every tab while any connection needs a new login.
 * Live through Realtime; it cannot be dismissed and disappears only once the app is reconnected.
 */
export function ReloginBanner() {
  const [connections, setConnections] = useState<Connection[]>([]);
  useEffect(() => watchConnections(setConnections), []);

  const out = connections.filter((c) => c.status === "needs_login");
  if (out.length === 0) return null;

  return (
    <div
      role="status"
      className="mb-4 flex items-center gap-3 rounded-control bg-up-soft px-3 py-1.5 ring-1 ring-inset ring-up/30"
    >
      <p className="min-w-0 flex-1 text-label font-semibold text-ink">{message(out.map((c) => appName(c.site)))}</p>
      <Link
        href={`/accounts?reconnect=${encodeURIComponent(out[0]!.site)}`}
        className="inline-flex min-h-11 shrink-0 items-center rounded-control px-2 text-label font-semibold text-up underline underline-offset-2"
      >
        Reconnect
      </Link>
    </div>
  );
}
