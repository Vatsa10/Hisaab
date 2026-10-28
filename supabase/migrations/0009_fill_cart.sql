-- Plan 6: add-only cart fills (one per cheapest-cart check and app).

create table cart_fills (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  request_id uuid not null references cart_requests on delete cascade,
  site text not null check (site in ('zepto','blinkit','instamart')),
  job_id uuid,
  status text not null default 'queued' check (status in
    ('queued','filling','done','partial','failed','needs_confirmation','interrupted')),
  plan jsonb not null check (jsonb_typeof(plan) = 'array'),
  items jsonb not null default '[]',
  priced_location jsonb not null,
  confirm_location jsonb,
  app_location jsonb,
  location_selected boolean not null default false,
  repriced jsonb,
  cart jsonb,
  summary jsonb,
  error text,
  counted boolean not null default true,
  confirmed_from uuid references cart_fills(id) on delete set null,
  open_url text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

-- needs_confirmation and failed fills do not hold the slot, so the user can retry or confirm.
create unique index one_fill_per_check_site on cart_fills (request_id, site)
  where status in ('queued','filling','done','partial','interrupted');
create index cart_fills_user_created on cart_fills (user_id, created_at desc);

alter table usage add column cart_fills int not null default 0;

alter table cart_fills enable row level security;
create policy own_rows on cart_fills for select to authenticated using (user_id = auth.uid());

-- Supabase's default privileges grant ALL on new tables to anon and authenticated; take it back.
revoke all on cart_fills from anon, authenticated;
grant select on cart_fills to authenticated;
grant all on cart_fills to service_role;

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table cart_fills;
  end if;
end $$;
