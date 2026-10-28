"use client";

import { useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import type { AppInfo } from "@/lib/apps";
import { DeleteAccountSheet } from "./DeleteAccountSheet";
import { DisconnectSheet } from "./DisconnectSheet";

export interface DataCardProps {
  /** The apps that are connected right now. */
  connected: readonly AppInfo[];
}

const ROW =
  "flex min-h-14 w-full items-center justify-between gap-3 px-4 text-left transition-colors duration-150 active:bg-canvas";

/** Accounts: "Your data". Disconnect an app (deletes its session and orders) or delete the account. */
export function DataCard({ connected }: DataCardProps) {
  const [disconnecting, setDisconnecting] = useState<AppInfo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  return (
    <>
      <Card as="section" aria-label="Your data" padding="none" className="mt-6">
        <h2 className="px-4 pb-2 pt-3.5 font-medium">Your data</h2>
        <ul>
          {connected.map((app) => (
            <li key={app.id} className="border-t border-line-soft">
              <button type="button" onClick={() => setDisconnecting(app)} className={ROW}>
                <span className="font-medium">Disconnect {app.name}</span>
                <span aria-hidden="true" className="text-label font-semibold text-ink-muted">
                  ›
                </span>
              </button>
            </li>
          ))}
          <li className="border-t border-line-soft">
            <button type="button" onClick={() => setDeleting(true)} className={ROW}>
              <span className="font-medium text-danger">Delete account</span>
              <span aria-hidden="true" className="text-label font-semibold text-danger">
                ›
              </span>
            </button>
          </li>
        </ul>
      </Card>

      {disconnecting ? (
        <DisconnectSheet
          app={disconnecting}
          onClose={() => setDisconnecting(null)}
          onDone={(app, n) => setToast(`${app.name} disconnected. ${n} ${n === 1 ? "order" : "orders"} deleted.`)}
        />
      ) : null}
      {deleting ? <DeleteAccountSheet onClose={() => setDeleting(false)} /> : null}

      <div
        role="status"
        className="pointer-events-none fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+5.5rem)] z-[60] flex justify-center px-4"
      >
        {toast ? (
          <p className="pointer-events-auto max-w-[420px] animate-fade-in rounded-control bg-ink px-4 py-3 text-label font-medium text-white shadow-sheet">
            {toast}
          </p>
        ) : null}
      </div>
    </>
  );
}
