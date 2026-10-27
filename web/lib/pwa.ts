/**
 * Drop the service worker's cached pages ("pages-*"). Called on sign-in and sign-out so one
 * account's cached screens never show to the next person on the same device. Never throws:
 * a missing or blocked Cache API must not stop sign-in or sign-out.
 */
export async function clearPageCache(): Promise<void> {
  try {
    if (typeof caches === "undefined" || !caches) return;
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith("pages-")).map((k) => caches.delete(k)));
  } catch {
    // ignore
  }
}
