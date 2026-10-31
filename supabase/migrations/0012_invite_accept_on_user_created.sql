-- Plan 4 parked minors. An invite is accepted only once GoTrue has really created the user: the sign-up hook
-- (which runs before the user is committed) no longer stamps it; an after-insert trigger on auth.users does.
create or replace function public.before_user_created(event jsonb) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_email text := lower(trim(event->'user'->>'email'));
  v_month text := to_char(now() at time zone 'Asia/Kolkata', 'YYYY-MM');
begin
  if v_email is not null and v_email <> '' and exists (select 1 from admins where email = v_email) then
    return '{}'::jsonb;
  end if;

  if v_email is null or v_email = '' or not exists (select 1 from invites where email = v_email) then
    if v_email is not null and v_email <> '' then
      insert into waitlist (email) values (v_email) on conflict do nothing;
    end if;
    return jsonb_build_object('error', jsonb_build_object('http_code', 403, 'message', 'invite_only'));
  end if;

  if exists (select 1 from budget_state where month = v_month and level = 'paused') then
    return jsonb_build_object('error', jsonb_build_object('http_code', 403, 'message', 'signups_paused'));
  end if;

  return '{}'::jsonb;
end;
$$;

create function public.mark_invite_accepted() returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  update invites set accepted_at = now() where email = lower(trim(new.email)) and accepted_at is null;
  return new;
end;
$$;
revoke execute on function public.mark_invite_accepted() from public, anon, authenticated, service_role;

create trigger invite_accepted_on_signup after insert on auth.users
  for each row execute function public.mark_invite_accepted();
