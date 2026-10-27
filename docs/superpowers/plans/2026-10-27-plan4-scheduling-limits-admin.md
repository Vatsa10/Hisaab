# Plan 4 of 7 — Scheduling, Limits, Budget, Admin View and Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connected apps sync daily on their own, "Refresh now" and cart checks have per-day limits, every job's
proxy GB / AI tokens / worker minutes are metered into `usage` and priced against a ₹5,000 monthly budget that
alerts and throttles itself, sign-up is invite-only with a waitlist, the admin has a PWA view for cost, site health,
invites and recipe approvals (including a supervised "learn" job), and users get Web Push or email notifications.

**Architecture:** Postgres stays the only coordination point. A one-shot `server.scheduler` tick (an Azure Container
Apps cron job every 15 minutes; a looping compose service locally) refreshes the stored budget level, enqueues due
`sync` jobs and dispatches a `notifications` outbox. The worker meters each job (relay byte counter, an OpenAI/Jev
token meter, wall time) into `usage`, raises notifications, and runs admin `learn` jobs whose POST-allowlist approvals
wait for the admin's decision over `NOTIFY` (as OTPs do). Sign-up is gated inside Supabase Auth by a Postgres
`before_user_created` hook. The PWA gains push subscription, limit display and an admin-only `/admin` route served by
`/admin/*` API endpoints (service connection; the PWA never sees other users' rows through RLS).

**Tech Stack:** as Plan 3, plus `pywebpush` 2 (VAPID Web Push), Python `smtplib` against Mailpit (`axllent/mailpit:v1.21`)
locally, `zoneinfo` (Asia/Kolkata), Azure Container Apps Jobs (Bicep, build-only).

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md`

**Precondition:** Plan 3 is merged and its `bash scripts/e2e_local.sh` is green. This plan uses Plan 3's views,
`POST /answer /profile /ask`, `server/categorize_pg.py`, `server/fake_sites.py` + `WORKER_FAKE=1`, and `web/`
(`components/ui/{Card,Button,TabBar,Money,Sheet}`, `lib/api.ts` `apiFetch`/`ApiError`, `lib/jobs.ts`
`watchJob`/`watchConnections`, `lib/format.ts` `formatINR`, the Accounts page with "Refresh now").

## Global Constraints

- Limits: 3 manual "Refresh now" per app per user per day; 10 cart checks per user per day; automatic sync every 24 h per connection, every 72 h while the budget is paused.
- Budget ₹5,000 per month; alert the admin by email at 80 %; at 100 % pause new sign-ups and cart checks and drop automatic syncs to every 3 days until the month resets; manual refresh stays within its daily limit.
- Budget figure, thresholds, unit costs and limits are read from env in `server/config.py` (`MONTHLY_BUDGET_INR=5000`, `PROXY_INR_PER_GB=400`, `AI_INR_PER_1K_TOKENS=0.05`, `WORKER_INR_PER_MINUTE=0.20`, `FIXED_MONTHLY_INR=0`, `BUDGET_ALERT_PCT=80`, `BUDGET_PAUSE_PCT=100`, `JEV_TOKENS_PER_CALL=1000`, `REFRESH_PER_APP_PER_DAY=3`, `CART_CHECKS_PER_DAY=10`, `SYNC_INTERVAL_H=24`, `PAUSED_SYNC_INTERVAL_H=72`) and set explicitly in `docker-compose.yml` and the Bicep env; no module other than `config.py` holds these numbers.
- "Day" and "month" are the calendar day/month in `APP_TZ=Asia/Kolkata` (limits reset at IST midnight; the budget resets on the 1st, IST).
- Strictly read-only toward delivery apps. A user's own sync never runs the exploring agent; `learn` jobs run only for admin accounts; a new POST-allowlist entry is only ever allowed after the admin approves it in the admin view.
- Never log OTPs, phone numbers, JWTs, cookies, push endpoints/keys, VAPID/SMTP secrets or full email addresses (mask as `a***@example.com`). New secret `Settings` fields use `field(repr=False)`.
- No real email or push in tests: senders are injected stubs; locally emails go to Mailpit only.
- UI follows Plan 3's Global Constraints (mockup look, `#0a7c55` accent, cards radius 14px, ₹ with Indian grouping, no horizontal scroll at 375px). Frontend tasks invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1` before writing UI (Plan 3 ruling R3); mockups and these constraints win conflicts.
- Every frontend task, after its code review, gets a separate design review (impeccable critique/audit + ui-ux-pro-max + design-taste-frontend-v1, Playwright screenshots at 390×844 compared with the approved mockups); its findings enter that task's fix loop (Plan 3 ruling R5).
- The PWA never uses the service-role key; admin data comes only from `/admin/*` API routes guarded by `require_admin`.
- New tables: RLS enabled; `anon`/`authenticated` get nothing except `select` on `usage` (own rows). Every new `security definer` function sets `search_path` and is executable only by the role that needs it.
- Each parallel implementer uses its own test database `ea_tN` (Plan 3 ruling R4). Every existing test and Plan 3's e2e keep passing.
- All work committed on `main`; never push; no AI attribution in commit messages. No deploy: Bicep is only built (`az bicep build`).

## Review Focus

- Two quick "Refresh now" taps, or a tap while a sync is already queued: the second gets 409 and must not use up one of the 3 daily refreshes. → T9 `test_refresh_conflict_does_not_consume_limit`.
- A refresh at 23:50 IST and another at 00:10 IST: the count resets at IST midnight, not UTC midnight. → T2 `test_refresh_left_resets_at_ist_midnight`.
- The cron job and a stray second scheduler tick overlap: no connection gets two sync jobs and no budget email is sent twice. → T10 `test_concurrent_ticks_enqueue_once`, `test_budget_alerts_sent_once_per_month`.
- A user's push subscription has expired (push service answers 410): it is deleted and the user still gets the email, with no retry loop. → T4 `test_gone_subscription_is_deleted_then_email`.
- The admin starts a learn, the job asks to allow a new POST, and nobody answers: the approval expires, the shared recipe is unchanged and the POST was never let through. → T8 `test_unanswered_approval_expires_and_keeps_recipe`.

## Spec rulings (made by this plan; record in the ledger)

- **R1 Sign-up gate = Supabase `before_user_created` Postgres hook** (`public.before_user_created(jsonb)`), enabled in `supabase/config.toml`. It runs inside GoTrue for email/password and Google alike, so no account exists for a non-invited email; it also adds that email to `waitlist` and sets `invites.accepted_at`. Works on local Supabase and is unit-testable on plain Postgres. Fallback if the pinned CLI rejects the hook key: a `before insert on auth.users` trigger calling the same function and raising (`invite_only`/`signups_paused`). No second API-side gate.
- **R2 Admin identity = `admins(email)` table**, seeded by `python cli.py add-admin <email>` (also invites it); checked as `admins.email = lower(auth.users.email)` for the JWT's `sub`, never from a client claim. "Test accounts" are additional admin emails.
- **R3 Day/month boundaries are IST** (`APP_TZ`).
- **R4 Refresh limit counts enqueued manual sync jobs** (`jobs.payload->>'trigger' = 'manual'` created today IST), so a 409 never consumes; refresh of a `needs_login` connection is refused (409) and of a paused site is refused (503). Scheduled jobs carry `trigger: "schedule"` and never count.
- **R5 Cost model:** ₹ = proxy_mb/1000·PROXY_INR_PER_GB + ai_tokens/1000·AI_INR_PER_1K_TOKENS + worker_seconds/60·WORKER_INR_PER_MINUTE, plus `FIXED_MONTHLY_INR` (Azure/Supabase base, default 0) once per month for the "all-in" budget. Jev calls have no token count, so each adds `JEV_TOKENS_PER_CALL` token-equivalents. `syncs` counts every connect/sync job run. Default unit costs are placeholders the owner sets in env.
- **R6 Budget level is stored** (`budget_state`, one row per IST month) by each scheduler tick (≤ 15 min lag) so the SQL sign-up hook, API and scheduler read one value; a month with no row is `ok`.
- **R7 Scheduler is its own one-shot process** (`python -m server.scheduler --once`), run by an ACA cron job every 15 minutes, not inside the worker (workers scale to zero).
- **R8 Drift:** a user sync that hits drift raises `sync.RecipeDrift`; the worker marks the recipe unhealthy and enqueues an unattended `learn` (`auto: true`, new POST entries denied) on a connected admin account. If that learn is applied the site is healthy again; if it fails, needs approval, or no admin account is connected, the site is paused and admins are emailed. The scheduler skips unhealthy or paused sites; manual refresh is refused only when paused.
- **R9 Approvals:** during an admin-driven learn, each new POST-allowlist entry waits for the admin (`APPROVAL_TIMEOUT_S=600`, like the OTP prompt). A learned orders recipe auto-applies when every POST entry is pre-existing or approved in that job and it has no account-specific values; otherwise (and always for kind `cart`) it is stored `pending` until the admin approves it.
- **R10 Learn jobs fail, never requeue,** on worker loss/shutdown (their approval exchange is gone); they get `MAX_LEARN_JOB_S=2400`.
- **R11 Notifications** go through a `notifications` outbox: push to every subscription of the user, else email to `auth.users.email`; admin alerts and invites are email-only. Local email path = Mailpit container; tests inject senders.
- **R12 Media blocking** (image/media/font requests aborted) applies only to user `sync` jobs (`SYNC_BLOCK_MEDIA=1`), never to login (captchas) or learn.

---

## Wave 0 (alone, first)

### Task 1: Schema, RLS and the invite-only sign-up hook

**Files:**
- Create: `supabase/migrations/0007_scheduling_limits_admin.sql`, `tests/server/test_schema_plan4.py`
- Modify: `supabase/config.toml` (hook block), `tests/server/bootstrap.sql` (role `supabase_auth_admin`; `auth.users` gets `email text`), `tests/server/conftest.py` (`TABLES` += new tables; truncate `auth.users` too), `tests/server/helpers.py` (`add_user(conn, user_id, email)`)

**Interfaces (Produces):**
- `usage(user_id uuid, day date, proxy_mb double precision, ai_tokens bigint, worker_seconds double precision, syncs int, cart_checks int)` — all counters `not null default 0`, PK `(user_id, day)`; RLS `own_rows` select for `authenticated`.
- `admins(email text pk, added_at)`, `invites(email text pk, invited_at, invited_by uuid, accepted_at)`, `waitlist(email text pk, joined_at, invited_at)` — each `check (email = lower(email))`.
- `recipe_approvals(id uuid pk default gen_random_uuid(), site, kind check in (orders,search,cart), reason check in ('post_allowlist','account_specific','cart','learned','drift_relearn'), proposed jsonb not null, current jsonb, diff jsonb not null default '[]', status default 'pending' check in ('pending','approved','rejected','expired','auto_applied'), job_id uuid, created_at, decided_at, decided_by uuid)`.
- `notifications(id uuid pk, user_id uuid, email text, kind, title, body, url default '/', dedupe_key text unique not null, status default 'pending' check in (pending,sent,failed), channel check in (push,email), attempts int default 0, error text, created_at, sent_at, check (num_nonnulls(user_id, email) = 1))`.
- `push_subscriptions(endpoint text pk, user_id uuid not null, p256dh, auth, created_at, last_ok_at)`.
- `budget_state(month text pk check (month ~ '^\d{4}-\d{2}$'), level check in (ok,alert,paused), spent_inr numeric, budget_inr numeric, updated_at)`.
- `alter table recipes add paused boolean not null default false, add health_note text`; index `jobs_user_site_created on jobs (user_id, site, created_at)`.
- `public.before_user_created(event jsonb) returns jsonb`, `security definer`, `set search_path = public, pg_temp`; email = `lower(trim(event->'user'->>'email'))`. Rules in order: admin email → `{}`; not invited → insert into `waitlist` (on conflict do nothing) and return `{"error":{"http_code":403,"message":"invite_only"}}`; `budget_state` for the current IST month is `paused` → `{"error":{"http_code":403,"message":"signups_paused"}}`; else set `invites.accepted_at = now()` where null and return `{}`. `execute` revoked from public/anon/authenticated, granted to `supabase_auth_admin`.
- `config.toml`: `[auth.hook.before_user_created]` `enabled = true`, `uri = "pg-functions://postgres/public/before_user_created"`.

- [ ] **Step 1: Write the failing tests** in `tests/server/test_schema_plan4.py`

```python
def hook(pg, email): return pg.execute("select before_user_created(%s)", (Jsonb({"user": {"email": email}}),)).fetchone()[0]

def test_hook_rejects_and_waitlists_unknown_email(pg):
    assert hook(pg, "New@X.com")["error"]["message"] == "invite_only"
    assert pg.execute("select email from waitlist").fetchall() == [("new@x.com",)]
def test_hook_allows_invited_email_and_marks_accepted(pg): ...       # → {} ; accepted_at not null
def test_hook_blocks_invited_when_budget_paused(pg): ...             # budget_state(current IST month,'paused') → 'signups_paused'
def test_hook_allows_admin_even_when_paused(pg): ...
def test_hook_not_executable_by_users(pg, as_user): ...              # as authenticated → InsufficientPrivilege
def test_usage_rls_own_rows(pg, as_user): ...                       # A sees only A's rows
def test_admin_tables_hidden_from_users(pg, as_user): ...           # select on invites, waitlist, admins, recipe_approvals,
                                                                    # notifications, push_subscriptions, budget_state → permission denied
def test_recipes_have_paused_and_health_note(pg): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_schema_plan4.py -v` → FAIL (no migration 0007).
- [ ] **Step 3: Implement** the migration, bootstrap/conftest/helpers changes and the `config.toml` block.
- [ ] **Step 4: Verify on real local Supabase:** `npx supabase stop && npx supabase start` (config change needs a restart), `npx supabase db reset` → 0001–0007 apply. `POST <api>/auth/v1/signup` with an uninvited email → HTTP 403, message `invite_only`, email present in `waitlist`; insert it into `invites` and sign up again → 200 with a session. If the CLI rejects the hook key, apply the R1 fallback trigger and record it in the ledger. Then `pytest -q` (TEST_DATABASE_URL set and unset) → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(db): usage, invites, approvals, notifications tables and invite-only sign-up hook"`

---

## Wave A (parallel after Task 1 is merged; disjoint files)

### Task 2: Config, usage metering store, limits and budget

**Files:**
- Modify: `server/config.py`, `tests/server/test_config.py`
- Create: `server/usage.py`, `server/limits.py`, `server/budget.py`, `tests/server/test_budget_limits.py`

**Interfaces (Produces):**
- `Settings` new fields (env name = upper-case field name; defaults as Global Constraints): `monthly_budget_inr: float`, `proxy_inr_per_gb`, `ai_inr_per_1k_tokens`, `worker_inr_per_minute`, `fixed_monthly_inr`, `budget_alert_pct: int`, `budget_pause_pct: int`, `jev_tokens_per_call: int`, `refresh_per_app_per_day: int`, `cart_checks_per_day: int`, `sync_interval_h: int`, `paused_sync_interval_h: int`, `app_tz: str = "Asia/Kolkata"`, `approval_timeout_s: int = 600`, `max_learn_job_s: int = 2400`, `sync_block_media: bool = True` (`SYNC_BLOCK_MEDIA` "1"/"0"), `scheduler_tick_s: int = 900`, `app_url: str = "http://localhost:3000"`, `vapid_public_key: str = ""`, `vapid_private_key` (repr=False), `vapid_subject: str = "mailto:admin@localhost"`, `smtp_host: str = ""`, `smtp_port: int = 1025`, `smtp_user: str = ""`, `smtp_password` (repr=False), `smtp_starttls: bool = False`, `email_from: str = "Expense Agent <no-reply@localhost>"`. Module constant `SITE_NAMES = {"zepto": "Zepto", "blinkit": "Blinkit", "swiggy": "Swiggy", "instamart": "Instamart", "zomato": "Zomato"}`.
- `usage.today(now: datetime | None = None, tz: str = "Asia/Kolkata") -> date`; `usage.month_of(now, tz) -> str` (`"YYYY-MM"`); `usage.day_bounds(now, tz) -> tuple[datetime, datetime]` (UTC start/end of the IST day); `usage.record(conn, user_id: str, day: date, *, proxy_mb=0.0, ai_tokens=0, worker_seconds=0.0, syncs=0, cart_checks=0) -> None` (upsert, adds).
- `limits.LimitExceeded(kind: str, limit: int, retry_after_s: int, message: str)`; `limits.BudgetPaused(kind: str, resumes_on: date, message: str)`.
- `limits.refresh_left(conn, user_id, site, now, s) -> int`; `limits.check_refresh(conn, user_id, site, now, s) -> None` (raises `LimitExceeded("refresh", 3, …, "You can refresh Zepto 3 times a day. Try again tomorrow.")`).
- `limits.cart_checks_left(conn, user_id, now, s) -> int`; `limits.consume_cart_check(conn, user_id, now, s) -> int` (left after consuming; raises `BudgetPaused("cart", <1st of next month>, "Cart checks are paused until 1 November.")` when `budget.is_paused`, else `LimitExceeded("cart", 10, …, "You can check 10 carts a day. Try again tomorrow.")`). Atomic: one `insert … on conflict do update set cart_checks = usage.cart_checks + 1 where usage.cart_checks < %s returning cart_checks`.
- `limits.seconds_until_reset(now, tz) -> int`; `limits.to_http(e) -> fastapi.HTTPException` (429 + `Retry-After` for `LimitExceeded`, 503 for `BudgetPaused`, `detail = e.message`).
- `budget.cost_inr(proxy_mb: float, ai_tokens: int, worker_seconds: float, s) -> float` (R5 formula, no fixed part); `budget.month_cost(conn, s, month: str) -> float` (all users' usage of that IST month + `fixed_monthly_inr`); `budget.level_for(spent: float, s) -> str` (`"ok"|"alert"|"paused"`); `@dataclass BudgetState(month, spent_inr, budget_inr, pct: int, level, previous_level)`; `budget.refresh_state(conn, s, now) -> BudgetState` (upserts `budget_state`); `budget.current_level(conn, now, tz) -> str`; `budget.is_paused(conn, now, tz) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_settings_reads_budget_and_unit_costs_from_env(monkeypatch): ...   # MONTHLY_BUDGET_INR=1234 → 1234.0, etc.; secrets not in repr
def test_cost_inr_formula(s):
    assert budget.cost_inr(1000, 10_000, 600, s) == pytest.approx(400 + 0.5 + 2.0)   # 402.5 at default unit costs
def test_month_cost_uses_ist_month_and_fixed(pg): ...   # usage day 2026-10-31 counts for "2026-10", 2026-11-01 does not; + FIXED_MONTHLY_INR
def test_level_thresholds(s):
    assert [budget.level_for(x, s) for x in (3999.99, 4000, 5000)] == ["ok", "alert", "paused"]
def test_refresh_state_reports_previous_level(pg): ...
def test_new_month_is_ok(pg): ...                          # 2026-10 paused; current_level on 2026-11-01 IST → "ok"
def test_usage_record_accumulates(pg): ...
def test_today_is_ist():
    assert usage.today(datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc)) == date(2026, 10, 28)
def test_refresh_left_counts_manual_jobs_today_only(pg): ...    # scheduled jobs and other sites don't count
def test_refresh_left_resets_at_ist_midnight(pg): ...           # manual job at 23:50 IST yesterday → 3 left at 00:10 IST
def test_check_refresh_message(pg): ...                         # 4th → LimitExceeded, message exact, retry_after_s > 0
def test_consume_cart_check_stops_at_ten(pg): ...               # 10 succeed, 11th raises; usage.cart_checks == 10
def test_consume_cart_check_is_atomic(pg): ...                  # two threads at 9 used → exactly one succeeds
def test_consume_cart_check_when_budget_paused(pg): ...         # BudgetPaused, resumes_on == date(2026, 11, 1)
```

- [ ] **Step 2: Run** `pytest tests/server/test_budget_limits.py tests/server/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement** the settings and the three modules.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): usage store, daily limits and budget levels from config"`

---

### Task 3: Engine metering, media blocking, drift error and the learn approval hook

**Files:**
- Create: `metering.py`, `tests/test_metering.py`, `tests/test_learn_approval.py`
- Modify: `agent.py`, `insights.py`, `categorize.py`, `server/categorize_pg.py` (its default classifier's OpenAI call), `guard.py`, `browser.py`, `sync.py`, `server/proxy_relay.py`, `tests/test_guard.py`, `tests/server/test_proxy_relay.py`

**Interfaces (Produces):**
- `metering.Meter` (`tokens: int = 0`, `jev_calls: int = 0`); `metering.meter() -> ContextManager[Meter]` (a `ContextVar`; restores the previous meter on exit); `metering.count_openai(resp) -> resp` (adds `resp.usage.total_tokens` when present, None-safe, no-op without an active meter); `metering.count_jev(n: int = 1) -> None`. Every `chat.completions.create` in `agent.py` (3), `insights.py` (2), `categorize.py` (1) and `server/categorize_pg.py` is wrapped in `count_openai`; both Jev `system_one` calls in `agent.py` call `count_jev()`.
- `guard.HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})`; `Guard(allowlist=None, login_mode=False, block_media=False)`; when `block_media and not login_mode` and `request.resource_type` is heavy, `_handle` aborts it before `decide`, increments `guard.media_blocked` and neither logs it nor appends it to `guard.blocked`.
- `browser.open_site(..., block_media: bool = False)`; `sync.fetch_orders(..., block_media: bool = False)` passes it through.
- `sync.RecipeDrift(RecipeError)` raised (same message as today) for the `allow_learn=False` drift case; "no recipe" stays a plain `RecipeError`.
- `agent.confirm_allowlist(entries, approved, approve: Callable[[list[dict]], bool] | None = None) -> bool` — with `approve`, it is called with only the not-yet-approved entries instead of `input()`; `validate`, `synthesize`, `learn_from_seed` and `discover(site, page, guard, capture, approve=None, preapproved: Iterable[str] = ())` thread `approve` through and seed `approved` with `preapproved` regexes. CLI behaviour unchanged when `approve` is None.
- `sync.learn(site, profile_dir=None, location=None, proxy_server=None, approve=None, preapproved=(), save: bool = True) -> dict` (opens the site without media blocking; `save=False` skips `recipes.save`).
- `Relay.bytes_total -> int` — bytes piped in both directions over the relay's life (`_pipe(reader, writer, count)`).

- [ ] **Step 1: Write the failing tests**

```python
def test_count_openai_adds_total_tokens():
    with metering.meter() as m:
        metering.count_openai(SimpleNamespace(usage=SimpleNamespace(total_tokens=1234)))
        metering.count_openai(SimpleNamespace(usage=None)); metering.count_jev()
    assert (m.tokens, m.jev_calls) == (1234, 1)
def test_no_meter_is_a_noop(): ...
def test_insights_counts_tokens(monkeypatch): ...              # stub client returns usage 50 per call → meter.tokens == 50 * calls
def test_media_blocked_in_sync_mode(): ...                     # fake route/request resource_type "image" → abort; guard.blocked == []
def test_media_not_blocked_in_login_mode(): ...                # login_mode=True, block_media=True → continue_
def test_open_site_passes_block_media(monkeypatch, tmp_path): ...
def test_fetch_orders_drift_raises_recipe_drift(monkeypatch): ...
def test_confirm_allowlist_uses_approve_callback(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("no terminal prompt"))
    seen = []; approved = {"^a$"}
    assert agent.confirm_allowlist([{"url_regex": "^a$"}, {"url_regex": "^b$"}], approved, approve=lambda e: seen.append(e) or True)
    assert seen == [[{"url_regex": "^b$"}]] and approved == {"^a$", "^b$"}
def test_discover_rejection_is_reported(monkeypatch): ...      # approve → False ⇒ validate error "user rejected the POST allowlist…"
def test_relay_counts_bytes(fake_upstream, target): ...        # after one 10 kB exchange, bytes_total >= 10_000
```

- [ ] **Step 2: Run** `pytest tests/test_metering.py tests/test_learn_approval.py tests/test_guard.py tests/server/test_proxy_relay.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green (CLI tests untouched).
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): token and proxy-byte metering, media blocking for syncs, approval callback for learning"`

---

### Task 4: Notifications outbox, Web Push and email (Mailpit locally)

**Files:**
- Create: `server/notify.py`, `tests/server/test_notify.py`
- Modify: `requirements.txt` (`pywebpush>=2.0`), `docker-compose.yml` (service `mailpit`: image `axllent/mailpit:v1.21`, ports `127.0.0.1:1025:1025` and `127.0.0.1:8025:8025`; `x-app-env` gains `SMTP_HOST: mailpit`, `SMTP_PORT: "1025"`, `APP_URL`, `VAPID_*` passthrough), `docs/deploy-local.md` (Mailpit UI at http://localhost:8025)

**Interfaces (Produces):**
- `notify.enqueue(conn, *, kind: str, title: str, body: str, url: str = "/", user_id: str | None = None, email: str | None = None, dedupe_key: str) -> str | None` (None when the dedupe key exists).
- `notify.alert_admins(conn, *, kind, title, body, dedupe_key) -> int` — one email row per `admins.email`, key `f"{dedupe_key}:{email}"`.
- `notify.save_subscription(conn, user_id, endpoint: str, p256dh: str, auth: str) -> None` (upsert; endpoint must be `https://`, else `ValueError`); `notify.delete_subscription(conn, user_id, endpoint) -> None`.
- `notify.PushGone(Exception)`; `push_send(sub: dict, payload: dict, s) -> None` (default: `pywebpush.webpush(..., vapid_private_key=s.vapid_private_key, vapid_claims={"sub": s.vapid_subject}, ttl=86400)`; HTTP 404/410 → `PushGone`); `mail_send(to: str, subject: str, text: str, s) -> None` (default: `smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10)`, STARTTLS/login when configured; text = body + `"\n\n" + s.app_url + url`).
- `notify.dispatch(conn, s, push_send=None, mail_send=None, limit: int = 50) -> int` — claims `pending` rows `FOR UPDATE SKIP LOCKED`; user rows: push payload `{"title","body","url"}` to each subscription (none, or VAPID unset → skip), a `PushGone` deletes that subscription, any success → `sent/push`; otherwise email to `auth.users.email` → `sent/email`; email rows → email. A failure increments `attempts` (error = exception type name only); `failed` after 5 attempts. Returns rows sent.
- `notify.format_inr(amount: float) -> str` (`"₹1,23,456"`, two decimals below ₹100); `notify.mask_email(e) -> str`.

- [ ] **Step 1: Write the failing tests** — `test_enqueue_dedupes`, `test_dispatch_prefers_push` (payload exactly `{"title","body","url"}`, mail stub not called, row `sent`/`push`), `test_dispatch_falls_back_to_email_without_subscription` (to = `auth.users.email`, subject = title), `test_gone_subscription_is_deleted_then_email`, `test_failure_retries_then_fails_after_5`, `test_alert_admins_emails_every_admin`, `test_dispatch_never_logs_endpoint_or_email` (caplog contains neither), `test_default_mail_send_uses_smtp_settings` (monkeypatched `smtplib.SMTP` records host/port/message), `test_subscription_requires_https`, `test_format_inr`.
- [ ] **Step 2: Run** `pytest tests/server/test_notify.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green; `docker compose config -q` → exit 0.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): notification outbox with Web Push, email fallback and local Mailpit"`

---

### Task 5: Recipe approvals, site health and admin learners

**Files:**
- Create: `server/recipe_admin.py`, `tests/server/test_recipe_admin.py`
- Modify: `server/store.py` (`put_recipe` also sets `paused=false, health_note=null`), `cli.py` (`_strip_identity_headers`/`_account_specific` move into `recipe_admin`; `cli.py` imports them under the same names so `publish-recipe` and its tests are unchanged)

**Interfaces (Produces):**
- `recipe_admin.is_admin(conn, user_id: str) -> bool` (admins ⨝ `auth.users` on lower email); `recipe_admin.admin_learner(conn, site) -> str | None` (oldest admin user whose connection for `site` is `connected`).
- `recipe_admin.sanitize_for_sharing(recipe: dict) -> tuple[dict, list[str]]` (identity headers stripped; JSON paths of account-specific values).
- `recipe_admin.diff(current: dict | None, proposed: dict) -> list[dict]` — `{"op": "add"|"remove"|"change", "path": "$.a.b[0]", "old"?, "new"?}`, sorted by path.
- `recipe_admin.Approver` — callable `(entries: list[dict]) -> bool` with `.approved: set[str]`; `recipe_admin.make_approver(listen_conn, job_conn, job_id, site, kind, timeout_s) -> Approver`: inserts a `post_allowlist` approval (`proposed={"read_post_allowlist": entries}`), sets job progress `stage="approval_needed", approval_id, entries`, `LISTEN approval_<id hex>`, returns True only on payload `"approved"` (progress `approval_received`); on timeout sets the row `expired` and returns False. `recipe_admin.deny_new_posts(entries) -> bool` returns False.
- `recipe_admin.apply_learned(conn, site, kind, recipe, approved: set[str], job_id, reason: str) -> dict` → `{"approval_id", "status", "version"}`: sanitises; `pending` (reason `cart` for kind cart, `account_specific` when flagged, `post_allowlist` when a POST regex is neither in the current recipe nor in `approved`); else `store.put_recipe` and an `auto_applied` row (reason as given: `learned` or `drift_relearn`) with the diff.
- `recipe_admin.decide(conn, approval_id, decision: "approve"|"reject", admin_user_id) -> str` — 409-worthy `NotPending` if not pending; `post_allowlist`: if its job is still running → status set and `pg_notify('approval_<hex>', 'approved'|'rejected')`, else `expired` + raise `JobEnded`; recipe rows: approve → `put_recipe(proposed)` and `approved`, reject → `rejected`.
- `recipe_admin.mark_drift(conn, site, kind, note) -> bool` (True only on the healthy → unhealthy transition); `pause_site(conn, site, kind, note)`; `resume_site(conn, site, kind)`; `is_paused(conn, site, kind="orders") -> bool`.

- [ ] **Step 1: Write the failing tests** — `test_is_admin_case_insensitive`, `test_admin_learner_needs_connected_account`, `test_sanitize_matches_publish_recipe` (Zomato `/users/123/ordering` flagged; Referer dropped), `test_diff_reports_add_remove_change`, `test_apply_without_new_posts_auto_applies` (version +1, row `auto_applied`), `test_apply_with_unapproved_post_is_pending` (recipe unchanged), `test_apply_with_approved_post_applies`, `test_cart_recipe_always_pending`, `test_approver_round_trip` (another connection decides → True; progress `approval_needed` then `approval_received`), `test_approver_timeout_expires_row`, `test_decide_ended_job_expires`, `test_decide_pending_recipe_publishes_and_unpauses`, `test_mark_drift_transitions_once`, `test_put_recipe_clears_pause`.
- [ ] **Step 2: Run** `pytest tests/server/test_recipe_admin.py tests/server/test_recipe_cloud.py -v` → new tests FAIL, old pass.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): recipe approvals with live POST-allowlist decisions, drift and pause state"`

---

### Task 6: PWA — notifications, refresh limits and invite-only sign-in

**Files:**
- Create: `web/lib/push.ts`, `web/lib/me.ts`, `web/lib/authErrors.ts`, `web/components/account/NotificationsCard.tsx`, `web/__tests__/push.test.ts`, `web/__tests__/limits.test.tsx`, `web/__tests__/authErrors.test.ts`
- Modify: `web/public/sw.js`, `web/components/connect/AppRow.tsx`, `web/app/(app)/accounts/page.tsx`, `web/app/(auth)/login/page.tsx`, `web/app/auth/callback/route.ts`

**Interfaces:**
- Consumes (Task 9 API): `GET /me` → `{user_id, email, is_admin}`; `GET /me/limits` → `{refresh_left: {[site]: number}, refresh_limit: 3, cart_checks_left, cart_checks_limit: 10, budget_paused: boolean, resets_at: string}`; `GET /push/vapid-key` → `{key}` (404 when unset); `POST /push/subscribe` `{endpoint, keys: {p256dh, auth}}` → 204; `POST /push/unsubscribe` `{endpoint}` → 204; refresh 429/409/503 `detail` strings shown verbatim.
- Produces: `push.ts`: `pushSupported(): boolean`, `isStandalone(): boolean`, `pushState(): Promise<"on"|"off"|"denied"|"unsupported"|"needs_install">`, `enablePush(): Promise<"on"|"denied"|"unsupported"|"needs_install">` (permission requested only from the button's click), `disablePush(): Promise<void>`, `urlB64ToUint8Array(s): Uint8Array`. `me.ts`: `getMe()`, `getLimits()`. `authErrors.ts`: `authErrorMessage(code: string, email?: string): string` — `invite_only` → "Expense Agent is invite-only for now. We've added {email} to the waitlist and will email you when a spot opens." (without email: "We've added you to the waitlist…"), `signups_paused` → "Sign-ups are paused until next month. Please try again then.", anything else → the generic sign-in error.
- `sw.js`: `push` → `showNotification(title, {body, icon: "/icons/192.png", data: {url}})`; `notificationclick` → focus an open client and navigate, else `openWindow(url)`; only same-origin paths starting with `/`.
- Accounts page: `NotificationsCard` copy — intro "Get notified when a sync finishes or an app logs you out."; states: off → "Turn on notifications"; on → "Notifications on" + "Turn off"; denied → "Notifications are off in Settings. We'll email you instead."; needs_install (iOS, not standalone) → "Add Expense Agent to your Home Screen (Share → Add to Home Screen) to get notifications. We'll email you meanwhile." `AppRow` refresh: "Refresh now · {n} left today"; at 0 disabled "No refreshes left today"; hidden for `needs_login` rows. An "Admin" row linking `/admin` appears only when `getMe().is_admin`.

- [ ] **Step 1: Write the failing tests** — `enablePush subscribes with the VAPID key and posts it` (mocked `PushManager`, `Notification.requestPermission` → "granted"), `needs_install on iOS when not standalone`, `denied permission returns denied and posts nothing`, `refresh shows remaining and disables at zero`, `429 detail is shown`, `needs_login row has no refresh`, `admin link only for admins`, `authErrorMessage maps invite_only and signups_paused`, `login page shows the waitlist message on invite_only`.
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm --dir web test`, `pnpm --dir web lint`, `pnpm --dir web build` → pass; Accounts at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): push notifications, refresh limits and invite-only sign-in messages"`

---

### Task 7: PWA — admin view

**Files:**
- Create: `web/app/(app)/admin/page.tsx`, `web/components/admin/{BudgetBar,CostsPanel,SitesPanel,InvitesPanel,ApprovalsPanel,RecipeDiff}.tsx`, `web/lib/admin.ts`, `web/__tests__/admin.test.tsx`

**Interfaces:**
- Consumes (Task 9 API; every route 403 `"admin only"` for non-admins):
  - `GET /admin/costs?month=YYYY-MM` → `{month, budget_inr, spent_inr, fixed_inr, pct, level, unit_costs: {proxy_inr_per_gb, ai_inr_per_1k_tokens, worker_inr_per_minute}, users: [{user_id, email, proxy_gb, ai_tokens, worker_minutes, syncs, cart_checks, inr}]}` (users sorted by `inr` desc)
  - `GET /admin/sites` → `[{site, kind, version, learned_at, healthy, paused, health_note, syncs_ok_7d, syncs_failed_7d, last_ok_at, admin_connected}]`; `POST /admin/sites/{site}/pause` / `/resume` → 204; `POST /admin/learn/{site}` → 202 `{job_id}` (409 detail shown)
  - `GET /admin/invites` → `{invites: [{email, invited_at, accepted_at}], waitlist: [{email, joined_at, invited_at}]}`; `POST /admin/invites {email}` → 201; `DELETE /admin/invites/{email}` → 204
  - `GET /admin/approvals?status=pending|all` → `[{id, site, kind, reason, status, proposed, current, diff, job_id, created_at, decided_at}]`; `POST /admin/approvals/{id}/decide {decision}` → 200 `{status}`
  - `watchJob(jobId, onChange)` from Plan 3 for the learn job's progress.
- Produces: `/admin` with a segmented control Costs · Sites · Invites · Approvals. Costs: month label, `BudgetBar` (spent / budget, pct; green ok, orange alert, red paused with "Sign-ups and cart checks paused"), unit-cost line, one card row per user (email, proxy GB 2 dp, AI tokens in k, worker minutes, ₹ via `formatINR`). Sites: per site name, recipe version, "learned <relative>", badge Healthy / Unhealthy / Paused (+ note), 7-day ok/failed counts, "Learn" (disabled with hint "Connect <App> on your own account first" unless `admin_connected`), Pause/Resume; a running learn shows its stage, and `approval_needed` shows the requested regexes with Approve / Reject inline. Invites: email field + "Invite", invites list (Pending/Joined, Revoke for pending), waitlist with "Invite" per row. Approvals: pending cards (site, kind, reason, created) with `RecipeDiff` (`+ path: value`, `− path`, `~ path: old → new`) and Approve / Reject; history of the last 20 decisions. 403 → "This page is for the admin."

- [ ] **Step 1: Write the failing tests** — `non-admin sees the admin-only message`, `costs list users by rupees with budget level colour`, `learn shows approval_needed entries and approve posts the decision`, `waitlist invite posts the email`, `recipe diff renders add/remove/change`, `learn disabled without admin connection`.
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint, build → pass; `/admin` at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): admin view for costs, site health, invites and recipe approvals"`

---

## Wave B (after Wave A is merged; parallel, disjoint files)

### Task 8: Worker — metering, notifications, drift and learn jobs

**Files:**
- Modify: `server/worker.py`, `server/jobs.py`, `server/fake_sites.py`
- Test: `tests/server/test_worker_plan4.py`, `tests/server/test_jobs.py` (stale learn), `tests/server/test_fake_sites.py`

**Interfaces:**
- Consumes: `usage.record/today`, `metering.meter`, `Relay.bytes_total`, `sync.fetch_orders(block_media=)`, `sync.RecipeDrift`, `sync.learn(..., approve, preapproved, save=False)`, `recipe_admin.*`, `notify.enqueue/alert_admins/dispatch/format_inr`, `config.SITE_NAMES`.
- Produces:
  - `worker.JOB_TYPES = ("connect", "sync", "learn")`.
  - `run_job` meters every job in its `finally` (never fails the job; errors logged by type name): `usage.record(conn, job.user_id, usage.today(tz=s.app_tz), proxy_mb=relay.bytes_total / 1e6, ai_tokens=meter.tokens + meter.jev_calls * s.jev_tokens_per_call, worker_seconds=<wall time>, syncs=1 if job.type in ("connect", "sync") else 0)`.
  - User syncs call `fetch_orders(..., block_media=s.sync_block_media)`.
  - Connect success → `notify.enqueue(kind="first_sync_done", title=f"{App} is connected", body=f"{n} orders · {format_inr(total)} found", url="/spend", user_id=…, dedupe_key=f"first_sync:{job.id}")`. A transition to `needs_login` (connection not already `needs_login`) → `kind="needs_login"`, title `f"{App} logged you out"`, body `"Reconnect to keep your spending up to date."`, url `/accounts`, key `f"needs_login:{job.id}"`. After enqueueing, `notify.dispatch(conn, s)` best-effort.
  - `RecipeDrift` in a sync → job failed `recipe_drift`; if `mark_drift` transitioned: enqueue `learn` for `admin_learner(conn, site)` with payload `{"reason": "drift", "auto": true}` (Conflict ignored), or, with no learner, `pause_site` + `alert_admins(kind="site_paused", title=f"{App} sync paused", body="Its order recipe stopped working and no admin account is connected to re-learn it.", dedupe_key=f"site_paused:{site}:{today}")`.
  - `learn` job: refused unless `recipe_admin.is_admin(job.user_id)` (`not_admin`); restores the admin's profile (missing → `needs_login`), uses their proxy; progress `stage="learning"`; approver = `make_approver(...)` (or `deny_new_posts` when `auto`); `preapproved` = current recipe's allowlist regexes; result → `apply_learned(..., reason="drift_relearn" if auto else "learned")`; progress `stage="learned", approval_id, status, version`. For `auto` jobs, a failure or a `pending` result → `pause_site` + `alert_admins(kind="site_paused", …)`. The heartbeat uses `s.max_learn_job_s` for learn jobs.
  - `jobs.requeue_stale` and `worker._abort_on_shutdown` treat `learn` like `connect` (failed `worker_lost` / `worker_shutdown`).
  - `fake_sites.learn(site, approve, preapproved=(), **_) -> dict` — deterministic recipe with endpoint `https://fake.<site>.test/api/orders`, method POST and `read_post_allowlist=[{"url_regex": "^https://fake\\.<site>\\.test/api/orders$", "why": "fake order list"}]`; calls `approve([entry])` unless preapproved; rejection → `sync.RecipeError("POST allowlist rejected")`. `WORKER_FAKE=1` routes learn jobs to it.

- [ ] **Step 1: Write the failing tests** — `test_sync_job_records_usage` (stub fetch calls `metering.count_openai` with 1234 tokens and `count_jev()` once → `ai_tokens == 2234`, `syncs == 1`, `worker_seconds > 0`), `test_sync_passes_block_media`, `test_metering_failure_does_not_fail_job`, `test_connect_success_enqueues_first_sync_notification` (title "Zepto is connected", body "2 orders · ₹200 found"), `test_needs_login_notifies_once`, `test_drift_marks_unhealthy_and_enqueues_admin_learn`, `test_drift_without_admin_connection_pauses_and_alerts`, `test_learn_job_requires_admin`, `test_learn_job_auto_applies_recipe_without_new_posts`, `test_learn_job_waits_for_approval` (decide from a second connection → recipe applied), `test_unanswered_approval_expires_and_keeps_recipe` (approval_timeout_s=1; version unchanged; row `expired`), `test_auto_learn_failure_pauses_site`, `test_stale_learn_is_failed_not_requeued`, `test_fake_learn_asks_for_approval`.
- [ ] **Step 2: Run** `pytest tests/server/test_worker_plan4.py tests/server/test_jobs.py tests/server/test_fake_sites.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(worker): meter usage, notify, handle drift and run supervised learn jobs"`

---

### Task 9: API — limits, me, push subscriptions and admin routes

**Files:**
- Modify: `server/api.py`, `cli.py` (commands `add-admin <email>` and `invite <email>`, using `DATABASE_URL`)
- Create: `server/admin.py`, `server/admin_api.py` (`APIRouter(prefix="/admin")`, included by `api.app`), `tests/server/test_api_plan4.py`, `tests/server/test_admin.py`

**Interfaces:**
- Consumes: `limits.*`, `budget.*`, `usage.*`, `recipe_admin.*`, `notify.*`, `metering.meter`, `config.SITE_NAMES`.
- Produces: the routes listed in Task 6 and Task 7 "Consumes", plus:
  - `POST /connections/{site}/refresh` checks, in order: 404 no connection; 409 `"Reconnect {App} first."` when `needs_login`; 409 connect active (existing); 503 `"{App} sync is paused while we fix it."` when `recipe_admin.is_paused`; `limits.check_refresh` → `limits.to_http`; enqueue with payload `{"trigger": "manual"}` (409 on Conflict, nothing consumed); 202 `{job_id, refresh_left}`. Budget pause does not block it.
  - `POST /ask` runs inside `metering.meter()` and records `ai_tokens` (+ Jev equivalents) for the caller.
  - `require_admin` dependency: `current_user` then `recipe_admin.is_admin` → 403 `"admin only"`.
  - `POST /admin/invites` validates the email (`admin.normalize_email`, 422 when invalid), `admin.invite(...)`, sets `waitlist.invited_at`, and enqueues `notify.enqueue(kind="invite", email=…, title="You're invited to Expense Agent", body=f"Sign up with this email at {s.app_url}/login.", dedupe_key=f"invite:{email}")`.
  - `POST /admin/learn/{site}`: 409 `"Connect {App} on your own account first."` unless the admin's connection is `connected`; 409 if a learn is active; enqueues `learn` for the admin's own `user_id` with payload `{"reason": "manual"}`.
  - `POST /admin/approvals/{id}/decide`: `NotPending`/`JobEnded` → 409.
  - `admin.py`: `normalize_email(e) -> str` (ValueError), `user_email(conn, user_id)`, `add_admin(conn, email)` (admins + invites), `invite(conn, email, invited_by) -> bool`, `revoke_invite(conn, email) -> bool` (raises `InviteAccepted`), `list_invites(conn) -> dict`, `usage_by_user(conn, month) -> list[dict]` (raw sums + email; ₹ added in the route via `budget.cost_inr`), `site_health(conn, now) -> list[dict]` (recipes for every connectable site, 7-day sync job outcomes from `jobs`, `admin_connected` for the caller).
  - CORS methods gain `DELETE`.

- [ ] **Step 1: Write the failing tests** — `test_refresh_limit_is_three_per_app_per_day` (4th → 429, exact detail, integer `Retry-After`), `test_refresh_limit_is_per_app`, `test_refresh_conflict_does_not_consume_limit`, `test_scheduled_syncs_do_not_count`, `test_refresh_needs_login_is_409`, `test_refresh_paused_site_is_503`, `test_refresh_allowed_while_budget_paused`, `test_me_and_limits`, `test_push_subscribe_and_unsubscribe`, `test_push_subscribe_rejects_non_https`, `test_vapid_key_404_when_unset`, `test_ask_records_ai_tokens`, `test_admin_routes_forbidden_for_non_admin` (every `/admin` route → 403), `test_admin_costs_converts_usage_to_inr` (1000 MB, 10 000 tokens, 600 s → `inr == 402.5`), `test_admin_invite_moves_waitlist_and_emails`, `test_admin_revoke_accepted_invite_is_409`, `test_admin_learn_requires_connected_admin_account`, `test_admin_decide_post_allowlist_notifies_waiting_job`, `test_admin_pause_resume_site`, `test_cli_add_admin_and_invite`; in `test_admin.py`: `test_usage_by_user_sums_ist_month`, `test_site_health_counts_last_7_days`, `test_normalize_email`.
- [ ] **Step 2: Run** `pytest tests/server/test_api_plan4.py tests/server/test_admin.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): refresh limits, push subscriptions, me and admin API"`

---

### Task 10: Scheduler — daily syncs, budget alerts, notification dispatch

**Files:**
- Create: `server/scheduler.py`, `tests/server/test_scheduler.py`
- Modify: `docker-compose.yml` (service `scheduler`: api image, `command: ["python", "-m", "server.scheduler"]`, app env + budget/unit-cost/limit env set explicitly), `deploy/azure/main.bicep` (`Microsoft.App/jobs@2024-03-01` `${prefix}-scheduler`, `triggerType: 'Schedule'`, `cronExpression: '*/15 * * * *'`, `replicaTimeout: 600`, `replicaRetryLimit: 0`, command `python -m server.scheduler --once`, gated by `deployApps`; budget/unit-cost/SMTP/VAPID/APP_URL env on API, worker and scheduler), `deploy/azure/env.deploy.example`, `docs/deploy-local.md`, `README.md` (Server section: scheduler, limits, budget env, admin setup with `cli.py add-admin`)

**Interfaces:**
- Consumes: `budget.refresh_state`, `notify.alert_admins/dispatch/format_inr`, `jobs.enqueue`, `Settings`.
- Produces:
  - `scheduler.due_connections(conn, now, interval_h: int) -> list[tuple[str, str]]` — connections with status `connected` (or `error` with `last_synced_at` not null), `last_synced_at` null or older than `interval_h`, no `sync`/`connect` job queued/running or created within `interval_h`, and whose `orders` recipe is not unhealthy or paused (a missing recipe row does not exclude).
  - `scheduler.tick(conn, s, now: datetime | None = None) -> dict` → `{"skipped": bool, "level", "enqueued": int, "sent": int}`: takes `pg_try_advisory_lock(727274)` (else `{"skipped": True}`), released in `finally`; `budget.refresh_state`; level ≥ alert → `alert_admins(kind="budget_alert", title=f"Budget at {pct}% for {Month YYYY}", body=f"{spent} of {budget} used. Sign-ups and cart checks pause at 100%.", dedupe_key=f"budget:{month}:alert")`; level paused → `alert_admins(kind="budget_paused", title=f"Budget reached for {Month YYYY}", body=f"{spent} of {budget} used. Sign-ups and cart checks are paused and automatic syncs run every 3 days until 1 {NextMonth}.", dedupe_key=f"budget:{month}:paused")` (amounts via `format_inr`); interval = `paused_sync_interval_h` if paused else `sync_interval_h`; enqueue each due connection with payload `{"trigger": "schedule"}` (Conflict ignored); `notify.dispatch`.
  - `scheduler.main(once: bool = False, max_loops: int | None = None)` — `--once` runs one tick and exits 0; otherwise ticks every `s.scheduler_tick_s` until SIGTERM. Logs counts only.

- [ ] **Step 1: Write the failing tests** — `test_tick_enqueues_daily_sync_for_due_connection` (last sync 25 h ago → one `sync` job, `trigger == "schedule"`), `test_tick_skips_recent_and_active`, `test_tick_skips_needs_login_disconnected_and_never_synced_error`, `test_tick_skips_unhealthy_or_paused_recipe`, `test_failed_scheduled_sync_not_retried_same_day`, `test_paused_budget_syncs_every_three_days` (48 h → not due, 73 h → due), `test_budget_alerts_sent_once_per_month` (two ticks over 80 % → one alert row per admin; over 100 % adds one paused row), `test_concurrent_ticks_enqueue_once` (advisory lock held on `pg2` → `skipped`), `test_tick_dispatches_pending_notifications`, `test_main_once_runs_one_tick`.
- [ ] **Step 2: Run** `pytest tests/server/test_scheduler.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green; `docker compose config -q` → 0; `az bicep build --file deploy/azure/main.bicep` → 0; `bash deploy/azure/test_guard.sh` → ALL PASS; `bash scripts/smoke_compose.sh` → SMOKE OK.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): scheduler for daily syncs, budget alerts and notification dispatch"`

---

## Wave C

### Task 11: End-to-end — invite-only, limits, notifications, admin and budget locally

**Files:**
- Create: `e2e/tests/plan4.spec.ts`, `e2e/helpers/mailpit.ts`, `e2e/helpers/stack.ts`
- Modify: `e2e/tests/product.spec.ts` (invite its email before sign-up), `scripts/e2e_local.sh`, `docs/e2e-local.md`

**Interfaces:**
- `scripts/e2e_local.sh` additionally: starts Mailpit (`docker run -d --rm --name ea-e2e-mailpit -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit:v1.21`), exports `SMTP_HOST=127.0.0.1 SMTP_PORT=1025 APP_URL=http://localhost:3000` to API and worker, runs `python cli.py add-admin admin@e2e.test`, exports `E2E_ADMIN_EMAIL`/`E2E_PY`/`DATABASE_URL` for Playwright, and stops Mailpit at the end.
- `mailpit.ts`: `waitForMail(to: string, subject: RegExp, timeoutMs = 30000)` via `GET http://127.0.0.1:8025/api/v1/search?query=to:"<to>"`; `clearMail()`.
- `stack.ts`: `invite(email)` (runs `$E2E_PY cli.py invite <email>`), `schedulerOnce(env: Record<string,string>)` (runs `$E2E_PY -m server.scheduler --once` with extra env).
- `plan4.spec.ts` (serial, iPhone 14 profile): `invite-only sign-up joins the waitlist` (uninvited email → waitlist message); `admin invites from the waitlist` (admin signs up, `/admin` → Invites → Invite → mail "You're invited to Expense Agent" → that user signs up); `first sync sends an email` (connect fake Zepto with `123456` → mail "Zepto is connected"); `refresh limit` ("Refresh now · 3 left today" → three refreshes, each waited to done → "No refreshes left today", a direct 4th `POST` → 429 with the exact detail); `admin sees cost per user` (row for that user with worker minutes > 0 and a ₹ amount); `admin learn with approval` (admin connects fake Zepto, Sites → Learn → approval card shows `^https://fake\.zepto\.test/api/orders$` → Approve → job learned → Sites shows version 1 and Healthy); `budget pause` (`schedulerOnce({MONTHLY_BUDGET_INR: "0.01"})` → mail "Budget reached" to the admin; a freshly invited email's sign-up shows "Sign-ups are paused…"; then `schedulerOnce({})` restores `ok`); `no horizontal scroll on /admin`.

- [ ] **Step 1: Write the specs** (failing until the stack runs).
- [ ] **Step 2: Implement** the helpers, script and docs changes.
- [ ] **Step 3: Run** `bash scripts/e2e_local.sh` → Plan 3's `product.spec.ts` and `plan4.spec.ts` all green; `pytest -q` and `pnpm --dir web test` still green.
- [ ] **Step 4: Commit** — `git commit -m "test(e2e): invite-only sign-up, limits, notifications, admin learn and budget pause locally"`

---

## Self-review

- **Spec coverage:** daily sync + no duplicates + skip needs_login (T10); budget-paused 3-day syncs (T2 levels, T10 interval); 3 refreshes/app/day with 429 and UI remaining (T2, T9, T6); cart checks 10/day helper (T2, enforced in Plan 5); `usage` table written by worker (T8) and API (T9 `/ask`, T2 cart helper); unit costs and ₹5,000 in config (T2); 80 % email, 100 % pause of sign-ups/cart/auto-sync, manual refresh unaffected (T1 hook, T2, T9, T10); invites/waitlist and admin opening invites (T1, T9, T7); admin view with cost per user, site recipe health, invites, approvals and learn (T7, T9); `recipe_approvals` with diff, status, decided_at (T1, T5); learning only on admin accounts, POST additions approved, drift auto-update without new POSTs else unhealthy + paused + alert (T3, T5, T8); Web Push with VAPID from env + email fallback for first sync, needs_login and admin budget alerts (T4, T6, T8, T10); media blocking in sync (T3, T8); local-only testing with Mailpit and fake worker (T4, T11); RLS on `usage` (T1).
- **Type consistency:** `limits.LimitExceeded/BudgetPaused/to_http`, `recipe_admin.is_admin/admin_learner/make_approver/apply_learned/decide/is_paused`, `notify.enqueue/alert_admins/dispatch/format_inr`, `usage.record/today`, `Relay.bytes_total`, `sync.RecipeDrift`, `sync.learn(..., approve, preapproved, save)` are named identically in producer and consumer tasks; API response shapes are defined once (Tasks 6/7 "Consumes") and implemented in Task 9.
- **Ordering:** Task 1 alone first (all server tests need migration 0007); Wave A file sets are disjoint (`cli.py` only in T5, then T9 in Wave B; `docker-compose.yml`/`docs/deploy-local.md` T4 then T10); Wave B file sets are disjoint.
