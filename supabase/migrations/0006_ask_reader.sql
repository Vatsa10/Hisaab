-- Dedicated database role for the Ask analyst's SQL (server/ask.py). The API connects as ask_reader via
-- ASK_DATABASE_URL and runs each query as `authenticated` with the caller's JWT claims, so row-level security
-- applies. ask_reader is not a superuser, cannot bypass RLS, cannot create roles or databases, and holds no
-- privileges of its own (NOINHERIT): it can only `set role authenticated`. It can therefore never switch to
-- postgres or service_role, even if a query managed to call set_config('role', ...).
--
-- No password is set here (migrations are committed). Locally scripts/dev_supabase.sh sets a random one and writes
-- ASK_DATABASE_URL to .env.server.local; elsewhere run:  alter role ask_reader login password '<secret>';
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'ask_reader') then
    create role ask_reader nologin noinherit nosuperuser nobypassrls nocreaterole nocreatedb noreplication;
  end if;
end $$;

-- The API re-checks at run time and refuses /ask if this role (or any role it belongs to) is a superuser or
-- bypasses RLS, so a pre-existing ask_reader with other attributes is caught there.
grant authenticated to ask_reader;
