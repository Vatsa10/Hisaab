-- One browser per Chrome profile: a user's connect and sync jobs for the same site must never run at the
-- same time. jobs.claim skips a candidate whose (user_id, site) already has a running job.
create unique index one_running_job_per_profile on jobs (user_id, site)
  where status = 'running';
