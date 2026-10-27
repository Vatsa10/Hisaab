-- Multi-user schema. All writes go through the service role; authenticated users get read-only,
-- row-filtered access. user_id is a plain uuid (no FK to auth.users).

create table profiles (
  user_id uuid primary key,
  display_name text,
  address text,
  lat double precision,
  lon double precision,
  created_at timestamptz not null default now()
);

create table connections (
  user_id uuid not null,
  site text not null,
  status text not null default 'disconnected'
    check (status in ('connected','needs_login','syncing','error','disconnected')),
  last_synced_at timestamptz,
  orders_page_url text,
  proxy_session_id text,
  profile_blob_key text,
  primary key (user_id, site)
);

create table orders (
  user_id uuid not null,
  site text not null,
  order_id text not null,
  sub_platform text,
  placed_at text,
  total_amount double precision,
  currency text,
  status text,
  merchant text,
  raw_json jsonb,
  primary key (user_id, site, order_id)
);

create table order_items (
  user_id uuid not null,
  site text not null,
  order_id text not null,
  idx int not null,
  name text,
  qty double precision,
  price double precision,
  primary key (user_id, site, order_id, idx),
  foreign key (user_id, site, order_id) references orders on delete cascade
);

create table item_categories (
  name_norm text primary key,
  category text not null
);

create table recipes (
  site text not null,
  kind text not null check (kind in ('orders','search','cart')),
  recipe jsonb not null,
  version int not null default 1,
  learned_at timestamptz not null default now(),
  healthy boolean not null default true,
  primary key (site, kind)
);

create table jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  type text not null check (type in ('connect','sync','learn','search','fill_cart')),
  site text,
  payload jsonb not null default '{}',
  status text not null default 'queued' check (status in ('queued','running','done','failed')),
  progress jsonb not null default '{}',
  error text,
  worker_id text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create unique index one_active_job on jobs (user_id, site, type)
  where status in ('queued','running');

create table consents (
  user_id uuid not null,
  site text not null,
  text_version text not null,
  agreed_at timestamptz not null default now(),
  primary key (user_id, site, text_version)
);

-- Row-level security
alter table profiles enable row level security;
alter table connections enable row level security;
alter table orders enable row level security;
alter table order_items enable row level security;
alter table item_categories enable row level security;
alter table recipes enable row level security;
alter table jobs enable row level security;
alter table consents enable row level security;

create policy own_rows on profiles for select to authenticated using (user_id = auth.uid());
create policy own_rows on connections for select to authenticated using (user_id = auth.uid());
create policy own_rows on orders for select to authenticated using (user_id = auth.uid());
create policy own_rows on order_items for select to authenticated using (user_id = auth.uid());
create policy own_rows on jobs for select to authenticated using (user_id = auth.uid());
create policy own_rows on consents for select to authenticated using (user_id = auth.uid());
create policy read_all on item_categories for select to authenticated using (true);
create policy read_all on recipes for select to authenticated using (true);

-- Grants: authenticated can only SELECT (RLS filters rows); service_role does all writes.
-- Supabase's default privileges grant ALL (including TRUNCATE, which ignores RLS) on new tables to anon and
-- authenticated, so take everything back first.
revoke all on profiles from anon, authenticated;
revoke all on connections from anon, authenticated;
revoke all on orders from anon, authenticated;
revoke all on order_items from anon, authenticated;
revoke all on item_categories from anon, authenticated;
revoke all on recipes from anon, authenticated;
revoke all on jobs from anon, authenticated;
revoke all on consents from anon, authenticated;
grant select on profiles, connections, orders, order_items, item_categories, recipes, jobs, consents
  to authenticated;
grant all on profiles, connections, orders, order_items, item_categories, recipes, jobs, consents
  to service_role;

-- Realtime: only on Supabase, where the publication exists.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table jobs;
  end if;
end $$;
