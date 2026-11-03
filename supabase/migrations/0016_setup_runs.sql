-- One-tap setup: one row per site while the admin's "Set up <app>" run is going or paused.
-- Server-only (the PWA reads it through /admin/journey).
create table setup_runs (
  site text primary key,
  user_id uuid not null,
  status text not null check (status in ('running','done','paused','stopped')),
  step text,
  message text,
  attempts jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table setup_runs enable row level security;
revoke all on setup_runs from anon, authenticated;
