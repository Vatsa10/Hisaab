"""Worker, Plan 5: compare jobs (search) on the user's own session, supervised search and food learning, and the
stale-check sweep. Postgres tests; the browser edges (search_products, search_restaurants, fetch_menu) are stubbed."""
import dataclasses
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import A, B, add_user, settings_for_tests

import llm
import search
import food_search
from browser import SessionExpired
from server import addresses_store, cart, food_cart, jobs, notify, recipe_admin, store, vault, worker
from server.config import DEV_PROFILE_KEY, Settings

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)
HOME = (22.3310, 73.1510)
ADDRESS = "12 Main Road, Gorwa, Vadodara, Gujarat"
RECIPE = {"home_url": "https://www.zepto.com/", "field_map": {}, "read_post_allowlist": []}
ITEMS = [{"text": "Amul Taza Milk 500ml", "qty": 2}, {"text": "Farm Eggs 6 pcs", "qty": 1}]
MILK = {"product_id": "p1", "name": "Amul Taza Milk 500ml", "brand": "Amul", "pack_size": "500 ml", "price": 29,
        "mrp": 32, "in_stock": True}
EGGS = {"product_id": "p2", "name": "Farm Eggs 6 pcs", "brand": None, "pack_size": "6 pcs", "price": 54,
        "mrp": 60, "in_stock": True}


@pytest.fixture(autouse=True)
def no_real_senders(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "mail_send", lambda to, subject, text, s: sent.append((to, subject)))
    monkeypatch.setattr(notify, "push_send", lambda sub, payload, s: sent.append(payload))
    return sent


def _store():
    return vault.store_from_url(os.environ["PROFILE_STORE_URL"])


def _seed_profile(user_id, profile="zepto"):
    d = Path(os.environ["PROFILE_ROOT"]).parent / f"seed-{user_id}-{profile}"
    vault.stamp(d, user_id, profile)
    (d / "Cookies").write_text("seeded")
    _store().put(vault.blob_key(user_id, profile), vault._seal(KEY, user_id, profile, vault._pack(d)))


def _grocery(pg, sites=("zepto",), items=ITEMS, seed=True):
    """A user with an address, these apps connected (with saved addresses, stored profiles and healthy search
    recipes), and a started grocery check. Returns the request id."""
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (A, ADDRESS, *HOME))
    for site in sites:
        account = "swiggy" if site == "instamart" else site
        if not store.get_connection(pg, A, account):
            store.set_connection(pg, A, account, status="connected")
            addresses_store.replace(pg, A, account, [
                {"address_id": f"{account}-home", "label": "Home", "line": "12 Main Road", "area": "Gorwa",
                 "city": "Vadodara", "lat": HOME[0], "lon": HOME[1], "is_default": True}])
            if seed:
                _seed_profile(A, account)
        store.put_recipe(pg, site, RECIPE, kind="search")
    return cart.start_check(pg, A, items, None, Settings())["request_id"]


def _run_all(pg, s=None, types=("search",)):
    ids = []
    while (job := jobs.claim(pg, "w", types)) is not None:
        worker.run_job(job, pg, s or settings_for_tests())
        ids.append(job.id)
    return ids


def _stub_search(monkeypatch, products=None, calls=None, **raises):
    def fake(site, queries, location, recipe, profile_dir, proxy_server=None, block_media=True, max_results=20):
        if calls is not None:
            calls.append({"site": site, "queries": list(queries), "location": location, "profile_dir": profile_dir,
                          "proxy_server": proxy_server, "block_media": block_media,
                          "restored": (Path(profile_dir) / "Cookies").exists()})
        if "exc" in raises:
            raise raises["exc"]
        rows = products or [MILK, EGGS]
        return {"products": {q: [r for r in rows if q.split()[0].lower() in r["name"].lower()] for q in queries},
                "errors": {}, "fee_info": {"delivery": 15}}
    monkeypatch.setattr(worker.search, "search_products", fake)


def _result(pg, rid, site="zepto"):
    return pg.execute("select status, error, items, meta from cart_results where request_id=%s and site=%s",
                      (rid, site)).fetchone()


def _job(pg):
    return pg.execute("select status, error from jobs where type='search' order by created_at").fetchall()


def _notes(pg):
    return pg.execute("select kind, title, body, url, user_id::text, dedupe_key from notifications").fetchall()


# ---------------------------------------------------------------- products op

def test_search_job_restores_and_saves_user_profile(pg, monkeypatch):
    rid = _grocery(pg)
    calls, saved = [], []
    real_save = vault.save_profile
    monkeypatch.setattr(worker.vault, "save_profile",
                        lambda st, k, user, name, d: saved.append((user, name)) or real_save(st, k, user, name, d))
    _stub_search(monkeypatch, calls=calls)
    _run_all(pg)
    assert saved == [(A, "zepto")]
    assert len(calls) == 1 and calls[0]["restored"] is True
    assert pg.execute("select profile_blob_key from connections where site='zepto'").fetchone()[0]
    assert _job(pg) == [("done", None)]


def test_search_job_uses_apps_saved_address(pg, monkeypatch):
    _grocery(pg)
    calls = []
    _stub_search(monkeypatch, calls=calls)
    _run_all(pg)
    loc = calls[0]["location"]
    assert (loc["lat"], loc["lon"]) == HOME and "Gorwa" in loc["query"]


def test_search_job_uses_users_sticky_proxy_session(pg, monkeypatch):
    log = {}

    class FakeRelay:
        bytes_total = 3_000_000

        def __init__(self, upstream):
            log["upstream"] = upstream
            self.url = "http://127.0.0.1:5555"

        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass

        def check(self, *a, **k):
            pass
    monkeypatch.setattr(worker, "Relay", FakeRelay)
    s = dataclasses.replace(settings_for_tests(), proxy_url_template="http://u-{session}:pw@gw.example.com:7000")
    _grocery(pg)
    pg.execute("update connections set proxy_session_id='abc123def456' where site='zepto'")
    calls = []
    _stub_search(monkeypatch, calls=calls)
    _run_all(pg, s)
    assert log["upstream"] == "http://u-abc123def456:pw@gw.example.com:7000"
    assert calls[0]["proxy_server"] == "http://127.0.0.1:5555"


def test_search_job_writes_matched_result_and_finalizes(pg, monkeypatch):
    rid = _grocery(pg)
    _stub_search(monkeypatch)
    _run_all(pg)
    status, error, items, meta = _result(pg, rid)
    assert (status, error) == ("ok", None)
    assert [i["status"] for i in items] == ["matched", "matched"]
    assert items[0]["packs"] == 2 and items[0]["line_total"] == 58
    assert meta["fee_info"] == {"delivery": 15}
    assert pg.execute("select status from cart_requests").fetchone() == ("done",)
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("connected",)


def test_search_job_records_usage(pg, monkeypatch):
    class FakeRelay:
        bytes_total = 2_000_000
        url = "http://127.0.0.1:5555"

        def __init__(self, upstream):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass

        def check(self, *a, **k):
            pass
    monkeypatch.setattr(worker, "Relay", FakeRelay)
    s = dataclasses.replace(settings_for_tests(), proxy_url_template="http://u-{session}:pw@gw.example.com:7000")
    judged = []

    def judge_factory(*a, **k):
        def judge(text, names):
            llm._record("judge", SimpleNamespace(usage=SimpleNamespace(input_tokens=300, output_tokens=40),
                                                 model="claude-haiku-5-5", stop_reason="end_turn"))
            judged.append(text)
            return [True for _ in names]
        return judge
    monkeypatch.setattr(worker, "_judge", judge_factory)
    _grocery(pg, items=[{"text": "Amul Taza Milk", "qty": 1}])
    real_match = worker.match.match_item

    def ask_judge(q, products, judge=None):
        judge(q.text, ["Amul Taza Toned Milk 1 L"])  # the matcher asks it for an uncertain item
        return real_match(q, products, None)
    monkeypatch.setattr(worker.match, "match_item", ask_judge)
    _stub_search(monkeypatch)
    _run_all(pg, s)
    proxy_mb, tokens, syncs = pg.execute("select proxy_mb, ai_tokens, syncs from usage").fetchone()
    assert float(proxy_mb) == pytest.approx(2.0)
    assert judged and tokens == 340 * len(judged) and syncs == 0


def test_search_job_passes_block_media(pg, monkeypatch):
    _grocery(pg)
    calls = []
    _stub_search(monkeypatch, calls=calls)
    _run_all(pg, dataclasses.replace(settings_for_tests(), sync_block_media=False))
    assert calls[0]["block_media"] is False
    cart.start_check(pg, A, ITEMS, None, Settings())
    _run_all(pg)
    assert calls[1]["block_media"] is True


def test_missing_request_fails_request_gone(pg, monkeypatch):
    _grocery(pg)
    pg.execute("delete from cart_requests")
    _stub_search(monkeypatch, exc=AssertionError("must not search"))
    _run_all(pg)
    assert _job(pg) == [("failed", "request_gone")]


def test_unhealthy_or_paused_recipe_is_unavailable(pg, monkeypatch):
    rid = _grocery(pg)
    pg.execute("update recipes set healthy=false where site='zepto' and kind='search'")
    _stub_search(monkeypatch, exc=AssertionError("must not search"))
    _run_all(pg)
    assert _result(pg, rid)[0] == "unavailable"
    assert pg.execute("select status from cart_requests").fetchone() == ("failed",)


# ---------------------------------------------------------------- logged out / errors

def test_logged_out_search_marks_needs_login_and_notifies(pg, monkeypatch):
    rid = _grocery(pg)
    saved = []
    monkeypatch.setattr(worker.vault, "save_profile", lambda *a, **k: saved.append(a))
    _stub_search(monkeypatch, exc=SessionExpired("HTTP 401"))
    _run_all(pg)
    assert _result(pg, rid)[0] == "needs_login"
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("needs_login",)
    notes = _notes(pg)
    assert len(notes) == 1
    assert notes[0][:4] == ("needs_login", "Zepto logged you out",
                            "Log in again to keep comparing prices and syncing orders.", "/accounts")
    assert notes[0][4] == A and not saved


def test_vault_error_is_needs_login(pg, monkeypatch):
    rid = _grocery(pg)

    def broken(*a, **k):
        raise vault.VaultError("unreadable")
    monkeypatch.setattr(worker.vault, "restore_profile", broken)
    _stub_search(monkeypatch, exc=AssertionError("must not search"))
    _run_all(pg)
    assert _result(pg, rid)[0] == "needs_login"
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("needs_login",)


def test_unserviceable_result(pg, monkeypatch):
    rid = _grocery(pg)
    _stub_search(monkeypatch, exc=search.Unserviceable("no store"))
    _run_all(pg)
    assert _result(pg, rid)[0] == "unserviceable"
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("connected",)


def test_other_error_is_failed_and_connection_untouched(pg, monkeypatch):
    rid = _grocery(pg)
    _stub_search(monkeypatch, exc=search.SearchTransient("home page did not load"))
    _run_all(pg)
    assert _result(pg, rid)[0] == "failed"
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("connected",)


def test_search_drift_enqueues_auto_learn_for_connected_admin(pg, monkeypatch):
    rid = _grocery(pg)
    add_user(pg, B, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    store.set_connection(pg, B, "zepto", status="connected")
    _stub_search(monkeypatch, exc=search.SearchDrift("no products_path"))
    _run_all(pg)
    status, error, *_ = _result(pg, rid)
    assert (status, error) == ("failed", "recipe_drift")
    row = pg.execute("select user_id::text, payload from jobs where type='learn'").fetchone()
    assert row[0] == B and row[1] == {"reason": "drift", "auto": True, "kind": "search"}
    assert pg.execute("select healthy from recipes where site='zepto' and kind='search'").fetchone() == (False,)


def test_search_drift_without_admin_pauses_and_alerts(pg, monkeypatch):
    rid = _grocery(pg)
    pg.execute("insert into admins(email) values ('boss@example.com')")
    _stub_search(monkeypatch, exc=search.SearchDrift("no products_path"))
    _run_all(pg)
    assert _result(pg, rid)[:2] == ("failed", "recipe_drift")
    assert pg.execute("select paused from recipes where site='zepto' and kind='search'").fetchone() == (True,)
    row = pg.execute("select kind, title, body, dedupe_key from notifications where email='boss@example.com'").fetchone()
    assert row[:3] == ("site_paused", "Zepto prices paused",
                       "Its price recipe stopped working and no admin account is connected to re-learn it.")
    assert row[3].startswith("site_paused:search:zepto:")


# ---------------------------------------------------------------- food ops

FOOD_RECIPE = {"home_url": "https://www.swiggy.com/", "field_map": {}, "read_post_allowlist": []}
LISTING = {"restaurant_id": "r1", "name": "Meghana Foods", "locality": "Gorwa", "lat": 22.331, "lon": 73.151,
           "distance_km": 0.1, "is_open": True, "url": "https://www.swiggy.com/restaurants/r1"}
MENU = {"is_open": True, "fee_info": {"delivery": 30}, "dishes": [
    {"dish_id": "d1", "name": "Chicken Biryani (Half)", "variant": None, "price": 220, "veg": False,
     "in_stock": True, "packaging": 15}]}


def _food_user(pg):
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (A, ADDRESS, *HOME))
    store.set_connection(pg, A, "swiggy", status="connected")
    _seed_profile(A, "swiggy")
    for kind in ("restaurants", "menu"):
        store.put_recipe(pg, "swiggy", FOOD_RECIPE, kind=kind)


def test_restaurants_op_fills_user_cache(pg, monkeypatch):
    _food_user(pg)
    seen = []
    monkeypatch.setattr(worker.food_search, "search_restaurants",
                        lambda site, query, location, recipe, profile_dir, proxy_server=None, block_media=True,
                        max_results=20: seen.append(query) or {"restaurants": [LISTING], "fee_info": {}})
    jid = jobs.enqueue(pg, A, "search", "swiggy", {"op": "restaurants", "query": "Meghana"})
    _run_all(pg)
    assert seen == ["Meghana"]
    assert food_cart.get_restaurants(pg, A, "swiggy", food_cart.geo_key(*HOME), "meghana") == [LISTING]
    assert pg.execute("select status, progress from jobs where id=%s", (jid,)).fetchone()[0] == "done"
    assert pg.execute("select progress from jobs where id=%s", (jid,)).fetchone()[0]["count"] == 1


def test_menu_op_fills_cache_without_request(pg, monkeypatch):
    _food_user(pg)
    monkeypatch.setattr(worker.food_search, "fetch_menu",
                        lambda site, rid, location, recipe, profile_dir, proxy_server=None, url=None,
                        block_media=True: MENU)
    jobs.enqueue(pg, A, "search", "swiggy", {"op": "menu", "restaurant_id": "r1", "url": LISTING["url"]})
    _run_all(pg)
    assert food_cart.get_menu(pg, A, "swiggy", "r1")["dishes"][0]["dish_id"] == "d1"


def test_menu_op_with_request_prices_and_finalizes(pg, monkeypatch):
    _food_user(pg)
    addresses_store.replace(pg, A, "swiggy", [{"address_id": "sw1", "label": "Home", "line": "12 Main Road",
                                               "area": "Gorwa", "city": "Vadodara", "lat": HOME[0], "lon": HOME[1],
                                               "is_default": True}])
    rid = str(pg.execute(
        "insert into cart_requests (user_id, mode, items, sites, area, lat, lon) values "
        "(%s,'food',%s,%s,'Gorwa',%s,%s) returning id",
        (A, __import__("psycopg.types.json", fromlist=["Jsonb"]).Jsonb([{"text": "Chicken Biryani (Half)", "qty": 1}]),
         ["swiggy"], *HOME)).fetchone()[0])
    seen = []
    monkeypatch.setattr(worker.food_search, "fetch_menu",
                        lambda site, r, location, recipe, profile_dir, proxy_server=None, url=None,
                        block_media=True: seen.append(url) or MENU)
    jobs.enqueue(pg, A, "search", "swiggy", {"op": "menu", "restaurant_id": "r1", "url": LISTING["url"],
                                             "request_id": rid})
    _run_all(pg)
    assert seen == [LISTING["url"]]
    status, error, items, meta = _result(pg, rid, "swiggy")
    assert status == "ok" and items[0]["status"] == "matched" and meta["fee_info"]["delivery"] == 30
    assert pg.execute("select status from cart_requests where id=%s", (rid,)).fetchone() == ("done",)


# ---------------------------------------------------------------- learn

def _admin(pg, site="zepto"):
    add_user(pg, B, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    store.set_connection(pg, B, site, status="connected")
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (B, ADDRESS, *HOME))
    _seed_profile(B, "swiggy" if site in ("swiggy", "instamart") else site)


def _search_recipe(url="https://www.zepto.com/api/search"):
    return {"home_url": "https://www.zepto.com/", "method": "GET", "endpoint": url, "read_post_allowlist": [],
            "field_map": {"products_path": "a", "product_id": "i", "name": "n", "price": "p"}}


def test_learn_search_and_food_jobs_apply_recipes(pg, monkeypatch):
    _admin(pg, "zepto")
    monkeypatch.setattr(worker.search_learn, "learn_search", lambda site, location, profile_dir, **k: _search_recipe())
    jid = jobs.enqueue(pg, B, "learn", "zepto", {"reason": "manual", "kind": "search"})
    _run_all(pg, types=("learn",))
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("done",)
    assert pg.execute("select version, healthy from recipes where site='zepto' and kind='search'").fetchone() == (1, True)

    store.set_connection(pg, B, "swiggy", status="connected")
    _seed_profile(B, "swiggy")
    rest = {"home_url": "https://www.swiggy.com/", "method": "GET", "endpoint": "https://www.swiggy.com/api/r",
            "read_post_allowlist": [], "field_map": {"restaurants_path": "a", "restaurant_id": "i", "name": "n"}}
    menu = {"home_url": "https://www.swiggy.com/", "method": "GET", "endpoint": "https://www.swiggy.com/api/m",
            "read_post_allowlist": [], "field_map": {"dishes_path": "a", "dish_id": "i", "name": "n", "price": "p"}}
    monkeypatch.setattr(worker.food_learn, "learn_food",
                        lambda site, location, profile_dir, **k: {"restaurants": rest, "menu": menu})
    jobs.enqueue(pg, B, "learn", "swiggy", {"reason": "manual", "kind": "food"})
    _run_all(pg, types=("learn",))
    rows = pg.execute("select kind, version from recipes where site='swiggy' order by kind").fetchall()
    assert rows == [("menu", 1), ("restaurants", 1)]


def test_learn_defaults_to_orders_without_kind(pg, monkeypatch):
    _admin(pg, "zepto")
    monkeypatch.setattr(worker.sync, "learn", lambda site, **k: {"orders_page_url": "https://www.zepto.com/orders",
                                                                  "endpoint": "https://www.zepto.com/api/o",
                                                                  "method": "GET", "read_post_allowlist": []})
    monkeypatch.setattr(worker.search_learn, "learn_search", lambda *a, **k: pytest.fail("not a search learn"))
    jobs.enqueue(pg, B, "learn", "zepto", {"reason": "manual"})
    _run_all(pg, types=("learn",))
    assert pg.execute("select kind from recipes where site='zepto'").fetchall() == [("orders",)]


def test_auto_learn_search_failure_pauses_and_alerts(pg, monkeypatch):
    _admin(pg, "zepto")
    store.put_recipe(pg, "zepto", _search_recipe(), kind="search")

    def boom(*a, **k):
        raise worker.sync.RecipeError("no product search response found")
    monkeypatch.setattr(worker.search_learn, "learn_search", boom)
    jobs.enqueue(pg, B, "learn", "zepto", {"reason": "drift", "auto": True, "kind": "search"})
    _run_all(pg, types=("learn",))
    assert pg.execute("select paused from recipes where site='zepto' and kind='search'").fetchone() == (True,)
    assert pg.execute("select kind, title from notifications where kind='site_paused'").fetchone() == (
        "site_paused", "Zepto prices paused")


def test_auto_learn_pending_pauses_search(pg, monkeypatch):
    _admin(pg, "zepto")
    store.put_recipe(pg, "zepto", _search_recipe(), kind="search")
    new_post = _search_recipe() | {"method": "POST", "endpoint": "https://www.zepto.com/api/v2/search",
                                   "read_post_allowlist": [{"url_regex": "^https://www\\.zepto\\.com/api/v2/search$",
                                                            "why": "search"}]}
    monkeypatch.setattr(worker.search_learn, "learn_search", lambda *a, **k: new_post)
    jobs.enqueue(pg, B, "learn", "zepto", {"reason": "drift", "auto": True, "kind": "search"})
    _run_all(pg, types=("learn",))
    assert pg.execute("select status from recipe_approvals where kind='search'").fetchone() == ("pending",)
    assert pg.execute("select paused from recipes where site='zepto' and kind='search'").fetchone() == (True,)


def test_learn_search_preapproves_current_allowlist(pg, monkeypatch):
    _admin(pg, "zepto")
    post = {"url_regex": "^https://www\\.zepto\\.com/api/search$", "why": "search"}
    store.put_recipe(pg, "zepto", _search_recipe() | {"read_post_allowlist": [post]}, kind="search")
    seen = {}
    monkeypatch.setattr(worker.search_learn, "learn_search",
                        lambda site, location, profile_dir, proxy_server=None, approve=None, preapproved=(), llm=None:
                        seen.update(pre=list(preapproved), llm=llm) or _search_recipe())
    jobs.enqueue(pg, B, "learn", "zepto", {"reason": "manual", "kind": "search"})
    _run_all(pg, types=("learn",))
    assert seen["pre"] == [post["url_regex"]] and callable(seen["llm"])


def test_learn_food_waits_for_post_approval(pg, pg2, monkeypatch):
    _admin(pg, "swiggy")
    post = {"url_regex": "^https://www\\.swiggy\\.com/api/search$", "why": "restaurant search"}
    rest = {"home_url": "https://www.swiggy.com/", "method": "POST", "endpoint": "https://www.swiggy.com/api/search",
            "read_post_allowlist": [post], "field_map": {"restaurants_path": "a", "restaurant_id": "i", "name": "n"}}
    menu = {"home_url": "https://www.swiggy.com/", "method": "GET", "endpoint": "https://www.swiggy.com/api/m",
            "read_post_allowlist": [], "field_map": {"dishes_path": "a", "dish_id": "i", "name": "n", "price": "p"}}

    def learn_food(site, location, profile_dir, proxy_server=None, approve=None, preapproved=(), llm=None):
        assert approve([post]) is True
        return {"restaurants": rest, "menu": menu}
    monkeypatch.setattr(worker.food_learn, "learn_food", learn_food)
    s = dataclasses.replace(settings_for_tests(), approval_timeout_s=20)
    jid = jobs.enqueue(pg, B, "learn", "swiggy", {"reason": "manual", "kind": "food"})
    job = jobs.claim(pg, "w", ("learn",))
    t = threading.Thread(target=worker.run_job, args=(job, pg, s))
    t.start()
    aid = None
    for _ in range(100):
        row = pg2.execute("select id from recipe_approvals where status='pending' and job_id=%s", (jid,)).fetchone()
        if row:
            aid = str(row[0])
            break
        threading.Event().wait(0.1)
    assert aid
    recipe_admin.decide(pg2, aid, "approve", B)
    t.join(30)
    assert pg.execute("select kind, version from recipes where site='swiggy' order by kind").fetchall() == [
        ("menu", 1), ("restaurants", 1)]


# ---------------------------------------------------------------- stale checks

def test_stale_request_is_finalized_failed(pg):
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (A, ADDRESS, *HOME))
    store.set_connection(pg, A, "zepto", status="connected")
    store.put_recipe(pg, "zepto", RECIPE, kind="search")
    addresses_store.replace(pg, A, "zepto", [{"address_id": "z", "label": "Home", "line": "12 Main Road",
                                              "area": "Gorwa", "city": "Vadodara", "lat": HOME[0], "lon": HOME[1],
                                              "is_default": True}])
    old = cart.start_check(pg, A, ITEMS, None, Settings())["request_id"]
    pg.execute("update cart_requests set created_at = now() - interval '26 minutes' where id=%s", (old,))
    checks = pg.execute("select cart_checks from usage where user_id=%s", (A,)).fetchone()[0]
    assert checks == 1
    from server import scheduler
    r = scheduler.tick(pg, Settings(smtp_host="mailpit"))
    assert r["carts_finalized"] == 1
    assert pg.execute("select status, error from cart_requests where id=%s", (old,)).fetchone() == ("failed", "timeout")
    assert pg.execute("select cart_checks from usage where user_id=%s", (A,)).fetchone()[0] == 0
