import time

import pytest

from helpers import A, auth_header, make_token


def _running_connect(client, pg, token, site="zepto"):
    client.post("/consents", json={"site": site}, headers=token)
    jid = client.post(f"/connections/{site}/connect", json={"phone": "9876543210"}, headers=token).json()["job_id"]
    pg.execute("update jobs set status='running' where id=%s", (jid,))
    return jid


def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_consent_is_stored(client, token_a, pg):
    assert client.post("/consents", json={"site": "swiggy"}, headers=token_a).status_code == 204
    assert client.post("/consents", json={"site": "swiggy"}, headers=token_a).status_code == 204  # idempotent
    rows = pg.execute("select user_id::text, site, text_version from consents").fetchall()
    assert rows == [(A, "swiggy", "2026-10-27")]


def test_consent_unknown_site_is_400(client, token_a):
    assert client.post("/consents", json={"site": "nope"}, headers=token_a).status_code == 400


def test_connect_needs_consent(client, token_a):
    r = client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a)
    assert r.status_code == 412


def test_connect_twice_conflicts(client, token_a):
    client.post("/consents", json={"site": "swiggy"}, headers=token_a)
    assert client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 202
    assert client.post("/connections/swiggy/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 409


def test_connect_stores_normalised_phone(client, token_a, pg):
    client.post("/consents", json={"site": "zepto"}, headers=token_a)
    r = client.post("/connections/zepto/connect", json={"phone": "+91 98765-43210"}, headers=token_a)
    assert r.status_code == 202
    row = pg.execute("select type, site, payload from jobs where id=%s", (r.json()["job_id"],)).fetchone()
    assert row == ("connect", "zepto", {"phone": "9876543210"})


def test_connect_bad_phone_is_422(client, token_a):
    client.post("/consents", json={"site": "zepto"}, headers=token_a)
    assert client.post("/connections/zepto/connect", json={"phone": "123"}, headers=token_a).status_code == 422


def test_connect_unknown_site_is_400(client, token_a):
    assert client.post("/connections/nope/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 400


def test_instamart_cannot_be_connected(client, token_a):
    assert client.post("/connections/instamart/connect", json={"phone": "9876543210"}, headers=token_a).status_code == 400


def test_otp_for_someone_elses_job_is_404(client, token_a, token_b, pg):
    jid = _running_connect(client, pg, token_a)
    assert client.post(f"/jobs/{jid}/otp", json={"otp": "1234"}, headers=token_b).status_code == 404


def test_otp_404_unless_running_connect(client, token_a, pg):
    client.post("/consents", json={"site": "zepto"}, headers=token_a)
    jid = client.post("/connections/zepto/connect", json={"phone": "9876543210"}, headers=token_a).json()["job_id"]
    assert client.post(f"/jobs/{jid}/otp", json={"otp": "1234"}, headers=token_a).status_code == 404  # queued
    pg.execute("update jobs set status='running', type='sync' where id=%s", (jid,))
    assert client.post(f"/jobs/{jid}/otp", json={"otp": "1234"}, headers=token_a).status_code == 404  # not connect
    assert client.post("/jobs/not-a-uuid/otp", json={"otp": "1234"}, headers=token_a).status_code == 404


def test_otp_reaches_listener_and_is_not_stored(client, token_a, pg, pg2):
    jid = _running_connect(client, pg, token_a)
    pg2.execute("listen otp_" + jid.replace("-", ""))
    assert client.post(f"/jobs/{jid}/otp", json={"otp": "1234"}, headers=token_a).status_code == 204
    got = []
    for n in pg2.notifies(timeout=3, stop_after=1):
        got.append((n.channel, n.payload))
    assert got == [("otp_" + jid.replace("-", ""), "1234")]
    row = pg.execute("select payload::text, progress::text, coalesce(error,'') from jobs where id=%s", (jid,)).fetchone()
    assert not any("1234" in col for col in row)


def test_refresh_without_connection_is_404(client, token_a):
    assert client.post("/connections/swiggy/refresh", headers=token_a).status_code == 404


def test_refresh_enqueues_then_conflicts(client, token_a, pg):
    pg.execute("insert into connections (user_id, site, status) values (%s,'swiggy','connected')", (A,))
    r = client.post("/connections/swiggy/refresh", headers=token_a)
    assert r.status_code == 202
    assert pg.execute("select type from jobs where id=%s", (r.json()["job_id"],)).fetchone() == ("sync",)
    assert client.post("/connections/swiggy/refresh", headers=token_a).status_code == 409


def test_rejects_missing_or_bad_token(client):
    assert client.post("/consents", json={"site": "zepto"}).status_code == 401
    bad = {"Authorization": "Bearer garbage"}
    assert client.post("/consents", json={"site": "zepto"}, headers=bad).status_code == 401
    wrong = {"Authorization": f"Bearer {make_token(A, secret='other-secret-of-32-bytes-or-more!!')}"}
    assert client.post("/consents", json={"site": "zepto"}, headers=wrong).status_code == 401
    expired = {"Authorization": f"Bearer {make_token(A, exp_in=-10)}"}
    assert client.post("/consents", json={"site": "zepto"}, headers=expired).status_code == 401
    wrong_aud = {"Authorization": f"Bearer {make_token(A, aud='other')}"}
    assert client.post("/consents", json={"site": "zepto"}, headers=wrong_aud).status_code == 401


def test_refresh_conflicts_while_connect_is_active(client, token_a, pg):
    client.post("/consents", json={"site": "zepto"}, headers=token_a)
    pg.execute("insert into connections (user_id, site, status) values (%s,'zepto','syncing')", (A,))
    jid = client.post("/connections/zepto/connect", json={"phone": "9876543210"}, headers=token_a).json()["job_id"]
    assert client.post("/connections/zepto/refresh", headers=token_a).status_code == 409  # queued
    pg.execute("update jobs set status='running' where id=%s", (jid,))
    assert client.post("/connections/zepto/refresh", headers=token_a).status_code == 409  # running
    pg.execute("update jobs set status='done' where id=%s", (jid,))
    assert client.post("/connections/zepto/refresh", headers=token_a).status_code == 202
