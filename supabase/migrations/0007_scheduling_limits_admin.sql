-- Plan 4: metering, budget state, admins and invites, recipe approvals, notifications, Web Push subscriptions,
-- and the invite-only sign-up hook (public.before_user_created, wired in supabase/config.toml).
--
-- Every new table has RLS on. anon and authenticated get nothing, except select on their own usage rows.
-- All writes go through the service role (API, worker, scheduler). Emails are stored lower-case.

-- Per-user, per-IST-day counters. The worker, the API (/ask) and the cart-check helper add to them.
create table usage (
  user_id uuid not null,
  day date not null,
  proxy_mb double precision not null default 0,
  ai_tokens bigint not null default 0,
  worker_seconds double precision not null default 0,
  syncs int not null default 0,
  cart_checks int not null default 0,
  primary key (user_id, day)
);

-- Admin identity (ruling R2): checked as admins.email = lower(auth.users.email) for the JWT's sub.
create table admins (
  email text primary key check (email = lower(email)),
  added_at timestamptz not null default now()
);

create table invites (
  email text primary key check (email = lower(email)),
  invited_at timestamptz not null default now(),
  invited_by uuid,
  accepted_at timestamptz
);

create table waitlist (
  email text primary key check (email = lower(email)),
  joined_at timestamptz not null default now(),
  invited_at timestamptz
);

-- Learned recipes and POST-allowlist additions that wait for the admin (ruling R9).
create table recipe_approvals (
  id uuid primary key default gen_random_uuid(),
  site text not null,
  kind text not null check (kind in ('orders','search','cart')),
  reason text not null check (reason in ('post_allowlist','account_specific','cart','learned','drift_relearn')),
  proposed jsonb not null,
  current jsonb,
  diff jsonb not null default '[]',
  status text not null default 'pending'
    check (status in ('pending','approved','rejected','expired','auto_applied')),
  job_id uuid,
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  decided_by uuid
);

create index recipe_approvals_status_created on recipe_approvals (status, created_at);

-- Notification outbox (ruling R11): a row goes to a user (push, else email) or to a bare email address.
create table notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  email text,
  kind text not null,
  title text not null,
  body text not null,
  url text not null default '/',
  dedupe_key text unique not null,
  status text not null default 'pending' check (status in ('pending','sent','failed')),
  channel text check (channel in ('push','email')),
  attempts int not null default 0,
  error text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  check (num_nonnulls(user_id, email) = 1)
);

create index notifications_pending on notifications (created_at) where status = 'pending';

create table push_subscriptions (
  endpoint text primary key,
  user_id uuid not null,
  p256dh text not null,
  auth text not null,
  created_at timestamptz not null default now(),
  last_ok_at timestamptz
);

create index push_subscriptions_user on push_subscriptions (user_id);

-- One row per IST month, written by each scheduler tick (ruling R6). A month with no row is 'ok'.
create table budget_state (
  month text primary key check (month ~ '^\d{4}-\d{2}$'),
  level text not null default 'ok' check (level in ('ok','alert','paused')),
  spent_inr numeric not null default 0,
  budget_inr numeric not null,
  updated_at timestamptz not null default now()
);

-- Site health (ruling R8): a paused site is skipped by the scheduler and refused for manual refresh.
alter table recipes
  add column paused boolean not null default false,
  add column health_note text;

-- Refresh limits count today's manual sync jobs per user and site (ruling R4).
create index jobs_user_site_created on jobs (user_id, site, created_at);

-- Row-level security
alter table usage enable row level security;
alter table admins enable row level security;
alter table invites enable row level security;
alter table waitlist enable row level security;
alter table recipe_approvals enable row level security;
alter table notifications enable row level security;
alter table push_subscriptions enable row level security;
alter table budget_state enable row level security;

create policy own_rows on usage for select to authenticated using (user_id = auth.uid());

-- Grants. Supabase's default privileges grant ALL on new tables to anon and authenticated; take it back.
revoke all on usage, admins, invites, waitlist, recipe_approvals, notifications, push_subscriptions, budget_state
  from anon, authenticated;
grant select on usage to authenticated;
grant all on usage, admins, invites, waitlist, recipe_approvals, notifications, push_subscriptions, budget_state
  to service_role;

-- Invite-only sign-up (ruling R1). GoTrue calls this as supabase_auth_admin before it creates any user, for
-- email/password and OAuth alike. security definer: it runs as the owner, so supabase_auth_admin needs no table
-- grants. Rules, in order:
--   1. an admin email is always allowed;
--   2. an email with no invite is added to the waitlist and refused (invite_only);
--   3. while the current IST month's budget is paused, invited sign-ups are refused (signups_paused);
--   4. otherwise the invite is marked accepted and the sign-up goes ahead.
create function public.before_user_created(event jsonb) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_email text := lower(trim(event->'user'->>'email'));
  v_month text := to_char(now() at time zone 'Asia/Kolkata', 'YYYY-MM');
begin
  if v_email is not null and v_email <> '' and exists (select 1 from admins where email = v_email) then
    return '{}'::jsonb;
  end if;

  if v_email is null or v_email = '' or not exists (select 1 from invites where email = v_email) then
    if v_email is not null and v_email <> '' then
      insert into waitlist (email) values (v_email) on conflict do nothing;
    end if;
    return jsonb_build_object('error', jsonb_build_object('http_code', 403, 'message', 'invite_only'));
  end if;

  if exists (select 1 from budget_state where month = v_month and level = 'paused') then
    return jsonb_build_object('error', jsonb_build_object('http_code', 403, 'message', 'signups_paused'));
  end if;

  update invites set accepted_at = now() where email = v_email and accepted_at is null;
  return '{}'::jsonb;
end;
$$;

revoke execute on function public.before_user_created(jsonb) from public, anon, authenticated;
grant usage on schema public to supabase_auth_admin;
grant execute on function public.before_user_created(jsonb) to supabase_auth_admin;
