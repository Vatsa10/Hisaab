-- Workers heartbeat running jobs so a long sync is not mistaken for a dead worker, and the one-running-job
-- lock is per Chrome profile (instamart shares swiggy's profile), not per site.
alter table jobs add column heartbeat_at timestamptz, add column profile text;

update jobs set profile = case site when 'instamart' then 'swiggy' else site end;

drop index one_running_job_per_profile;
create unique index one_running_job_per_profile on jobs (user_id, profile)
  where status = 'running';
