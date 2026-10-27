# Deploying to Azure Container Apps

Two Container Apps (API and Chrome worker) plus ACR, Storage and Log Analytics, defined in
`deploy/azure/main.bicep`. Both apps scale to zero. Default region: `centralindia`.

## Prerequisites

- Azure CLI, logged in (`az login`) with a subscription selected (`az account set`).
- `az bicep install` (once).
- A Supabase project, its Postgres connection string, and its JWT secret.
- OpenAI and TypeSafe API keys, a profile master key, and a proxy URL template.
- Python on PATH (the script uses it to write the parameters file).

## One-time: Supabase migrations

Apply every file in `supabase/migrations/` in order (`0001_init.sql`, `0002_...`) against the
Supabase database, via the SQL editor or `psql "$DATABASE_URL" -f <file>`. The worker's queue
lives in the `jobs` table.

## Deploy

```bash
cp deploy/azure/env.deploy.example deploy/azure/.env.deploy   # fill in; gitignored
bash deploy/azure/deploy.sh <resource-group>
```

The script: creates the resource group; deploys infrastructure with `deployApps=false` (so the
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
- ACR Basic (about INR 450/month) is the largest fixed cost; Storage Standard_LRS and Log Analytics
  (30-day retention, pay per GB) are small at this volume.
- The worker is 2 vCPU / 4 GiB; it bills per second only while running.

## Logs

Azure portal: resource group, `<prefix>-logs` workspace, Logs, query `ContainerAppConsoleLogs_CL`
(filter `ContainerAppName_s`). Or live: `az containerapp logs show -g <rg> -n expagent-worker --follow`.
