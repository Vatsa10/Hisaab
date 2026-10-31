"""Warm Chrome (Plan 7 Task 16 fix): one Chrome per (user, profile, mode) kept between jobs, closed on idle,
shutdown, a different user or mode, and any failed job. Chrome and Playwright are faked."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from helpers import A, B, add_user, order, settings_for_tests
from test_worker_plan7 import _claim

import browser
from server.config import Settings
from server import worker


ROUTES = []


class FakeProc:
    launched = []

    def __init__(self, args, **k):
        self.dead = False
        self.args = args
        FakeProc.launched.append(self)

    def poll(self):
        return 1 if self.dead else None

    def wait(self, timeout=None):
        self.dead = True

    def kill(self):
        self.dead = True


@pytest.fixture
def chrome(monkeypatch, tmp_path):
    FakeProc.launched = []
    browser.close_warm()
    monkeypatch.setattr(browser.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(browser, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(browser, "_free_port", lambda: 9)
    monkeypatch.setattr(browser, "_wait_for_cdp", lambda *a, **k: None)
    pw = MagicMock()
    ctx = MagicMock()
    ctx.pages = [MagicMock()]
    ROUTES.clear()
    ctx.route.side_effect = lambda pattern, handler: ROUTES.append(handler)
    ctx.unroute_all.side_effect = lambda **k: ROUTES.clear()
    br = pw.chromium.connect_over_cdp.return_value
    br.contexts = [ctx]
    br.new_browser_cdp_session.return_value.send.return_value = {"targetInfos": []}
    monkeypatch.setattr(browser, "sync_playwright", lambda: MagicMock(start=lambda: pw))
    yield FakeProc.launched
    browser.close_warm()


def _job_open(key, tmp_path, user="u"):
    with browser.warm_scope(key):
        with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / user):
            pass


def test_same_key_launches_chrome_once(chrome, tmp_path):
    _job_open(("u", "zepto", False), tmp_path)
    _job_open(("u", "zepto", False), tmp_path)
    assert len(chrome) == 1 and not chrome[0].dead


def test_different_user_or_mode_launches_fresh_and_closes_the_old(chrome, tmp_path):
    _job_open(("u", "zepto", False), tmp_path)
    _job_open(("v", "zepto", False), tmp_path, "v")
    assert len(chrome) == 2 and chrome[0].dead and not chrome[1].dead
    _job_open(("v", "zepto", True), tmp_path, "v")  # cart mode never reuses a non-cart browser
    assert len(chrome) == 3 and chrome[1].dead


def test_idle_expiry_launches_fresh(chrome, tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(browser.POOL, "clock", lambda: now[0])
    browser.POOL.idle_s = 120
    _job_open(("u", "zepto", False), tmp_path)
    now[0] = 60
    _job_open(("u", "zepto", False), tmp_path)
    assert len(chrome) == 1
    now[0] = 400
    browser.close_idle_warm()
    assert chrome[0].dead and browser.POOL.handle is None
    _job_open(("u", "zepto", False), tmp_path)
    assert len(chrome) == 2


def test_shutdown_closes_the_pool(chrome, tmp_path):
    _job_open(("u", "zepto", False), tmp_path)
    cleaned = []
    browser.POOL.handle.cleanups.append(lambda: cleaned.append(1))
    browser.close_warm()
    assert chrome[0].dead and browser.POOL.handle is None and cleaned == [1]


def test_a_failing_job_closes_the_browser(chrome, tmp_path):
    with pytest.raises(browser.SessionExpired):
        with browser.warm_scope(("u", "zepto", False)):
            with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / "u"):
                raise browser.SessionExpired("401")
    assert chrome[0].dead and browser.POOL.handle is None


def test_login_never_uses_the_pool(chrome, tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(browser, "_open_site_cold", lambda *a, **k: used.append(1) or MagicMock())
    with browser.warm_scope(("u", "zepto", False)):
        browser.open_site("zepto", login_mode=True, profile_dir=tmp_path)
    assert used == [1] and chrome == []


def test_worker_reuses_chrome_for_the_same_user_only(pg, chrome, monkeypatch):
    add_user(pg, B, "b@example.com")

    def fetch(site, since, **k):
        with browser.open_site(site, allowlist=[], profile_dir=k["profile_dir"]):
            pass
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    s = settings_for_tests()
    for user in (A, A, B):
        jid, job = _claim(pg, "sync", user=user)
        worker.run_job(job, pg, s)
    assert len(chrome) == 2 and chrome[0].dead and not chrome[1].dead
    assert Path(browser.POOL.handle.profile).exists()  # the warm Chrome keeps its profile directory
    kept = Path(browser.POOL.handle.profile)
    browser.close_warm()
    assert not kept.exists()  # closing the pool removes the profile directory it kept


# ---------------------------------------------------------------- fix round 2

@pytest.fixture
def saves(monkeypatch):
    """Spy on vault saves; every Chrome wait() and save is logged in order."""
    from server import vault
    events = []
    monkeypatch.setattr(worker.vault, "save_profile", lambda store, mk, uid, prof, d: events.append(("save", uid)) or "k")
    real_wait = FakeProc.wait

    def wait(self, timeout=None):
        events.append(("wait",))
        real_wait(self, timeout)
    monkeypatch.setattr(FakeProc, "wait", wait)
    return events


def _sync(pg, monkeypatch, user=A, fetch=None):
    def default(site, since, **k):
        with browser.open_site(site, allowlist=[], profile_dir=k["profile_dir"]):
            pass
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch or default)
    jid, job = _claim(pg, "sync", user=user)
    worker.run_job(job, pg, settings_for_tests())
    return jid


def test_no_vault_save_while_chrome_is_warm_then_one_save_after_close(pg, chrome, saves, monkeypatch):
    _sync(pg, monkeypatch)
    _sync(pg, monkeypatch)
    assert [e for e in saves if e[0] == "save"] == []  # the profile is not tarred under a live Chrome
    browser.close_warm()
    assert saves[-2:] == [("wait",), ("save", A)]  # saved once, after Chrome exited
    assert sum(1 for e in saves if e[0] == "save") == 1


def test_a_failed_close_never_saves(pg, chrome, saves, monkeypatch):
    _sync(pg, monkeypatch)
    browser.close_warm(save=False)
    assert [e for e in saves if e[0] == "save"] == []
    _sync(pg, monkeypatch, fetch=lambda *a, **k: (_ for _ in ()).throw(browser.SessionExpired("401")))
    assert [e for e in saves if e[0] == "save"] == [] and browser.POOL.handle is None


def test_switching_user_saves_the_old_profile_once(pg, chrome, saves, monkeypatch):
    add_user(pg, B, "b@example.com")
    _sync(pg, monkeypatch, user=A)
    _sync(pg, monkeypatch, user=B)
    assert [e for e in saves if e[0] == "save"] == [("save", A)]


class FakeRelay:
    def __init__(self, n):
        self.bytes_total = n


def test_record_usage_counts_only_the_jobs_own_proxy_bytes():
    sent = {}
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (1,)
    from server import usage
    real = usage.record
    usage.record = lambda c, uid, day, **k: sent.update(k)
    try:
        worker._record_usage(conn, MagicMock(user_id=A, type="sync", id="j"), Settings(), MagicMock(
            ai_tokens=lambda n: 0), FakeRelay(15_000_000), 1.0, base_bytes=10_000_000)
    finally:
        usage.record = real
    assert sent["proxy_mb"] == 5.0


def test_warm_job_meters_the_delta_on_the_kept_relay(pg, chrome, monkeypatch):
    _sync(pg, monkeypatch)
    relay = FakeRelay(10_000_000)  # the first job's own bytes are already behind it
    browser.POOL.handle.relay = relay

    def fetch(site, since, **k):
        relay.bytes_total += 3_000_000
        return [order("1")]
    _sync(pg, monkeypatch, fetch=fetch)
    mb = pg.execute("select sum(proxy_mb) from usage where user_id=%s", (A,)).fetchone()[0]
    assert round(mb, 3) == 3.0


def test_warm_chrome_is_not_reused_after_another_worker_saved_the_profile(pg, chrome, tmp_path, monkeypatch):
    from server import sites, vault
    s = settings_for_tests()
    _sync(pg, monkeypatch)
    assert len(chrome) == 1
    first, name = chrome[0], sites.profile_for("zepto")
    other = tmp_path / "other"
    vault.stamp(other, A, name)  # another worker saved a newer copy of this user's profile
    vault.save_profile(vault.store_from_url(s.profile_store_url), s.profile_master_key, A, name, other)
    _sync(pg, monkeypatch)
    assert first.dead and len(chrome) == 2  # the stale session was dropped and the profile restored cold


def test_reused_key_has_only_the_new_jobs_guard(chrome, tmp_path):
    key = ("u", "zepto", False)
    with browser.warm_scope(key):
        with browser.open_site("zepto", allowlist=[{"url": "a"}], profile_dir=tmp_path / "u", block_media=True) as (
                _c, _p, g1, _cap):
            assert len(ROUTES) == 1 and ROUTES[0].__self__ is g1
    with browser.warm_scope(key):
        with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / "u", block_media=False) as (
                _c, _p, g2, _cap):
            assert len(chrome) == 1 and len(ROUTES) == 1
            assert ROUTES[0].__self__ is g2 and g2.block_media is False and g2 is not g1


def _newer_blob(tmp_path):
    from server import sites, vault
    s = settings_for_tests()
    name = sites.profile_for("zepto")
    d = tmp_path / "other"
    vault.stamp(d, A, name)
    vault.store_from_url(s.profile_store_url).put(
        vault.blob_key(A, name), vault._seal(s.profile_master_key, A, name, vault._pack(d)))


def test_close_time_save_is_skipped_when_another_worker_saved_newer(pg, chrome, saves, tmp_path, monkeypatch):
    _sync(pg, monkeypatch)
    _newer_blob(tmp_path)
    browser.close_warm()
    assert [e for e in saves if e[0] == "save"] == []


def test_close_time_save_is_skipped_when_the_user_is_gone(pg, chrome, saves, monkeypatch):
    _sync(pg, monkeypatch)
    pg.execute("delete from auth.users where id=%s", (A,))
    browser.close_warm()
    assert [e for e in saves if e[0] == "save"] == []


def test_close_time_save_uses_a_fresh_connection_when_the_jobs_is_closed(pg, chrome, saves, monkeypatch):
    _sync(pg, monkeypatch)
    pg.close()
    browser.close_warm()
    assert [e for e in saves if e[0] == "save"] == [("save", A)]
