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


def test_fake_fetch_reports_progress_in_batches(monkeypatch):
    monkeypatch.delenv("FAKE_PAGE_DELAY_S", raising=False)
    seen = []
    out = fake_sites.fetch_orders("zepto", None, on_progress=lambda c, a: seen.append((c, a)))
    assert [c for c, _ in seen] == [8, 16, 24, 28] and len(out) == 28
    final_spend = sum(o["total_amount"] for o in out if o["status"] not in ("cancelled", "refunded", "failed"))
    assert seen[-1][1] == pytest.approx(final_spend)
    assert all(a1 <= a2 for (_, a1), (_, a2) in zip(seen, seen[1:]))


def test_fake_fetch_waits_between_pages_only_when_asked(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fake_sites.time, "sleep", sleeps.append)
    monkeypatch.delenv("FAKE_PAGE_DELAY_S", raising=False)
    fake_sites.fetch_orders("zepto", None, on_progress=lambda c, a: None)
    assert sleeps == []
    monkeypatch.setenv("FAKE_PAGE_DELAY_S", "0.5")
    fake_sites.fetch_orders("zepto", None, on_progress=lambda c, a: None)
    assert sleeps == [0.5, 0.5, 0.5]  # between the 4 pages, none before the first


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


def test_fake_main_starts_with_only_a_local_database_url(monkeypatch, tmp_path):
    """No vault key, dev-key opt-in or proxy template: fake mode needs none of them to get through start-up."""
    for k in ("PROFILE_MASTER_KEY", "ALLOW_DEV_PROFILE_KEY", "PROXY_URL_TEMPLATE", "PROFILE_STORE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/none")
    monkeypatch.setenv("PROXY_URL_TEMPLATE", "not a url {")  # would be rejected by a real worker
    monkeypatch.setenv("PROFILE_ROOT", str(tmp_path / "profiles"))
    worker.main(max_loops=0)
    assert (tmp_path / "profiles").is_dir()


def test_real_main_still_validates_vault_key(monkeypatch):
    for k in ("PROFILE_MASTER_KEY", "ALLOW_DEV_PROFILE_KEY", "WORKER_FAKE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/none")
    with pytest.raises(ValueError, match="PROFILE_MASTER_KEY"):
        worker.main(max_loops=0)


def test_fake_refusal_not_masked_by_other_config_errors(monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.supabase.co:5432/postgres")
    monkeypatch.setenv("PROFILE_MASTER_KEY", "!!!not base64!!!")
    with pytest.raises(RuntimeError, match="WORKER_FAKE"):
        worker.main(max_loops=0)


FAKE_POST = {"url_regex": "^https://fake\\.zepto\\.test/api/orders$", "why": "fake order list"}


def test_fake_learn_asks_for_approval():
    asked = []
    recipe = fake_sites.learn("zepto", lambda entries: asked.append(entries) or True)
    assert asked == [[FAKE_POST]]
    assert recipe["endpoint"] == "https://fake.zepto.test/api/orders" and recipe["method"] == "POST"
    assert recipe["read_post_allowlist"] == [FAKE_POST]
    assert fake_sites.learn("zepto", lambda e: asked.append(e) or True) == recipe  # deterministic
    asked.clear()
    assert fake_sites.learn("zepto", lambda e: asked.append(e) or True,
                            preapproved=[FAKE_POST["url_regex"]]) == recipe
    assert asked == []
    with pytest.raises(sync.RecipeError, match="POST allowlist rejected"):
        fake_sites.learn("zepto", lambda e: False)


def test_fake_mode_routes_learn_jobs(pg, fake_mode, monkeypatch):
    from helpers import add_user

    from server import store
    monkeypatch.setattr(sync, "learn", lambda *a, **k: pytest.fail("real learn must not run"))
    add_user(pg, A, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    store.put_recipe(pg, "zepto", {"orders_page_url": "https://fake.zepto.test/orders",
                                   "read_post_allowlist": [FAKE_POST]})
    jobs.enqueue(pg, A, "learn", "zepto", {"reason": "drift", "auto": True})
    worker.run_job(jobs.claim(pg, "w", ("learn",)), pg, settings_for_tests())
    assert pg.execute("select status, error, progress->>'status' from jobs").fetchone() == \
        ("done", None, "auto_applied")
    assert pg.execute("select version, recipe->>'endpoint' from recipes").fetchone() == \
        (2, "https://fake.zepto.test/api/orders")


# ---------------------------------------------------------------- compare fakes (Plan 5 T9)

from server import addresses_store, cart, food_cart, store  # noqa: E402
from server.config import Settings  # noqa: E402

PLACE = (22.3310, 73.1510)
LINE = "12 Main Road"


def _world(pg, connected, lat=PLACE[0], lon=PLACE[1]):
    """A user at PLACE with these apps connected (saved address there) and every compare recipe learned by the fake."""
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)",
               (A, "12 Main Road, Gorwa, Vadodara, Gujarat", lat, lon))
    for site in connected:
        account = "swiggy" if site == "instamart" else site
        if not store.get_connection(pg, A, account):
            store.set_connection(pg, A, account, status="connected", last_synced_at=datetime.now())
            addresses_store.replace(pg, A, account, [{"address_id": f"{account}-home", "label": "Home",
                                                      "line": LINE, "area": "Gorwa", "city": "Vadodara",
                                                      "lat": lat, "lon": lon, "is_default": True}])
    for site in ("zepto", "blinkit", "instamart"):
        store.put_recipe(pg, site, fake_sites.learn(site, lambda e: True, kind="search"), kind="search")
    for site in ("swiggy", "zomato"):
        for kind, recipe in fake_sites.learn(site, lambda e: True, kind="food").items():
            store.put_recipe(pg, site, recipe, kind=kind)


def _run(pg):
    while (job := jobs.claim(pg, "w", ("search",))) is not None:
        worker.run_job(job, pg, settings_for_tests())


GROCERY_LIST = [{"text": "Amul Taza Milk 500ml", "qty": 2}, {"text": "Aashirvaad Atta 5kg", "qty": 1},
                {"text": "Farm Eggs 6 pcs", "qty": 1}, {"text": "Lay's Classic", "qty": 1},
                {"text": "Britannia Brown Bread", "qty": 1}]


def _check(pg, items=None):
    rid = cart.start_check(pg, A, items or GROCERY_LIST, None, Settings())["request_id"]
    _run(pg)
    return cart.get_request(pg, A, rid)


def test_fake_cart_ranks_zepto_blinkit_instamart(pg, fake_mode):
    _world(pg, ("zepto", "blinkit", "swiggy"))
    cards = _check(pg)["summary"]["cards"]
    assert [c["site"] for c in cards] == ["zepto", "blinkit", "instamart"]
    assert [c["total_label"] for c in cards][:2] == ["₹422", "₹451"]
    assert cards[1]["delta_label"] == "+₹29"


@pytest.mark.xfail(strict=True, reason="match.match_item cannot swap \"Lay's Classic\" to a sibling that drops "
                   "\"classic\" (its must-keep rule), so Instamart shows a Pick instead of the spec's swap and "
                   "₹479; needs a ruling on match.py (Plan 5 T2)")
def test_fake_instamart_swaps_lays(pg, fake_mode):
    _world(pg, ("zepto", "blinkit", "swiggy"))
    summary = _check(pg)["summary"]
    cards = summary["cards"]
    assert [c["total_label"] for c in cards] == ["₹422", "₹451", "₹479"]
    assert [c.get("delta_label") for c in cards] == [None, "+₹29", "+₹57"]
    assert cards[2]["swaps"] == ["Lay's Classic Salted Chips 52 g swapped for Lay's Magic Masala Chips 52 g"]
    assert summary["split"]["line"] == "No item is cheaper on another app, so one order on Zepto is best."


def test_fake_only_connected_apps_priced(pg, fake_mode):
    _world(pg, ("zepto",))
    cards = {c["site"]: c for c in _check(pg)["summary"]["cards"]}
    assert cards["zepto"]["status"] == "ok"
    assert cards["blinkit"]["note"] == "Connect Blinkit to compare."
    assert cards["instamart"]["note"] == "Connect Swiggy to compare Instamart."


def test_fake_runtime_logout(pg, fake_mode, monkeypatch):
    monkeypatch.setenv("FAKE_LOGGED_OUT_SITES", "blinkit")
    _world(pg, ("zepto", "blinkit"))
    cards = {c["site"]: c for c in _check(pg)["summary"]["cards"]}
    assert cards["blinkit"]["status"] == "needs_login" and cards["zepto"]["status"] == "ok"
    assert pg.execute("select status from connections where site='blinkit'").fetchone() == ("needs_login",)
    assert pg.execute("select count(*) from notifications where kind='needs_login'").fetchone() == (1,)


def test_fake_milk_is_pick_one(pg, fake_mode):
    _world(pg, ("zepto",))
    _check(pg, [{"text": "Milk", "qty": 1}])
    zepto = pg.execute("select items from cart_results where site='zepto'").fetchone()[0]
    assert zepto[0]["status"] == "pick" and len(zepto[0]["candidates"]) >= 2


def _find(results, name):
    return next(r for r in results if r["name"] == name)


def _lookup(pg, query):
    out = food_cart.lookup_restaurants(pg, A, query, None, Settings())
    assert out["status"] == "searching"
    _run(pg)
    out = food_cart.lookup_restaurants(pg, A, query, None, Settings())
    assert out["status"] == "ready"
    return out["results"]


def _food(pg, pair, items):
    ids = {f"{s}_id": l["restaurant_id"] for s, l in pair["listings"].items() if l}
    rid = food_cart.start_food_check(pg, A, {"name": pair["name"], **ids, "confirmed": True}, items, None,
                                     Settings())["request_id"]
    _run(pg)
    return cart.get_request(pg, A, rid)


def test_fake_food_meghana(pg, fake_mode):
    _world(pg, ("swiggy", "zomato"))
    pair = _find(_lookup(pg, "Meghana"), "Meghana Foods")
    assert pair["confidence"] == "sure"
    req = _food(pg, pair, [{"text": "Chicken Biryani (Half)", "qty": 2}, {"text": "Paneer Biryani", "qty": 1}])
    cards = {c["site"]: c for c in req["summary"]["cards"]}
    assert [cards["swiggy"]["total_label"], cards["zomato"]["total_label"]] == ["₹836", "₹878"]
    assert cards["zomato"]["delta_label"] == "+₹42"
    assert req["summary"]["best_site"] == "swiggy"
    zomato = pg.execute("select items from cart_results where site='zomato'").fetchone()[0]
    swiggy = pg.execute("select items from cart_results where site='swiggy'").fetchone()[0]
    assert zomato[1]["line_total"] == 250 < swiggy[1]["line_total"] == 260  # Paneer is cheapest on Zomato


def test_fake_behrouz_pairs_nearby_branch(pg, fake_mode):
    _world(pg, ("swiggy", "zomato"))
    results = [r for r in _lookup(pg, "Behrouz") if r["name"].startswith("Behrouz")]
    pairs = [r for r in results if r["listings"]["swiggy"] and r["listings"]["zomato"]]
    singles = [r for r in results if not (r["listings"]["swiggy"] and r["listings"]["zomato"])]
    assert len(pairs) == 1 and pairs[0]["confidence"] == "sure" and pairs[0]["locality"] == "Gorwa"
    assert len(singles) == 1 and singles[0]["note"] == "Only on Zomato"
    assert singles[0]["listings"]["zomato"]["locality"] == "Alkapuri"


def test_fake_truffles_asks_and_burger_singh_closed_on_zomato(pg, fake_mode):
    _world(pg, ("swiggy", "zomato"))
    truffles = next(r for r in _lookup(pg, "Truffles") if r["listings"]["swiggy"] and r["listings"]["zomato"])
    assert truffles["confidence"] == "ask"
    pair = next(r for r in _lookup(pg, "Burger Singh") if r["listings"]["swiggy"] and r["listings"]["zomato"])
    req = _food(pg, pair, [{"text": "Burger", "qty": 1}])
    cards = {c["site"]: c for c in req["summary"]["cards"]}
    assert cards["zomato"]["status"] == "closed"


def test_fake_learn_asks_approval_for_post_recipes():
    asked = []
    recipe = fake_sites.learn("instamart", lambda e: asked.append(e) or True, kind="search")
    assert asked[0][0]["url_regex"] == "^https://fake\\.instamart\\.test/api/search$"
    assert recipe["method"] == "POST"
    assert fake_sites.learn("zepto", lambda e: pytest.fail("GET needs no approval"), kind="search")["method"] == "GET"
    asked.clear()
    food = fake_sites.learn("zomato", lambda e: asked.append(e) or True, kind="food")
    assert set(food) == {"restaurants", "menu"} and food["restaurants"]["method"] == "POST"
    assert asked[0][0]["url_regex"] == "^https://fake\\.zomato\\.test/api/search$"
    swiggy = fake_sites.learn("swiggy", lambda e: pytest.fail("GET needs no approval"), kind="food")
    assert swiggy["restaurants"]["method"] == "GET" and swiggy["menu"]["method"] == "GET"
    with pytest.raises(sync.RecipeError):
        fake_sites.learn("instamart", lambda e: False, kind="search")
    assert fake_sites.learn("zepto", lambda e: False, preapproved=["^https://fake\\.zepto\\.test/api/orders$"])
