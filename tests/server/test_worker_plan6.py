"""Worker, Plan 6: fill_cart jobs on the user's session, supervised cart learning, lost-fill cleanup."""
import os
import threading
import time
from pathlib import Path

import pytest
from helpers import A, B, add_user, settings_for_tests
from psycopg.types.json import Jsonb
from test_fill_store import NOW, cart_recipe, connect, ready

import browser
import cart_fill
import cart_learn
import sync
from server import fill, jobs, recipe_admin, scheduler, store, vault, worker
from server.config import DEV_PROFILE_KEY, OPEN_APP_URLS

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)


@pytest.fixture(autouse=True)
def no_real_senders(monkeypatch):
    from server import notify
    sent = []
    monkeypatch.setattr(notify, "mail_send", lambda to, subject, text, s: sent.append((to, subject)))
    monkeypatch.setattr(notify, "push_send", lambda sub, payload, s: sent.append(payload))
    return sent


def _seed_profile(user_id, profile="zepto"):
    d = Path(os.environ["PROFILE_ROOT"]).parent / f"seed-{user_id}-{profile}"
    vault.stamp(d, user_id, profile)
    (d / "Cookies").write_text("seeded")
    store_ = vault.store_from_url(os.environ["PROFILE_STORE_URL"])
    store_.put(vault.blob_key(user_id, profile), vault._seal(KEY, user_id, profile, vault._pack(d)))


def _outcome(status="done", **k):
    items = [{"idx": 0, "label": "milk", "name": "Amul Milk", "product_id": "p1", "packs": 2, "status": "added",
              "added_qty": 2, "skip_reason": None}]
    cart = {"subtotal": 58, "total": 73, "fees": {}, "line_count": 1, "existing_count": 0}
    return cart_fill.outcome(status, items, cart, app_location={"lat": 22.33, "lon": 73.16, "area": "Gorwa"}, **k)


def valid_cart_recipe(site="zepto") -> dict:
    """test_cart_recipe's valid zepto cart recipe, on the site's own host (check_cart_recipe passes it)."""
    import importlib.util
    import json
    spec = importlib.util.spec_from_file_location("_valid_cart", Path(__file__).parents[1] / "test_cart_recipe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    VALID = mod.RECIPE
    word = "swiggy" if site == "instamart" else site
    return json.loads(json.dumps(VALID).replace("zepto", word)) | {"site": site}


def _setup(pg, site="zepto", seed=True):
    s = settings_for_tests()
    rid = ready(pg, site, user=A)
    pg.execute("update recipes set recipe = %s where site=%s and kind='cart'",
               (Jsonb(valid_cart_recipe(site)), site))
    if seed:
        _seed_profile(A, "swiggy" if site == "instamart" else site)
    made = fill.start_fill(pg, A, rid, site, NOW, s)
    return rid, made["fill_id"], made["job_id"], s


def _run(pg, s):
    job = jobs.claim(pg, "w", ("fill_cart",))
    worker.run_job(job, pg, s)
    return job


def _job(pg, jid):
    return pg.execute("select status, error, progress from jobs where id=%s", (jid,)).fetchone()


def _notes(pg):
    return pg.execute("select kind, title, body, url, dedupe_key from notifications order by created_at").fetchall()


# ---------------------------------------------------------------- fill jobs

def test_fill_job_runs_engine_and_finishes(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    seen = {}

    def engine(site, plan, priced, recipe, search_recipe, **k):
        seen.update(k, site=site, plan=plan, priced=priced)
        k["on_stage"]("adding")
        k["on_item"]({"idx": 0, "label": "milk", "name": "Amul Milk", "product_id": "p1", "packs": 2,
                      "status": "added", "added_qty": 2, "skip_reason": None})
        return _outcome()
    monkeypatch.setattr(cart_fill, "fill_cart", engine)
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert f["status"] == "done" and f["summary"]["title"].startswith("Added") and f["started_at"] is not None
    assert f["open_url"] == OPEN_APP_URLS["zepto"]
    assert _job(pg, jid)[:2] == ("done", None)
    assert _job(pg, jid)[2]["done"] == 1 and seen["max_location_m"] == s.cart_location_max_m
    assert seen["confirm_location"] is None and seen["site"] == "zepto"
    row = pg.execute("select profile_blob_key from connections where user_id=%s and site='zepto'", (A,)).fetchone()
    assert row[0]  # the profile was saved back


def test_fill_job_uses_users_profile_and_sticky_proxy(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg, "instamart")
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: _outcome())
    restored = []
    real = vault.restore_profile
    monkeypatch.setattr(vault, "restore_profile", lambda st, key, uid, name, d: restored.append((uid, name)) or
                        real(st, key, uid, name, d))
    _run(pg, s)
    assert restored == [(A, "swiggy")]
    assert fill.get_fill(pg, fid)["status"] == "done"
    assert pg.execute("select proxy_session_id from connections where user_id=%s and site='swiggy'",
                      (A,)).fetchone()[0]


def test_fill_job_never_logs_in(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(sync, "login", lambda *a, **k: pytest.fail("a fill never logs in"))
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: _outcome())
    _run(pg, s)
    assert fill.get_fill(pg, fid)["status"] == "done"


def test_fill_gone_fails_the_job(pg):
    rid, fid, jid, s = _setup(pg)
    pg.execute("delete from cart_fills where id=%s", (fid,))
    _run(pg, s)
    assert _job(pg, jid)[:2] == ("failed", "fill_gone")


def test_cart_recipe_unavailable_fails_the_fill(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: pytest.fail("no recipe, no engine"))
    pg.execute("update recipes set paused = true where site='zepto' and kind='cart'")
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"]) == ("failed", "cart_unavailable") and f["counted"] is False


def test_connection_not_connected_fails_needs_login(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: pytest.fail("not connected"))
    connect(pg, "zepto", A, "needs_login")
    _run(pg, s)
    assert fill.get_fill(pg, fid)["error"] == "needs_login"


def test_question_timeout_is_skip_not_failure(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    answers = []

    def prompt_factory(*a, **k):
        def prompt(text, kind="otp", choices=None):
            raise sync.OtpTimeout(kind)
        return prompt
    monkeypatch.setattr(worker, "make_prompt", prompt_factory)

    def engine(site, plan, priced, recipe, search_recipe, **k):
        answers.append(k["ask"]("Which milk?", [{"id": "a"}]))
        return _outcome()
    monkeypatch.setattr(cart_fill, "fill_cart", engine)
    _run(pg, s)
    assert answers == [None] and fill.get_fill(pg, fid)["status"] == "done"


def test_question_progress_carries_choices(pg):
    rid, fid, jid, s = _setup(pg)
    job = jobs.claim(pg, "w", ("fill_cart",))
    import psycopg
    listen = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    prompt = worker.make_prompt(listen, pg, job.id, 5)
    choices = [{"id": "p9", "label": "Add milk"}, {"id": "skip", "label": "Skip this item"}]
    out = []
    t = threading.Thread(target=lambda: out.append(prompt("Which milk?", "question", choices)))
    t.start()
    for _ in range(50):
        prog = _job(pg, job.id)[2]
        if prog.get("stage") == "question_needed":
            break
        time.sleep(0.05)
    assert prog["question"] == "Which milk?" and prog["choices"] == choices
    pg.execute("select pg_notify(%s, %s)", ("otp_" + job.id.replace("-", ""), "p9"))
    t.join(5)
    assert out == ["p9"]
    listen.close()


def test_session_expired_marks_needs_login_and_refunds(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: (_ for _ in ()).throw(browser.SessionExpired("x")))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("failed", "needs_login", False)
    assert store.get_connection(pg, A, "zepto")["status"] == "needs_login"
    assert any(n[0] == "needs_login" for n in _notes(pg))
    assert _job(pg, jid)[0] == "failed"


def test_cart_drift_pauses_cart_and_alerts(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    pg.execute("insert into admins(email) values ('admin@example.com')")
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: (_ for _ in ()).throw(cart_fill.CartDrift("x")))
    _run(pg, s)
    assert fill.get_fill(pg, fid)["error"] == "recipe_drift"
    assert recipe_admin.is_paused(pg, "zepto", "cart")
    notes = [n for n in _notes(pg) if n[0] == "site_paused"]
    assert len(notes) == 1 and notes[0][1] == "Zepto cart filling paused"
    assert notes[0][4].startswith("site_paused:cart:zepto:")


def test_other_error_fails_the_fill_cleanly(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom 987654")))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    # the fill had started: its adds may be in the app's cart, so it is interrupted (kept, never refunded)
    assert f["status"] == "interrupted" and f["error"] == "RuntimeError: boom ***"


def test_engine_stop_code_is_interrupted(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: _outcome("failed", error="cart_read_failed"))
    _run(pg, s)
    assert fill.get_fill(pg, fid)["status"] == "interrupted"


def test_fill_notifies_once(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: _outcome())
    _run(pg, s)
    notes = [n for n in _notes(pg) if n[0] == "cart_filled"]
    assert len(notes) == 1
    assert notes[0][2] == "Open Zepto to review and pay." and notes[0][3] == f"/cart/{rid}?fill={fid}"
    assert notes[0][4] == f"cart_filled:{fid}"


def test_reprice_notifies_cart_reprice(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    repriced = [{"idx": 0, "name": "Amul Milk", "product_id": "p1", "old_price": 58.0, "new_price": 60.0,
                 "in_stock": True}]
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: cart_fill.outcome(
        "needs_confirmation", [], None, app_location={"lat": 1, "lon": 2, "area": "x"}, repriced=repriced))
    _run(pg, s)
    note = [n for n in _notes(pg) if n[0] == "cart_reprice"]
    assert len(note) == 1 and note[0][1] == "Check Zepto prices" and note[0][2].startswith("Items there")


def test_fake_mode_fill(pg, monkeypatch, tmp_path):
    from server import fake_sites
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: pytest.fail("real engine"))
    rid, fid, jid, s = _setup(pg, seed=False)
    pg.execute("update recipes set recipe = %s where site='zepto' and kind='cart'",
               (Jsonb(fake_sites.learn("zepto", lambda e: True, kind="cart")["recipe"]),))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert f["status"] in ("done", "partial") and f["open_url"] == "https://fake.zepto.test/cart"
    assert (tmp_path / f"{A}-zepto.json").exists()


# ---------------------------------------------------------------- lost fills

def test_lost_fill_job_is_not_requeued(pg):
    rid, fid, jid, s = _setup(pg)
    job = jobs.claim(pg, "w", ("fill_cart",))
    pg.execute("update jobs set heartbeat_at = now() - interval '1 hour', started_at = now() - interval '1 hour' "
               "where id=%s", (job.id,))
    worker._requeue_stale(pg, s)
    assert _job(pg, jid)[:2] == ("failed", "worker_lost")
    assert jobs.claim(pg, "w2", ("fill_cart",)) is None


def test_shutdown_aborts_fill_without_requeue(pg):
    rid, fid, jid, s = _setup(pg)
    job = jobs.claim(pg, "w", ("fill_cart",))
    assert worker._abort_on_shutdown(job, pg, "w", s) is True
    assert _job(pg, jid)[:2] == ("failed", "worker_shutdown")


def test_tick_finalizes_lost_fills(pg):
    rid, fid, jid, s = _setup(pg)
    jobs.claim(pg, "w", ("fill_cart",))
    pg.execute("update jobs set status='failed', error='worker_lost' where id=%s", (jid,))
    r = scheduler.tick(pg, s, NOW)
    assert r["fills_finalized"] == 1
    assert fill.get_fill(pg, fid)["status"] == "failed"


def test_job_types_include_fill_cart():
    assert worker.JOB_TYPES == ("connect", "sync", "learn", "search", "fill_cart") or \
        "fill_cart" in worker.JOB_TYPES


# ---------------------------------------------------------------- cart learning

SEARCH = {"kind": "search", "field_map": {"products_path": "p"}, "read_post_allowlist": []}


def _admin_cart(pg, with_search=True, with_address=True, connected=True):
    add_user(pg, B, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    if connected:
        store.set_connection(pg, B, "zepto", status="connected")
    if with_search:
        store.put_recipe(pg, "zepto", SEARCH, kind="search")
    if with_address:
        pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,'Somewhere',22.3,73.1) "
                   "on conflict (user_id) do update set address='Somewhere', lat=22.3, lon=73.1", (B,))
    _seed_profile(B, "zepto")


def _learn(pg, s=None):
    jid = jobs.enqueue(pg, B, "learn", "zepto", {"reason": "manual", "kind": "cart"})
    job = jobs.claim(pg, "w", ("learn",))
    worker.run_job(job, pg, s or settings_for_tests())
    return jid


RECIPE = {"kind": "cart", "site": "zepto", "home_url": "https://www.zepto.com/",
          "cart_allowlist": [], "location_allowlist": [], "read_post_allowlist": []}


def test_learn_cart_job_ends_pending(pg, monkeypatch):
    _admin_cart(pg)
    cart_recipe(pg, "zepto")
    pg.execute("delete from recipes where kind='cart'")
    seen = {}

    def learn(site, **k):
        seen.update(k, site=site)
        k["approve"]([{"url_regex": "^https://www\\.zepto\\.com/api/cart$", "why": "add", "op": "cart"}])
        return {"recipe": RECIPE, "probe_item": "Amul Milk"}
    monkeypatch.setattr(cart_learn, "learn_cart", learn)
    monkeypatch.setattr(recipe_admin.Approver, "__call__", lambda self, entries: True)
    jid = _learn(pg)
    status, err, progress = _job(pg, jid)
    assert (status, err) == ("done", None)
    assert progress["stage"] == "learned" and progress["status"] == "pending" and progress["approval_id"]
    assert progress["note"] == ("Learning added 2 × Amul Milk to your Zepto cart. "
                                "Remove it in Zepto if you don't want it.")
    row = pg.execute("select kind, status from recipe_approvals where job_id=%s", (jid,)).fetchone()
    assert row == ("cart", "pending")
    assert store.get_recipe(pg, "zepto", "cart") is None
    assert seen["search_recipe"] == SEARCH and seen["location"]["lat"] == 22.3


def test_learn_cart_needs_search_recipe_and_connection(pg, monkeypatch):
    monkeypatch.setattr(cart_learn, "learn_cart", lambda *a, **k: pytest.fail("preconditions"))
    _admin_cart(pg, with_search=False)
    assert _job(pg, _learn(pg))[1] == "no_search_recipe"


def test_learn_cart_needs_connection(pg, monkeypatch):
    monkeypatch.setattr(cart_learn, "learn_cart", lambda *a, **k: pytest.fail("preconditions"))
    _admin_cart(pg, connected=False)
    assert _job(pg, _learn(pg))[1] == "needs_login"


def test_learn_cart_needs_address(pg, monkeypatch):
    monkeypatch.setattr(cart_learn, "learn_cart", lambda *a, **k: pytest.fail("preconditions"))
    _admin_cart(pg, with_address=False)
    assert _job(pg, _learn(pg))[1] == "no_address"


def test_learn_cart_requires_admin(pg, monkeypatch):
    monkeypatch.setattr(cart_learn, "learn_cart", lambda *a, **k: pytest.fail("admin only"))
    add_user(pg, B, "plain@example.com")
    assert _job(pg, _learn(pg))[1] == "not_admin"


def test_only_fill_and_cart_learn_use_cart_mode(pg, monkeypatch):
    """The engines open the site with a cart policy; the order sync and the orders learn do not."""
    modes = []

    class Stop(Exception):
        pass

    def open_site(site, **k):
        modes.append(k.get("cart_policy") is not None)
        raise Stop

    monkeypatch.setattr(browser, "open_site", open_site)
    rid, fid, jid, s = _setup(pg)
    _run(pg, s)
    assert modes == [True]
    assert fill.get_fill(pg, fid)["status"] == "interrupted"


# ---------------------------------------------------------------- final-review fixes (I1, I2, I4, M5)

def _raise(e):
    def engine(*a, **k):
        raise e
    return engine


def _sent(e, n=1):
    setattr(e, cart_fill.ADDS_SENT_ATTR, n)
    return e


def test_fill_chrome_is_never_kept_warm(pg, monkeypatch):
    """I1: a fill runs outside any warm scope, so its cart-mode Chrome closes with the fill."""
    rid, fid, jid, s = _setup(pg)
    scopes = []
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: scopes.append(browser._warm_key.get()) or _outcome())
    _run(pg, s)
    assert scopes == [None] and browser.POOL.handle is None
    assert fill.get_fill(pg, fid)["status"] == "done"
    row = pg.execute("select profile_blob_key from connections where user_id=%s and site='zepto'", (A,)).fetchone()
    assert row[0]  # saved at once (no warm Chrome to defer the save to)


def test_session_expired_after_an_add_is_interrupted(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", _raise(_sent(browser.SessionExpired("x"))))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("interrupted", "needs_login", True)
    assert store.get_connection(pg, A, "zepto")["status"] == "needs_login"


def test_throttle_after_an_add_is_interrupted(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", _raise(_sent(browser.SiteThrottled("rate_limited"))))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("interrupted", "site_busy", True)


def test_throttle_before_any_add_fails_and_refunds(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", _raise(browser.SiteThrottled("rate_limited")))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("failed", "site_busy", False)


def test_unconfirmed_add_is_interrupted_not_drift(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", _raise(_sent(cart_fill.CartInterrupted("add_unconfirmed"))))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("interrupted", "add_unconfirmed", True)
    assert not recipe_admin.is_paused(pg, "zepto", "cart")


def test_drift_after_an_add_is_interrupted_and_does_not_pause(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    monkeypatch.setattr(cart_fill, "fill_cart", _raise(_sent(cart_fill.CartDrift("x"))))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert f["status"] == "interrupted" and not recipe_admin.is_paused(pg, "zepto", "cart")


def test_invalid_cart_recipe_fails_the_fill_and_pauses(pg, monkeypatch):
    """I4: the stored cart recipe is checked again before every fill."""
    rid, fid, jid, s = _setup(pg)
    bad = valid_cart_recipe() | {"cart_allowlist": [{"url_regex": ".*", "why": "all", "op": "cart",
                                                      "qty_path": "item.quantity"}]}
    pg.execute("update recipes set recipe = %s where site='zepto' and kind='cart'", (Jsonb(bad),))
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: pytest.fail("an invalid recipe never fills"))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("failed", "cart_unavailable", False)
    assert recipe_admin.is_paused(pg, "zepto", "cart")


def test_question_progress_carries_its_index(pg):
    """M5: each question of a fill job is numbered on the progress, so a late answer can be told apart."""
    rid, fid, jid, s = _setup(pg)
    job = jobs.claim(pg, "w", ("fill_cart",))
    import psycopg
    listen = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    prompt = worker.make_prompt(listen, pg, job.id, 5)
    indexes = []
    for n in range(2):
        out = []
        t = threading.Thread(target=lambda: out.append(prompt("Which?", "question", [{"id": "a"}])))
        t.start()
        for _ in range(50):
            prog = _job(pg, job.id)[2]
            if prog.get("stage") == "question_needed":
                break
            time.sleep(0.05)
        indexes.append(prog["question_index"])
        pg.execute("select pg_notify(%s, %s)", ("otp_" + job.id.replace("-", ""), "a"))
        t.join(5)
    assert indexes == [0, 1]
    listen.close()


def test_prompt_ignores_an_indexed_answer_to_an_earlier_question(pg):
    """M5: the worker re-checks the index the API relays, so a late answer can't race into the next question."""
    import json as _json
    rid, fid, jid, s = _setup(pg)
    job = jobs.claim(pg, "w", ("fill_cart",))
    import psycopg
    listen = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    prompt = worker.make_prompt(listen, pg, job.id, 5)
    chan = "otp_" + job.id.replace("-", "")
    out = []
    t = threading.Thread(target=lambda: out.append(prompt("Which?", "question", [{"id": "a"}, {"id": "b"}])))
    t.start()
    for _ in range(50):
        if _job(pg, job.id)[2].get("stage") == "question_needed":
            break
        time.sleep(0.05)
    pg.execute("select pg_notify(%s, %s)", (chan, _json.dumps({"question_index": 7, "answer": "a"})))
    pg.execute("select pg_notify(%s, %s)", (chan, _json.dumps({"question_index": 0, "answer": "b"})))
    t.join(5)
    assert out == ["b"]
    listen.close()
