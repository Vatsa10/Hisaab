-- Plan 4 final review: notifications are claimed ('sending') before they are sent outside the transaction, and
-- AI usage records input and output tokens separately (they are priced differently).
alter table notifications add column if not exists claimed_at timestamptz;
alter table notifications drop constraint if exists notifications_status_check;
alter table notifications add constraint notifications_status_check
  check (status in ('pending','sending','sent','failed'));

-- ai_tokens stays the total (including Jev token-equivalents); these two are the priced Claude split.
alter table usage add column if not exists ai_input_tokens bigint not null default 0;
alter table usage add column if not exists ai_output_tokens bigint not null default 0;
alter table usage_erased add column if not exists ai_input_tokens bigint not null default 0;
alter table usage_erased add column if not exists ai_output_tokens bigint not null default 0;
