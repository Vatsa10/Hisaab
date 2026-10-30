# Deploying to Azure Container Apps

Two Container Apps (API and Chrome worker), a scheduler job, plus ACR, Storage, Key Vault and Log Analytics, defined in
`deploy/azure/main.bicep`. Both apps scale to zero. Default region: `centralindia`.

## Prerequisites

- Azure CLI, logged in (`az login`) with a subscription selected (`az account set`).
- `az bicep install` (once).
- A Supabase project, its session-pooler connection strings (see below), its JWT secret and its JWKS URL.
- A clean git tree: `deploy.sh` refuses to run with uncommitted or untracked changes, because the image tag is the commit sha.
- Anthropic (Claude Haiku 5.5) and TypeSafe API keys, a profile master key, and a proxy URL template.
- Python on PATH (the script uses it to write the parameters file).

## One-time: Supabase migrations

Apply every file in `supabase/migrations/` in order (`0001_init.sql`, `0002_...`) against the
Supabase database, via the SQL editor or `psql "$DATABASE_URL" -f <file>`. The worker's queue
lives in the `jobs` table.

## Database URL (session pooler, sslmode=require)

`DATABASE_URL` (API, worker, scheduler job and the KEDA scaler) and `ASK_DATABASE_URL` (the API's `ask_reader` login,
migration 0006) must both be the Supabase **session pooler** with TLS, for example
`postgresql://postgres.<ref>:<pw>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require`.

- Not the direct host (`db.<ref>.supabase.co`): it resolves to IPv6 only, and Container Apps cannot reach it.
- Not port 6543: that is the transaction pooler, which breaks `LISTEN/NOTIFY` (the worker's wake-ups) and session state.
- `deploy.sh` checks both URLs with `config.check_database_url` before any `az` call, and the services check again at
  start-up. The URL is passed through the environment, never argv, and error messages never print it.

## Deploy

```bash
cp deploy/azure/env.deploy.example deploy/azure/.env.deploy   # fill in; gitignored
bash deploy/azure/deploy.sh <resource-group>
```

The script: refuses a dirty git tree, validates the database URLs, creates the resource group; deploys infrastructure with `deployApps=false` (so the
registry exists); builds `api:<git sha>` and `worker:<git sha>` with `az acr build`; then deploys
the apps. It prints the API URL. Secrets are passed through a temporary parameters file, never
on the command line, and end up only as Container Apps secrets. This spends Azure credit.

## Cost notes (budget: INR 5,000/month)

- Both apps have min replicas 0: no compute cost when idle. API max 1, worker max 2.
- The worker wakes via a KEDA `postgresql` scaler while `jobs` has rows with `status in ('queued','running')`,
  so a replica is not scaled in mid-job (counting only queued rows would let KEDA remove it once a job is claimed).
  Stale `running` rows are cleared by the heartbeat-based requeue, so they cannot pin a replica for long.
  On a scale-in the worker gets `SIGTERM`: it stops claiming jobs and lets the current one run for up to
  `DRAIN_S` (540 s); a job still running then is aborted (a sync goes straight back to the queue, a connect fails
  with `worker_shutdown`), all inside the `terminationGracePeriodSeconds: 600` grace period. A job that hangs
  past `MAX_JOB_S` (1200 s) stops heart-beating and the worker exits; with `STALE_JOB_S` 300 s the job is
  reclaimed about five minutes later.
  The scaler polls the database (about every 30 s), so Supabase must accept connections from Azure.
- ACR Basic (about INR 450/month) is the largest fixed cost; Storage Standard_LRS, Key Vault (one secret) and Log
  Analytics are small at this volume.
- The worker is 2 vCPU / 4 GiB; it bills per second only while running.

## Logs

Azure portal: resource group, `<prefix>-logs` workspace, Logs, query `ContainerAppConsoleLogs_CL`
(filter `ContainerAppName_s`). Or live: `az containerapp logs show -g <rg> -n expagent-worker --follow`.

## Log Analytics cost

Ingestion is billed per GB after any free allowance. The workspace is capped at 0.2 GB/day (`workspaceCapping` in
`main.bicep`): once the cap is hit, further logs for that day are dropped, which is the point. Retention is 30 days,
and server logs carry no email or phone, so deleted accounts leave nothing behind after that window. The worker logs
counts and ids only (no cookies, tokens, OTPs, phone numbers, answers or addresses). Check the current price on the
Azure pricing page for Log Analytics before you rely on a number; raise `dailyQuotaGb` only if you need more history.

## Owner decision D1: profile key management

The profile vault encrypts each user's Chrome profile with a key derived from one master key.

- **Option A (default, implemented):** the master key is a secret in Azure Key Vault (RBAC, soft delete, purge
  protection). The managed identity holds "Key Vault Secrets User"; only the worker app references the secret
  (a Key Vault reference, `profile-master-key`), and the API has no copy at all. Per-user keys are derived from it
  with HKDF. Cost: one vault, one secret, a few cents a month. Rotation: replace the secret version, then restart the
  worker. A wrong or rotated key is detected by the vault's key canary (a key check inside every blob), so the worker
  refuses to restore profiles with it instead of corrupting them.
- **Option B:** a separate envelope key per user in Key Vault (the master key never leaves the vault, and revoking one
  user's key is possible). Cost: a Key Vault key per user plus a wrap/unwrap call on every profile restore and save
  (vault operations are billed per 10,000), and roughly one to two engineering days: a key-management module, a
  migration that re-wraps existing blobs, and tests. Worth it only if per-user key revocation becomes a requirement.

The build does not wait on D1: it ships option A. Switching later means re-encrypting existing blobs, not redeploying
the apps.

## Deploy without running it

`az bicep build --file deploy/azure/main.bicep` validates the template locally; `tests/server/test_deploy_config.py`
checks it against `server/config.py` and `docker-compose.yml`. `deploy.sh` is only for the owner to run.
