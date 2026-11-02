"""OTP auto-forward: pairing, device tokens, SMS relay to a waiting job only, rate limits, log scrubbing."""
import json
import logging
import time
from pathlib import Path

import pytest
from helpers import A, B

from server import erase, otp_devices

FIXTURE = Path(__file__).parent.parent / "fixtures" / "otp_sms.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
ZOMATO_SMS = {"sender": "VM-ZOMATO", "body": "582913 is your OTP to log in to Zomato. Do not share it."}


@pytest.fixture(autouse=True)
def _fresh_limits():
    otp_devices.pair_limiter.reset()
    otp_devices.sms_limiter.reset()
    otp_devices.pair_fail_limiter.reset()
    otp_devices.seen_codes.reset()
    yield
    otp_devices.pair_limiter.reset()
    otp_devices.sms_limiter.reset()
    otp_devices.pair_fail_limiter.reset()
    otp_devices.seen_codes.reset()


def _ms(offset_s=0.0):
    return int((time.time() + offset_s) * 1000)


def _pair(client, token, name="Pixel"):
    code = client.post("/otp-devices/pairing", headers=token).json()["code"]
    r = client.post("/otp-devices/pair", json={"code": code, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _dev(tok):
    return {"Authorization": f"Bearer {tok}"}


def _waiting_job(pg, user, site="zomato", jtype="connect", stage="otp_needed"):
    return str(pg.execute("insert into jobs(user_id, type, site, profile, status, progress) values (%s,%s,%s,%s||'_'||%s,'running',"
                          "jsonb_build_object('stage', %s::text, 'prompt_at', now())) returning id", (user, jtype, site, user, site, stage)).fetchone()[0])


def _send(client, tok, sms=ZOMATO_SMS, ts=None):
    return client.post("/otp-devices/sms", json={**sms, "ts": _ms() if ts is None else ts}, headers=_dev(tok))


def _notified(pg2, jid, timeout=1.5):
    return [n.payload for n in pg2.notifies(timeout=timeout, stop_after=1)]


# ------------------------------------------------------------------ extraction vectors (shared with Android tests)

@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_vectors(case):
    brand = otp_devices.brand_for_sender(case["sender"])
    assert brand == case["brand"]
    code = otp_devices.extract_code(case["body"]) if brand else None
    assert code == case["code"]


def test_fixture_covers_every_brand_and_negatives():
    assert {c["brand"] for c in CASES if c["code"]} == {"zomato", "swiggy", "zepto", "blinkit"}
    assert sum(1 for c in CASES if c["code"] is None) >= 5


# ------------------------------------------------------------------ pairing

def test_pairing_happy_path(client, token_a, pg):
    r = client.post("/otp-devices/pairing", headers=token_a)
    assert r.status_code == 200
    code = r.json()["code"]
    assert r.json()["expires_in"] == 600 and len(code) == 8 and set(code) <= set(otp_devices.CODE_ALPHABET)
    stored = pg.execute("select code_hash from otp_pairings").fetchall()
    assert len(stored) == 1 and stored[0][0] != code and code not in stored[0][0]
    out = client.post("/otp-devices/pair", json={"code": code.lower(), "name": "Pixel 8"}).json()
    assert set(out) == {"token", "device_id", "senders"} and out["senders"]
    row = pg.execute("select user_id::text, name, token_hash from otp_devices").fetchone()
    assert row[0] == A and row[1] == "Pixel 8" and out["token"] not in row[2]
    assert client.get("/otp-devices/config", headers=_dev(out["token"])).json()["senders"] == out["senders"]


def test_pairing_needs_user_token(client):
    assert client.post("/otp-devices/pairing").status_code == 401


def test_reused_code_fails(client, token_a):
    code = client.post("/otp-devices/pairing", headers=token_a).json()["code"]
    assert client.post("/otp-devices/pair", json={"code": code, "name": "x"}).status_code == 200
    assert client.post("/otp-devices/pair", json={"code": code, "name": "y"}).status_code == 400


def test_expired_code_fails(client, token_a, pg):
    code = client.post("/otp-devices/pairing", headers=token_a).json()["code"]
    pg.execute("update otp_pairings set expires_at = now() - interval '1 second'")
    assert client.post("/otp-devices/pair", json={"code": code, "name": "x"}).status_code == 400


def test_wrong_and_malformed_codes_fail(client):
    assert client.post("/otp-devices/pair", json={"code": "ABCDEFGH", "name": "x"}).status_code == 400
    assert client.post("/otp-devices/pair", json={"code": 12345678}).status_code == 400
    assert client.post("/otp-devices/pair", content=b"not json").status_code == 400


def test_pairing_rate_limit_five_per_hour(client, token_a):
    for _ in range(5):
        assert client.post("/otp-devices/pairing", headers=token_a).status_code == 200
    assert client.post("/otp-devices/pairing", headers=token_a).status_code == 429


def test_pair_rate_limited_per_ip(client):
    codes = [client.post("/otp-devices/pair", json={"code": "ABCDEFGH"}).status_code for _ in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429


# ------------------------------------------------------------------ device list and revoke

def test_list_never_returns_token_and_revoke_is_immediate(client, token_a, token_b):
    dev = _pair(client, token_a)
    listed = client.get("/otp-devices", headers=token_a).json()["devices"]
    assert [d["id"] for d in listed] == [dev["device_id"]]
    assert dev["token"] not in json.dumps(listed) and "token_hash" not in json.dumps(listed)
    assert client.get("/otp-devices", headers=token_b).json()["devices"] == []
    assert client.delete(f"/otp-devices/{dev['device_id']}", headers=token_b).status_code == 404
    assert client.delete(f"/otp-devices/{dev['device_id']}", headers=token_a).status_code == 204
    assert client.get("/otp-devices/config", headers=_dev(dev["token"])).status_code == 401
    assert _send(client, dev["token"]).status_code == 401
    assert client.get("/otp-devices", headers=token_a).json()["devices"] == []


def test_unknown_device_token_is_401(client):
    assert client.get("/otp-devices/config", headers=_dev("nope")).status_code == 401
    assert client.post("/otp-devices/sms", json={}).status_code == 401


# ------------------------------------------------------------------ SMS relay

def test_relays_to_waiting_connect_job(client, token_a, pg, pg2):
    tok = _pair(client, token_a)["token"]
    jid = _waiting_job(pg, A)
    pg2.execute("listen otp_" + jid.replace("-", ""))
    r = _send(client, tok)
    assert r.status_code == 204 and r.content == b""
    assert _notified(pg2, jid) == ["582913"]
    assert pg.execute("select count(*) from jobs where progress::text like '%582913%'").fetchone()[0] == 0


def test_relays_to_waiting_fill_job_and_instamart_via_swiggy(client, token_a, pg, pg2):
    tok = _pair(client, token_a)["token"]
    jid = _waiting_job(pg, A, site="instamart", jtype="fill_cart")
    pg2.execute("listen otp_" + jid.replace("-", ""))
    _send(client, tok, {"sender": "AD-SWIGGY", "body": "Your OTP for Swiggy login is 309184."})
    assert _notified(pg2, jid) == ["309184"]


@pytest.mark.parametrize("why", ["wrong_brand", "no_job", "not_waiting", "queued", "stale", "future", "bad_sender",
                                 "no_code", "garbage"])
def test_dropped_silently(client, token_a, pg, pg2, why):
    tok = _pair(client, token_a)["token"]
    site = "zepto" if why == "wrong_brand" else "zomato"
    stage = "otp_received" if why == "not_waiting" else "otp_needed"
    jid = None if why == "no_job" else _waiting_job(pg, A, site=site, stage=stage)
    if why == "queued":
        pg.execute("update jobs set status = 'queued' where id = %s", (jid,))
    if jid:
        pg2.execute("listen otp_" + jid.replace("-", ""))
    sms, ts = dict(ZOMATO_SMS), _ms()
    if why == "stale":
        ts = _ms(-121)
    if why == "future":
        ts = _ms(61)
    if why == "bad_sender":
        sms["sender"] = "AX-HDFCBK"
    if why == "no_code":
        sms["body"] = "Use code TRYNEW for 60% off above Rs 1599"
    if why == "garbage":
        r = client.post("/otp-devices/sms", content=b"\x00not json", headers=_dev(tok))
    else:
        r = _send(client, tok, sms, ts)
    assert r.status_code == 204 and r.content == b""
    if jid:
        assert _notified(pg2, jid, timeout=0.5) == []


def test_another_users_job_never_receives_the_code(client, token_a, pg, pg2):
    tok_a = _pair(client, token_a)["token"]
    jid_b = _waiting_job(pg, B)
    pg2.execute("listen otp_" + jid_b.replace("-", ""))
    assert _send(client, tok_a).status_code == 204
    assert _notified(pg2, jid_b, timeout=0.5) == []


def test_sms_rate_limit_ten_per_minute(client, token_a):
    tok = _pair(client, token_a)["token"]
    codes = [_send(client, tok).status_code for _ in range(11)]
    assert codes[:10] == [204] * 10 and codes[10] == 429


def test_logs_never_carry_body_code_or_sender(client, token_a, pg, pg2, caplog):
    caplog.set_level(logging.DEBUG)
    tok = _pair(client, token_a)["token"]
    jid = _waiting_job(pg, A)
    pg2.execute("listen otp_" + jid.replace("-", ""))
    _send(client, tok)
    _send(client, tok, {"sender": "AX-HDFCBK", "body": "OTP 451209 for txn"})
    client.post("/otp-devices/sms", content=b"{broken 777123", headers=_dev(tok))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("582913", "451209", "777123", "ZOMATO", "HDFCBK", "Zomato", tok):
        assert secret not in text
    assert "relayed=1" in text


# ------------------------------------------------------------------ account deletion

def test_account_deletion_removes_devices_and_pairings(client, token_a, token_b, pg, tmp_path):
    from server import vault
    _pair(client, token_a)
    client.post("/otp-devices/pairing", headers=token_a)
    _pair(client, token_b)
    assert {"otp_devices", "otp_pairings"} <= set(erase.user_tables(pg))
    erase.delete_account(pg, vault.LocalStore(tmp_path), A)
    assert pg.execute("select count(*) from otp_devices where user_id = %s", (A,)).fetchone()[0] == 0
    assert pg.execute("select count(*) from otp_pairings where user_id = %s", (A,)).fetchone()[0] == 0
    assert pg.execute("select count(*) from otp_devices where user_id = %s", (B,)).fetchone()[0] == 1


def test_tables_are_server_only(pg):
    for t in ("otp_devices", "otp_pairings"):
        assert pg.execute("select relrowsecurity from pg_class where relname = %s", (t,)).fetchone()[0] is True
        assert pg.execute("select count(*) from pg_policies where tablename = %s", (t,)).fetchone()[0] == 0
        for role in ("anon", "authenticated"):
            assert pg.execute("select has_table_privilege(%s, %s, 'select')", (role, t)).fetchone()[0] is False


# ------------------------------------------------------------------ replay, freshness, global ceiling, purge

def test_replay_is_dropped_and_older_than_prompt_is_dropped(client, token_a, pg, pg2):
    tok = _pair(client, token_a)["token"]
    jid = _waiting_job(pg, A)
    pg2.execute("listen otp_" + jid.replace("-", ""))
    _send(client, tok, ts=_ms(-30))                      # sent before the prompt: stale
    assert _notified(pg2, jid, 0.5) == []
    _send(client, tok)                                   # fresh
    assert _notified(pg2, jid) == ["582913"]
    _send(client, tok)                                   # replay of the same code
    assert _notified(pg2, jid, 0.5) == []


def test_global_failed_pair_ceiling_delays_but_valid_code_works(client, token_a, monkeypatch):
    monkeypatch.setattr(otp_devices, "pair_limiter", otp_devices.Window(1000, 3600))
    monkeypatch.setattr(otp_devices, "pair_fail_limiter", otp_devices.Window(2, 3600))
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(otp_devices.asyncio, "sleep", fake_sleep)
    for _ in range(4):
        assert client.post("/otp-devices/pair", json={"code": "ABCDEFGH"}).status_code == 400
    assert len(slept) == 2
    code = client.post("/otp-devices/pairing", headers=token_a).json()["code"]
    assert client.post("/otp-devices/pair", json={"code": code}).status_code == 200
    assert len(slept) == 2


def test_old_pairing_rows_are_purged_on_new_code(client, token_a, pg):
    client.post("/otp-devices/pairing", headers=token_a)
    pg.execute("update otp_pairings set expires_at = now() - interval '2 days', used_at = now() - interval '2 days'")
    client.post("/otp-devices/pairing", headers=token_a)
    assert pg.execute("select count(*) from otp_pairings").fetchone()[0] == 1
