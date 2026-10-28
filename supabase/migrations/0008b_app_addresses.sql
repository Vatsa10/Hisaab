-- Saved delivery addresses, read live from each app's own saved-address list (read-only: never created, edited
-- or deleted on the app). Rows are per user and per account site (Instamart's live under swiggy); the worker
-- replaces a site's rows after a successful login or sync. Users read only their own rows; all writes go through
-- the service role. Addresses are user data: they are not in the Ask allowlist and never appear in logs.
--
-- Depends only on 0001 (profiles). Independent of 0007 and 0008.

create table app_addresses (
  user_id uuid not null,
  site text not null,
  address_id text not null,
  label text,
  line text,
  area text,
  city text,
  lat double precision,
  lon double precision,
  is_default boolean not null default false,
  fetched_at timestamptz not null default now(),
  primary key (user_id, site, address_id)
);

alter table app_addresses enable row level security;
create policy own_rows on app_addresses for select to authenticated using (user_id = auth.uid());

-- Supabase's default privileges grant ALL (including TRUNCATE, which ignores RLS) to anon and authenticated.
revoke all on app_addresses from anon, authenticated;
grant select on app_addresses to authenticated;
grant all on app_addresses to service_role;

-- The saved address the user picked: {"site": ..., "address_id": ...}, or null for a typed address.
alter table profiles add column chosen jsonb
  check (chosen is null or (jsonb_typeof(chosen) = 'object' and chosen ? 'site' and chosen ? 'address_id'));

-- Realtime: only on Supabase, where the publication exists. The picker updates live after a connect.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table app_addresses;
  end if;
end $$;
