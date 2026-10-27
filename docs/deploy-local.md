# Running the stack locally (Docker Compose)

## Quick start

```bash
docker compose up -d --build
curl localhost:8000/healthz
docker compose logs -f worker
docker compose down -v      # stop and delete the database and vault volumes
```

`bash scripts/smoke_compose.sh` does the above and prints `SMOKE OK` once the API answers `/healthz` and the
worker has logged `worker started` (it then tears the stack down).

Images are `linux/amd64` (Google Chrome has no Linux arm64 build); on Apple Silicon Docker runs them under emulation.

## Services

| Service | What it does | Port |
|---|---|---|
| `db` | Postgres 16 with dev credentials (`postgres`/`postgres`) | 54330 |
| `migrate` | One-shot: applies `tests/server/bootstrap.sql` (Supabase role/auth stand-ins), then `supabase/migrations/*.sql` in sorted order, then exits | - |
| `api` | `uvicorn server.api:app` (FastAPI) | 8000 |
| `worker` | `python -m server.worker` under tini and Xvfb (`DISPLAY=:99`), driving real headful Google Chrome over CDP. `shm_size: 1gb`, vault in the `vault` named volume at `/data/vault` | - |

Both images run as the non-root user `app`. The worker sets `PROFILE_ROOT=/tmp/profiles` and
`CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage"` (Chrome's sandbox needs privileges the container does not have).

## Configuration

Compose reads these from your shell or a `.env` file next to `docker-compose.yml`:

- `SUPABASE_JWT_SECRET` - defaults to a dev-only value; must match the secret that signs your tokens.
- `OPENAI_API_KEY`, `TYPESAFE_API_KEY` (optional), `PROFILE_MASTER_KEY`, `PROXY_URL_TEMPLATE` - default to empty.

`PROFILE_STORE_URL` is `file:///data/vault` in compose; the vault and proxy settings are consumed by worker code
that lands in a later task.

The `.dockerignore` keeps `.env`, browser profiles, `expenses.db`, logs, reports, runs, learned recipes, `.git`
and `.superpowers` out of the images.

## Pointing at a real Supabase

1. Apply `supabase/migrations/*.sql` to the Supabase project (SQL editor or `supabase db push`); skip `bootstrap.sql`,
   Supabase already provides the roles and `auth` schema.
2. Run only the services you need against it, overriding the environment, for example:

   ```bash
   COMPOSE_DATABASE_URL='postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres' \
   SUPABASE_JWT_SECRET='<project JWT secret>' \
   docker compose up -d --build --no-deps api worker
   ```

   `--no-deps` skips the local `db` and `migrate`. `COMPOSE_DATABASE_URL` is deliberately not named
   `DATABASE_URL`, so a `DATABASE_URL` already in your shell cannot redirect the stack by accident. The worker
   holds a `LISTEN` connection, so use the direct (port 5432) or session-mode connection string, not the
   transaction pooler.
