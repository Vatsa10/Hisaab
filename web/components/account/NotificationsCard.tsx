"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { disablePush, enablePush, pushState, type PushState } from "@/lib/push";

const NOTE: Partial<Record<PushState, string>> = {
  denied: "Notifications are off in Settings. We'll email you instead.",
  needs_install:
    "Add Hisaab to your Home Screen (Share → Add to Home Screen) to get notifications. We'll email you meanwhile.",
};

/** Accounts: turn push notifications on or off for this device. Permission is asked only on the tap. */
export function NotificationsCard() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    pushState()
      .then((s) => live && setState(s))
      .catch(() => live && setState("unsupported"));
    return () => {
      live = false;
    };
  }, []);

  async function toggle(turnOn: boolean) {
    setBusy(true);
    setProblem(null);
    try {
      if (turnOn) setState(await enablePush());
      else {
        await disablePush();
        setState("off");
      }
    } catch {
      setProblem("Couldn't change notifications. Try again in a bit.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card as="section" aria-label="Notifications" className="mt-6">
      <h2 className="font-medium">Notifications</h2>
      <p className="mt-0.5 text-label text-ink-muted">Get notified when a sync finishes or an app logs you out.</p>

      {state === "off" ? (
        <div className="mt-3">
          <Button variant="secondary" fullWidth loading={busy} onClick={() => toggle(true)}>
            Turn on notifications
          </Button>
        </div>
      ) : null}
      {state === "on" ? (
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-label font-semibold text-accent">Notifications on</p>
          <Button variant="ghost" loading={busy} onClick={() => toggle(false)}>
            Turn off
          </Button>
        </div>
      ) : null}
      {state && NOTE[state] ? (
        <p className="mt-3 rounded-field bg-canvas px-3.5 py-2.5 text-label text-ink-muted">{NOTE[state]}</p>
      ) : null}
      {problem ? (
        <p role="alert" className="mt-3 rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
          {problem}
        </p>
      ) : null}
    </Card>
  );
}
