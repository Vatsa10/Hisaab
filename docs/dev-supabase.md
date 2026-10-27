# Local Supabase for development

The PWA (Plan 3) signs users in with Supabase Auth and reads their data through row-level security. Run the
whole stack locally with the Supabase CLI (pinned to 2.122.0, via `npx`). Docker Desktop must be running.

## Start

```bash
bash scripts/dev_supabase.sh
```

This runs `npx supabase start` (safe to repeat), then writes two gitignored files:

| File | Contents |
| --- | --- |
| `web/.env.local` | `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `NEXT_PUBLIC_API_URL=http://localhost:8000` |
| `.env.server.local` | `DATABASE_URL` (local Supabase Postgres), `SUPABASE_JWKS_URL`, and `SUPABASE_JWT_SECRET` commented out |

The PWA only ever uses the three `NEXT_PUBLIC_*` values. It never gets the service-role key.

Local ports: API 54321, Postgres 54322, Studio 54323, Mailpit 54324. Edge runtime and analytics are disabled in
`supabase/config.toml` to keep startup light.

## Tokens: JWKS, not the shared secret

Local Supabase signs access tokens with ES256, so the API verifies them through `SUPABASE_JWKS_URL`. In
`server/auth.py` a set `SUPABASE_JWT_SECRET` wins and only allows HS256, which would reject these tokens,
so the script leaves the secret commented out. Load the env into the API with, for example:

```bash
set -a; . ./.env.server.local; set +a
uvicorn server.api:app --port 8000
```

## Reset the database

```bash
npx --yes supabase@2.122.0 db reset     # re-applies supabase/migrations/0001..0005
npx --yes supabase@2.122.0 status
npx --yes supabase@2.122.0 stop         # when you are done
```

## Auth settings

- Email sign-up is on, `enable_confirmations = false` locally (no email round trip).
- `site_url = http://localhost:3000`, redirect `http://localhost:3000/auth/callback`.
- Google sign-in is off by default. To try it, set `enabled = true` under `[auth.external.google]` in
  `supabase/config.toml` and export `SUPABASE_AUTH_GOOGLE_CLIENT_ID` and `SUPABASE_AUTH_GOOGLE_SECRET` before
  starting. Do not commit the edit with real values.

## Views

Migration 0005 adds `norm_name()` and the `security_invoker` views `monthly_spend`, `category_spend` and
`merchant_spend`. They exclude cancelled, refunded and failed orders and return only the signed-in user's rows
(`authenticated` can select; `anon` cannot). It also adds `connections` to the `supabase_realtime`
publication (a no-op on plain Postgres).
