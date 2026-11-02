-- OTP auto-forward (Android companion). Server-only tables: RLS on with no policies, no grants to anon or
-- authenticated, so only the service role reaches them. Tokens and pairing codes are stored as sha256 hex only.

create table if not exists otp_devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text,
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  last_seen_at timestamptz,
  revoked_at timestamptz
);
create index if not exists otp_devices_user on otp_devices (user_id);
alter table otp_devices enable row level security;
revoke all on otp_devices from anon, authenticated;
grant all on otp_devices to service_role;

create table if not exists otp_pairings (
  code_hash text primary key check (code_hash ~ '^[0-9a-f]{64}$'),
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '10 minutes',
  used_at timestamptz
);
create index if not exists otp_pairings_user on otp_pairings (user_id, created_at);
alter table otp_pairings enable row level security;
revoke all on otp_pairings from anon, authenticated;
grant all on otp_pairings to service_role;
