"""Usage metering, daily limits and budget levels (IST days and months)."""
import threading
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from helpers import A, B

from server import budget, limits, usage
from server.config import Settings

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def s():
    return Settings()


def _job(pg, user, site, trigger, created):
    pg.execute("insert into jobs (user_id, type, site, profile, payload, status, created_at) "
               "values (%s, 'sync', %s, %s, %s::jsonb, 'done', %s)",
               (user, site, "swiggy" if site == "instamart" else site, '{"trigger": "%s"}' % trigger, created))


def test_cost_inr_formula(s):
    assert budget.cost_inr(1000, 10_000, 600, s) == pytest.approx(400 + 0.5 + 2.0)


def test_level_thresholds(s):
    assert [budget.level_for(x, s) for x in (3999.99, 4000, 5000)] == ["ok", "alert", "paused"]


def test_today_is_ist():
    assert usage.today(datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc)) == date(2026, 10, 28)
    assert usage.month_of(datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc), "Asia/Kolkata") == "2026-11"


def test_day_bounds_are_utc_of_ist_day():
    start, end = usage.day_bounds(datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 27, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 28, 18, 30, tzinfo=timezone.utc)


def test_usage_record_accumulates(pg):
    d = date(2026, 10, 27)
    usage.record(pg, A, d, proxy_mb=1.5, ai_tokens=100, worker_seconds=2.0, syncs=1)
    usage.record(pg, A, d, proxy_mb=0.5, ai_tokens=50, worker_seconds=1.0, cart_checks=1)
    assert pg.execute("select proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks from usage "
                      "where user_id = %s and day = %s", (A, d)).fetchall() == [(2.0, 150, 3.0, 1, 1)]


def test_month_cost_uses_ist_month_and_fixed(pg):
    usage.record(pg, A, date(2026, 10, 31), proxy_mb=1000)
    usage.record(pg, B, date(2026, 10, 2), worker_seconds=600)
    usage.record(pg, A, date(2026, 11, 1), proxy_mb=1000)
    s = Settings(fixed_monthly_inr=100)
    assert budget.month_cost(pg, s, "2026-10") == pytest.approx(400 + 2.0 + 100)
    assert budget.month_cost(pg, s, "2026-11") == pytest.approx(400 + 100)


def test_refresh_state_reports_previous_level(pg):
    s = Settings(fixed_monthly_inr=0)
    now = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
    st = budget.refresh_state(pg, s, now)
    assert (st.month, st.level, st.previous_level, st.pct) == ("2026-10", "ok", "ok", 0)
    usage.record(pg, A, date(2026, 10, 27), proxy_mb=10_000)   # 4000 rupees = 80 %
    st = budget.refresh_state(pg, s, now)
    assert (st.level, st.previous_level, st.pct, st.spent_inr, st.budget_inr) == ("alert", "ok", 80, 4000.0, 5000.0)
    st = budget.refresh_state(pg, s, now)
    assert (st.level, st.previous_level) == ("alert", "alert")
    usage.record(pg, A, date(2026, 10, 27), proxy_mb=3000)
    st = budget.refresh_state(pg, s, now)
    assert (st.level, st.previous_level) == ("paused", "alert")
    assert budget.current_level(pg, now, "Asia/Kolkata") == "paused" and budget.is_paused(pg, now, "Asia/Kolkata")


def test_new_month_is_ok(pg):
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10','paused',5000,5000)")
    assert budget.current_level(pg, datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc), "Asia/Kolkata") == "paused"
    # 18:30 UTC on the 31st is 00:00 IST on 1 November
    assert budget.current_level(pg, datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc), "Asia/Kolkata") == "ok"


def test_refresh_left_counts_manual_jobs_today_only(pg, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    assert limits.refresh_left(pg, A, "zepto", now, s) == 3
    _job(pg, A, "zepto", "manual", now - timedelta(hours=1))
    _job(pg, A, "zepto", "schedule", now - timedelta(hours=2))
    _job(pg, A, "blinkit", "manual", now - timedelta(hours=1))
    _job(pg, B, "zepto", "manual", now - timedelta(hours=1))
    assert limits.refresh_left(pg, A, "zepto", now, s) == 2
    assert limits.refresh_left(pg, A, "blinkit", now, s) == 2


def test_refresh_left_resets_at_ist_midnight(pg, s):
    # 23:50 IST on the 27th = 18:20 UTC; 00:10 IST on the 28th = 18:40 UTC
    for _ in range(3):
        _job(pg, A, "zepto", "manual", datetime(2026, 10, 27, 18, 20, tzinfo=timezone.utc))
    assert limits.refresh_left(pg, A, "zepto", datetime(2026, 10, 27, 18, 25, tzinfo=timezone.utc), s) == 0
    assert limits.refresh_left(pg, A, "zepto", datetime(2026, 10, 27, 18, 40, tzinfo=timezone.utc), s) == 3


def test_check_refresh_message(pg, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    for _ in range(3):
        limits.check_refresh(pg, A, "zepto", now, s)
        _job(pg, A, "zepto", "manual", now)
    with pytest.raises(limits.LimitExceeded) as e:
        limits.check_refresh(pg, A, "zepto", now, s)
    assert e.value.message == "You can refresh Zepto 3 times a day. Try again tomorrow."
    assert e.value.kind == "refresh" and e.value.limit == 3 and e.value.retry_after_s > 0


def test_seconds_until_reset():
    assert limits.seconds_until_reset(datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc), "Asia/Kolkata") == 1800


def test_consume_cart_check_stops_at_ten(pg, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    assert limits.cart_checks_left(pg, A, now, s) == 10
    assert [limits.consume_cart_check(pg, A, now, s) for _ in range(10)] == list(range(9, -1, -1))
    with pytest.raises(limits.LimitExceeded) as e:
        limits.consume_cart_check(pg, A, now, s)
    assert e.value.message == "You can check 10 carts a day. Try again tomorrow."
    assert pg.execute("select cart_checks from usage where user_id = %s", (A,)).fetchone() == (10,)
    assert limits.cart_checks_left(pg, A, now, s) == 0


def test_consume_cart_check_is_atomic(pg, _applied, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    usage.record(pg, A, usage.today(now), cart_checks=9)
    results = []
    barrier = threading.Barrier(2)

    def go():
        with psycopg.connect(_applied, autocommit=True) as c:
            barrier.wait(timeout=10)
            try:
                results.append(limits.consume_cart_check(c, A, now, s))
            except limits.LimitExceeded:
                results.append("limit")

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(map(str, results)) == ["0", "limit"]
    assert pg.execute("select cart_checks from usage where user_id = %s", (A,)).fetchone() == (10,)


def test_consume_cart_check_when_budget_paused(pg, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10','paused',5000,5000)")
    with pytest.raises(limits.BudgetPaused) as e:
        limits.consume_cart_check(pg, A, now, s)
    assert e.value.resumes_on == date(2026, 11, 1)
    assert e.value.message == "Cart checks are paused until 1 November."
    assert pg.execute("select count(*) from usage").fetchone() == (0,)


def test_to_http():
    h = limits.to_http(limits.LimitExceeded("cart", 10, 120, "msg"))
    assert (h.status_code, h.detail, h.headers["Retry-After"]) == (429, "msg", "120")
    h = limits.to_http(limits.BudgetPaused("cart", date(2026, 11, 1), "paused"))
    assert (h.status_code, h.detail) == (503, "paused")


def test_refresh_state_concurrent_ticks_report_one_transition(pg, _applied):
    s = Settings()
    now = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
    usage.record(pg, A, date(2026, 10, 27), proxy_mb=10_000)   # 80 %: alert
    barrier = threading.Barrier(2)
    seen = []

    def tick():
        with psycopg.connect(_applied, autocommit=True) as c:
            barrier.wait(timeout=10)
            seen.append(budget.refresh_state(c, s, now).previous_level)

    ts = [threading.Thread(target=tick) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(seen) == ["alert", "ok"]


def test_level_uses_unrounded_spend_and_pct_floors(pg):
    s = Settings()
    assert budget.level_for(3999.999, s) == "ok"
    usage.record(pg, A, date(2026, 10, 27), proxy_mb=9999.999)   # 3999.9996 rupees, stores as 4000.0
    st = budget.refresh_state(pg, s, datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc))
    assert (st.level, st.pct, st.spent_inr) == ("ok", 79, 4000.0)


def test_naive_now_is_taken_as_utc():
    naive = datetime(2026, 10, 27, 18, 0)
    assert limits.seconds_until_reset(naive, "Asia/Kolkata") == 1800
    assert usage.today(naive, "Asia/Kolkata") == date(2026, 10, 27)


def test_usage_record_cart_fills(pg):
    d = date(2026, 10, 27)
    usage.record(pg, A, d, cart_fills=2)
    usage.record(pg, A, d, cart_fills=1, syncs=1)
    assert pg.execute("select cart_fills, syncs from usage where user_id = %s", (A,)).fetchone() == (3, 1)


def test_consume_cart_fill_stops_at_five(pg, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    assert limits.cart_fills_left(pg, A, now, s) == 5
    assert [limits.consume_cart_fill(pg, A, now, s) for _ in range(5)] == [4, 3, 2, 1, 0]
    with pytest.raises(limits.LimitExceeded) as e:
        limits.consume_cart_fill(pg, A, now, s)
    assert (e.value.kind, e.value.limit) == ("fill", 5)
    assert e.value.retry_after_s == limits.seconds_until_reset(now, s.app_tz)
    assert e.value.message == "You can fill 5 carts a day. Try again tomorrow."
    assert pg.execute("select cart_fills, cart_checks from usage where user_id = %s", (A,)).fetchone() == (5, 0)
    assert limits.cart_fills_left(pg, A, now, s) == 0
    assert limits.cart_fills_left(pg, B, now, s) == 5


def test_consume_cart_fill_is_atomic(pg, _applied, s):
    now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    usage.record(pg, A, usage.today(now), cart_fills=4)
    results = []
    barrier = threading.Barrier(2)

    def go():
        with psycopg.connect(_applied, autocommit=True) as c:
            barrier.wait(timeout=10)
            try:
                results.append(limits.consume_cart_fill(c, A, now, s))
            except limits.LimitExceeded:
                results.append("limit")

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(map(str, results)) == ["0", "limit"]
    assert pg.execute("select cart_fills from usage where user_id = %s", (A,)).fetchone() == (5,)


def test_consume_cart_fill_when_budget_paused(pg, s):
    now = datetime(2026, 12, 27, 6, 0, tzinfo=timezone.utc)
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-12','paused',5000,5000)")
    with pytest.raises(limits.BudgetPaused) as e:
        limits.consume_cart_fill(pg, A, now, s)
    assert (e.value.kind, e.value.resumes_on) == ("fill", date(2027, 1, 1))
    assert e.value.message == "Filling carts is paused until 1 January."
    assert pg.execute("select count(*) from usage").fetchone() == (0,)


def test_refund_cart_fill_never_below_zero(pg):
    d = date(2026, 10, 27)
    usage.record(pg, A, d, cart_fills=1)
    limits.refund_cart_fill(pg, A, d)
    limits.refund_cart_fill(pg, A, d)
    limits.refund_cart_fill(pg, B, d)               # no row: nothing happens
    assert pg.execute("select user_id::text, cart_fills from usage").fetchall() == [(A, 0)]


def test_cost_inr_prices_input_and_output_separately(s):
    from dataclasses import replace
    s = replace(s, ai_inr_per_1k_input_tokens=0.01, ai_inr_per_1k_output_tokens=0.05)
    # 10k in + 2k out, plus 1k Jev token-equivalents (unsplit, priced at the output rate)
    assert budget.cost_inr(0, 13_000, 0, s, 10_000, 2_000) == pytest.approx(0.10 + 0.10 + 0.05)


def test_month_cost_is_read_inside_the_budget_lock(pg, pg2, monkeypatch):
    seen = []
    real = budget.month_cost

    def spy(conn, s, month):
        try:
            pg2.execute("select 1 from budget_state where month = %s for update nowait", (month,))
            seen.append("unlocked")
        except psycopg.errors.LockNotAvailable:
            seen.append("locked")
        pg2.rollback()
        return real(conn, s, month)
    monkeypatch.setattr(budget, "month_cost", spy)
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10','ok',0,5000)")
    budget.refresh_state(pg, Settings(fixed_monthly_inr=0), datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc))
    assert seen == ["locked"]


def test_budget_resets_at_the_ist_month_boundary(pg):
    s = Settings(fixed_monthly_inr=5000)   # the whole budget: October is paused
    last = datetime(2026, 10, 31, 23, 59, tzinfo=IST)
    first = datetime(2026, 11, 1, 0, 1, tzinfo=IST)
    st = budget.refresh_state(pg, s, last)
    assert (st.month, st.level) == ("2026-10", "paused")
    assert budget.is_paused(pg, last, "Asia/Kolkata")
    st = budget.refresh_state(pg, Settings(fixed_monthly_inr=0), first)
    assert (st.month, st.level, st.previous_level) == ("2026-11", "ok", "ok")
    assert not budget.is_paused(pg, first, "Asia/Kolkata")
    assert budget.current_level(pg, last, "Asia/Kolkata") == "paused"
