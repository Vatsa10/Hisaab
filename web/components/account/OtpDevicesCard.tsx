"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { apiFetch } from "@/lib/api";

export interface OtpDevice {
  id: string;
  name: string | null;
  created_at: string | null;
  last_seen_at: string | null;
}

interface Pairing {
  code: string;
  expiresAt: number;
}

export const OTP_COPY =
  "Android only. We read OTP texts from Zomato, Swiggy, Zepto and Blinkit and only while you are connecting an app. iPhone can't do this; tap the suggested code on the keyboard instead.";

function mmss(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

/** Accounts: pair the Hisaab OTP Android app so login codes fill themselves in while connecting. */
export function OtpDevicesCard() {
  const [devices, setDevices] = useState<OtpDevice[]>([]);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [left, setLeft] = useState(0);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch<{ devices: OtpDevice[] }>("/otp-devices");
      const next = res?.devices ?? [];
      setDevices((prev) => {
        if (next.some((d) => !prev.some((p) => p.id === d.id))) setPairing(null);
        return next;
      });
    } catch {
      // the list is a convenience; pairing still works
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!pairing) return;
    const tick = () => {
      const s = Math.max(0, Math.round((pairing.expiresAt - Date.now()) / 1000));
      setLeft(s);
      if (s === 0) setPairing(null);
    };
    tick();
    const t = setInterval(tick, 1000);
    const poll = setInterval(() => void load(), 5000);
    return () => {
      clearInterval(t);
      clearInterval(poll);
    };
  }, [pairing, load]);

  async function startPairing() {
    setBusy(true);
    setProblem(null);
    try {
      const res = await apiFetch<{ code: string; expires_in: number }>("/otp-devices/pairing", { method: "POST" });
      setPairing({ code: res.code, expiresAt: Date.now() + res.expires_in * 1000 });
    } catch {
      setProblem("Couldn't make a pairing code. Try again in a bit.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    setProblem(null);
    try {
      await apiFetch(`/otp-devices/${id}`, { method: "DELETE" });
      setDevices((d) => d.filter((x) => x.id !== id));
    } catch {
      setProblem("Couldn't remove that phone. Try again in a bit.");
    }
  }

  return (
    <Card as="section" aria-label="Auto-fill OTP on Android" className="mt-6">
      <h2 className="font-medium">Auto-fill OTP on Android</h2>
      <p className="mt-0.5 text-label text-ink-muted">{OTP_COPY}</p>

      {pairing ? (
        <div className="mt-3 rounded-field bg-canvas px-3.5 py-3">
          <p className="text-label text-ink-muted">Type this code in the Hisaab OTP app on your phone</p>
          <p className="mt-1 font-mono text-2xl font-semibold tracking-[0.2em]" aria-label="Pairing code">
            {pairing.code}
          </p>
          <p className="mt-1 text-label text-ink-muted" aria-live="polite">
            Expires in {mmss(left)}
          </p>
        </div>
      ) : (
        <div className="mt-3">
          <Button variant="secondary" fullWidth loading={busy} onClick={startPairing}>
            Pair an Android phone
          </Button>
        </div>
      )}

      {devices.length > 0 ? (
        <ul className="mt-3 divide-y divide-line" aria-label="Paired phones">
          {devices.map((d) => (
            <li key={d.id} className="flex min-h-11 items-center justify-between gap-3 py-1.5">
              <span className="truncate">{d.name || "Android phone"}</span>
              <Button variant="ghost" onClick={() => revoke(d.id)} aria-label={`Revoke ${d.name || "Android phone"}`}>
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      ) : null}

      {problem ? (
        <p role="alert" className="mt-3 rounded-field bg-danger-soft px-3.5 py-2.5 text-label text-danger">
          {problem}
        </p>
      ) : null}
    </Card>
  );
}
