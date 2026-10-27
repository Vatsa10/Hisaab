/*
 * Expense Agent service worker (registered in production only, see ServiceWorkerRegistrar).
 *
 * What it handles, and nothing else:
 *   - same-origin page navigations: network first; the last good copy is the offline fallback
 *   - same-origin immutable assets (/_next/static/, /icons/): cache first
 * Everything else goes straight to the network untouched. In particular the FastAPI service
 * (NEXT_PUBLIC_API_URL) and Supabase (REST, auth, realtime) are other origins and are never
 * intercepted or cached, and non-GET requests and the /auth/ callback are never touched.
 */

const VERSION = "v1";
const PAGES = `pages-${VERSION}`;
const STATIC = `static-${VERSION}`;

const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Offline</title>
<style>body{margin:0;min-height:100dvh;display:grid;place-items:center;background:#f6f7f9;color:#14161a;
font:15px/1.45 -apple-system,"SF Pro Text","Segoe UI",sans-serif;padding:24px;box-sizing:border-box}
main{max-width:320px}h1{font-size:20px;margin:0 0 6px}p{margin:0;color:#5f6670}</style></head>
<body><main><h1>You're offline</h1><p>Your spending will be here when you're back online.</p></main></body></html>`;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== PAGES && k !== STATIC).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function cacheable(res) {
  return res && res.ok && !res.redirected && res.type !== "opaqueredirect";
}

async function networkFirst(event) {
  const req = event.request;
  try {
    const res = await fetch(req);
    if (cacheable(res)) {
      const copy = res.clone();
      event.waitUntil(caches.open(PAGES).then((cache) => cache.put(req, copy)));
    }
    return res;
  } catch {
    const cache = await caches.open(PAGES);
    const cached = await cache.match(req);
    if (cached) return cached;
    return new Response(OFFLINE_HTML, {
      status: 503,
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(STATIC);
  const cached = await cache.match(req);
  if (cached) return cached;
  const res = await fetch(req);
  if (cacheable(res)) await cache.put(req, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // API and Supabase live on other origins: never intercept or cache them.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/auth/")) return;

  if (req.mode === "navigate") {
    event.respondWith(networkFirst(event));
    return;
  }
  if (url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(cacheFirst(req));
  }
});
