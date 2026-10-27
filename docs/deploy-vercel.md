# Deploying the PWA to Vercel

The PWA in `web/` is a standalone Next.js 15 project. It needs no `vercel.json`. `next.config.mjs` already sets the
security headers and the service worker's no-cache headers, and Vercel detects everything else. Do not deploy
until every plan is built and verified locally (`bash scripts/e2e_local.sh` is green; see `docs/e2e-local.md`).

## Project settings

| Setting | Value |
| --- | --- |
| Root Directory | `web` |
| Framework Preset | Next.js (detected) |
| Node.js Version | 22.x |
| Install Command | default (detects pnpm 10 from `packageManager` and `pnpm-lock.yaml`) |
| Build Command / Output | defaults (`next build`, `.next`) |
| Function Region | `bom1` (Mumbai), next to a Supabase project in `ap-south-1` and the Azure backend in Central India |
| Skip deployments when there are no changes to the root directory | on |

The `e2e/` folder is outside the root directory, so Playwright never gets installed on Vercel.

## Environment variables

Set these for Production, and for Preview if you use previews. They are the only configuration the PWA reads:

| Name | Value |
| --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the project's anon (public) key |
| `NEXT_PUBLIC_API_URL` | the API's public HTTPS URL, e.g. `https://<api-app>.<region>.azurecontainerapps.io` |

- Next inlines `NEXT_PUBLIC_*` into the bundle at build time, so redeploy after changing any of them.
- Never add the service-role key, a database URL or any other secret to the Vercel project. Every `NEXT_PUBLIC_*`
  value is public by design.

## Settings outside Vercel

1. **Supabase Auth → URL Configuration**
   - Site URL: the production origin, e.g. `https://<your-domain>`.
   - Redirect URLs: `https://<your-domain>/auth/callback`. For previews also add
     `https://*-<your-team>.vercel.app/auth/callback`.

   Sign-up confirmation links and Google sign-in come back to `/auth/callback`. Supabase refuses any other host.
2. **API CORS**: set `CORS_ORIGINS` on the API to the PWA's origin(s), comma-separated, for example
   `https://<your-domain>,https://<project>.vercel.app`. Without it, every API call from the PWA fails in the
   browser.
3. **Google sign-in** (optional): in the Google Cloud OAuth client, the authorised redirect URI is Supabase's
   (`https://<project-ref>.supabase.co/auth/v1/callback`), not Vercel's.

## Checking a deployment

- `https://<your-domain>/login` loads. Signing up or in lands on `/spend`.
- `https://<your-domain>/manifest.webmanifest` and `/sw.js` load. `/sw.js` is served with
  `Cache-Control: no-cache, no-store, must-revalidate`.
- In Safari on the iPhone, Share → Add to Home Screen opens the app full-screen.
- Accounts → Connect reaches the OTP step. That proves the API URL, CORS and Realtime from the deployed origin.
