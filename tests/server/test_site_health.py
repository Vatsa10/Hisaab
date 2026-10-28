"""Per-site backoff and the error budget."""
from datetime import datetime, timedelta, timezone

import pytest
from helpers import A
from psycopg.types.json import Jsonb

from server import recipe_admin, site_health
from server.config import Settings

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
S = Settings()


def _throttle(pg, site="zepto", now=NOW, retry=0, kind="rate_limited"):
    return site_health.record_throttle(pg, site, kind, retry, S, now=now, rand=lambda: 0)


def _secs(pg, site="zepto", now=NOW):
    return (site_health.cooldown_until(pg, site, now=now) - now).total_seconds()


def test_backoff_doubles_and_caps(pg):
    for want in (60, 120, 240, 480):
        until = _throttle(pg)
        assert (until - NOW).total_seconds() == want
    for _ in range(3):
        _throttle(pg)  # level 7 -> 3840 capped
    until = _throttle(pg)  # level 8
    assert (until - NOW).total_seconds() == 3600


def test_retry_after_wins_when_longer(pg):
    assert (_throttle(pg, retry=500) - NOW).total_seconds() == 500
    pg.execute("delete from site_backoff")
    assert (_throttle(pg, retry=10) - NOW).total_seconds() == 60


def test_level_resets_after_quiet_period(pg):
    for _ in range(4):
        _throttle(pg)
    later = NOW + timedelta(hours=25)
    until = _throttle(pg, now=later)
    assert (until - later).total_seconds() == 60
    assert pg.execute("select level, events from site_backoff where site='zepto'").fetchone() == (1, 5)


def test_throttle_on_instamart_cools_swiggy(pg):
    _throttle(pg, site="instamart", kind="challenge")
    assert _secs(pg, "swiggy") == 60
    assert _secs(pg, "instamart") == 60
    assert site_health.cooldown_until(pg, "zepto", now=NOW) is None
    assert pg.execute("select last_kind from site_backoff where site='swiggy'").fetchone() == ("challenge",)


def test_success_resets_level(pg):
    _throttle(pg)
    _throttle(pg)
    site_health.record_success(pg, "zepto")
    assert pg.execute("select level, until from site_backoff where site='zepto'").fetchone() == (0, None)
    assert site_health.cooldown_until(pg, "zepto", now=NOW) is None
    assert (_throttle(pg) - NOW).total_seconds() == 60
    site_health.record_success(pg, "blinkit")  # no row: stays absent
    assert pg.execute("select count(*) from site_backoff where site='blinkit'").fetchone() == (0,)


def test_cooldown_ends_at_until(pg):
    until = _throttle(pg)
    assert site_health.cooldown_until(pg, "zepto", now=until) is None


def test_raise_if_cooling_message_and_retry_after(pg):
    site_health.raise_if_cooling(pg, "zepto", now=NOW)  # nothing yet
    _throttle(pg, retry=200)
    with pytest.raises(site_health.SiteCooling) as ei:
        site_health.raise_if_cooling(pg, "zepto", now=NOW)
    e = ei.value
    assert e.site == "zepto"
    assert e.message == "Zepto is limiting requests right now. Try again in about 4 minutes."
    h = site_health.to_http(e)
    assert h.status_code == 503
    assert h.headers["Retry-After"] == "200"


# ---------------------------------------------------------------- error budget

def _recipe(pg, site="zepto", kind="orders"):
    pg.execute("insert into recipes(site, kind, recipe, version) values (%s,%s,%s,1)", (site, kind, Jsonb({})))


def _syncs(pg, n_ok, failures, site="zepto", ago_h=1):
    when = NOW - timedelta(hours=ago_h)
    for i in range(n_ok):
        pg.execute("insert into jobs(user_id,type,site,profile,status,finished_at) values (%s,'sync',%s,%s,'done',%s)",
                   (A, site, site, when))
    for err in failures:
        pg.execute("insert into jobs(user_id,type,site,profile,status,error,finished_at) "
                   "values (%s,'sync',%s,%s,'failed',%s,%s)", (A, site, site, err, when))


def _admins(pg):
    for i, e in enumerate(("a@x.com", "b@x.com")):
        pg.execute("insert into admins(email) values (%s)", (e,))


def _alerts(pg):
    return pg.execute("select count(*) from notifications where kind='error_budget'").fetchone()[0]


def test_error_budget_pauses_and_alerts_once(pg):
    _recipe(pg)
    _admins(pg)
    _syncs(pg, 6, ["RuntimeError: boom"] * 4)
    assert site_health.check_error_budget(pg, S, NOW) == [("zepto", "orders")]
    assert recipe_admin.is_paused(pg, "zepto")
    note = "Paused automatically: 4 of 10 syncs failed in the last 24 hours."
    assert pg.execute("select health_note from recipes").fetchone() == (note,)
    assert _alerts(pg) == 2
    row = pg.execute("select title, body from notifications where kind='error_budget' limit 1").fetchone()
    assert row == ("Zepto sync paused: too many failures", note + " Check it in the admin view, then resume it.")
    assert site_health.check_error_budget(pg, S, NOW) == []
    assert _alerts(pg) == 2


def test_user_caused_failures_do_not_count(pg):
    _recipe(pg)
    _syncs(pg, 6, ["needs_login"] * 3 + ["worker_lost"] * 3)
    assert site_health.check_error_budget(pg, S, NOW) == []
    assert not recipe_admin.is_paused(pg, "zepto")
    stats = site_health.failure_stats(pg, NOW, 24)
    assert stats == [{"site": "zepto", "kind": "orders", "total": 6, "failed": 0, "pct": 0}]


def test_below_min_jobs_never_pauses(pg):
    _recipe(pg)
    _syncs(pg, 0, ["RuntimeError: x"] * 9)
    assert site_health.check_error_budget(pg, S, NOW) == []
    assert site_health.failure_stats(pg, NOW, 24)[0]["pct"] == 100


def test_pct_rounds_down(pg):
    _syncs(pg, 2, ["RuntimeError: x"])
    assert site_health.failure_stats(pg, NOW, 24)[0]["pct"] == 33


def test_missing_recipe_row_is_skipped(pg):
    _syncs(pg, 0, ["RuntimeError: x"] * 10)
    assert site_health.check_error_budget(pg, S, NOW) == []


def test_failures_before_resume_do_not_count(pg):
    _recipe(pg)
    _syncs(pg, 0, ["RuntimeError: x"] * 10, ago_h=2)
    recipe_admin.pause_site(pg, "zepto", "orders", "n")
    recipe_admin.resume_site(pg, "zepto", "orders")
    pg.execute("update recipes set paused_changed_at = %s", (NOW - timedelta(hours=1),))
    _syncs(pg, 0, ["RuntimeError: y"] * 2, ago_h=0)
    assert site_health.check_error_budget(pg, S, NOW) == []
    assert not recipe_admin.is_paused(pg, "zepto")


def _request(pg, mode="grocery"):
    return pg.execute("insert into cart_requests(user_id, mode, items, sites, area, lat, lon) "
                      "values (%s,%s,'[]',%s,'a',1,1) returning id",
                      (A, mode, ["zepto"])).fetchone()[0]


def _result(pg, rid, site, status, hours_ago=1):
    pg.execute("insert into cart_results(request_id,user_id,site,status,priced_at) values (%s,%s,%s,%s,%s)",
               (rid, A, site, status, NOW - timedelta(hours=hours_ago)))


def test_search_budget_counts_failed_results_only(pg):
    _recipe(pg, "zepto", "search")
    _admins(pg)
    for i in range(6):
        _result(pg, _request(pg), "zepto", "ok")
    for i in range(3):
        _result(pg, _request(pg), "zepto", "unavailable")
        _result(pg, _request(pg), "zepto", "needs_login")
    assert site_health.check_error_budget(pg, S, NOW) == []
    for i in range(4):
        _result(pg, _request(pg), "zepto", "failed")
    assert site_health.check_error_budget(pg, S, NOW) == [("zepto", "search")]
    assert pg.execute("select title from notifications limit 1").fetchone() == ("Zepto prices paused: too many failures",)
    assert "4 of 10 price checks failed" in pg.execute("select health_note from recipes").fetchone()[0]


def test_food_failures_pause_menu_recipe_not_orders(pg):
    _recipe(pg, "swiggy", "orders")
    _recipe(pg, "swiggy", "menu")
    for i in range(10):
        _result(pg, _request(pg, "food"), "swiggy", "failed")
    assert site_health.check_error_budget(pg, S, NOW) == [("swiggy", "menu")]
    assert not recipe_admin.is_paused(pg, "swiggy", "orders")
    assert recipe_admin.is_paused(pg, "swiggy", "menu")


def test_exactly_at_budget_does_not_pause(pg):
    _recipe(pg)
    _syncs(pg, 7, ["RuntimeError: x"] * 3)
    assert site_health.failure_stats(pg, NOW, 24)[0]["pct"] == 30
    assert site_health.check_error_budget(pg, S, NOW) == []
    assert not recipe_admin.is_paused(pg, "zepto")


def test_timeout_errors_are_site_faults(pg):
    _recipe(pg)
    _syncs(pg, 6, ["otp_timeout"] * 4)
    assert site_health.check_error_budget(pg, S, NOW) == [("zepto", "orders")]


def test_pause_trigger_stamps_and_blocks_earlier_failures(pg):
    """The real recipes_paused_changed trigger (DB now()), not a hand-set stamp."""
    _recipe(pg)
    assert pg.execute("select paused_changed_at from recipes").fetchone() == (None,)
    recipe_admin.pause_site(pg, "zepto", "orders", "n")
    assert pg.execute("select paused_changed_at is not null and paused_changed_at <= now() from recipes"
                      ).fetchone() == (True,)
    pg.execute("update recipes set health_note='x'")
    first = pg.execute("select paused_changed_at from recipes").fetchone()[0]
    recipe_admin.resume_site(pg, "zepto", "orders")
    stamp, db_now = pg.execute("select paused_changed_at, now() from recipes").fetchone()
    assert stamp >= first
    for _ in range(10):
        pg.execute("insert into jobs(user_id,type,site,profile,status,error,finished_at) "
                   "values (%s,'sync','zepto','zepto','failed','RuntimeError: x',%s)",
                   (A, db_now - timedelta(hours=1)))
    later = db_now + timedelta(seconds=1)
    assert site_health.failure_stats(pg, later, 24) == []
    assert site_health.check_error_budget(pg, S, later) == []
