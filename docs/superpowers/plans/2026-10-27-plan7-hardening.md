# Plan 7 of 7: Hardening and Pre-Deploy Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the product safe to deploy. Users can disconnect an app or delete their account, and a test proves
nothing remains. The PWA has a plain-words privacy page. Each site gets rate-limit backoff with a cool-down, plus an
error budget that pauses the site and alerts the admin. Users hear about session expiry (once per episode, plus one
reminder). The plan also closes every pre-deploy gap carried over from Plans 1–6, then finishes with a full local e2e
and an owner checklist. Nothing is deployed.

**Architecture:** Postgres stays the only coordination point. Migration 0010 adds job attempts and `run_after`, a
`site_backoff` table, `connections.needs_login_at`, `usage_erased` (so deleted users' spend still counts toward the
budget) and a `paused_changed_at` stamp on `recipes`. Three new pure-ish server modules hold the new rules:
`server/erase.py` (wipes and the "nothing remains" scan), `server/site_health.py` (backoff and error budget) and
`server/session.py` (needs-login transitions and reminders). The worker, API and scheduler call them. Every write a
worker makes for a job happens inside `jobs.owned(...)`, a transaction that locks the job row. As a result, a deleted
account, a disconnected app or a requeued job can never be resurrected by a browser that is still running.

**Tech Stack:** as Plans 3–6 (Python 3.12, FastAPI, psycopg 3, `cryptography`, Playwright-over-CDP Chrome; Next.js 15 +
TypeScript + Tailwind 3.4, Vitest, Playwright e2e; local Supabase; Bicep, az CLI). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md`. These parts are binding: Build order
item 7, "Data control", "Privacy page", Security ("Deletion: account deletion removes rows, profiles and blobs. A test
proves nothing remains", "Logs", "Chrome profiles", "Secrets"), Sync ("needs_login … the user is notified"), and Known
risks (per-site backoff, pausing a site).

**Precondition:** Plans 1–6 are merged and `bash scripts/e2e_local.sh` is green. If Plan 6's file is absent when this
plan is executed, assume it delivered the following: a `fill_cart` job type, worker
`JOB_TYPES = ("connect", "sync", "learn", "search", "fill_cart")`, a `cart` guard mode, a fill route that enqueues
`fill_cart`, and migration `0009_*.sql`. Grep for each one before relying on it. This plan uses:
- From Plan 4: `notify.enqueue/alert_admins/dispatch`, `recipe_admin.pause_site/is_paused`, `usage.today`,
  `budget.month_cost`, `scheduler.tick`, `admin.site_health`, `server/admin_api.py`, `config.SITE_NAMES`, Mailpit.
- From Plan 5: `jobs.enqueue(profile=)`, `jobs.search_profile`, `cart_results`, the worker's `_do_search`.
- From Plan 3: the web `components/ui/*`, `lib/api.ts`, `lib/jobs.ts`, the Accounts page and `ConsentSheet`.

## Global Constraints

- Strictly read-only toward delivery apps, except Plan 6's add-to-cart. Nothing in this plan clicks, posts or writes
  anything new on a delivery app.
- Deletion is complete: after `DELETE /account`, no row in any `public` or `auth` table contains the user's id or email
  (as text), and no blob key starts with `<user_id>/`. The only thing kept is the user's spend folded anonymously into
  `usage_erased` (per day, no id). Server logs carry no email or phone and expire with Log Analytics retention (30 days).
- Disconnecting an app removes the following for that app's Chrome profile group (`swiggy` covers `instamart`): the
  `connections` row, the orders and order items, the jobs, the consents and the encrypted profile blob. Nothing else is
  removed.
- New tunables live only in `server/config.py`, are read from env, and are set explicitly in `docker-compose.yml` and
  the Bicep env: `MAX_JOB_ATTEMPTS=3`, `BACKOFF_BASE_S=60`, `BACKOFF_MAX_S=3600`, `BACKOFF_RESET_H=24`,
  `ERROR_BUDGET_WINDOW_H=24`, `ERROR_BUDGET_MIN_JOBS=10`, `ERROR_BUDGET_MAX_FAILURE_PCT=30`, `NEEDS_LOGIN_REMIND_H=72`.
- Hosted `DATABASE_URL` (API, worker, scheduler, KEDA scaler) is the Supabase **session** pooler with `sslmode=require`,
  for example `postgresql://postgres.<ref>:<pw>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require`.
  Port 6543 (the transaction pooler, which breaks LISTEN/NOTIFY) is refused.
- Only the worker holds `PROFILE_MASTER_KEY`. The API gets `PROFILE_STORE_URL` (the store holds ciphertext only), so it
  can delete blobs, and `SUPABASE_JWKS_URL`.
- Never log OTPs, phone numbers, digit runs of 4 or more typed by a user, JWTs, cookies, emails or proxy credentials.
  Chrome inherits no secret environment variables.
- UI follows Plan 3's Global Constraints: background `#f6f7f9`, white cards with radius 14px, accent `#0a7c55`, ₹ with
  Indian grouping, no horizontal scroll at 375px, safe-area padding. Destructive actions use `#b42318` text on white,
  never a filled red button.
  - Frontend tasks invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1` before
    writing UI (Plan 3 R3).
  - After code review, each frontend task gets a separate design review: impeccable critique/audit, ui-ux-pro-max and
    design-taste-frontend-v1, with Playwright screenshots at 390×844 compared against the approved mockups and these
    constraints. Its findings enter that task's fix loop (Plan 3 R5).
- New tables have RLS on and give `anon`/`authenticated` nothing. `service_role` gets all.
- Each parallel implementer uses its own test database `ea_tN` on `127.0.0.1:54329`, for example
  `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/ea_t3 pytest -q`.
- Every existing test and every e2e spec (`product`, `plan4`, `plan5`, `plan6`) keeps passing.
- All work is committed on `main`. Never push, no AI attribution in commit messages, and no deploy: Bicep is only
  built with `az bicep build`, and `deploy.sh` is never run.

## Review Focus

- **A challenge page served with HTTP 403** (Cloudflare/Akamai/PerimeterX markers) must be treated as throttling. It
  must not be treated as "logged out": the connection is not marked `needs_login` and the user gets no "logged you out"
  notification. → T5 `test_challenge_403_is_throttle_not_session_expired`.
- **An account deleted while its sync is running.** The worker's later order writes, connection update and profile
  upload must all be dropped, so nothing reappears. → T12 `test_sync_finishing_after_account_deletion_writes_nothing`.
- **The admin resumes a site the error budget paused.** Failures from before the resume must not re-pause it at the next
  scheduler tick. → T7 `test_failures_before_resume_do_not_count`.
- **Disconnecting Swiggy** also wipes the Instamart orders and the shared profile, and leaves Zomato and other users
  untouched. → T11 `test_disconnect_swiggy_wipes_instamart_too`.
- **Sync jobs for a site in cool-down** are neither claimed nor counted by the KEDA scaler, so no replica stays awake
  for them, and the cool-down consumes no attempts. → T3 `test_claim_skips_cooling_site_sync_only`,
  T14 `test_keda_query_matches_python`.

## Spec rulings (made by this plan; record in the ledger)

Details live in the owning task; these lines are the decisions.

- **R1** Deletion is synchronous in the API: one transaction over every `public` table with `user_id` (discovered, so
  Plan 6 tables are covered), email-keyed rows, `*_by` actor columns and `auth.users` (SQL, no service-role key);
  blobs after commit with 3 tries; 503 + idempotent retry on blob failure; no tombstone. → T11.
- **R2** Deleted users' spend is folded per day into `usage_erased` and still counts toward the budget. → T11.
- **R3** Disconnect also deletes that app's consent (reconnect asks again); cart results and usage stay. → T11.
- **R4** Every worker write for a job runs in `jobs.owned` (job row `FOR UPDATE`); deletion and disconnect delete job
  rows first, so in-flight writes finish before them and later writes find no row. → T3, T12.
- **R5** Backoff is per host group (`same_profile`: swiggy ⇄ instamart) on 429 and challenge pages; a throttled sync
  is requeued with `run_after`, user-present jobs fail `site_busy`, search records `failed/blocked`; while cooling,
  sync is not claimed or scaled for, the API answers 503, search is `unavailable`. → T5, T7, T3, T11, T12, T14.
- **R6** Error budget per (site, kind): orders from failed `sync` jobs outside `NOT_SITE_FAULTS`, search from failed
  `cart_results`; the window never reaches before `recipes.paused_changed_at`; over budget → pause + admin email. → T7.
- **R7** `session.mark_needs_login` is the single session-expiry path: notify on transition only, one reminder per
  episode, deep link `/accounts?reconnect=<site>`; replaces Plan 4's inline notification. → T8, T12.
- **R8** Zomato recipes are templated (`{account}`, `{orders_page_url}`) and bound per user; `--force` is refused for
  `PER_ACCOUNT_SITES`. → T6, T12.
- **R9** Vault blobs carry a magic, a format version and a key check; profiles carry an owner canary; legacy blobs
  still restore. → T4.
- **R10** SIGTERM drain never requeues a sync while its browser may live; `interrupt_main` only under the drain lock
  for the current job; `_Drain.finish()` absorbs a pending interrupt. → T12.
- **R11** Logs mask digit runs (≥ 4 digits); the model's history keeps them (CAPTCHA answers). → T6.
- **R12** Owner decision D1 (key management): the master key moves to Key Vault behind a Container Apps Key Vault
  reference with HKDF per-user keys (default, built); per-user Key Vault envelope keys are documented as the
  alternative; nothing blocks on the owner. → T14, T15.

---

## Wave 0 (parallel; disjoint files)

### Task 1: Migration 0010

**Files:** Create `supabase/migrations/0010_hardening.sql`, `tests/server/test_schema_plan7.py`. Modify `tests/server/conftest.py` (`TABLES` += `site_backoff`, `usage_erased`).

**Interfaces (Produces):**
- `alter table jobs add column attempts int not null default 0, add column run_after timestamptz`; index `jobs_type_finished (type, site, finished_at)`.
- `alter table connections add column needs_login_at timestamptz`.
- `site_backoff(site text pk, level int not null default 0, until timestamptz, last_kind text check (last_kind in ('rate_limited','challenge')), events int not null default 0, updated_at timestamptz not null default now())`.
- `usage_erased(day date pk, proxy_mb double precision, ai_tokens bigint, worker_seconds double precision, syncs int, cart_checks int)`. All counters are `not null default 0`.
- `alter table recipes add column paused_changed_at timestamptz`. The trigger `recipes_paused_changed` (`before update`)
  sets it to `now()` when `paused` changes.
- RLS is enabled on both new tables, `revoke all … from anon, authenticated`, and `grant all … to service_role`.

- [ ] **Step 1: Write the failing tests**

```python
def test_jobs_have_attempts_and_run_after(pg): ...           # defaults 0 / NULL
def test_new_tables_hidden_from_users(pg, as_user): ...      # select site_backoff / usage_erased → InsufficientPrivilege
def test_pause_toggle_stamps_paused_changed_at(pg):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    pg.execute("update recipes set paused = true where site='zepto'")
    assert pg.execute("select paused_changed_at is not null from recipes").fetchone() == (True,)
def test_unrelated_recipe_update_keeps_stamp(pg): ...        # updating health_note leaves paused_changed_at unchanged
def test_backoff_kind_checked(pg): ...                       # last_kind 'x' → CheckViolation
```

- [ ] **Step 2: Run** `pytest tests/server/test_schema_plan7.py -v` → FAIL (no column `attempts`).
- [ ] **Step 3: Implement** the migration and the conftest change.
- [ ] **Step 4: Verify** with `npx supabase db reset`: migrations 0001–0010 apply. Then run `pytest -q` with `TEST_DATABASE_URL` set and unset → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(db): job attempts, site backoff, needs-login stamp, erased usage and pause stamp"`

### Task 2: Settings and hosted-environment checks

**Files:** Modify `server/config.py`, `tests/server/test_config.py`.

**Interfaces (Produces):**
- New `Settings` fields, with the env name equal to the upper-case field name and defaults as in Global Constraints:
  `max_job_attempts: int`, `backoff_base_s: int`, `backoff_max_s: int`, `backoff_reset_h: int`,
  `error_budget_window_h: int`, `error_budget_min_jobs: int`, `error_budget_max_failure_pct: int`,
  `needs_login_remind_h: int`.
- `settings(for_worker=False)` never loads `PROFILE_MASTER_KEY`: `profile_master_key` is `None` even when the env var is
  set. Only `for_worker=True` reads and validates it.
- `check_database_url(url: str, role: str) -> None`, where role is `"api"|"worker"|"scheduler"|"scaler"`. A local URL
  (`local_db.is_local_db`) passes. Otherwise it raises `ValueError` when `sslmode` is not one of
  `require|verify-ca|verify-full`, or when the port is 6543. The messages name the rule and never the URL.
- `check_hosted(s: Settings, role: str) -> None`. It runs `check_database_url`. For role `api` with a non-local
  database, it also requires `s.jwks_url`. It raises `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_api_settings_ignore_master_key(monkeypatch):
    monkeypatch.setenv("PROFILE_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    assert settings().profile_master_key is None and settings(for_worker=True).profile_master_key == b"k" * 32
def test_hardening_tunables_from_env(monkeypatch): ...       # MAX_JOB_ATTEMPTS=5 → 5; defaults 3/60/3600/24/24/10/30/72
@pytest.mark.parametrize("url,ok", [
    ("postgresql://postgres:pw@127.0.0.1:54329/postgres", True),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require", True),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres", False),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require", False),
])
def test_check_database_url(url, ok): ...
def test_check_database_url_error_never_contains_password(): ...   # "pw" not in str(exc)
def test_hosted_api_requires_jwks(): ...                     # non-local db, jwks_url "" → ValueError; set → passes
```

- [ ] **Step 2: Run** `pytest tests/server/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): hardening settings; API never loads the profile key; hosted DB and JWKS checks"`

---

## Wave A (parallel after Wave 0 is merged; disjoint files)

### Task 3: Queue: attempts cap, run_after, cool-down skip, write fence, one site-profile module

**Files:** Create `server/sites.py`, `tests/server/test_sites.py`. Modify `server/jobs.py`, `tests/server/test_jobs.py`.

**Interfaces (Produces):**
- `sites.SITE_PROFILE: dict[str, str]` is derived once from `browser.SITES` (`{site: cfg["profile"]}` where the profile
  differs from the site). It also defines `profile_for(site) -> str`, `db_site(site) -> str` (from `db_site`, default
  the site), `same_profile(site) -> tuple[str, ...]` (all sites that share its profile, sorted; `("instamart", "swiggy")`
  for either), and `CONNECTABLE = ("zepto", "blinkit", "swiggy", "zomato")`. `jobs.SITE_PROFILE` is
  `sites.SITE_PROFILE` (a re-export, not a copy).
- `jobs.JobGone(Exception)`. `jobs.owned(conn, job_id, worker_id) -> ContextManager[None]` opens a transaction and runs
  `SELECT 1 FROM jobs WHERE id=%s AND status='running' AND worker_id=%s FOR UPDATE`. When no row comes back it raises
  `JobGone`.
- `claim(...)` also requires `run_after IS NULL OR run_after <= now()`, and skips `type='sync'` rows whose site has
  `site_backoff.until > now()`. Its UPDATE sets `attempts = attempts + 1`.
- `requeue(conn, job_id, worker_id, run_after: datetime | None = None, max_attempts: int = 3) -> str | None` returns
  `"queued"`, `"failed"` (attempts ≥ max: `error='too_many_attempts'`, phone dropped, `finished_at` set), or `None` when
  the job is not ours.
- `requeue_stale(conn, older_than_s=600, max_attempts: int = 3) -> int`. The types in
  `NO_RETRY_TYPES = ("connect", "learn", "fill_cart")` fail with `worker_lost`. Others requeue unless they are at the
  cap (then `too_many_attempts`).
- `jobs.scaler_query(types: Sequence[str]) -> str` is the single source of the KEDA query. It counts
  `type = ANY(types)` rows that are either `running`, or `queued` with `run_after` due and not a cooling `sync`. It uses
  a literal `in ('a','b',…)` list and single-quoted strings only.

- [ ] **Step 1: Write the failing tests**

```python
def test_profile_mapping_single_source():
    assert jobs.SITE_PROFILE is sites.SITE_PROFILE and sites.profile_for("instamart") == "swiggy"
    hits = [p for p in Path("server").glob("*.py") if p.name != "sites.py" and '"instamart": "swiggy"' in p.read_text("utf-8")]
    assert hits == []
def test_same_profile_groups_swiggy_and_instamart(): ...
def test_claim_increments_attempts(pg): ...
def test_requeue_caps_attempts(pg): ...                      # 3rd claim + requeue → "failed", error too_many_attempts
def test_requeue_with_run_after_is_not_claimable_until_due(pg): ...
def test_claim_skips_cooling_site_sync_only(pg):
    pg.execute("insert into site_backoff(site, level, until) values ('zepto', 1, now() + interval '5 min')")
    jobs.enqueue(pg, A, "sync", "zepto"); jobs.enqueue(pg, B, "connect", "zepto", {"phone": "9"})
    assert jobs.claim(pg, "w", ("sync", "connect")).type == "connect"
    assert jobs.claim(pg, "w", ("sync",)) is None
    assert pg.execute("select attempts from jobs where type='sync'").fetchone() == (0,)
def test_owned_raises_when_job_requeued_or_deleted(pg): ...
def test_owned_blocks_concurrent_delete_until_commit(pg, pg2): ...   # pg holds owned(); pg2 delete waits (lock_timeout 200ms → LockNotAvailable)
def test_stale_fill_cart_is_failed_not_requeued(pg): ...
def test_scaler_query_counts_only_handled_due_jobs(pg): ...  # execute scaler_query(("sync",)) → excludes learn, future run_after, cooling sync
```

- [ ] **Step 2: Run** `pytest tests/server/test_jobs.py tests/server/test_sites.py -v` → FAIL.
- [ ] **Step 3: Implement.** Keep the existing `enqueue(profile=)` default by calling `sites.profile_for(site)`.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): job attempts cap, delayed requeue, cool-down skip, write fence and one site-profile map"`

### Task 4: Vault format version, key canary and owner check

**Files:** Modify `server/vault.py`, `tests/server/test_vault.py`.

**Interfaces (Produces):** Implement R9.
- Constants: `MAGIC = b"EAPV"`, `FORMAT_VERSION = 1`, `CANARY_NAME = ".ea-profile.json"`.
- Exceptions: `VaultKeyMismatch(VaultError)` and `ProfileOwnerMismatch(VaultError)`.
- `stamp(profile_dir: Path, user_id: str, profile: str) -> None` and
  `check_owner(profile_dir: Path, user_id: str, profile: str) -> None`. `check_owner` raises `ProfileOwnerMismatch`
  when the canary is missing or differs.
- `save_profile(...)` calls `check_owner` before packing, so `store.put` is never called on a mismatch.
- `restore_profile(...)` reads v1 or legacy. A wrong `key_check` raises `VaultKeyMismatch`. An unknown version raises
  `VaultError("unsupported vault format")`. After unpacking, a canary naming another user raises `ProfileOwnerMismatch`
  and the directory is wiped. A legacy blob with no canary is stamped.
- `delete_profile(store, user_id: str, profile: str) -> int` deletes exactly `blob_key(user_id, profile)`.

- [ ] **Step 1: Write the failing tests**: `test_v1_blob_layout` (starts with `b"EAPV\x01"`, round-trips),
  `test_legacy_blob_still_restores_and_is_stamped`, `test_wrong_master_key_is_key_mismatch`,
  `test_flipped_header_byte_fails_auth`, `test_unknown_version_rejected`,
  `test_save_refuses_other_users_profile` (stamped for B and saved as A → `ProfileOwnerMismatch`, store empty),
  `test_save_refuses_unstamped_dir`, `test_restore_with_foreign_canary_wipes_dir`,
  `test_delete_profile_removes_only_that_profile` (swiggy deleted, zomato kept).
- [ ] **Step 2: Run** `pytest tests/server/test_vault.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(vault): format version and key canary; refuse to save a profile stamped for another user"`

### Task 5: Chrome launch hygiene and throttle detection

**Files:** Modify `browser.py`, `sync.py`, `tests/test_seams.py`. Create `tests/test_throttle.py`.

**Interfaces (Produces):**
- `browser.CHROME_ENV_ALLOW`: `PATH, HOME, USER, LOGNAME, DISPLAY, TZ, LANG, LANGUAGE, LC_ALL, XDG_RUNTIME_DIR, XDG_CONFIG_HOME, XDG_CACHE_HOME, TMPDIR, FONTCONFIG_PATH, FONTCONFIG_FILE, SYSTEMROOT, WINDIR, COMSPEC, PATHEXT, LOCALAPPDATA, APPDATA, USERPROFILE, TEMP, TMP, PROGRAMFILES, PROGRAMFILES(X86), PROGRAMDATA, NUMBER_OF_PROCESSORS, PROCESSOR_ARCHITECTURE`.
  Names are compared case-insensitively.
- `browser.chrome_env(base: Mapping[str, str] | None = None) -> dict[str, str]` returns only the allowed names.
  `open_site` passes `env=chrome_env()` to `Popen`.
- With `proxy_server`, `open_site` adds `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.
- `browser.SiteThrottled(Exception)` with `.kind` (`"rate_limited"|"challenge"`) and `.retry_after_s: int | None`.
- `browser.CHALLENGE_MARKERS` is a compiled case-insensitive regex for:
  `cf-chl|challenge-platform|cf_chl_opt|Attention Required! \| Cloudflare|px-captcha|_pxCaptcha|perimeterx|captcha-delivery|datadome|Access Denied.{0,200}Reference #|Request unsuccessful\. Incapsula`.
- `browser.classify_block(status: int, text: str) -> str | None` returns `"rate_limited"` for 429, `"challenge"` for
  any status whose body matches the markers, and otherwise `None`.
- `browser.parse_retry_after(value: str | None, now: datetime | None = None) -> int | None` accepts seconds or an
  HTTP-date and caps the result at 3600.
- `fetch_in_page`: `_FETCH_JS` also returns `retryAfter` (the `retry-after` header). `classify_block` is checked
  **before** the 401/403 → `SessionExpired` rule.
- `browser.raise_if_blocked(page) -> None` checks the page title plus the first 20,000 characters of
  `document.documentElement.outerHTML` against the markers.
- `sync.fetch_orders` calls `raise_if_blocked` after the first `goto`. `replay` raises `SiteThrottled("rate_limited")`
  when a captured hit has status 429. Both re-raise `SiteThrottled` without falling through to replay or drift.

- [ ] **Step 1: Write the failing tests**

```python
def test_chrome_env_drops_secrets(monkeypatch):
    for k in ("DATABASE_URL", "OPENAI_API_KEY", "PROFILE_MASTER_KEY", "PROXY_URL_TEMPLATE", "SUPABASE_JWT_SECRET"):
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("DISPLAY", ":99"); monkeypatch.setenv("TZ", "Asia/Kolkata")
    env = browser.chrome_env()
    assert "secret" not in env.values() and env["DISPLAY"] == ":99" and env["TZ"] == "Asia/Kolkata"
def test_open_site_passes_scrubbed_env_and_webrtc_flag(monkeypatch, tmp_path): ...   # stub Popen records env + args
def test_classify_429_is_rate_limited(): ...
def test_challenge_403_is_throttle_not_session_expired(): ...   # fetch_in_page with status 403 + "cf-chl" body → SiteThrottled("challenge")
def test_plain_403_is_still_session_expired(): ...
def test_parse_retry_after_seconds_and_date(): ...
def test_fetch_orders_propagates_throttle_without_replay(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/test_throttle.py tests/test_seams.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green (the CLI tests are unchanged).
- [ ] **Step 5: Commit**: `git commit -m "feat(engine): scrubbed Chrome environment, WebRTC leak flag, rate-limit and challenge detection"`

### Task 6: Digit-run log masking and per-account (Zomato) recipe templating

**Files:** Modify `agent.py`, `recipes.py`, `cli.py` (`publish-recipe`), `server/recipe_admin.py` (`sanitize_for_sharing`). Create `tests/test_log_masking.py`, `tests/test_recipe_template.py`.

**Interfaces (Produces):**
- `agent.DIGIT_RUN = re.compile(r"\d(?:[ -]?\d){3,}")`.
- `agent.mask_log(text: str, secrets: dict[str, str]) -> str` = `DIGIT_RUN.sub("<DIGITS>", mask(text, secrets))`.
  `Discovery.record` and the per-step `log.info` line use it. `ask_user`'s history line keeps `mask(...)` (R11).
- `recipes.PER_ACCOUNT_SITES = ("zomato",)` and `recipes.TemplateError(ValueError)`.
- `recipes.templatize(recipe: dict) -> dict` (R8) does three things:
  - The account prefix is `re.match(r"^/users/[^/]+", path(orders_page_url))`. Every string value containing it gets
    `{account}` in its place.
  - `orders_page_url` becomes `"{orders_page_url}"`.
  - The `nav_trace` steps through the first one whose `url_after` is the orders-page path are replaced by
    `{"action": "navigate", "url": "{orders_page_url}"}`.
  - A recipe without that prefix is returned unchanged.
- `recipes.bind(recipe: dict, orders_page_url: str | None) -> dict` substitutes both placeholders. A templated recipe
  with a missing or non-matching URL raises `TemplateError`. A recipe without placeholders is returned as is.
- `cli.py publish-recipe` templatizes a site in `PER_ACCOUNT_SITES` first. If that site is still flagged, or if
  `--force` is passed for it, the command exits 1 with
  `"zomato recipes are per-account: publish refuses --force; re-learn so the orders page can be templated."`.
- `recipe_admin.sanitize_for_sharing` templatizes `PER_ACCOUNT_SITES` before flagging.

- [ ] **Step 1: Write the failing tests**

```python
def test_mask_log_hides_digit_runs_and_secrets():
    assert agent.mask_log("otp 482910, call 98765 43210 or 2", {"PHONE": "9999999999"}) == "otp <DIGITS>, call <DIGITS> or 2"
def test_record_masks_user_answer_digits(tmp_path): ...       # ask_user answer "it's 4821" → jsonl has <DIGITS>; history keeps "4821"
def test_templatize_zomato_fixture(): ...                     # neutral slug test-user-123456789; _account_specific(result) == []
def test_templatize_drops_name_clicks_before_orders_page(): ...  # no step with target text of the admin's name remains
def test_bind_uses_each_users_orders_page(): ...              # two users → two different url_after paths
def test_bind_without_orders_page_raises(): ...
def test_publish_recipe_refuses_force_for_zomato(monkeypatch): ...   # exit code 1, message exact, nothing written
def test_sanitize_for_sharing_templates_zomato(): ...
```

- [ ] **Step 2: Run** `pytest tests/test_log_masking.py tests/test_recipe_template.py tests/server/test_recipe_cloud.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(recipes): template per-account Zomato recipes and refuse --force; mask digit runs in agent logs"`

### Task 7: Site health: backoff and error budget

**Files:** Create `server/site_health.py`, `tests/server/test_site_health.py`.

**Interfaces (Produces):** These implement R5 and R6.

**Backoff:**
- `SiteCooling(Exception)(site, until: datetime, message: str)`.
- `record_throttle(conn, site, kind, retry_after_s, s, now=None, rand=random.random) -> datetime` upserts every
  `sites.same_profile(site)` key with `level`, `until`, `last_kind`, `events + 1`, and returns `until`.
- `record_success(conn, site) -> None` sets `level=0, until=NULL` for the group, but only where the level is above 0.
- `cooldown_until(conn, site, now=None) -> datetime | None`.
- `raise_if_cooling(conn, site, now=None) -> None` raises `SiteCooling` with the message
  `f"{App} is limiting requests right now. Try again in about {ceil(minutes)} minutes."`.
- `to_http(e: SiteCooling) -> HTTPException` returns 503 with an integer `Retry-After`.

**Error budget:**
- `NOT_SITE_FAULTS` holds the R6 errors.
- `failure_stats(conn, now, window_h) -> list[dict]` returns dicts with `site, kind, total, failed, pct`, where `pct`
  is an int rounded down.
- `check_error_budget(conn, s, now) -> list[tuple[str, str]]` runs `recipe_admin.pause_site(conn, site, kind, note)`
  with `note = f"Paused automatically: {failed} of {total} {'syncs' if kind == 'orders' else 'price checks'} failed in the last {h} hours."`.
  It also runs
  `notify.alert_admins(kind="error_budget", title=f"{App} {'sync' if kind == 'orders' else 'prices'} paused: too many failures", body=note + " Check it in the admin view, then resume it.", dedupe_key=f"error_budget:{site}:{kind}:{usage.today(now)}")`.
  It skips sites that are already paused and sites with no recipe row, and returns the pairs it paused.
  If Plan 5's Food mode is present, food `cart_results` (swiggy/zomato) are grouped under the recipe kind that produced
  them (`menu`), so a failing food search pauses `menu`, never the `orders` recipe.

- [ ] **Step 1: Write the failing tests**: `test_backoff_doubles_and_caps` (levels 1–4 →
  60/120/240/480 s with `rand=lambda: 0`; level 8 → 3600), `test_retry_after_wins_when_longer`,
  `test_level_resets_after_quiet_period` (last event 25 h ago → level 1),
  `test_throttle_on_instamart_cools_swiggy`, `test_success_resets_level`,
  `test_raise_if_cooling_message_and_retry_after`,
  `test_error_budget_pauses_and_alerts_once` (10 syncs, 4 failed `RuntimeError: …` → paused, one alert row per admin;
  a second call adds nothing), `test_user_caused_failures_do_not_count` (`needs_login`, `worker_lost`),
  `test_below_min_jobs_never_pauses`, `test_failures_before_resume_do_not_count` (pause, resume at T, 10 failures
  before T and 2 after → not paused), `test_search_budget_counts_failed_results_only`.
- [ ] **Step 2: Run** `pytest tests/server/test_site_health.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): per-site rate-limit backoff and an error budget that pauses and alerts"`

### Task 8: Session expiry: one transition, one reminder

**Files:** Create `server/session.py`, `tests/server/test_session.py`.

**Interfaces (Produces):**
- `mark_needs_login(conn, user_id: str, site: str, *, now: datetime | None = None) -> bool`. The `site` argument is
  `sites.db_site(site)`. The function does the following in one statement-safe transaction:
  - `UPDATE connections SET status='needs_login', needs_login_at=now WHERE … AND status <> 'needs_login' RETURNING needs_login_at`.
    This works for an existing row only. When there is no row it returns False.
  - On the transition, it deletes this connection's `queued` `sync` jobs for `same_profile(site)`.
  - It enqueues the notification with
    `kind="needs_login", title=f"{App} logged you out", body="Reconnect to keep your spending up to date.", url=f"/accounts?reconnect={site}", dedupe_key=f"needs_login:{user_id}:{site}:{int(needs_login_at.timestamp())}"`.
  - It returns True only on the transition.
- `remind(conn, s, now) -> int`. For connections in `needs_login` with `needs_login_at < now − needs_login_remind_h`, it
  enqueues
  `kind="needs_login_reminder", title=f"{App} is still logged out", body="Your spending stops updating until you reconnect. It takes a minute.", url=f"/accounts?reconnect={site}", dedupe_key=f"needs_login_reminder:{user_id}:{site}:{int(needs_login_at.timestamp())}"`
  and returns the number of rows newly enqueued.

- [ ] **Step 1: Write the failing tests**: `test_first_expiry_notifies_with_deep_link`,
  `test_second_expiry_same_episode_is_silent`, `test_new_episode_after_reconnect_notifies_again`
  (status → connected and `needs_login_at` NULL, then expire → second row), `test_queued_syncs_dropped_on_transition`,
  `test_reminder_once_after_72h` (71 h → 0; 73 h → 1; again → 0), `test_no_connection_row_is_noop`.
- [ ] **Step 2: Run** `pytest tests/server/test_session.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): session-expiry transition with one notification and one reminder per episode"`

### Task 9: PWA privacy page, disconnect and delete account

**Files:**
- Create `web/app/privacy/page.tsx`, `web/components/account/{DisconnectSheet,DeleteAccountSheet,DataCard}.tsx`,
  `web/lib/account.ts`, `web/__tests__/privacy.test.tsx`, `web/__tests__/dataControls.test.tsx`.
- Modify `web/middleware.ts` (`/privacy` is public), `web/app/(auth)/login/page.tsx`,
  `web/components/connect/ConsentSheet.tsx`, `web/app/(app)/accounts/page.tsx`.

**Interfaces:**
- **API consumed (built in Task 11):**
  - `POST /connections/{site}/disconnect` → 200 `{site, orders_deleted}`.
  - `DELETE /account` with body `{confirm: "DELETE"}` → 200 `{deleted: true}`, or 503 with `detail`.
  - Cool-down 503 `detail`s are shown verbatim.
- **`lib/account.ts`:**
  - `disconnectApp(site): Promise<{orders_deleted: number}>`.
  - `deleteAccount(): Promise<void>`. It retries a 503 up to 3 times, 2 s apart. Then it runs `disablePush()` (Plan 4)
    and `supabase.auth.signOut()`, and navigates to `/login?deleted=1`.
- **The `/privacy` page** is static, readable signed out, styled with the Plan 3 card look, and has a back link. It
  has the title "Privacy", the line "Updated 27 October 2026", and these sections with this exact copy:
  1. **What we do**: "Expense Agent reads your order history and the prices of groceries you ask about. We never touch payment: we can't see or use your cards, UPI or wallets, and we never place an order."
  2. **What we read**: "Your past orders on the apps you connect (Zepto, Blinkit, Swiggy with Instamart, Zomato): date, shop or restaurant, items, amounts and status. For price checks, live prices at your delivery area."
  3. **What we store**: "Your orders, your delivery address and its map position, and an encrypted copy of each app's sign-in session so you don't need a new code every day. We never store the one-time codes you type. Your phone number is kept only while a sign-in is running."
  4. **Filling a cart**: "Only when you tap \"Fill cart\", we add the items you chose to that app's cart. We never empty your cart, check out, pay, apply coupons or change addresses. You pay in the app yourself."
  5. **Who sees it**: "Only you. We never sell or share your data. The admin view shows cost and usage per account, not your orders."
  6. **The risk**: "Delivery apps may flag or briefly lock an account that is used automatically. We keep requests slow and back off when an app pushes back, but we can't rule it out."
  7. **Your controls**: "Disconnect an app on the Accounts tab to delete its session and orders. Delete your account to remove everything, including your sign-in. Deletion is immediate; server logs keep only an anonymous id and expire within 30 days."
- **Links to `/privacy`:** a "Privacy" link on the login page, "How we handle your data" in `ConsentSheet`, and a
  "Privacy" row on Accounts.
- **Accounts page:**
  - `?reconnect=<site>` opens that site's connect flow at the phone step (consent already stored), or at the consent
    sheet when consent is missing.
  - `DataCard` ("Your data") holds a "Disconnect" row per connected app and a "Delete account" row.
- **`DisconnectSheet`:**
  - Title: "Disconnect {App}?"
  - Body: "We'll delete your {App} sign-in session and the {n} {App} orders we stored. Your {App} account itself isn't
    touched. You can connect again any time." For Swiggy, append: "Instamart orders go too, because they come through
    your Swiggy sign-in."
  - Buttons: "Disconnect {App}" (destructive) and "Cancel".
  - Done: a toast "{App} disconnected. {n} orders deleted."
- **`DeleteAccountSheet`:**
  - Title: "Delete your account?"
  - Body: "This deletes everything we hold for you: app sessions, every order, price checks, notifications and your
    sign-in. It can't be undone. Your delivery-app accounts aren't touched."
  - Field label: "Type DELETE to confirm". The button "Delete my account" is enabled only when the field is exactly
    `DELETE`.
- **Login with `?deleted=1`** shows "Your account and all its data are deleted."

- [ ] **Step 1: Invoke** `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`.
- [ ] **Step 2: Write the failing tests**:
  - `privacy page renders all seven sections and says we never touch payment`
  - `privacy is reachable signed out` (middleware matcher test)
  - `consent sheet links to privacy`
  - `disconnect sheet shows the order count and the Instamart note for Swiggy`
  - `disconnect posts and shows the toast`
  - `delete button stays disabled until DELETE is typed`
  - `delete retries a 503 then signs out to /login?deleted=1`
  - `reconnect query opens the phone step`
  - `login shows the deleted message`
- [ ] **Step 3: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** `pnpm --dir web test`, `pnpm --dir web lint` and `pnpm --dir web build` → all pass. `/privacy` and `/accounts` at 390×844 have no horizontal scroll.
- [ ] **Step 6: Commit**: `git commit -m "feat(web): privacy page, disconnect an app and delete account"`
- [ ] **Step 7: Code review, then a separate design review** (R5). Fix the findings and commit.

### Task 10: PWA re-login banner and admin site health

**Files:** Create `web/components/account/ReloginBanner.tsx`, `web/__tests__/relogin.test.tsx`, `web/__tests__/adminSites.test.tsx`. Modify `web/app/(app)/layout.tsx`, `web/components/admin/SitesPanel.tsx`, `web/lib/admin.ts` (types).

**Interfaces:**
- **Banner:** reads `connections(site, status)` through the Supabase client and stays live through `watchConnections`
  (Plan 3). For `needs_login` rows it shows "{App} logged you out." or "{App1} and {App2} logged you out.", with a
  "Reconnect" link to `/accounts?reconnect=<first site>`.
  - It sits under the safe-area top and above the content on every tab.
  - It can't be dismissed, and it is hidden when no connection needs login.
- **`GET /admin/sites`** gains `cooldown_until: string | null`, `backoff_level: number`, `failures_window: number` and
  `jobs_window: number` (Task 11).
- **`SitesPanel` adds:**
  - An orange badge "Cooling down until {HH:mm} · level {n}" while `cooldown_until` is in the future.
  - The line "Failures ({h} h): {failures} of {jobs} ({pct}%)".
  - A note starting "Paused automatically" shown under the Paused badge. Resume is unchanged from Plan 4.

- [ ] **Step 1: Invoke** the three design skills.
- [ ] **Step 2: Write the failing tests**: `banner lists logged-out apps and links to reconnect`,
  `banner hidden when all connected`, `banner updates live` (simulated Realtime change),
  `cooling badge shows until time and level`, `failure line shows pct`, `auto-pause note visible`.
- [ ] **Step 3: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** tests, lint and build → pass. `/spend` with the banner and `/admin` at 390×844 have no horizontal scroll.
- [ ] **Step 6: Commit**: `git commit -m "feat(web): re-login banner and admin site cool-down and error-rate display"`
- [ ] **Step 7: Code review, then a separate design review** (R5). Fix the findings and commit.

---

## Wave B (parallel after Wave A is merged; disjoint files)

### Task 11: Erase, the data-control API and cool-down refusals

**Files:**
- Create `server/erase.py`, `tests/server/test_erase.py`, `tests/server/test_api_plan7.py`.
- Modify:
  - `server/api.py`: the routes, plus a lifespan step `config.check_hosted(settings(), "api")`.
  - `server/admin.py` (`site_health` fields) and `server/budget.py` (`month_cost` + `usage_erased`).
  - The API module that holds Plan 5's cart-check route and Plan 6's fill route (find it with
    `grep -n "jobs.enqueue" server/*.py`).
  - `cli.py` (`verify-erased`) and `tests/server/helpers.py` (`seed_everything(conn, user_id, email) -> None`: one row
    in every user-keyed table, plus a blob).

**Interfaces (Produces):**
- `erase.user_tables(conn) -> list[str]`: tables in `public` with a `user_id` column, children before parents
  (from `pg_constraint`).
- `erase.delete_account(conn, store, user_id: str) -> dict`, returning `{"rows": int, "blobs": int}`. It implements R1
  and R2 in one transaction:
  - Lock the job rows (`FOR UPDATE`), fold the user's usage into `usage_erased`, then delete the user tables.
  - Delete the email rows of `admins`, `invites`, `waitlist` and `notifications` (by `lower(email)`, the email read from
    `auth.users`).
  - Set `invited_by`/`decided_by`/`updated_by` (any `uuid` column named `*_by`) to NULL.
  - Run `delete from auth.users`.
  - After commit, run `vault.delete_user_profiles` with 3 tries. Failure raises `BlobDeleteFailed`. A second call with
    no rows left only deletes blobs.
- `erase.disconnect_site(conn, store, user_id, site) -> dict`, returning `{"orders_deleted", "blobs"}`. It applies R3:
  - In one transaction, lock and delete the jobs where `site = ANY(same_profile(site))`, then delete the orders, order
    items, connection and consents for `db_site(site)`.
  - Then delete the profile blob with `vault.delete_profile(store, user_id, profile_for(site))`.
- `erase.remaining(conn, store, user_id, email) -> list[str]` returns every `schema.table` in `public` and `auth` with a
  row whose `t::text` contains the id or the email, plus `blob:<key>` for any key under `<user_id>/`. An empty list
  means nothing remains.
- **Routes:**
  - `POST /connections/{site}/disconnect` returns 200 `{site, orders_deleted}`. It answers 400 `"unknown site"`, and for
    `instamart` 400 `"Instamart is part of your Swiggy connection."`. It is idempotent.
  - `DELETE /account` with body `{confirm: Literal["DELETE"]}` (422 otherwise) returns 200 `{deleted: true}`. A
    `BlobDeleteFailed` gives 503 `"Your data is deleted, but stored sessions couldn't be removed yet. Tap Delete again."`.
  - Both routes use `vault.store_from_url(settings().profile_store_url)` and never the master key.
- **Cool-down refusals:** every route that enqueues `connect`, a manual `sync` or `fill_cart` calls
  `site_health.raise_if_cooling` first and maps it through `site_health.to_http`. These checks run after the existing
  404/409 checks and before any limit is consumed.
- `admin.site_health` rows gain `cooldown_until`, `backoff_level`, `failures_window` and `jobs_window` (from
  `failure_stats` with `error_budget_window_h`).
- `budget.month_cost` adds the month's `usage_erased`.
- `python cli.py verify-erased <user_id> <email>` prints `NOTHING REMAINS` and exits 0, or prints the remaining entries
  and exits 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_delete_account_leaves_nothing(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, "a7@e2e.test"); helpers.seed_everything(pg, B, "b7@e2e.test")
    erase.delete_account(pg, store, A)
    assert erase.remaining(pg, store, A, "a7@e2e.test") == []
    assert erase.remaining(pg, store, B, "b7@e2e.test") != []          # B untouched
def test_user_tables_cover_every_user_id_table(pg): ...          # equals information_schema list (incl. Plan 5/6 tables)
def test_deleted_usage_still_counts_toward_budget(pg, s): ...    # month_cost unchanged by the deletion
def test_delete_account_waits_for_in_flight_write(pg, pg2): ...  # owned() on pg2 → delete blocks until commit, then removes the new orders
def test_delete_account_blob_failure_is_retryable(pg, tmp_path): ...   # store.delete_prefix raises 3× → BlobDeleteFailed; 2nd call → blobs 1
def test_disconnect_swiggy_wipes_instamart_too(pg, tmp_path): ...  # swiggy+instamart orders, jobs, consents, swiggy blob gone; zomato and B kept
def test_disconnect_keeps_cart_results_and_usage(pg, tmp_path): ...
def test_disconnect_and_delete_routes(client, token_a, pg, monkeypatch): ...   # 200 shapes, 422 without DELETE, instamart 400
def test_connect_refresh_fill_refused_while_cooling(client, token_a, pg): ...  # 503, detail exact, Retry-After int, no limit consumed
def test_api_startup_rejects_hosted_db_without_sslmode(monkeypatch): ...
def test_admin_sites_reports_cooldown_and_failures(client, admin_token, pg): ...
def test_cli_verify_erased(pg, tmp_path, monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_erase.py tests/server/test_api_plan7.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Verify on real local Supabase.** Run `npx supabase db reset` and start the API on the local Supabase DB.
  Sign up an invited user and `DELETE /account` with their token → 200. Then:
  - `select count(*) from auth.identities where user_id = '<id>'` → 0.
  - `python cli.py verify-erased <id> <email>` → `NOTHING REMAINS`.
  - Signing in with that email fails.
  Then run `pytest -q` with `TEST_DATABASE_URL` set and unset → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): account deletion and app disconnect that leave nothing behind; cool-down refusals"`

### Task 12: Worker hardening

**Files:** Modify `server/worker.py`, `server/fake_sites.py`. Create `tests/server/test_worker_plan7.py`. Modify `tests/server/test_worker.py` (drain tests).

**Interfaces:**
- **Consumes:** `jobs.owned/JobGone/requeue(run_after, max_attempts)`, `sites.*`, `vault.stamp/VaultError`,
  `browser.SiteThrottled`, `recipes.bind/TemplateError`, `site_health.record_throttle/record_success/cooldown_until`,
  `session.mark_needs_login`, `config.check_hosted`.
- **Produces:**
  - `main()` calls `check_hosted(s, "worker")` after `settings(for_worker=…)`. The profile name comes from
    `sites.profile_for`, `db_site` from `sites.db_site`, and `jobs.requeue_stale(conn, s.stale_job_s, s.max_job_attempts)`.
  - **Profile.** After restore, or on a fresh directory, run `vault.stamp(profile_dir, job.user_id, profile_name)`.
    `save_profile` runs inside `jobs.owned(conn, job.id, job.worker_id)`: `vault.save_profile` and then
    `set_connection(profile_blob_key=…)`. `JobGone` is logged as `"job %s no longer ours; profile not saved"` and
    skipped.
  - **Writes.** `_do_sync`'s `upsert_orders` and `set_connection`, and the connect success update (which also sets
    `needs_login_at=None`), run inside `jobs.owned`. `JobGone` ends the job quietly without calling `jobs.finish`.
  - **Recipes.** The `recipe_loader` for orders returns
    `recipes.bind(store.get_recipe(conn, site), own.get("orders_page_url"))`.
  - **Expiry.** `SessionExpired`, an unusable vault on a `sync`, and `TemplateError` all run
    `session.mark_needs_login(conn, job.user_id, db_site)` and then fail the job with `needs_login`. This replaces
    Plan 4's inline needs_login notification. Plan 4's `notify.dispatch` call after it stays.
  - **Throttle** (R5). A sync runs `until = record_throttle(...)`, then `jobs.requeue(conn, job.id, worker_id,
    run_after=until, max_attempts=s.max_job_attempts)`, with progress `stage="cooling_down", until`. A
    `connect`/`learn`/`fill_cart` fails with `site_busy` (connection `error` if it was `syncing`). A `search` writes
    Plan 5's `failed`/`blocked` result.
    - A search job whose site is cooling records `unavailable` with error `cooling_down` without opening a browser.
    - A successful sync or search runs `record_success(conn, job.site)`.
  - **Drain** (R10).
    - `_Drain._watch`: for `sync` jobs it only sets `aborted` and interrupts. For the other types it fails the job first,
      as today.
    - `interrupt_main` is called under `self._lock` only when `self._job is job and not self.stopped.is_set()`.
    - `_Drain.finish()` sets `stopped` under the lock and swallows a `KeyboardInterrupt` raised while doing so when
      `aborted`. `main`'s `finally` calls it.
    - The main thread's `KeyboardInterrupt` handler runs `_abort_on_shutdown` for a sync (requeue) after `run_job` has
      unwound, so Chrome has already exited.
  - **Fake mode.** `fake_sites.fetch_orders(site, since, orders_page_url=None, **_)` raises
    `browser.SessionExpired("fake")` when `orders_page_url` ends with `/expired`, and
    `browser.SiteThrottled("rate_limited", 60)` when it ends with `/throttle`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_finishing_after_account_deletion_writes_nothing(pg, tmp_path, monkeypatch): ...
    # stub fetch deletes the account (erase.delete_account on a second connection) before returning orders
    # → no orders/connection rows for A, no blob under A/, job row gone, no exception escapes run_job
def test_requeued_job_does_not_save_profile(pg, tmp_path, monkeypatch): ...      # job requeued mid-run → store.put never called
def test_save_profile_stamps_and_checks_owner(pg, tmp_path, monkeypatch): ...
def test_zomato_sync_binds_users_orders_page(pg, monkeypatch): ...
def test_zomato_without_orders_page_is_needs_login(pg, monkeypatch): ...
def test_session_expired_notifies_once_across_two_jobs(pg, monkeypatch): ...     # two failing syncs → one notification row
def test_throttled_sync_requeues_with_run_after_and_cools_site(pg, monkeypatch): ...
def test_throttled_connect_fails_site_busy(pg, monkeypatch): ...
def test_cooling_search_is_unavailable_without_browser(pg, monkeypatch): ...
def test_success_resets_backoff(pg, monkeypatch): ...
def test_drain_does_not_requeue_sync_before_unwind(pg, monkeypatch): ...  # watchdog fires while run_job blocks → row still running under worker until unwind
def test_watchdog_never_interrupts_after_main_returned(monkeypatch): ...  # interrupt_main stub records 0 calls
def test_pending_interrupt_absorbed_by_finish(): ...                       # _thread.interrupt_main(); drain.aborted=True; drain.finish() → no exception
def test_fake_expired_and_throttle_urls(pg, fake_mode): ...
def test_worker_refuses_hosted_db_without_sslmode(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_worker_plan7.py tests/server/test_worker.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "fix(worker): fence writes by job ownership, back off throttled sites, one session-expiry path, safe drain"`

### Task 13: Scheduler runs the error budget and reminders

**Files:** Modify `server/scheduler.py`, `tests/server/test_scheduler.py`.

**Interfaces:**
- `tick` (inside its advisory lock, after enqueueing and before `notify.dispatch`) calls
  `site_health.check_error_budget(conn, s, now)` and `session.remind(conn, s, now)`. The result dict gains
  `"paused_sites": [[site, kind], …]` and `"reminders": int`.
- `due_connections` skips sites whose `cooldown_until` is in the future.
- `main` calls `config.check_hosted(s, "scheduler")`.

- [ ] **Step 1: Write the failing tests**: `test_tick_pauses_site_over_error_budget_and_emails_admins`,
  `test_tick_sends_needs_login_reminder_once`, `test_tick_skips_cooling_site`, `test_tick_result_has_new_keys`.
- [ ] **Step 2: Run** `pytest tests/server/test_scheduler.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit**: `git commit -m "feat(server): scheduler enforces the site error budget and sends needs-login reminders"`

### Task 14: Deploy readiness (Bicep, deploy.sh, worker image, compose, docs)

**Files:**
- Modify `deploy/azure/main.bicep`, `deploy/azure/deploy.sh`, `deploy/azure/guard.sh`, `deploy/azure/test_guard.sh`,
  `deploy/azure/env.deploy.example`, `docker/worker.Dockerfile`, `docker-compose.yml`, `docs/deploy-azure.md`.
- Create `docker/chrome-policies/webrtc.json`, `tests/server/test_deploy_config.py`.

**Interfaces:**
- **Bicep** (`main.bicep`):
  - **API app:** no `profile-master-key` secret and no `PROFILE_MASTER_KEY` env. It gets `PROFILE_STORE_URL`
    (secretRef `profile-store-url`) and `SUPABASE_JWKS_URL` (new param `supabaseJwksUrl`).
  - **Hardening env:** the Global Constraints tunables go on the API, the worker and Plan 4's scheduler job.
  - **Worker app:** env `TZ=Asia/Kolkata`.
  - **Ask reader (Plan 3 carry-over):** API app gets `ASK_DATABASE_URL` (secretRef `ask-database-url`, the `ask_reader`
    login from migration 0006 via the session pooler with `sslmode=require`); `docker-compose.yml` sets it too, with the
    compose migrate step setting a local `ask_reader` password. `test_deploy_config.py` asserts both are wired, and
    `/ask` returns 200 (not 503) in `scripts/smoke_compose.sh`.
  - **KEDA query:** exactly `jobs.scaler_query(worker.JOB_TYPES)`.
  - **Key Vault** (R12): a `Microsoft.KeyVault/vaults@2023-07-01` resource named `${prefix}-kv-${take(uniq, 6)}`, with
    RBAC authorization, soft delete and purge protection. It holds the secret `profile-master-key`, created from the
    secure param, and the managed identity gets the role "Key Vault Secrets User"
    (`4633458b-17de-408a-b874-0445c86b69e6`).
    - The worker's `profile-master-key` secret becomes `{ name: 'profile-master-key', keyVaultUrl: <secretUri>, identity: identity.id }`.
  - **Log Analytics:** `workspaceCapping: { dailyQuotaGb: json('0.2') }`.
- **`guard.sh`:** `git_tree_clean <dir>` returns 0 only when `git -C <dir> status --porcelain` is empty (untracked files
  count as dirty).
  - `deploy.sh` exits 1 with `"refusing to deploy: the git tree has uncommitted or untracked changes"` before any `az`
    call.
  - It requires `SUPABASE_JWKS_URL`.
  - It validates `DATABASE_URL` with `config.check_database_url(…, "worker")`. The URL is passed through the
    environment, never through argv.
- **`env.deploy.example`:** the session-pooler URL example with `?sslmode=require` and the line
  `# never the transaction pooler (port 6543)`, plus `SUPABASE_JWKS_URL=https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`.
- **`worker.Dockerfile`:**
  - Install `tzdata` and set `ENV TZ=Asia/Kolkata`.
  - Copy `docker/chrome-policies/webrtc.json` (`{"WebRtcIPHandlingPolicy": "disable_non_proxied_udp"}`) to
    `/etc/opt/chrome/policies/managed/`.
- **`docker-compose.yml`:** the new tunables go explicitly into `x-app-env`, and the worker gets `TZ: Asia/Kolkata`.
- **`docs/deploy-azure.md` sections:**
  - "Database URL (session pooler, sslmode=require)", covering why not the direct host (IPv6-only) and why not 6543.
  - "Log Analytics cost": ingestion is billed per GB after any free allowance, the workspace is capped at 0.2 GB/day,
    retention is 30 days, the worker logs counts and ids only, and the owner should check the current price on the
    Azure pricing page.
  - "Owner decision D1: profile key management" (R12): option A (default, implemented) is the master key in Key Vault
    with HKDF per-user keys. Option B is per-user envelope keys in Key Vault. The doc gives B's cost and effort, notes
    that rotation is detected by the vault's key canary, and states that the build does not wait on D1.

- [ ] **Step 1: Write the failing tests**

```python
def test_api_container_has_no_master_key(): ...          # parse main.bicep text: the api block has no 'PROFILE_MASTER_KEY' / 'profile-master-key'
def test_api_gets_store_url_and_jwks(): ...
def test_worker_tz_and_key_vault_reference(): ...
def test_keda_query_matches_python():
    q = re.search(r"query: '(.*)'\n", BICEP).group(1).replace("\\'", "'")
    assert " ".join(q.split()) == " ".join(jobs.scaler_query(worker.JOB_TYPES).split())
def test_hardening_tunables_set_everywhere(): ...         # each env name appears in compose x-app-env and the Bicep api/worker/scheduler env
def test_compose_env_values_parse(): ...                  # settings() under compose values → defaults from Global Constraints
```
Add `test_guard.sh` cases: `clean repo` → 0, `untracked file` → 1, `modified file` → 1 (each in a `mktemp -d` git repo).

- [ ] **Step 2: Run** `pytest tests/server/test_deploy_config.py -v` and `bash deploy/azure/test_guard.sh` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Verify.** Each of these must pass:
  - `az bicep build --file deploy/azure/main.bicep` → exit 0.
  - `docker compose config -q` → 0.
  - `bash deploy/azure/test_guard.sh` → ALL PASS.
  - Build the worker image (`docker compose build worker`). Then `docker compose run --rm --entrypoint sh worker -c 'date +%Z; cat /etc/opt/chrome/policies/managed/webrtc.json'` prints `IST` and the policy.
  - `bash scripts/smoke_compose.sh` → SMOKE OK.
  - `pytest -q` → green.
  Do not run `deploy.sh`.
- [ ] **Step 5: Commit**: `git commit -m "build(deploy): API without the profile key, Key Vault reference, pooler URL checks, typed KEDA query, IST and WebRTC policy in the worker, clean-tree guard"`

---

## Wave C

### Carry-overs folded into Task 12 and Task 15 (controller, Plan 3 ledger)

- Task 12: make `tests/server/test_worker.py::test_heartbeat_runs_during_job` deterministic (drive the heartbeat by an
  injected clock or event, not a 1 s wall timer); it flaked under full-suite load.
- Task 15: PWA launch images also for iPhone SE (375×667 @2x), 16/17 Pro (402×874 @3x), Pro Max (440×956 @3x) and Air
  (420×912 @3x); `lib/supabase/middleware.ts` redirect keeps a `#hash` out of the pathname. Both get a test.

### Task 15: Full-product local e2e and the pre-deploy checklist

**Files:** Create `e2e/tests/plan7.spec.ts`, `docs/deploy-checklist.md`. Modify `e2e/helpers/stack.ts`
(`setOrdersPage(email, site, url)`, `insertFinishedJobs(site, n, failed, error)` for a throwaway user,
`verifyErased(userId, email)` running `$E2E_PY cli.py verify-erased`, `userId(email)`), `scripts/e2e_local.sh` (exports
the hardening tunables with `ERROR_BUDGET_MIN_JOBS=5`), `docs/e2e-local.md`, `docs/e2e-checklist.md` (pooler URL; a
"Plan 7 checks on a real account" section: disconnect, delete, `verify-erased`), `README.md` (data controls, tunables).

**Interfaces:** `plan7.spec.ts`, serial, iPhone 14 profile:
1. `privacy is public` — signed out `/privacy` shows "We never touch payment"; the login page links to it.
2. `session expiry` — connect fake Zepto (`123456`); `setOrdersPage(email, "zepto", "https://fake.zepto.test/expired")`;
   "Refresh now" → banner "Zepto logged you out.", Mailpit mail "Zepto logged you out"; "Reconnect" opens the phone
   step; a direct refresh → 409 `"Reconnect Zepto first."`.
3. `throttle cool-down` — reconnect; `setOrdersPage(…, "/throttle")`; refresh → admin Sites shows "Cooling down until";
   a direct refresh → 503, detail starts "Zepto is limiting requests", `Retry-After` present.
4. `error budget` — `insertFinishedJobs("blinkit", 5, 3, "RuntimeError: boom")`; `schedulerOnce({})` → admin mail
   "Blinkit sync paused: too many failures", Sites shows "Paused automatically: 3 of 5 syncs failed…"; Resume, then
   `schedulerOnce({})` → still healthy.
5. `disconnect` — normal orders page, sync, Disconnect Zepto → toast with the count; Spend empty state; Accounts shows
   "Connect ›" for Zepto.
6. `delete account` — connect again; Delete account, type `DELETE` → `/login` deleted message;
   `verifyErased(id, email)` prints `NOTHING REMAINS`; signing in again fails.
7. `no horizontal scroll` on `/privacy` and on `/accounts` with the data card open.

`docs/deploy-checklist.md` (owner; ordered checkboxes, each with a "verify" line): Supabase project in ap-south-1,
`npx supabase link` + `db push` applies 0001–0010, auth hook on, Google provider, `site_url` and redirects, Realtime
publication (`jobs`, `connections`, `cart_requests`, `cart_results`); session-pooler `DATABASE_URL` with
`sslmode=require`; `SUPABASE_JWKS_URL` (curl returns keys); decision D1 recorded; `PROFILE_MASTER_KEY` generated once and
backed up offline (losing it logs every user out); proxy vendor with sticky Indian residential sessions and
`PROXY_URL_TEMPLATE`; VAPID, SMTP sender, `APP_URL`, `CORS_ORIGINS`; budget unit costs from real prices; fee rules
verified (Plan 5 seeds are placeholders); `cli.py add-admin`; orders, search and cart recipes learned and approved on
the owner's accounts, Zomato only via templating, never `--force`; Vercel `NEXT_PUBLIC_*`; `deploy.sh` from a clean
tree, then `/healthz`; the owner-run real-OTP e2e (`docs/e2e-checklist.md`, including the Plan 7 section); first week:
admin cost view and Log Analytics ingestion vs the 0.2 GB/day cap; rollback = redeploy the previous image SHA
(migrations are additive).

- [ ] **Step 1: Write the spec** (fails until the stack runs).
- [ ] **Step 2: Implement** helpers, script, checklist and docs.
- [ ] **Step 3: Run** `bash scripts/e2e_local.sh` → `product`, `plan4`, `plan5`, `plan6`, `plan7` all green; `pytest -q`
  (TEST_DATABASE_URL set and unset), `pnpm --dir web test`, `pnpm --dir web build`, `bash deploy/azure/test_guard.sh`,
  `az bicep build --file deploy/azure/main.bicep` → green.
- [ ] **Step 4: Commit** — `git commit -m "test(e2e): privacy, session expiry, cool-down, error budget, disconnect and deletion end to end; pre-deploy checklist"`

---

## Self-review

- **Spec item 7:** deletion with a "nothing remains" proof (T11, T15), disconnect (T11, T9), privacy page (T9),
  per-site backoff (T5, T7, T3, T11, T12, T13, T14), error budget (T1, T7, T13, T10), session-expiry notifications
  (T8, T12, T13, T10). Known risks: backoff and pausing (T7, T12); log masking (T6); profile encryption (T4).
- **Carry-overs:** pooler + sslmode (T2, T14), API without master key (T2, T14), JWKS (T2, T11, T14), WebRTC + TZ
  (T5, T14), scrubbed Chrome env (T5), digit masking (T6), vault version + canary (T4), save_profile owner check (T4,
  T12), attempts cap (T3), KEDA type filter (T3, T14), one `SITE_PROFILE` module (T3), dirty-tree refusal (T14), Log
  Analytics note (T14), drain race and overlap (T12), Zomato templating (T6, T12), Key Vault decision (T14, T15),
  final e2e + checklist (T15).
- **Type consistency:** `jobs.owned/JobGone/requeue/scaler_query`, `sites.*`, `site_health.*`, `session.*`, `erase.*`,
  `browser.SiteThrottled`, `recipes.templatize/bind/TemplateError`, `vault.stamp/delete_profile` are named the same in
  producer and consumer tasks.
- **Ordering:** Wave 0, Wave A and Wave B file sets are disjoint within each wave; T9 and T10 share no web files.
