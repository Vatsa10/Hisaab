# Plan 2 of 7 — Cloud Worker on Azure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Plan 1 worker runs in an Azure Container App: real Google Chrome under Xvfb, each user's Chrome profile
encrypted in Azure Blob between jobs, each user's traffic through their own sticky Indian residential proxy session,
heartbeats instead of a fixed stale window, and container images plus Bicep to deploy API and worker cheaply.

**Architecture:** Wave A tasks touch disjoint files and run in parallel (each in its own git worktree, merged to
`main` in task order): carry-overs in the agent (T1), queue heartbeat and profile-aware locking (T2), the profile vault
(T3), a local proxy relay that adds upstream credentials (T4), container images and a local compose stack (T5), and the
Azure Bicep + deploy script (T6). Wave B (T7) wires vault, relay and heartbeat into the worker and browser. T8 is the
owner-run end-to-end check.

**Tech Stack:** Python 3.11+, psycopg 3, `cryptography` (AES-GCM, HKDF), `azure-storage-blob`, asyncio (relay),
Docker (python:3.12-slim + google-chrome-stable + Xvfb), Azure Container Apps (KEDA PostgreSQL scaler), Azure
Container Registry, Azure Storage, Bicep, az CLI.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md`

## Global Constraints

- Read-only guard behaviour is unchanged; no job type in this plan writes to a delivery-app account.
- Profiles are encrypted at rest with AES-256-GCM; the per-user key is HKDF-SHA256 of `PROFILE_MASTER_KEY` (32 bytes, base64 in env) with `info = b"profile:" + user_id`; plaintext profile directories are deleted after every job and at worker start-up.
- The proxy relay listens on `127.0.0.1` only and forwards only to the configured upstream; proxy credentials, cookies, OTPs and phone numbers never appear in logs, errors or job rows.
- Each user gets one sticky proxy session per site profile: `connections.proxy_session_id`, created once (12 hex chars) and reused.
- Budget ₹5,000/month: API and worker Container Apps scale to zero (min replicas 0); worker max replicas 2; images built in ACR (no local push needed).
- Secrets only from environment / Container Apps secrets: `DATABASE_URL`, `SUPABASE_JWT_SECRET`/`SUPABASE_JWKS_URL`, `OPENAI_API_KEY`, `TYPESAFE_API_KEY`, `PROFILE_MASTER_KEY`, `PROFILE_STORE_URL` (Azure Storage connection string or `file://<dir>`), `PROXY_URL_TEMPLATE` (e.g. `http://user-{session}:pass@gw.example.com:7000`; empty = no proxy).
- All work is committed on `main`; never push. No AI attribution in commit messages.
- Every existing test keeps passing (196 with TEST_DATABASE_URL set).

## Review Focus

- A worker dies mid-job: the profile isn't uploaded, the next job restores the last good blob, and the leftover plaintext directory is wiped at the next start-up. → T7 `test_startup_wipes_leftover_profiles` and `test_crash_mid_job_keeps_last_good_profile`.
- The upstream proxy is down or rejects credentials: the job fails within 30 s with error `"proxy_unavailable"`, not a long browser hang. → T4 `test_relay_reports_upstream_auth_failure` and T7 `test_proxy_unavailable_fails_fast`.
- A blob is corrupted, tampered with, or decrypted with another user's key: restore raises `VaultError`, the worker starts a fresh profile and marks the connection `needs_login`; the loop never crashes. → T3 `test_tampered_blob_raises` / `test_other_users_key_cannot_decrypt` and T7 `test_corrupt_profile_marks_needs_login`.
- A long legitimate job is never requeued while its worker is alive and heart-beating; a silent one is. → T2 `test_heartbeating_job_is_not_requeued` and T7 `test_heartbeat_runs_during_job`.
- Instamart and Swiggy share one Chrome profile: a running Swiggy sync must block an Instamart job for the same user. → T2 `test_shared_profile_blocks_second_running_job`.

---

## Wave A (parallel; disjoint files)

### Task 1: Plan 1 carry-overs in the agent

**Files:**
- Modify: `agent.py` (`Discovery.ask_user`, `Discovery.run`/`record`), `sync.py` (`login`)
- Test: `tests/test_carryovers.py`

**Interfaces:**
- Produces: `Discovery(..., runs_dir: Path | None = None)` and `LoginAgent(..., runs_dir=None)` — when given, the step log goes to `runs_dir/<site>-<kind>-<time>.jsonl` instead of the global `RUNS`.
- Produces: `sync.login(..., runs_dir: Path | None = None, on_orders_url: Callable[[str], None] | None = None) -> bool` — when `on_orders_url` is given it is called with the verified orders-page URL and the global `recipes/<site>.hint.txt` is NOT written; the agent's failure note is printed with both phone and OTP masked (`agent.mask(note, la.secrets)`).
- `ask_user` records `user answered: <answer with self.secrets masked>` (restoring the answer text for the model; Plan 1 had reduced it to a length).

- [ ] **Step 1: Write the failing tests**

```python
def test_ask_user_answer_reaches_history_with_secrets_masked():
    d = Discovery("zepto", fake_page(), None, None, prompt=lambda q, kind="question": "click Orders, my number is 9876543210")
    d.secrets["PHONE"] = "9876543210"; d.ask_user("stuck?")
    assert d.history[-1] == "user answered: click Orders, my number is <PHONE>"

def test_runs_dir_overrides_global_run_log(tmp_path):
    d = Discovery("zepto", fake_page(), None, None, runs_dir=tmp_path)
    d.run_log = None; d.record({"step": 1})   # record() must create the file under runs_dir
    assert list(tmp_path.glob("zepto-learn-*.jsonl"))

def test_login_reports_orders_url_without_writing_hint(monkeypatch, tmp_path):
    ...  # sync.login with open_site/goto/LoginAgent.run/snapshot stubbed to a logged-in orders page;
    # assert on_orders_url received "https://www.zepto.com/account/orders" and recipes.hint_path("zepto") was not created
```
(`fake_page()` is the minimal page stub already used in `tests/test_login.py`. In the last test redirect `recipes.RECIPES` to `tmp_path` so a write would be visible.)

- [ ] **Step 2: Run** `pytest tests/test_carryovers.py -v` → FAIL.
- [ ] **Step 3: Implement** per Interfaces; defaults keep CLI behaviour (global RUNS, hint file written).
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "fix: per-run log dir, orders-url callback and masked answers for unattended runs"`

---

### Task 2: Queue heartbeat and profile-aware locking

**Files:**
- Create: `supabase/migrations/0003_heartbeat_and_profile_lock.sql`
- Modify: `server/jobs.py`, `tests/server/helpers.py` (DB guard gaps)
- Test: `tests/server/test_jobs.py`, `tests/server/test_db_guard.py`, `tests/server/test_schema.py`

**Interfaces:**
- Migration 0003: `alter table jobs add column heartbeat_at timestamptz, add column profile text;` backfill `profile = case site when 'instamart' then 'swiggy' else site end`; drop the 0002 index; create unique index `one_running_job_per_profile` on `jobs(user_id, profile) where status = 'running'`.
- `SITE_PROFILE: dict[str, str] = {"instamart": "swiggy"}` in `server/jobs.py`; `enqueue` sets `profile = SITE_PROFILE.get(site, site)`.
- `heartbeat(conn, job_id: str, worker_id: str) -> bool` — sets `heartbeat_at = now()` only for `status='running' and worker_id=%s`; returns whether a row was updated.
- `claim` sets `heartbeat_at = now()`; `requeue_stale(conn, older_than_s)` uses `coalesce(heartbeat_at, started_at)`; skip-on-violation now matches index `one_running_job_per_profile`.
- `tests/server/helpers.py` guard: refuse when a `hostaddr` parameter is present and not local; when the URL has no host, use `PGHOST` (refuse if remote).

- [ ] **Step 1: Write the failing tests**

```python
def test_heartbeating_job_is_not_requeued(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set started_at = now() - interval '2 hours'")
    assert jobs.heartbeat(pg, jid, "w1") is True
    assert jobs.requeue_stale(pg, 2400) == 0

def test_silent_job_is_requeued(pg):
    jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set heartbeat_at = now() - interval '41 minutes'")
    assert jobs.requeue_stale(pg, 2400) == 1

def test_heartbeat_from_other_worker_is_ignored(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    assert jobs.heartbeat(pg, jid, "w2") is False

def test_shared_profile_blocks_second_running_job(pg):
    jobs.enqueue(pg, A, "sync", "swiggy"); jobs.enqueue(pg, A, "sync", "instamart")
    assert jobs.claim(pg, "w1", ("sync",)) is not None
    assert jobs.claim(pg, "w2", ("sync",)) is None

def test_guard_refuses_remote_hostaddr(): assert not is_local_test_db("postgresql://u:p@localhost/db?hostaddr=10.0.0.5")
def test_guard_uses_pghost_when_url_has_no_host(monkeypatch):
    monkeypatch.setenv("PGHOST", "db.example.com"); assert not is_local_test_db("postgresql:///postgres")
```
(Use the guard function's real name from `tests/server/helpers.py`.)

- [ ] **Step 2: Run** `pytest tests/server -v` (TEST_DATABASE_URL set) → new tests FAIL.
- [ ] **Step 3: Implement** migration 0003 and the `jobs.py` / helpers changes.
- [ ] **Step 4: Run** `pytest -q` with TEST_DATABASE_URL set and unset → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): heartbeat-based stale detection and profile-aware job locking"`

---

### Task 3: Encrypted profile vault

**Files:**
- Create: `server/vault.py`
- Modify: `requirements.txt` (add `azure-storage-blob>=12.19`, `cryptography>=42`)
- Test: `tests/server/test_vault.py`

**Interfaces:**
- `class VaultError(Exception)`.
- `class LocalStore(root: Path)` and `class BlobStore(connection_string: str, container: str = "profiles")`, both with `put(key: str, data: bytes) -> None`, `get(key: str) -> bytes | None`, `delete_prefix(prefix: str) -> int`.
- `store_from_url(url: str)` — `file://<dir>` → `LocalStore`, anything else → `BlobStore(url)`.
- `blob_key(user_id: str, profile: str) -> str` = `f"{user_id}/{profile}.tar.gz.enc"`.
- `save_profile(store, master_key: bytes, user_id: str, profile: str, profile_dir: Path) -> str` (tar.gz of the dir excluding `Cache`, `Code Cache`, `GPUCache`, `DawnCache`, `Service Worker/CacheStorage`, `Crashpad`; AES-256-GCM with a random 12-byte nonce prefixed; returns the blob key).
- `restore_profile(store, master_key: bytes, user_id: str, profile: str, dest_dir: Path) -> bool` — False when no blob; raises `VaultError` on bad tag/format; refuses archive members with absolute paths or `..`.
- `delete_user_profiles(store, user_id: str) -> int`.
- `master_key_from_env(value: str) -> bytes` — base64, must decode to 32 bytes, else `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_round_trip_restores_files_and_skips_caches(tmp_path):
    src = make_profile(tmp_path / "src")          # Default/Cookies, Default/Cache/x, Local State
    store = vault.LocalStore(tmp_path / "store"); key = os.urandom(32)
    vault.save_profile(store, key, A, "zepto", src)
    assert vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
    assert (tmp_path / "dst/Default/Cookies").read_bytes() == b"cookie-db"
    assert not (tmp_path / "dst/Default/Cache").exists()

def test_blob_is_encrypted(tmp_path): ...  # raw stored bytes contain no b"cookie-db"
def test_other_users_key_cannot_decrypt(tmp_path): ...  # saved for A, restore for B with same master → VaultError
def test_tampered_blob_raises(tmp_path): ...  # flip one byte in the stored blob → VaultError
def test_missing_blob_returns_false(tmp_path): ...
def test_archive_with_parent_path_is_refused(tmp_path): ...  # hand-made encrypted tar with "../evil" → VaultError, nothing written outside dst
def test_delete_user_profiles_only_touches_that_user(tmp_path): ...
def test_master_key_must_be_32_bytes(): ...
```
(Cross-user decryption fails because the AAD is `user_id + "/" + profile` and the key is derived per user.)

- [ ] **Step 2: Run** `pytest tests/server/test_vault.py -v` → FAIL (no module).
- [ ] **Step 3: Implement `server/vault.py`** per Interfaces; `BlobStore` is a thin wrapper over `BlobServiceClient.from_connection_string` (not exercised by tests unless env `AZURITE_CONNECTION_STRING` is set, in which case one round-trip test runs against Azurite).
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): encrypted per-user Chrome profile vault"`

---

### Task 4: Local proxy relay for authenticated upstream proxies

**Files:**
- Create: `server/proxy_relay.py`
- Modify: `browser.py` (`open_site` gains `proxy_server: str | None = None` → adds `--proxy-server=<value>` and `--proxy-bypass-list=<-loopback>` to the Chrome command line)
- Test: `tests/server/test_proxy_relay.py`

**Interfaces:**
- `class ProxyUnavailable(Exception)`.
- `proxy_url_for(template: str, session_id: str) -> str` — `template.format(session=session_id)`.
- `class Relay` — context manager: `Relay(upstream_url: str)`; `__enter__` starts an asyncio server on `127.0.0.1:0` in a daemon thread and returns `self` with `.port` and `.url` (`http://127.0.0.1:<port>`); handles `CONNECT host:port` (tunnels via upstream `CONNECT` with `Proxy-Authorization: Basic …` from the upstream URL's user:password) and absolute-URI plain HTTP requests (forwarded with the same header); `__exit__` stops it; `.check(timeout_s=10) -> None` performs one CONNECT to `www.google.com:443` through upstream and raises `ProxyUnavailable("auth")` on 407, `ProxyUnavailable("unreachable")` on connect error/timeout.
- Credentials are never logged; log lines carry host:port of the target only at debug level.

- [ ] **Step 1: Write the failing tests** (a fake upstream proxy on 127.0.0.1 that requires `Proxy-Authorization: Basic dXNlci14eXo6cGFzcw==` and tunnels to a tiny local HTTP server)

```python
def test_relay_tunnels_with_upstream_credentials(fake_upstream, target): ...  # GET through relay reaches target; upstream saw the auth header
def test_relay_reports_upstream_auth_failure(fake_upstream_rejecting): ...      # Relay(...).check() raises ProxyUnavailable("auth")
def test_relay_reports_unreachable_upstream(): ...                             # upstream port closed → ProxyUnavailable("unreachable") within 10 s
def test_relay_binds_loopback_only(fake_upstream): ...                          # relay socket address is 127.0.0.1
def test_proxy_url_for_formats_session(): assert proxy_url_for("http://user-{session}:p@h:1", "abc123") == "http://user-abc123:p@h:1"
def test_open_site_passes_proxy_server(monkeypatch, tmp_path): ...              # same _Dead Popen trick as tests/test_seams.py; args contain --proxy-server=http://127.0.0.1:9999
```
(`.check()` is pointed at the local target instead of google in tests via a `probe_host` parameter defaulting to `"www.google.com:443"`.)

- [ ] **Step 2: Run** `pytest tests/server/test_proxy_relay.py -v` → FAIL.
- [ ] **Step 3: Implement** `server/proxy_relay.py` and the `browser.open_site` parameter.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): local relay for authenticated residential proxies"`

---

### Task 5: Container images and local compose stack

**Files:**
- Create: `docker/worker.Dockerfile`, `docker/api.Dockerfile`, `docker/worker-entrypoint.sh`, `docker-compose.yml`, `.dockerignore`, `docs/deploy-local.md`, `scripts/smoke_compose.sh`

**Interfaces:**
- Worker image: `python:3.12-slim`, installs `google-chrome-stable` from Google's apt repo, `xvfb`, `fonts-noto` (Indic + emoji fonts), `tini`; copies the repo minus `.dockerignore` (which excludes `.env`, `profiles*/`, `expenses.db`, `logs/`, `reports/`, `runs/`, `recipes/*.json`, `recipes/*.hint.txt`, `.superpowers/`, `.git/`, `tests/`); runs as non-root user `app`; env `PROFILE_ROOT=/tmp/profiles`, `CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage"`; entrypoint: `tini -- worker-entrypoint.sh` which starts `Xvfb :99 -screen 0 1280x960x24 &`, exports `DISPLAY=:99`, then `exec python -m server.worker`.
- API image: `python:3.12-slim`, same requirements, no Chrome; `uvicorn server.api:app --host 0.0.0.0 --port 8000`; non-root.
- `docker-compose.yml`: `db` (postgres:16, port 54330), `migrate` (one-shot: applies `tests/server/bootstrap.sql` then `supabase/migrations/*.sql` in order with psql, then exits), `api` (port 8000, depends on migrate), `worker` (depends on migrate; `PROFILE_STORE_URL=file:///data/vault`, a named volume for `/data/vault`; `shm_size: 1gb`).
- `scripts/smoke_compose.sh`: `docker compose up -d --build`, waits up to 120 s for `curl -fs localhost:8000/healthz` and for the worker log line `worker started`, prints `SMOKE OK`, then `docker compose down -v`.

- [ ] **Step 1: Write `scripts/smoke_compose.sh`** first (the failing check) and run it → FAIL (no compose file).
- [ ] **Step 2: Write the Dockerfiles, entrypoint, `.dockerignore`, `docker-compose.yml`.**
- [ ] **Step 3: Run** `bash scripts/smoke_compose.sh`
Expected: `SMOKE OK`. Also `docker run --rm --entrypoint google-chrome <worker-image> --version` prints a Chrome version.
- [ ] **Step 4: Write `docs/deploy-local.md`** (how to run the stack, what each service does, how to point it at a real Supabase).
- [ ] **Step 5: Commit** — `git commit -m "build: container images and local compose stack for API and worker"`

(The worker logs `worker started` once at the top of `main()`; if Plan 1's worker doesn't, this task adds that single log line to `server/worker.py` — it is the only Python edit allowed in this task.)

---

### Task 6: Azure infrastructure as code

**Files:**
- Create: `deploy/azure/main.bicep`, `deploy/azure/deploy.sh`, `docs/deploy-azure.md`

**Interfaces:**
- `main.bicep` parameters: `location` (default `centralindia`), `prefix` (default `expagent`), secure strings `databaseUrl`, `supabaseJwtSecret`, `openaiApiKey`, `typesafeApiKey`, `profileMasterKey`, `proxyUrlTemplate`.
- Resources: Log Analytics workspace (30-day retention), Container Apps managed environment (Consumption), Azure Container Registry (Basic, admin disabled; apps pull with a user-assigned managed identity + AcrPull), Storage account (Standard_LRS, StorageV2, TLS1.2, no public blob access) + blob container `profiles`, Container App `<prefix>-api` (external ingress port 8000, min 0 / max 1, 0.5 vCPU / 1 GiB, secrets from params, `/healthz` probe), Container App `<prefix>-worker` (no ingress, min 0 / max 2, 2 vCPU / 4 GiB, KEDA `postgresql` scale rule: query `select count(*) from jobs where status = 'queued'`, `targetQueryValue: 1`, connection from the `databaseUrl` secret), worker env `PROFILE_STORE_URL` from the storage connection string secret, `CHROME_EXTRA_ARGS`, `PROFILE_ROOT=/tmp/profiles`.
- Outputs: `apiUrl`, `acrLoginServer`.
- `deploy.sh <resource-group>`: `az group create` (if missing), `az acr build` for `docker/api.Dockerfile` → `api:<git sha>` and `docker/worker.Dockerfile` → `worker:<git sha>`, then `az deployment group create` with images and secrets read from a local `deploy/azure/.env.deploy` (gitignored; template `deploy/azure/env.deploy.example` committed).

- [ ] **Step 1: Run** `az bicep build --file deploy/azure/main.bicep` → FAIL (file missing).
- [ ] **Step 2: Write `main.bicep`, `deploy.sh`, `env.deploy.example`; add `deploy/azure/.env.deploy` to `.gitignore`.**
- [ ] **Step 3: Run** `az bicep build --file deploy/azure/main.bicep` → exit 0, no errors (warnings listed in the report). Run `bash -n deploy/azure/deploy.sh` → exit 0.
- [ ] **Step 4: Write `docs/deploy-azure.md`** (prerequisites, one-time Supabase migration, `deploy.sh` usage, cost notes: scale-to-zero, Basic ACR ≈ the largest fixed cost, where to see logs).
- [ ] **Step 5: Commit** — `git commit -m "build: Azure Container Apps infrastructure and deploy script"`

Do NOT run `az deployment group create` or `az acr build` — deploying spends the owner's Azure credit and is gated on the owner.

---

## Wave B (after Wave A is merged)

### Task 7: Worker uses the vault, the relay and heartbeats

**Files:**
- Modify: `server/worker.py`, `server/config.py`, `browser.py` (`CHROME_EXTRA_ARGS` env appended to the Chrome command line)
- Test: `tests/server/test_worker.py`

**Interfaces:**
- Consumes: T1 `sync.login(..., runs_dir, on_orders_url)`, T2 `jobs.heartbeat`, `jobs.SITE_PROFILE`, T3 `vault.*`, T4 `Relay`, `proxy_url_for`, `ProxyUnavailable`, `open_site(..., proxy_server)`.
- `Settings` gains `profile_store_url: str` (env `PROFILE_STORE_URL`, default `file://vault_local`), `profile_master_key: bytes | None` (env `PROFILE_MASTER_KEY`; required when `profile_store_url` isn't `file://`), `proxy_url_template: str` (env `PROXY_URL_TEMPLATE`, default empty = no proxy), `heartbeat_s: int = 30`.
- Per job: profile dir `PROFILE_ROOT/<job id>` (fresh); `restore_profile` (VaultError → start empty and, for `sync` jobs, mark connection `needs_login` and fail with `"needs_login"`); ensure `connections.proxy_session_id` (create `secrets.token_hex(6)` if null); if a template is set, `Relay(proxy_url_for(...))` + `relay.check()` (ProxyUnavailable → `finish(error="proxy_unavailable")`, connection unchanged) and pass `proxy_server=relay.url`; heartbeat thread calling `jobs.heartbeat` every `heartbeat_s` until the job ends (stops on its own when heartbeat returns False); on success `save_profile` and store the blob key in `connections.profile_blob_key`; always `shutil.rmtree` the job's profile dir; `runs_dir` = a temp dir removed after the job; `on_orders_url` stores `connections.orders_page_url`.
- `main()` wipes `PROFILE_ROOT` at start-up; handles SIGTERM like KeyboardInterrupt (current job: connect → finish `"worker_shutdown"`; sync → left `running` for requeue).
- `fetch_orders` receives `profile_dir`, `location` and (new) `proxy_server` via `open_site` — add `proxy_server` passthrough to `sync.fetch_orders` and `sync.login` (one-line plumbing each).

- [ ] **Step 1: Write the failing tests** — `test_startup_wipes_leftover_profiles`, `test_crash_mid_job_keeps_last_good_profile` (sync stub raises after the profile dir was modified → blob unchanged, dir removed), `test_successful_job_saves_encrypted_profile`, `test_corrupt_profile_marks_needs_login`, `test_proxy_unavailable_fails_fast` (template pointing at a closed port; job error `proxy_unavailable` in < 30 s), `test_proxy_session_id_created_once`, `test_heartbeat_runs_during_job` (heartbeat_s=1, stubbed sync sleeps 2.5 s → `heartbeat_at` advanced), `test_orders_url_stored_on_connection`.
- [ ] **Step 2: Run** `pytest tests/server/test_worker.py -v` → new tests FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** `pytest -q` (set and unset) and `bash scripts/smoke_compose.sh` → green / `SMOKE OK`.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): worker restores encrypted profiles, routes via user proxy and heartbeats"`

### Task 8: Owner-run end-to-end checklist

**Files:**
- Create: `docs/e2e-checklist.md`

- [ ] **Step 1: Write the checklist** — local: `docker compose up`, create an HS256 token for a test user id, `POST /consents`, `POST /connections/zepto/connect` with the owner's phone, watch `jobs.progress` reach `otp_needed`, `POST /jobs/<id>/otp`, expect job `done`, `connections.status=connected`, orders rows, `profile_blob_key` set, and a second `POST /connections/zepto/refresh` that syncs without OTP (profile restored). Azure: the same against the deployed API URL after `deploy.sh`, with `PROXY_URL_TEMPLATE` set.
- [ ] **Step 2: Commit** — `git commit -m "docs: end-to-end checklist for the cloud worker"`
