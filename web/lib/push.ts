import { ApiError, apiFetch } from "@/lib/api";

export type PushState = "on" | "off" | "denied" | "unsupported" | "needs_install";

const isIOS = () => typeof navigator !== "undefined" && /iPhone|iPad|iPod/.test(navigator.userAgent);

export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  const mm = typeof window.matchMedia === "function" ? window.matchMedia("(display-mode: standalone)").matches : false;
  return mm || (navigator as unknown as { standalone?: boolean }).standalone === true;
}

export function pushSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window &&
    Boolean((window as unknown as { PushManager?: unknown }).PushManager) &&
    "Notification" in window
  );
}

/** iOS only delivers web push to an installed Home Screen app. */
const needsInstall = () => isIOS() && !isStandalone();

export function urlB64ToUint8Array(s: string): Uint8Array {
  const padded = (s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function currentSubscription(): Promise<PushSubscription | null> {
  // `ready` never settles when no worker is registered (dev builds): don't hang the card.
  const reg = await Promise.race([
    navigator.serviceWorker.ready,
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("no service worker")), 4000)),
  ]);
  return reg.pushManager.getSubscription();
}

export async function pushState(): Promise<PushState> {
  if (needsInstall()) return "needs_install";
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  try {
    return (await currentSubscription()) ? "on" : "off";
  } catch {
    return "unsupported";
  }
}

/** Call only from a click handler: it is what asks for the permission. */
export async function enablePush(): Promise<"on" | "denied" | "unsupported" | "needs_install"> {
  if (needsInstall()) return "needs_install";
  if (!pushSupported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";
  try {
    const { key } = await apiFetch<{ key: string }>("/push/vapid-key");
    const reg = await navigator.serviceWorker.ready;
    const sub =
      (await reg.pushManager.getSubscription()) ??
      (await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(key) as BufferSource,
      }));
    const json = sub.toJSON();
    await apiFetch("/push/subscribe", {
      method: "POST",
      body: JSON.stringify({ endpoint: json.endpoint, keys: { p256dh: json.keys?.p256dh, auth: json.keys?.auth } }),
    });
    return "on";
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return "unsupported";
    throw e;
  }
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  await apiFetch("/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint }) });
}
