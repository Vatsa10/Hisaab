"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { AppInfo } from "@/lib/apps";
import { CONSENT_POINTS, CONSENT_VERSION } from "./copy";
import { ShieldIcon } from "./icons";

export interface ConsentBodyProps {
  app: AppInfo;
}

/**
 * The consent text (version 2026-10-27), shown once per app before its phone step. It is the
 * body of the connect sheet; `ConsentAction` is the sheet's pinned footer.
 */
export function ConsentBody({ app }: ConsentBodyProps) {
  return (
    <div className="flex flex-col gap-4">
      <span aria-hidden="true" className="grid size-11 place-items-center rounded-control bg-accent-soft text-accent">
        <ShieldIcon size={24} />
      </span>
      <ul className="flex flex-col gap-3">
        {CONSENT_POINTS(app.name).map((point, i) => (
          <li key={point} className="flex gap-3">
            <span
              aria-hidden="true"
              className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-canvas text-caption font-semibold tabular-nums text-ink-muted"
            >
              {i + 1}
            </span>
            <span className="text-body">{point}</span>
          </li>
        ))}
      </ul>
      <p className="text-label text-ink-muted">Consent version {CONSENT_VERSION}. You agree once per app.</p>
    </div>
  );
}

export interface ConsentActionProps {
  app: AppInfo;
  /** Called once the consent is stored. */
  onAgreed: () => void;
}

/** "I understand": stores the consent for this app (POST /consents) and moves on. */
export function ConsentAction({ app, onAgreed }: ConsentActionProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function agree() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/consents", {
        method: "POST",
        body: JSON.stringify({ site: app.id, text_version: CONSENT_VERSION }),
      });
      onAgreed();
    } catch {
      setBusy(false);
      setError("Couldn't save that. Check your connection and try again.");
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {error ? (
        <p role="alert" className="text-label text-danger">
          {error}
        </p>
      ) : null}
      <Button size="lg" fullWidth loading={busy} onClick={agree}>
        I understand
      </Button>
    </div>
  );
}
