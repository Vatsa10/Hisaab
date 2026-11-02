-- Learned login scripts (kind 'login'): a site's login steps up to "send OTP", replayed with no model.
alter table recipes drop constraint recipes_kind_check,
  add constraint recipes_kind_check check (kind in ('orders','search','cart','restaurants','menu','addresses','login'));
alter table recipe_approvals drop constraint recipe_approvals_kind_check,
  add constraint recipe_approvals_kind_check check (kind in ('orders','search','cart','restaurants','menu','addresses','login'));
alter table recipe_approvals drop constraint recipe_approvals_reason_check,
  add constraint recipe_approvals_reason_check check (reason in ('post_allowlist','account_specific','cart','learned',
                                                                 'drift_relearn','login_learned','login_drift'));
