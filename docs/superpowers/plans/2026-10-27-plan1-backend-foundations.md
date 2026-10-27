# Plan 1 of 7 — Backend Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A multi-user backend: Supabase Postgres schema with row-level security, a job queue, a FastAPI service
for user actions (consent, connect, OTP, refresh), and a worker that runs the existing login and sync code per user
and writes orders to Postgres. Everything runs locally with Docker before Plan 2 moves the worker to Azure.

**Architecture:** New `server/` package beside the existing CLI modules, which keep working unchanged for SQLite.
The existing `agent`/`sync`/`browser` modules get three small seams: a prompt callback instead of `input()`, a per-user
profile directory and location, and a `fetch_orders` function split out of `sync_site`. Jobs are rows claimed with
`FOR UPDATE SKIP LOCKED`; OTPs travel API → worker over Postgres `NOTIFY` and are never stored. The PWA (Plan 3)
watches job progress through Supabase Realtime on the `jobs` table.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, psycopg 3, PyJWT, Supabase Postgres (plain Postgres 16 in Docker for
tests), existing Playwright/Jev/OpenAI code.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md`

**Plans in this series:** 1 Backend foundations (this) · 2 Cloud worker on Azure (Chrome+Xvfb container, Blob
profiles, residential proxies) · 3 PWA (Next.js on Vercel) · 4 Scheduling, limits, budget, admin view · 5 Cheapest
cart · 6 Fill cart · 7 Hardening (deletion, privacy page, backoff).

## Global Constraints

- OTPs are never written to disk, a database row, or a log; they travel only as a `NOTIFY` payload.
- The phone number is stored in a `connect` job's payload only while the job runs; it is removed when the job finishes or fails.
- Users can read only their own rows (`orders`, `order_items`, `connections`, `jobs`, `consents`, `profiles`); workers and the API write with the service role.
- Read-only guard behaviour (`guard.py`) is unchanged by this plan; no job type in this plan writes to a delivery-app account.
- Consent is required before connecting a site: `consents(user_id, site, text_version, agreed_at)`; current `text_version` is `"2026-10-27"`.
- Secrets come from environment variables only (`DATABASE_URL`, `SUPABASE_JWT_SECRET` or `SUPABASE_JWKS_URL`, plus the existing `OPENAI_API_KEY`, `TYPESAFE_API_KEY`).
- The existing CLI (`python cli.py ...`) and its 70 tests keep passing.

## Review Focus

- The user never types the OTP: the job must fail with error `"otp_timeout"` after 180 s and release the worker, not hang. → Task 6 test `test_prompt_otp_times_out`.
- A worker dies mid-job: the job stays `running` forever unless reclaimed; `requeue_stale` must hand it back after 10 minutes. → Task 3 test `test_requeue_stale_running_jobs`.
- The user taps Connect twice: a second active `connect` job for the same user and site must be refused (HTTP 409), not run two browsers on one profile. → Task 5 test `test_connect_twice_conflicts`.
- User A sends an OTP for user B's job id: must be 404 and nothing sent. → Task 5 test `test_otp_for_someone_elses_job_is_404`.
- The phone number lingers in a finished job's payload: it must be gone on both success and failure. → Task 3 test `test_finish_strips_phone`.

---

### Task 1: Postgres schema with row-level security

**Files:**
- Create: `supabase/migrations/0001_init.sql`
- Create: `tests/server/bootstrap.sql` (stand-ins for Supabase's `auth.uid()`, `auth.users`, and the `authenticated`/`service_role` roles, for plain Postgres in tests)
- Create: `tests/server/conftest.py`
- Create: `tests/server/test_schema.py`
- Modify: `requirements.txt` (add `fastapi>=0.110`, `uvicorn>=0.29`, `psycopg[binary]>=3.1`, `PyJWT[crypto]>=2.8`)
- Modify: `README.md` (a "Server (local)" section: start Postgres, run tests)

**Interfaces:**
- Produces tables: `profiles(user_id uuid pk, display_name text, address text, lat double precision, lon double precision, created_at timestamptz)`, `connections(user_id uuid, site text, status text check in ('connected','needs_login','syncing','error','disconnected'), last_synced_at timestamptz, orders_page_url text, proxy_session_id text, profile_blob_key text, pk(user_id, site))`, `orders(user_id uuid, site text, order_id text, sub_platform text, placed_at text, total_amount double precision, currency text, status text, merchant text, raw_json jsonb, pk(user_id, site, order_id))`, `order_items(user_id, site, order_id, idx int, name text, qty double precision, price double precision, pk(user_id, site, order_id, idx), fk → orders on delete cascade)`, `item_categories(name_norm text pk, category text)`, `recipes(site text, kind text check in ('orders','search','cart'), recipe jsonb, version int, learned_at timestamptz, healthy bool default true, pk(site, kind))`, `jobs(id uuid pk default gen_random_uuid(), user_id uuid, type text check in ('connect','sync','learn','search','fill_cart'), site text, payload jsonb default '{}', status text check in ('queued','running','done','failed') default 'queued', progress jsonb default '{}', error text, worker_id text, created_at, started_at, finished_at timestamptz)`, `consents(user_id uuid, site text, text_version text, agreed_at timestamptz default now(), pk(user_id, site, text_version))`.
- Partial unique index `one_active_job` on `jobs(user_id, site, type) where status in ('queued','running')`.
- RLS enabled on every table; policies `select using (user_id = auth.uid())` on profiles, connections, orders, order_items, jobs, consents; `item_categories` and `recipes` readable by `authenticated`; no insert/update/delete policies for `authenticated` (all writes go through the service role).
- `jobs` added to the `supabase_realtime` publication when it exists (guarded `do $$ ... $$` block so plain Postgres skips it).
- Fixture `pg` (in `conftest.py`): a psycopg connection to `TEST_DATABASE_URL` with bootstrap + migration applied to a fresh schema per test session; tests skip when `TEST_DATABASE_URL` is unset. Fixture `as_user(pg, user_id)`: context manager that does `set local role authenticated; set local request.jwt.claim.sub = '<id>'`.

- [ ] **Step 1: Start a throwaway Postgres for tests**

Run: `docker run -d --name ea-pg -p 54329:5432 -e POSTGRES_PASSWORD=postgres postgres:16` and set `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/postgres`
Expected: `docker ps` lists `ea-pg` as Up.

- [ ] **Step 2: Write the failing tests**

```python
def test_user_sees_only_own_orders(pg, as_user):
    pg.execute("insert into orders(user_id, site, order_id, total_amount) values (%s,'zepto','1',100),(%s,'zepto','2',200)", (A, B))
    with as_user(pg, A):
        assert pg.execute("select order_id from orders").fetchall() == [("1",)]

def test_user_cannot_write_orders(pg, as_user):
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute("insert into orders(user_id, site, order_id) values (%s,'zepto','9')", (A,))

def test_one_active_job_per_user_site_type(pg):
    pg.execute("insert into jobs(user_id, type, site) values (%s,'connect','swiggy')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("insert into jobs(user_id, type, site) values (%s,'connect','swiggy')", (A,))

def test_recipes_readable_by_any_user(pg, as_user):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    with as_user(pg, A):
        assert pg.execute("select count(*) from recipes").fetchone() == (1,)
```
(`A`, `B` are two fixed UUIDs at module top. A failed insert inside `as_user` must not poison the session: the fixture wraps each block in a savepoint.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/server/test_schema.py -v`
Expected: FAIL (relation "orders" does not exist).

- [ ] **Step 4: Write `supabase/migrations/0001_init.sql`, `tests/server/bootstrap.sql`, `tests/server/conftest.py`** per the Interfaces block.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/server/test_schema.py -v` then `pytest -q`
Expected: 4 passed; full suite still green (server tests skip if `TEST_DATABASE_URL` unset).

- [ ] **Step 6: Commit**

```bash
git add supabase tests/server requirements.txt README.md
git commit -m "feat(server): add Postgres schema with row-level security"
```

---

### Task 2: Per-user order storage

**Files:**
- Create: `server/__init__.py`, `server/store.py`
- Test: `tests/server/test_store.py`

**Interfaces:**
- Consumes: Task 1 schema; the order dict shape produced by `recipes.extract_orders` (`order_id, placed_at, total_amount, currency, status, merchant, sub_platform, items[{name, qty, price}], raw`).
- Produces: `upsert_orders(conn, user_id: str, site: str, orders: list[dict], sub_platform: str | None = None) -> int` (same semantics as `db.upsert_orders`: update on conflict, replace items); `set_connection(conn, user_id: str, site: str, **fields) -> None` (insert-or-update of `connections`); `get_profile(conn, user_id: str) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_upsert_is_idempotent_per_user(pg):
    o = order("1", items=[{"name": "Milk", "qty": 1, "price": 30}])
    store.upsert_orders(pg, A, "zepto", [o]); store.upsert_orders(pg, A, "zepto", [o])
    assert pg.execute("select count(*) from orders").fetchone() == (1,)
    assert pg.execute("select count(*) from order_items").fetchone() == (1,)

def test_same_order_id_for_two_users_is_two_rows(pg):
    store.upsert_orders(pg, A, "zepto", [order("1")]); store.upsert_orders(pg, B, "zepto", [order("1")])
    assert pg.execute("select count(*) from orders").fetchone() == (2,)

def test_set_connection_upserts(pg):
    store.set_connection(pg, A, "swiggy", status="syncing")
    store.set_connection(pg, A, "swiggy", status="connected", orders_page_url="https://www.swiggy.com/my-account/orders")
    assert pg.execute("select status, orders_page_url from connections").fetchall() == [("connected", "https://www.swiggy.com/my-account/orders")]
```
(`order()` helper mirrors `tests/test_db.py::order`; `raw` is stored as jsonb.)

- [ ] **Step 2: Run tests to verify they fail** — `pytest tests/server/test_store.py -v` → FAIL (no module `server.store`).

- [ ] **Step 3: Implement `server/store.py`** with the three functions; one transaction per `upsert_orders` call.

- [ ] **Step 4: Run tests to verify they pass** — `pytest tests/server/test_store.py -v` → 3 passed.

- [ ] **Step 5: Commit** — `git add server tests/server/test_store.py && git commit -m "feat(server): add per-user order storage"`

---

### Task 3: Job queue

**Files:**
- Create: `server/jobs.py`
- Test: `tests/server/test_jobs.py`

**Interfaces:**
- Consumes: Task 1 `jobs` table.
- Produces: dataclass `Job(id: str, user_id: str, type: str, site: str, payload: dict)`; `enqueue(conn, user_id: str, type: str, site: str, payload: dict | None = None) -> str` (raises `jobs.Conflict` on the `one_active_job` violation); `claim(conn, worker_id: str, types: tuple[str, ...]) -> Job | None` (oldest queued, `FOR UPDATE SKIP LOCKED`, sets `running`, `started_at`, `worker_id`); `set_progress(conn, job_id: str, **progress) -> None` (merges into `progress` jsonb); `finish(conn, job_id: str, error: str | None = None) -> None` (status `done`/`failed`, `finished_at`, removes `payload->'phone'`); `requeue_stale(conn, older_than_s: int = 600) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_two_workers_never_claim_the_same_job(pg, pg2):
    jobs.enqueue(pg, A, "sync", "zepto")
    first, second = jobs.claim(pg, "w1", ("sync",)), jobs.claim(pg2, "w2", ("sync",))
    assert first is not None and second is None

def test_enqueue_conflict_while_active(pg):
    jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210"})
    with pytest.raises(jobs.Conflict):
        jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210"})

def test_finish_strips_phone(pg):
    jid = jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210", "x": 1})
    jobs.finish(pg, jid, error="otp_timeout")
    assert pg.execute("select status, payload, error from jobs").fetchone() == ("failed", {"x": 1}, "otp_timeout")

def test_requeue_stale_running_jobs(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set started_at = now() - interval '11 minutes'")
    assert jobs.requeue_stale(pg, 600) == 1
    assert jobs.claim(pg, "w2", ("sync",)).id == jid

def test_set_progress_merges(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    jobs.set_progress(pg, jid, stage="syncing"); jobs.set_progress(pg, jid, orders_found=12)
    assert pg.execute("select progress from jobs").fetchone()[0] == {"stage": "syncing", "orders_found": 12}
```
(`pg2` is a second independent connection fixture added to `conftest.py`; `claim` must commit so the lock is held only for the claim.)

- [ ] **Step 2: Run** `pytest tests/server/test_jobs.py -v` → FAIL.
- [ ] **Step 3: Implement `server/jobs.py`** per Interfaces.
- [ ] **Step 4: Run** `pytest tests/server/test_jobs.py -v` → 5 passed.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): add Postgres job queue"`

---

### Task 4: Seams in the agent for unattended runs

**Files:**
- Modify: `agent.py` (`Discovery.__init__`, `ask_user`, `LoginAgent.__init__`, `LoginAgent.enter_otp`, `LoginAgent.type_value`)
- Modify: `browser.py` (`open_site`)
- Modify: `sync.py` (split `fetch_orders` out of `sync_site`; `login` gains `prompt`)
- Test: `tests/test_seams.py`

**Interfaces:**
- Produces: `Discovery(site, page, guard, capture, prompt: Callable[[str], str] = input)`; `LoginAgent(site, page, guard, capture, phone, location="", dry_run=False, prompt=input)`; every former `input(...)` call in `agent.py` goes through `self.prompt(text)`; a prompt that raises `TimeoutError` ends the run with `done = {"success": False, "note": "otp_timeout" or "ask_timeout"}`.
- Produces: `open_site(site, allowlist=None, login_mode=False, profile_dir: Path | None = None, location: dict | None = None)` — `profile_dir` overrides `PROFILES / cfg["profile"]`, `location` overrides `load_settings()["location"]`.
- Produces: `sync.fetch_orders(site: str, since: str | None, profile_dir: Path | None = None, location: dict | None = None) -> list[dict]` (fast path → replay → re-learn, exactly as `sync_site` today); `sync_site` keeps its signature and becomes `fetch_orders` + `db.upsert_orders`. `sync.login(site, phone=None, dry_run=False, prompt=input, profile_dir=None, location=None) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_otp_comes_from_prompt_not_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() called"))
    la = LoginAgent("zepto", page_with_otp_field(), None, None, "9000000000", prompt=lambda q: "4321")
    la.phone_typed = True; la.on_observe()
    assert la.otp_entered and la.secrets["OTP"] == "4321"

def test_prompt_timeout_ends_run_cleanly():
    def slow(q): raise TimeoutError
    la = LoginAgent("zepto", page_with_otp_field(), None, None, "9000000000", prompt=slow)
    la.phone_typed = True; la.on_observe()
    assert la.done == {"success": False, "note": "otp_timeout", "url": la.page.url}

def test_open_site_uses_given_profile_dir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **k: seen.setdefault("args", args) or _Dead())
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "u1-zepto"):
            pass
    assert f"--user-data-dir={tmp_path / 'u1-zepto'}" in seen["args"]
```
(`page_with_otp_field()` is a headless Playwright page whose content is `<input autocomplete=one-time-code>`; reuse the pattern from the existing local smoke tests. `_Dead` is a Popen stand-in whose `poll()` returns 1 so `_wait_for_cdp` raises immediately.)

- [ ] **Step 2: Run** `pytest tests/test_seams.py -v` → FAIL.
- [ ] **Step 3: Implement the seams** per Interfaces; no behaviour change when defaults are used.
- [ ] **Step 4: Run** `pytest -q` → all old tests plus 3 new pass.
- [ ] **Step 5: Commit** — `git commit -m "refactor: inject prompt, profile dir and location for unattended runs"`

---

### Task 5: API for consent, connect, OTP and refresh

**Files:**
- Create: `server/config.py`, `server/auth.py`, `server/api.py`
- Test: `tests/server/test_api.py`

**Interfaces:**
- Consumes: `server.jobs.enqueue/Conflict`, Task 1 tables.
- Produces: `Settings` (frozen dataclass from env: `database_url`, `jwt_secret`, `jwks_url`, `consent_version="2026-10-27"`, `otp_timeout_s=180`); `settings() -> Settings`.
- Produces: `user_id_from_token(token: str, s: Settings) -> str` — HS256 with `jwt_secret` when set, else RS256/ES256 via `jwt.PyJWKClient(jwks_url)`; audience `"authenticated"`; raises `auth.Unauthorized`.
- Produces FastAPI `app` with bearer auth on every route:
  - `POST /consents` body `{site}` → 204, stores `(user, site, consent_version)`.
  - `POST /connections/{site}/connect` body `{phone}` → 202 `{job_id}`; 400 if site not in `browser.SITES` or `instamart`; 412 if no consent for `(user, site, consent_version)`; 409 on `jobs.Conflict`; phone normalised with `sync.normalize_phone` (422 on ValueError).
  - `POST /jobs/{job_id}/otp` body `{otp}` → 204; 404 unless the job is the caller's, type `connect`, status `running`; sends `NOTIFY otp_<job id without dashes>, '<otp>'` (via `select pg_notify(...)`); never logs the body.
  - `POST /connections/{site}/refresh` → 202 `{job_id}`; 404 if no connection; 409 on conflict.
  - `GET /healthz` (no auth) → `{"ok": true}`.

- [ ] **Step 1: Write the failing tests** (FastAPI `TestClient`, HS256 tokens signed with a test secret, `pg` fixture via `TEST_DATABASE_URL`)

```python
def test_connect_needs_consent(client, token_a):
    r = client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a)
    assert r.status_code == 412

def test_connect_twice_conflicts(client, token_a):
    client.post("/consents", json={"site": "swiggy"}, headers=token_a)
    assert client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 202
    assert client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 409

def test_otp_for_someone_elses_job_is_404(client, token_a, token_b, pg):
    client.post("/consents", json={"site": "zepto"}, headers=token_a)
    jid = client.post("/connections/zepto/connect", json={"phone": "9876543210"}, headers=token_a).json()["job_id"]
    pg.execute("update jobs set status='running' where id=%s", (jid,))
    assert client.post(f"/jobs/{jid}/otp", json={"otp": "1234"}, headers=token_b).status_code == 404

def test_otp_reaches_listener_and_is_not_stored(client, token_a, pg, pg2):
    ...  # pg2 LISTENs on otp_<id>; POST otp; pg2 receives "1234"; no table contains "1234"

def test_rejects_missing_or_bad_token(client):
    assert client.post("/consents", json={"site": "zepto"}).status_code == 401

def test_instamart_cannot_be_connected(client, token_a):
    assert client.post("/connections/instamart/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 400
```
(The no-storage assertion scans `jobs.payload`, `jobs.progress`, `jobs.error` for the OTP string.)

- [ ] **Step 2: Run** `pytest tests/server/test_api.py -v` → FAIL.
- [ ] **Step 3: Implement `server/config.py`, `server/auth.py`, `server/api.py`** per Interfaces; one psycopg connection per request from a small pool (`psycopg_pool` is optional; a connection per request is fine at this scale).
- [ ] **Step 4: Run** `pytest tests/server/test_api.py -v` → 6 passed.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): add API for consent, connect, OTP relay and refresh"`

---

### Task 6: Worker that runs connect and sync jobs

**Files:**
- Create: `server/worker.py`
- Test: `tests/server/test_worker.py`
- Modify: `README.md` ("Server (local)": `uvicorn server.api:app` and `python -m server.worker`)

**Interfaces:**
- Consumes: `jobs.claim/set_progress/finish/requeue_stale`, `store.upsert_orders/set_connection/get_profile`, `sync.login(..., prompt, profile_dir, location)`, `sync.fetch_orders(...)`, `browser.SITES`.
- Produces: `make_otp_prompt(listen_conn, job_conn, job_id: str, timeout_s: int) -> Callable[[str], str]` — on call: `set_progress(stage="otp_needed", question=<text>)`, waits on `LISTEN otp_<id>` up to `timeout_s`, returns the payload, sets `stage="otp_received"`; raises `TimeoutError` on timeout.
- Produces: `run_job(job: Job, conn, s: Settings) -> None` — `connect`: `set_connection(status="syncing")`, `sync.login(...)` with the prompt, then the first sync; `sync`: `fetch_orders` + `upsert_orders`, progress `orders_found`, `set_connection(status="connected", last_synced_at=now())`; `SessionExpired` → `set_connection(status="needs_login")` and `finish(error="needs_login")`; any other exception → `finish(error=<type: first line>)`. Profile dir: `Path(os.getenv("PROFILE_ROOT", "profiles_users")) / user_id / browser.SITES[site]["profile"]`. Location from `get_profile`.
- Produces: `main(worker_id: str | None = None, poll_s: float = 2.0) -> None` — loop: `requeue_stale`, `claim(("connect", "sync"))`, `run_job`, `finish`; one job at a time per process.

- [ ] **Step 1: Write the failing tests**

```python
def test_prompt_otp_round_trip(pg, pg_listen, pg2):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    ask = worker.make_otp_prompt(pg_listen, pg, jid, timeout_s=5)
    threading.Timer(0.5, lambda: pg2.execute("select pg_notify(%s, %s)", (f"otp_{jid.replace('-', '')}", "4321"))).start()
    assert ask("Enter OTP") == "4321"
    assert pg.execute("select progress->>'stage' from jobs").fetchone() == ("otp_received",)

def test_prompt_otp_times_out(pg, pg_listen):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    with pytest.raises(TimeoutError):
        worker.make_otp_prompt(pg_listen, pg, jid, timeout_s=1)("Enter OTP")

def test_sync_job_stores_orders(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1"), order("2")])
    jid = jobs.enqueue(pg, A, "sync", "zepto"); job = jobs.claim(pg, "w", ("sync",))
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select count(*) from orders where user_id=%s", (A,)).fetchone() == (2,)
    assert pg.execute("select status from connections").fetchone() == ("connected",)

def test_expired_session_marks_needs_login(pg, monkeypatch):
    def expired(*a, **k): raise SessionExpired("HTTP 401")
    monkeypatch.setattr(worker.sync, "fetch_orders", expired)
    jid = jobs.enqueue(pg, A, "sync", "zepto"); worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("needs_login",)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "needs_login")
```

- [ ] **Step 2: Run** `pytest tests/server/test_worker.py -v` → FAIL.
- [ ] **Step 3: Implement `server/worker.py`** per Interfaces.
- [ ] **Step 4: Run** `pytest -q` → everything green.
- [ ] **Step 5: Manual end-to-end check (real browser, your own account):** start `ea-pg`, apply the migration, run `uvicorn server.api:app` and `python -m server.worker`; with an HS256 test token, `POST /consents {"site":"zepto"}`, `POST /connections/zepto/connect`, then `POST /jobs/<id>/otp` with the OTP your phone receives. Expected: job `done`, `connections.status = connected`, orders rows for that user.
- [ ] **Step 6: Commit** — `git commit -m "feat(server): add worker running connect and sync jobs"`
