-- Every job names the Chrome profile it locks (the one-running-job index is per profile): a NULL profile
-- would escape that lock. Backfill any row written before the column existed, then require it.
update jobs set profile = case site when 'instamart' then 'swiggy' else site end where profile is null;

alter table jobs alter column profile set not null;
