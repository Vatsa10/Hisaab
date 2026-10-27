from datetime import datetime, timedelta

import pytest
from helpers import A, settings_for_tests

import sync
from server import fake_sites, jobs, worker


@pytest.fixture
def fake_mode(monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setattr(sync, "login", lambda *a, **k: pytest.fail("real login must not run"))
    monkeypatch.setattr(sync, "fetch_orders", lambda *a, **k: pytest.fail("real sync must not run"))


def _prompts(monkeypatch, answers):
    kinds = []

    def make(*a, **k):
        def prompt(text, kind="otp"):
            kinds.append(kind)
            return answers[kind]
        return prompt
    monkeypatch.setattr(worker, "make_prompt", make)
    return kinds


def _connect(pg, phone="9876543210", site="zepto"):
    jobs.enqueue(pg, A, "connect", site, {"phone": phone})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())


def test_fake_connect_with_right_otp_stores_orders(pg, fake_mode, monkeypatch):
    kinds = _prompts(monkeypatch, {"otp": "123456"})
    _connect(pg)
    assert kinds == ["otp"]
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert pg.execute("select status, error from jobs").fetchone() == ("done", None)
    assert pg.execute("select count(*) from orders where user_id=%s", (A,)).fetchone()[0] > 0


def test_fake_wrong_otp_is_login_failed(pg, fake_mode, monkeypatch):
    _prompts(monkeypatch, {"otp": "000000"})
    _connect(pg)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "login_failed")
    assert pg.execute("select status from connections").fetchone() == ("error",)
    assert pg.execute("select count(*) from orders").fetchone() == (0,)


def test_fake_question_then_otp(pg, fake_mode, monkeypatch):
    kinds = _prompts(monkeypatch, {"otp": "123456", "question": "Vadodara"})
    _connect(pg, phone="9876540000")
    assert kinds == ["question", "otp"]
    assert pg.execute("select status from connections").fetchone() == ("connected",)


def test_fake_sync_swiggy_has_food_and_instamart(pg, fake_mode):
    jobs.enqueue(pg, A, "sync", "swiggy")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    rows = pg.execute("select distinct sub_platform from orders where site='swiggy'").fetchall()
    assert {r[0] for r in rows} == {None, "instamart"}


def test_fake_mode_refuses_remote_db(monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    with pytest.raises(RuntimeError):
        worker._check_fake_mode("postgresql://u:p@db.example.supabase.co:5432/postgres")
    worker._check_fake_mode("postgresql://postgres:postgres@localhost:54329/x")  # local is fine


def test_fake_mode_refusal_only_applies_when_enabled(monkeypatch):
    monkeypatch.delenv("WORKER_FAKE", raising=False)
    worker._check_fake_mode("postgresql://u:p@db.example.supabase.co:5432/postgres")
    monkeypatch.setenv("WORKER_FAKE", "true")  # only an explicit "1" turns fake mode on
    assert not worker._fake_mode()


def test_main_refuses_remote_db_in_fake_mode(monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.supabase.co:5432/postgres")
    monkeypatch.setattr(worker, "settings", lambda for_worker=False: settings_remote())
    with pytest.raises(RuntimeError):
        worker.main(max_loops=1)


def settings_remote():
    import dataclasses
    from server.config import Settings
    return dataclasses.replace(Settings(database_url="", jwt_secret="x" * 32, jwks_url=""),
                               database_url="postgresql://u:p@db.example.supabase.co:5432/postgres")


@pytest.mark.parametrize("site", ["zomato", "swiggy", "zepto", "blinkit", "instamart"])
def test_fake_orders_deterministic_recent_and_shaped(site):
    a, b = fake_sites.fetch_orders(site, None), fake_sites.fetch_orders(site, None)
    assert a == b and a
    now = datetime.now()
    placed = [datetime.fromisoformat(o["placed_at"]) for o in a]
    assert all(now - timedelta(days=185) <= p <= now for p in placed)
    assert max(placed) > now - timedelta(days=30) and min(placed) < now - timedelta(days=120)
    assert any(o["status"] == "cancelled" for o in a)
    assert len({o["order_id"] for o in a}) == len(a)
    for o in a:
        assert {"order_id", "placed_at", "total_amount", "currency", "status", "merchant", "sub_platform",
                "items", "raw"} <= set(o)
        assert o["items"] and all(i["name"] for i in o["items"])
    if site in ("zepto", "blinkit", "instamart"):
        assert all(i["price"] is None for o in a for i in o["items"])


def test_fake_swiggy_mixes_instamart_rows():
    subs = {o["sub_platform"] for o in fake_sites.fetch_orders("swiggy", None)}
    assert subs == {None, "instamart"}


def test_fake_login_logic():
    asked = []
    assert fake_sites.login("zepto", "9876543210", lambda t, k="otp": (asked.append(k), "123456")[1])
    assert not fake_sites.login("zepto", "9876543210", lambda t, k="otp": "1")
    asked.clear()
    fake_sites.login("zomato", "9000000000", lambda t, k="otp": (asked.append(k), "123456")[1])
    assert asked == ["question", "otp"]
