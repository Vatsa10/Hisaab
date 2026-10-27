# End-to-end checklist (owner-run)

Proves the cloud worker works with a real Zepto account: consent, connect with a real OTP, orders synced, profile
saved to the vault, then a second sync that needs no OTP. Run it locally first. The Azure section at the end is
only for after you decide to deploy.

Shell: Git Bash on Windows, from the repo root, Docker Desktop running. Placeholders are in `<angle brackets>`;
never paste your real phone number or address into a file or commit.

## 0. Prerequisites

- Python deps installed on the host (`pip install -r requirements.txt`): the token snippet and `publish-recipe` run on the host.
- `.env` next to `docker-compose.yml` (or exported in your shell) with `OPENAI_API_KEY` set. The worker may need it
  for the login agent. Leave `PROFILE_MASTER_KEY` empty locally: compose sets `ALLOW_DEV_PROFILE_KEY=1` and the
  worker uses the fixed development key with the `file:///data/vault` vault.
- Proxy template: the worker substitutes the per-user sticky session with Python `str.format(session=...)`, so
  `PROXY_URL_TEMPLATE` must contain exactly `{session}` and no other literal `{` or `}`. Shape (placeholders, not real
  values): `http://<user>-session-{session}:<pass>@<host>:<port>`.
- Leave `PROXY_URL_TEMPLATE` empty for the local run (the worker then goes out directly from your machine). Set it
  only if you want to test the proxy path.

## 1. Start the stack

```bash
docker compose up -d --build
curl localhost:8000/healthz
```

Expected: `{"ok":true}`. If it is not up yet, wait and retry.

```bash
docker compose logs worker | grep "worker started"
```

Expected: a line containing `worker started id=`.

Both containers must be running: `docker compose ps` shows `api`, `db`, `worker` as running (`migrate` is exited 0).

## 2. Publish the zepto recipe (once)

The worker only replays recipes stored in the database. It never learns on a user's account, so without this the
job fails with `no_recipe`. The recipe is learned on your machine with your own browser, then uploaded to the
compose database (the stack from step 1 must be running).

If `recipes/zepto.json` already exists on your machine, skip login and learn and go straight to publish. Otherwise
log in to Zepto on the host first (learn raises SessionExpired without a logged-in host profile):

```bash
python cli.py login zepto          # interactive; skip if recipes/zepto.json already exists
python cli.py learn zepto          # writes recipes/zepto.json; skip if it already exists
export DATABASE_URL='postgresql://postgres:postgres@localhost:54330/postgres'   # host-side CLI only
python cli.py publish-recipe zepto
```

`DATABASE_URL` here is used only by the host-side CLI; the containers have their own.

Expected: `published zepto recipe as version 1` (a number; it increases on each re-publish).

Published recipes are shared by every user, so `publish-recipe` drops `Referer`/`Origin`/`Cookie` headers and
refuses (printing only the JSON paths) when a value looks tied to your account: a `/users/<x>`, `/user/<x>` or
`/profile/<x>` segment, or 8+ digits in a URL path. Make those values generic, or re-run with `--force` after review.

Check:

```bash
docker compose exec db psql -U postgres -c "select site, kind, version, healthy from recipes"
```

Expected: a row `zepto | orders | 1 | t`.

Note: `docker compose down -v` wipes the database, so the recipe must be published again after it.

## 3. Mint a test token

The API accepts an HS256 token signed with `SUPABASE_JWT_SECRET`, audience `authenticated`, `sub` = the user id.
The compose default secret is `local-dev-jwt-secret-change-me-0123456789` (use your override if you set one).

```bash
export SUPABASE_JWT_SECRET='local-dev-jwt-secret-change-me-0123456789'
export USER_ID='00000000-0000-0000-0000-0000000000e2'
export TOKEN=$(python -c "import jwt,time,os; print(jwt.encode({'sub':os.environ['USER_ID'],'aud':'authenticated','exp':int(time.time())+3600}, os.environ['SUPABASE_JWT_SECRET'], algorithm='HS256'))")
export API=http://localhost:8000
echo "${TOKEN:0:20}..."
```

Expected: the start of a JWT beginning `eyJ`. The token lasts one hour; re-run the `TOKEN=` line if it expires
(you will see 401 `invalid token`). This is the same recipe as `make_token` in `tests/server/helpers.py`.

Sanity check that auth works (404 `no connection` is the correct answer here, 401 is not):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/connections/zepto/refresh \
  -H "Authorization: Bearer $TOKEN"
```

Expected: `404`.

## 4. Record consent

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/consents \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"site":"zepto"}'
```

Expected: `204`. Repeating it is harmless (also `204`).

```bash
docker compose exec db psql -U postgres -c "select site, text_version from consents where user_id='$USER_ID'"
```

Expected: one row `zepto | 2026-10-27`.

## 5. Connect (real phone, real OTP)

Use your own number as 10 digits or with country code, e.g. `+91<10-digit-number>`.

```bash
export PHONE='+91<your-10-digit-number>'
export JOB_ID=$(curl -s -X POST $API/connections/zepto/connect \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"phone\":\"$PHONE\"}" \
  | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "$JOB_ID"
```

Expected: a job id (uuid) is printed (the API answered 202). If `JOB_ID` is empty, run the same curl with `-i`
(and without the `| python ...` part) to see the error status: `412` = consent missing (step 4 not done),
`409` = a connect job is already active (finish or wait for it, see Troubleshooting), `422` = bad phone.

### Watch the job

Poll every 3 seconds (stop with Ctrl+C):

```bash
while true; do docker compose exec -T db psql -U postgres -c "select status, progress, error from jobs where id='$JOB_ID'"; sleep 3; done
```

Sequence you should see: `queued`, then `running` with progress `{}` or earlier stages, then
`running | {"stage": "otp_needed", "question": "..."}`. If the site asks something other than an OTP (for
example a location or name), the stage is `question_needed`; the answer goes through the same endpoint in
step 6.

Also watch the worker:

```bash
docker compose logs -f worker
```

The OTP SMS reaches your phone once the stage is `otp_needed`. You have 180 seconds.

## 6. Send the OTP

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/jobs/$JOB_ID/otp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"otp":"<code-from-your-phone>"}'
```

Expected: `204`. `404` means the job is not running (finished, timed out, or wrong id). If the stage was
`question_needed`, send the answer text in the same `otp` field.

Within a minute or two the job progress goes `otp_received` (or `answer_received`), then `synced`:

```bash
docker compose exec db psql -U postgres -c "select status, progress, error from jobs where id='$JOB_ID'"
```

Expected final: `done | {"stage": "synced", "orders_found": <n>, ...} | (empty error)`.

## 7. Verify the results

Connection status and vault pointer:

```bash
docker compose exec db psql -U postgres -c "select site, status, last_synced_at, profile_blob_key, orders_page_url, proxy_session_id from connections where user_id='$USER_ID'"
```

Expected: `zepto | connected | <timestamp> | <non-empty key> | <orders url> | <12 hex chars>`.
`profile_blob_key` must be set: that is the encrypted profile in the vault.

Orders:

```bash
docker compose exec db psql -U postgres -c "select count(*) from orders where user_id='$USER_ID' and site='zepto'"
docker compose exec db psql -U postgres -c "select order_id, placed_at, total_amount, status from orders where user_id='$USER_ID' order by placed_at desc limit 5"
```

Expected: a count greater than 0, equal to `orders_found` from the job progress (if the first count is lower, the same
orders were de-duplicated). Items: `select count(*) from order_items where user_id='$USER_ID'` is greater than 0.

The phone must be gone from the finished job:

```bash
docker compose exec db psql -U postgres -c "select payload from jobs where id='$JOB_ID'"
```

Expected: `{}` (no `phone` key).

The encrypted blob is in the vault volume:

```bash
docker compose exec worker sh -c "find /data/vault -type f | head"
```

Expected: at least one file.

## 8. Refresh without an OTP (the vault test)

This is the main check: the worker restores the saved profile into a fresh directory, so no login is needed.

```bash
export JOB2=$(curl -s -X POST $API/connections/zepto/refresh \
  -H "Authorization: Bearer $TOKEN" \
  | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "$JOB2"
```

Expected: 202 with a new `job_id`.

```bash
docker compose exec db psql -U postgres -c "select type, status, progress, error from jobs where id='$JOB2'"
```

Expected within about a minute: `sync | done | {"stage": "synced", "orders_found": <n>} |`. The stage must NEVER be
`otp_needed` for this job. Then:

```bash
docker compose exec db psql -U postgres -c "select status, last_synced_at from connections where user_id='$USER_ID' and site='zepto'"
docker compose exec db psql -U postgres -c "select count(*) from orders where user_id='$USER_ID' and site='zepto'"
```

Expected: `connected` with `last_synced_at` newer than in step 7; the order count the same or higher (no duplicates).

PASS = steps 5-8 all matched.

## Useful commands

Worker logs (the worker never logs the phone or OTP):

```bash
docker compose logs -f worker              # follow
docker compose logs --tail 200 worker      # recent
docker compose logs api
```

All recent jobs for the test user:

```bash
docker compose exec db psql -U postgres -c "select id, type, site, status, progress->>'stage' as stage, error, created_at from jobs where user_id='$USER_ID' order by created_at desc limit 10"
```

Start over for the same user (keeps the stack and recipe):

```bash
docker compose exec db psql -U postgres -c "delete from orders where user_id='$USER_ID'; delete from connections where user_id='$USER_ID'; delete from jobs where user_id='$USER_ID'"
docker compose exec worker sh -c "rm -rf /data/vault/*"
```

Order matters: deleting `orders` cascades to `order_items`. Clearing the vault as well makes the next connect do a
fresh login.

## Tear down

```bash
docker compose down -v       # stops everything and deletes the database AND the vault volume
```

Use `docker compose down` (no `-v`) to keep the data. After `down -v`, redo step 1 (start), step 2 (recipe)
and step 4 (consent).

## Troubleshooting

The failure reason is in `jobs.error`:

```bash
docker compose exec db psql -U postgres -c "select error from jobs where id='$JOB_ID'"
```

| `jobs.error` | Meaning | Fix |
|---|---|---|
| `no_recipe: ...` | No zepto recipe in the database | Do step 2 (`python cli.py publish-recipe zepto` with `DATABASE_URL` pointing at the compose DB, port 54330), then POST connect again. |
| `otp_timeout` (or `question_timeout`) | No answer arrived within 180 s | POST `/connections/zepto/connect` again, then send the OTP within 180 s of `otp_needed`. The connection is left in `error`; a new connect clears it. |
| `needs_login` | A sync found the session expired, or no usable profile in the vault (connection becomes `needs_login`) | POST `/connections/zepto/connect` again (new OTP). |
| `proxy_unavailable` | `PROXY_URL_TEMPLATE` is set but the proxy did not answer | Check the template (it must contain the literal `{session}` and no other braces; see below) and the proxy credentials; or unset `PROXY_URL_TEMPLATE` for a local run. The worker log shows only the proxy host. |
| `login_failed` | The login agent did not reach the orders page (wrong OTP, site changed) | Retry with a fresh OTP; if it repeats, re-learn the recipe and republish. |
| `worker_lost` | Worker died mid-connect | Re-POST connect. |

Other:

- `409 a connect job is already active`: a previous connect is still `queued`/`running`. Wait up to 180 s for it to
  time out, or check its row; a job whose worker died is failed automatically after `STALE_JOB_S` (300 s).
- Job stays `queued`: the worker is not running or cannot reach the database. `docker compose ps`, then
  `docker compose logs worker`.
- `PROFILE_MASTER_KEY is not set` in the worker log: use the compose file as shipped (it sets `ALLOW_DEV_PROFILE_KEY=1`).
  Outside a `file://` vault a real `PROFILE_MASTER_KEY` is mandatory.
- `PROXY_URL_TEMPLATE is invalid` in the worker log (the worker exits at start-up): the template did not parse as
  `http://<user>:<password>@<host>:<port>` with `{session}`; percent-encode `/ # ? @ :` inside the user or password.
- Chrome crashes on start: `shm_size: 1gb` and `--no-sandbox` are already set in compose; check
  `docker compose logs worker` for Xvfb errors.
- `401 invalid token`: wrong secret or expired token; redo step 3 and make sure the secret equals the one the `api`
  container uses.

---

## Azure (ONLY after the owner decides to deploy)

Do not run this section until you have decided to deploy. Deploying spends Azure credit (see
`docs/deploy-azure.md`). Pass the local checklist first.

Requirements before starting:

- Supabase migrations applied (`supabase/migrations/*.sql`, all three in order).
- `deploy/azure/.env.deploy` filled in (copied from `env.deploy.example`) with a real `PROFILE_MASTER_KEY` and
  **`PROXY_URL_TEMPLATE` set** (with `{session}`, see step 0). Do not run the Azure flow without a proxy: a datacenter IP will be blocked by the
  site and the job fails with `proxy_unavailable` or a login failure. There is no dev key on Azure
  (`PROFILE_MASTER_KEY` is mandatory outside a `file://` vault).
- The zepto recipe published to the Supabase database (step 2, with the Supabase `DATABASE_URL`).

```bash
bash deploy/azure/deploy.sh <resource-group>
```

It prints the API URL. There is no `docker compose` here: skip step 1, and repeat step 2 (publish-recipe, with
`DATABASE_URL` = the Supabase direct connection string) and steps 3 to 8 with these changes: `API` is the deployed
URL, the JWT secret is Supabase's, and SQL runs with psql:

```bash
export API='https://<api-url-printed-by-deploy.sh>'
export DATABASE_URL='<supabase session/direct connection string>'     # port 5432
export SUPABASE_JWT_SECRET='<your Supabase JWT secret>'
export USER_ID='<a test user uuid>'
# mint TOKEN as in step 3 with this secret
# DATABASE_URL is used only by the host-side CLI and psql; it is not sent to the API
```

Differences from local:

- Run SQL with `psql "$DATABASE_URL" -c "<query>"` instead of `docker compose exec db psql ...`.
- The worker scales from zero: after the connect POST, the job stays `queued` for up to about a minute while the
  KEDA scaler wakes a replica (it polls about every 30 s), then `running`, then `otp_needed`. Send the OTP only
  after `otp_needed`, and within 180 s.
- Worker logs: `az containerapp logs show -g <resource-group> -n expagent-worker --follow` (name is
  `<PREFIX>-worker` if you set `PREFIX`).
- The vault is Azure Storage, so `connections.profile_blob_key` set is the check; the `find /data/vault` command does not apply.
- Expected results are otherwise identical, including the OTP-free second `POST /connections/zepto/refresh`.
- Tear down by deleting the resource group (`az group delete -n <resource-group>`), which removes everything and stops
  billing. Do that when done testing if you do not intend to keep it running.
