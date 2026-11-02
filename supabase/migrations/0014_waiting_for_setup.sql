-- A user who logged in to a site that has no orders recipe yet is connected but waiting for an admin to set the
-- site up. Set when the connect job ends that way, cleared by the first successful sync.
alter table connections add column if not exists waiting_for_setup_at timestamptz;
