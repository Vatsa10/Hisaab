-- Plan 7 hardening (minimal slice, from Task 1's Interfaces): job attempts and delayed retry, per-site back-off,
-- the needs-login stamp and the recipe pause stamp. Idempotent so it merges with the other minimal 0010 slice.
-- Plan 7 Task 1 completes it (jobs_type_finished index, usage_erased).

alter table jobs add column if not exists attempts int not null default 0,
                 add column if not exists run_after timestamptz;

create index if not exists jobs_type_finished on jobs (type, site, finished_at);

alter table connections add column if not exists needs_login_at timestamptz;

create table if not exists site_backoff (
  site text primary key,
  level int not null default 0,
  until timestamptz,
  last_kind text check (last_kind in ('rate_limited','challenge')),
  events int not null default 0,
  updated_at timestamptz not null default now()
);
alter table site_backoff enable row level security;
revoke all on site_backoff from anon, authenticated;
grant all on site_backoff to service_role;

alter table recipes add column if not exists paused_changed_at timestamptz;
alter table recipes add column if not exists paused_reason text check (paused_reason in ('auto', 'manual'));
create or replace function recipes_paused_changed() returns trigger language plpgsql as $$
begin
  if new.paused is distinct from old.paused then new.paused_changed_at := now(); end if;
  return new;
end $$;
drop trigger if exists recipes_paused_changed on recipes;
create trigger recipes_paused_changed before update on recipes
  for each row execute function recipes_paused_changed();

-- Deleted users' spend, folded per IST day with no user id, so it still counts toward the monthly budget.
create table if not exists usage_erased (
  day date primary key,
  proxy_mb double precision not null default 0,
  ai_tokens bigint not null default 0,
  worker_seconds double precision not null default 0,
  syncs int not null default 0,
  cart_checks int not null default 0,
  cart_fills int not null default 0
);
alter table usage_erased enable row level security;
revoke all on usage_erased from anon, authenticated;
grant all on usage_erased to service_role;

-- Speed budgets: per-job phase timings ({phases:[{phase,ms}], total_ms, slow}).
alter table jobs add column if not exists timings jsonb;
