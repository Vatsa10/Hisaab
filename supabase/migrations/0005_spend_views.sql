-- Per-user spend views for the PWA. Every view is security_invoker, so the caller's RLS on orders /
-- order_items applies: a signed-in user sees only their own rows. Cancelled, refunded and failed orders
-- never count as spend (same rule as report.py).

-- Same normalisation as db.norm_name: lower-case, non-word characters to spaces, collapse, trim.
create or replace function norm_name(name text) returns text
language sql immutable parallel safe
as $$
  select btrim(regexp_replace(regexp_replace(lower(name), '[^\w\s]', ' ', 'g'), '\s+', ' ', 'g'))
$$;

create view monthly_spend with (security_invoker = true) as
select o.user_id,
       substr(o.placed_at, 1, 7) as month,
       o.site,
       o.sub_platform,
       count(*) as orders,
       round(sum(o.total_amount)::numeric, 2) as amount
from orders o
where coalesce(o.status, '') not in ('cancelled', 'refunded', 'failed')
group by o.user_id, substr(o.placed_at, 1, 7), o.site, o.sub_platform;

-- Category spend is item-level, so it may not add up to order totals (fees, discounts). Items without a
-- price (Zepto, Blinkit) get the order total split across the order's items by quantity.
create view category_spend with (security_invoker = true) as
select x.user_id,
       x.month,
       x.category,
       round(sum(x.item_amount)::numeric, 2) as amount
from (
  select o.user_id,
         substr(o.placed_at, 1, 7) as month,
         case when o.site = 'zomato'
                or (o.site = 'swiggy' and coalesce(o.sub_platform, '') <> 'instamart')
              then 'food delivery'
              else coalesce(c.category, 'uncategorized') end as category,
         coalesce(i.price,
                  o.total_amount * coalesce(i.qty, 1)
                    / nullif(sum(coalesce(i.qty, 1)) over (partition by i.user_id, i.site, i.order_id), 0)
         ) as item_amount
  from order_items i
  join orders o on o.user_id = i.user_id and o.site = i.site and o.order_id = i.order_id
  left join item_categories c on c.name_norm = norm_name(i.name)
  where coalesce(o.status, '') not in ('cancelled', 'refunded', 'failed')
) x
group by x.user_id, x.month, x.category;

create view merchant_spend with (security_invoker = true) as
select o.user_id,
       coalesce(o.merchant, '(unknown)') as merchant,
       o.site,
       count(*) as orders,
       round(sum(o.total_amount)::numeric, 2) as amount,
       max(o.placed_at) as last_order_at
from orders o
where coalesce(o.status, '') not in ('cancelled', 'refunded', 'failed')
group by o.user_id, coalesce(o.merchant, '(unknown)'), o.site;

-- Supabase's default privileges grant ALL on new relations to anon and authenticated; take it back.
revoke all on monthly_spend, category_spend, merchant_spend from anon, authenticated;
grant select on monthly_spend, category_spend, merchant_spend to authenticated;
grant all on monthly_spend, category_spend, merchant_spend to service_role;

-- Realtime: the PWA watches connection status. Only on Supabase, where the publication exists.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table connections;
  end if;
end $$;
