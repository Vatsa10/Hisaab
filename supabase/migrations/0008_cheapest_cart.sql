-- Plan 5: cheapest-cart requests and results, admin-edited fee rules, food search caches.

create table cart_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  mode text not null default 'grocery' check (mode in ('grocery','food')),
  items jsonb not null check (jsonb_typeof(items) = 'array'),
  -- food: {name, locality, listings: {site: {restaurant_id, name, locality, lat, lon, distance_km}},
  --        confirmed: bool, declined: [site]}
  restaurant jsonb,
  sites text[] not null,
  status text not null default 'searching' check (status in ('searching','done','failed')),
  area text not null,
  lat double precision not null,
  lon double precision not null,
  choices jsonb not null default '{}',
  summary jsonb,
  error text,
  created_at timestamptz not null default now(),
  priced_at timestamptz
);

create index cart_requests_user_created on cart_requests (user_id, created_at desc);

create table cart_results (
  request_id uuid not null references cart_requests on delete cascade,
  user_id uuid not null,
  site text not null check (site in ('zepto','blinkit','instamart','swiggy','zomato')),
  status text not null check (status in
    ('ok','failed','unserviceable','unavailable','not_connected','needs_login','closed','not_listed')),
  items jsonb not null default '[]',
  -- observed fee_info, distance_km, is_open, menu_fetched_at
  meta jsonb not null default '{}',
  subtotal numeric(10,2),
  fees jsonb,
  total numeric(10,2),
  fees_estimated boolean,
  error text,
  priced_at timestamptz not null default now(),
  primary key (request_id, site)
);

-- Admin-edited fee rules, one row per site. Validated by server/fees.py before they are saved.
create table fee_rules (
  site text primary key check (site in ('zepto','blinkit','instamart','swiggy','zomato')),
  rules jsonb not null,
  version int not null default 1,
  verified boolean not null default false,
  source text not null default 'seed',
  updated_at timestamptz not null default now(),
  updated_by uuid
);

create table restaurant_cache (
  user_id uuid not null,
  site text not null,
  geo_key text not null,
  query_norm text not null,
  results jsonb not null,
  fetched_at timestamptz not null default now(),
  primary key (user_id, site, geo_key, query_norm)
);

create table menu_cache (
  user_id uuid not null,
  site text not null,
  restaurant_id text not null,
  menu jsonb not null,
  fetched_at timestamptz not null default now(),
  primary key (user_id, site, restaurant_id)
);

-- Food recipes: restaurant search and menu reads.
alter table recipes drop constraint recipes_kind_check,
  add constraint recipes_kind_check check (kind in ('orders','search','cart','restaurants','menu','addresses'));
alter table recipe_approvals drop constraint recipe_approvals_kind_check,
  add constraint recipe_approvals_kind_check check (kind in ('orders','search','cart','restaurants','menu','addresses'));

-- Seed fee rules. All unverified: an admin checks them against the apps before launch.
insert into fee_rules (site, rules) values
  ('zepto', '{"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 99, "fee": 0}], "handling": 15,
              "small_cart": {"below": 99, "fee": 25},
              "membership": {"name": "Zepto Pass", "waives": ["delivery", "small_cart"]},
              "surge": {"possible": true}, "source_url": null,
              "note": "Seed values; verify against the app before launch."}'),
  ('blinkit', '{"delivery": [{"min_subtotal": 0, "fee": 25}, {"min_subtotal": 199, "fee": 0}], "handling": 9,
                "small_cart": {"below": 99, "fee": 20},
                "surge": {"possible": true}, "source_url": null,
                "note": "Seed values; verify against the app before launch."}'),
  ('instamart', '{"delivery": [{"min_subtotal": 0, "fee": 35}, {"min_subtotal": 199, "fee": 0}], "handling": 10,
                  "small_cart": {"below": 99, "fee": 15},
                  "membership": {"name": "Swiggy One", "waives": ["delivery"]},
                  "surge": {"possible": true}, "source_url": null,
                  "note": "Seed values; verify against the app before launch."}'),
  ('swiggy', '{"kind": "food",
               "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": 7, "fee": 45}, {"max_km": null, "fee": 65}],
               "packaging_per_item": 10, "platform": 20, "small_order": {"below": 149, "fee": 20},
               "gst": {"food_pct": 5, "fees_pct": 18},
               "membership": {"name": "Swiggy One", "waives": ["delivery"]},
               "surge": {"possible": true}, "source_url": null,
               "note": "Seed values; verify against the app before launch."}'),
  ('zomato', '{"kind": "food",
               "delivery_by_km": [{"max_km": 3, "fee": 40}, {"max_km": 7, "fee": 55}, {"max_km": null, "fee": 75}],
               "packaging_per_item": 20, "platform": 10, "small_order": {"below": 149, "fee": 25},
               "gst": {"food_pct": 5, "fees_pct": 18},
               "membership": {"name": "Zomato Gold", "waives": ["delivery"]},
               "surge": {"possible": true}, "source_url": null,
               "note": "Seed values; verify against the app before launch."}');

-- RLS: users read their own cart requests and results; everything else is service-role only.
alter table cart_requests enable row level security;
alter table cart_results enable row level security;
alter table fee_rules enable row level security;
alter table restaurant_cache enable row level security;
alter table menu_cache enable row level security;

create policy own_rows on cart_requests for select to authenticated using (user_id = auth.uid());
create policy own_rows on cart_results for select to authenticated using (user_id = auth.uid());

-- Supabase's default privileges grant ALL on new tables to anon and authenticated; take it back.
revoke all on cart_requests, cart_results, fee_rules, restaurant_cache, menu_cache from anon, authenticated;
grant select on cart_requests, cart_results to authenticated;
grant all on cart_requests, cart_results, fee_rules, restaurant_cache, menu_cache to service_role;

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table cart_requests;
    alter publication supabase_realtime add table cart_results;
  end if;
end $$;
