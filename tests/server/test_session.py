"""Session expiry: one transition, one reminder per episode."""
from datetime import datetime, timedelta, timezone

from helpers import A, B

from server import jobs, session, store
from server.config import Settings

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
S = Settings()


def _conn(pg, user=A, site="zepto", status="connected"):
    store.set_connection(pg, user, site, status=status)


def _notes(pg, kind=None):
    q = "select kind, title, body, url, dedupe_key, user_id from notifications"
    if kind:
        return pg.execute(q + " where kind=%s order by created_at", (kind,)).fetchall()
    return pg.execute(q).fetchall()


def test_first_expiry_notifies_with_deep_link(pg):
    _conn(pg)
    assert session.mark_needs_login(pg, A, "zepto", now=NOW) is True
    assert pg.execute("select status, needs_login_at from connections").fetchone() == ("needs_login", NOW)
    [(kind, title, body, url, key, uid)] = _notes(pg)
    assert (kind, title, body, url) == ("needs_login", "Zepto logged you out",
                                        "Reconnect to keep your spending up to date.", "/accounts?reconnect=zepto")
    assert key == f"needs_login:{A}:zepto:{int(NOW.timestamp())}"
    assert str(uid) == A


def test_second_expiry_same_episode_is_silent(pg):
    _conn(pg)
    assert session.mark_needs_login(pg, A, "zepto", now=NOW) is True
    assert session.mark_needs_login(pg, A, "zepto", now=NOW + timedelta(hours=1)) is False
    assert len(_notes(pg)) == 1
    assert pg.execute("select needs_login_at from connections").fetchone() == (NOW,)


def test_new_episode_after_reconnect_notifies_again(pg):
    _conn(pg)
    session.mark_needs_login(pg, A, "zepto", now=NOW)
    pg.execute("update connections set status='connected', needs_login_at=NULL")
    assert session.mark_needs_login(pg, A, "zepto", now=NOW + timedelta(days=2)) is True
    assert len(_notes(pg, "needs_login")) == 2


def test_queued_syncs_dropped_on_transition(pg):
    _conn(pg, site="swiggy")
    _conn(pg, B, site="swiggy")
    jobs.enqueue(pg, A, "sync", "instamart")
    jobs.enqueue(pg, B, "sync", "swiggy")
    pg.execute("insert into jobs(user_id,type,site,profile,status) values (%s,'search','swiggy','swiggy','queued')", (A,))
    session.mark_needs_login(pg, A, "swiggy", now=NOW)
    rows = pg.execute("select user_id, type from jobs order by type").fetchall()
    assert [(str(u), t) for u, t in rows] == [(A, "search"), (B, "sync")]


def test_reminder_once_after_72h(pg):
    _conn(pg)
    session.mark_needs_login(pg, A, "zepto", now=NOW)
    assert session.remind(pg, S, NOW + timedelta(hours=71)) == 0
    assert session.remind(pg, S, NOW + timedelta(hours=73)) == 1
    assert session.remind(pg, S, NOW + timedelta(hours=74)) == 0
    [(kind, title, body, url, key, _)] = _notes(pg, "needs_login_reminder")
    assert title == "Zepto is still logged out"
    assert body == "Your spending stops updating until you reconnect. It takes a minute."
    assert url == "/accounts?reconnect=zepto"
    assert key == f"needs_login_reminder:{A}:zepto:{int(NOW.timestamp())}"


def test_no_connection_row_is_noop(pg):
    assert session.mark_needs_login(pg, A, "zepto", now=NOW) is False
    assert _notes(pg) == []
    assert pg.execute("select count(*) from connections").fetchone() == (0,)
