# Pre-deploy checklist (owner)

**The owner deploys.** Nothing in this repository deploys itself and no agent runs these steps: you create the cloud
accounts, hold the secrets and run `deploy.sh`. Work top to bottom; each step has a **verify** line, and you do not
move on until it passes. Tick the box when it does.

Everything below was rehearsed on fake sites with `bash scripts/e2e_local.sh` (the `product`, `plan4`, `plan5`, `plan6`
and `plan7` specs). That proves the code, not your accounts: sections 7 to 9 are the part only you can do.

## 1. Supabase

- [ ] **Project in ap-south-1 (Mumbai).** Create it in the dashboard.
  Verify: Settings > General shows region `ap-south-1`.
- [ ] **Link and push the migrations.** `npx supabase link --project-ref <ref>`, then `npx supabase db push`.
  Verify: `0001` to `0010` are all listed as applied (`npx supabase migration list`).
  The API takes row locks on `auth.users` (`FOR KEY SHARE` for sign-up and `FOR UPDATE` for account deletion, both
  in one transaction). The local e2e runs them as the `postgres` role. On Supabase, check once that the role in your
  `DATABASE_URL` may do the same: run `select id from auth.users limit 1 for update;` inside `begin; ... rollback;`
  in the SQL editor with that role (use a psql session on the pooler URL with the same user). Verify: no
  `permission denied for table users`. If it is denied, stop: account deletion would fail for every user.
- [ ] **Auth hook on.** Authentication > Hooks: the custom access token hook points at the function from the
  migrations. Verify: sign in once and decode the JWT; it carries the hook's claims.
- [ ] **Google provider** enabled with your OAuth client; the redirect URL is the one Supabase shows.
  Verify: "Sign in with Google" on the deployed PWA returns to `/spend`.
- [ ] **`site_url` and redirects.** Authentication > URL configuration: `site_url` is the PWA's address, and the
  redirect allow-list holds that address with `/auth/callback`.
  Verify: an email confirmation link opens the PWA, not `localhost`.
- [ ] **Realtime publication** holds `jobs`, `connections`, `cart_requests` and `cart_results` (and `app_addresses`
  if the migration lists it). Verify: `select tablename from pg_publication_tables where pubname = 'supabase_realtime';`.

## 2. Database connection and keys

- [ ] **Session-pooler `DATABASE_URL`** (port 5432 on the pooler host, not the transaction pooler on 6543: the worker
  `LISTEN`s for OTPs) with `?sslmode=require`.
  Verify: `psql "$DATABASE_URL" -c "select 1"` works from your laptop.
- [ ] **`SUPABASE_JWKS_URL`** = `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`.
  Verify: `curl -s "$SUPABASE_JWKS_URL"` returns a JSON body with a `keys` array. The API verifies tokens against it
  and needs no JWT secret.
- [ ] **Decision D1 (profile key management) recorded.** The default is built: the master key lives in Key Vault
  behind a Container Apps reference, with per-user keys derived from it. Write down in `docs/deploy-azure.md`
  which option you chose (default, or per-user Key Vault envelope keys). The build does not wait on it.
  Verify: the decision and the date are in that file.
- [ ] **`PROFILE_MASTER_KEY` generated once** (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)
  and **backed up offline** (password manager plus a printed copy). **Losing it logs every user out of every app**:
  all stored sessions become unreadable and everyone must reconnect.
  Verify: you can read the backup back, and the key is only in Key Vault / the secret store, never in git.

## 3. Proxy and notifications

- [ ] **Proxy vendor** with sticky Indian residential sessions; set `PROXY_URL_TEMPLATE` (with the session-id
  placeholder). Verify: a connect from the worker shows an Indian IP at the vendor's dashboard.
- [ ] **VAPID keys** (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT=mailto:you@...`).
  Verify: enabling notifications in the PWA subscribes without an error.
- [ ] **SMTP sender and port.** Set `SMTP_HOST`, `SMTP_PORT`, user and password. Many clouds (Azure included) block
  outbound port 25: use 587 (STARTTLS) or 465 from a provider that allows it. Verify: from the deployed worker
  container (or a Container Apps job with the same settings) send one mail to yourself; it arrives. Without this,
  invites, logged-out and error-budget mails never leave.
- [ ] **`APP_URL`** = the PWA's public address (links in mails). Verify: the invite mail's link opens the PWA.
- [ ] **`CORS_ORIGINS`** = exactly the PWA's origin (scheme and host, no trailing slash; comma-separated for more).
  The default is `http://localhost:3000`, which blocks the deployed PWA. Verify:
  `curl -s -D- -o /dev/null -H "Origin: https://<pwa>" "$API/healthz"` shows `access-control-allow-origin` for it, and
  a request from another origin does not.

## 4. Money and prices

- [ ] **Budget unit costs from real prices:** `PROXY_INR_PER_GB`, `AI_INR_PER_1K_TOKENS`, `WORKER_INR_PER_MINUTE`,
  `FIXED_MONTHLY_INR`, `MONTHLY_BUDGET_INR`. The defaults are guesses. Verify: the admin Cost view shows the numbers
  you expect after the first sync.
- [ ] **Fee rules verified.** Plan 5's seeded delivery, handling and small-cart fees are placeholders. Compare each app
  against a real cart and edit the rules. Verify: the cheapest-cart total for one real list matches the app's own
  cart total within a rupee or two.

## 5. Admin and recipes

- [ ] **`python cli.py add-admin you@example.com`** against the production database. Verify: the Admin tab appears.
- [ ] **Recipes learned and approved on your own accounts**: orders, grocery search and the Zepto and Blinkit cart
  recipes, from the admin Sites tab, each approved in Approvals. **Zomato only through templating, never
  `--force`** (a forced publish can bake your account's ids into a recipe every user then runs). Verify: every Sites
  card says Healthy with a recipe version, and the cart recipe shows "It can never check out, pay or change
  addresses."

## 6. Web and deploy

- [ ] **Vercel `NEXT_PUBLIC_*`** set: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
  `NEXT_PUBLIC_API_URL` (the deployed API). Verify: the deployed login page loads and talks to the API.
- [ ] **`deploy.sh` from a clean tree** (`git status` empty; the script refuses a dirty one). Then
  `curl "$API/healthz"`. Verify: `{"ok":true}` with a 200, and the Container Apps revisions are healthy.

## 7. Privacy wording (owner confirmation)

- [ ] Read `/privacy` on the deployed PWA as a stranger would. It states: payment is never touched, nothing is ever
  ordered, what is stored, who sees it (only the user; Claude is sent only what it needs to answer), the risk that
  an app may flag automated use, and that deletion is immediate with logs kept only under an anonymous id for up to
  30 days. **Confirm each sentence is true for your deployment** (for example, the log retention you set in Log
  Analytics) and that you accept it. Change `web/app/privacy/page.tsx` if not. Verify: you have signed off in writing.

## 8. The real-OTP run (only you can do it)

- [ ] Follow `docs/e2e-checklist.md` end to end with your own phone and OTP, **including its "Plan 7 checks on a
  real account" section** (disconnect, delete account, `verify-erased`). Fake sites cannot prove the real apps' login
  and ordering pages. Use a throwaway account for the delete test.
  Verify: connect shows your real orders, a refresh needs no OTP, a disconnect removes the orders, a deletion ends
  with `NOTHING REMAINS`.

## 9. The first week

- [ ] Watch the admin **cost view** daily against your budget.
- [ ] Watch **Log Analytics ingestion** against the 0.2 GB/day cap (Azure Monitor > Log Analytics workspace > Usage
  and estimated costs). Going over costs money; lower log levels or sampling if you approach it.
- [ ] Watch the admin Sites tab for cool-downs and automatic pauses. Resume a paused site only after you checked the
  app is reachable.

## Rollback

Redeploy the previous image SHA (`deploy.sh` with the earlier tag; the Container Apps revision list keeps them).
Migrations are additive, so the older code runs against the newer schema. Do not run `supabase db reset` on the
production project, ever.
