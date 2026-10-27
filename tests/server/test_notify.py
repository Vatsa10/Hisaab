"""Notification outbox: dedupe, push first then email, retries, masking, default senders."""
import logging
import uuid

import pytest

from server import notify
from server.config import Settings

U = str(uuid.uuid4())
EP = "https://push.example.com/send/SECRET-ENDPOINT-TOKEN"


def S(**kw):
    base = dict(vapid_private_key="vapid-priv", vapid_subject="mailto:ops@example.com",
                smtp_host="mailpit", smtp_port=1025, app_url="https://hisaab.example.com")
    base.update(kw)
    return Settings(**base)


class Rec:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, *a):
        self.calls.append(a)
        if self.exc:
            raise self.exc


def user(pg, email="asha@example.com"):
    pg.execute("insert into auth.users (id, email) values (%s,%s)", (U, email))


def sub(pg, endpoint=EP):
    notify.save_subscription(pg, U, endpoint, "p256", "authkey")


def row(pg):
    return pg.execute("select status, channel, attempts, error from notifications").fetchone()


def test_enqueue_dedupes(pg):
    a = notify.enqueue(pg, kind="k", title="t", body="b", user_id=U, dedupe_key="d1")
    assert a
    assert notify.enqueue(pg, kind="k", title="t", body="b", user_id=U, dedupe_key="d1") is None
    assert pg.execute("select count(*) from notifications").fetchone()[0] == 1


def test_dispatch_prefers_push(pg):
    user(pg)
    sub(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", url="/x", user_id=U, dedupe_key="d")
    push, mail = Rec(), Rec()
    assert notify.dispatch(pg, S(), push, mail) == 1
    assert push.calls[0][1] == {"title": "T", "body": "B", "url": "/x"}
    assert push.calls[0][0]["endpoint"] == EP
    assert not mail.calls
    assert row(pg)[:2] == ("sent", "push")


def test_dispatch_falls_back_to_email_without_subscription(pg):
    user(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", user_id=U, dedupe_key="d")
    push, mail = Rec(), Rec()
    assert notify.dispatch(pg, S(), push, mail) == 1
    assert not push.calls
    assert mail.calls[0][:3] == ("asha@example.com", "T", "B\n\nhttps://hisaab.example.com/")
    assert row(pg)[:2] == ("sent", "email")


def test_vapid_unset_skips_push(pg):
    user(pg)
    sub(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", user_id=U, dedupe_key="d")
    push, mail = Rec(), Rec()
    notify.dispatch(pg, S(vapid_private_key=""), push, mail)
    assert not push.calls and mail.calls and row(pg)[1] == "email"


def test_gone_subscription_is_deleted_then_email(pg):
    user(pg)
    sub(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", user_id=U, dedupe_key="d")
    push, mail = Rec(notify.PushGone()), Rec()
    assert notify.dispatch(pg, S(), push, mail) == 1
    assert pg.execute("select count(*) from push_subscriptions").fetchone()[0] == 0
    assert mail.calls and row(pg)[:2] == ("sent", "email")


def test_failure_retries_then_fails_after_5(pg):
    user(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", user_id=U, dedupe_key="d")
    mail = Rec(ConnectionError("smtp down for asha@example.com"))
    for i in range(1, 5):
        assert notify.dispatch(pg, S(), Rec(), mail) == 0
        assert row(pg) == ("pending", None, i, "ConnectionError")
    notify.dispatch(pg, S(), Rec(), mail)
    assert row(pg) == ("failed", None, 5, "ConnectionError")
    assert notify.dispatch(pg, S(), Rec(), mail) == 0
    assert len(mail.calls) == 5


def test_alert_admins_emails_every_admin(pg):
    pg.execute("insert into admins (email) values ('a@example.com'), ('b@example.com')")
    assert notify.alert_admins(pg, kind="k", title="T", body="B", dedupe_key="x") == 2
    assert notify.alert_admins(pg, kind="k", title="T", body="B", dedupe_key="x") == 0
    keys = {r[0] for r in pg.execute("select dedupe_key from notifications")}
    assert keys == {"x:a@example.com", "x:b@example.com"}
    mail = Rec()
    assert notify.dispatch(pg, S(), Rec(), mail) == 2
    assert {c[0] for c in mail.calls} == {"a@example.com", "b@example.com"}


def test_dispatch_never_logs_endpoint_or_email(pg, caplog):
    caplog.set_level(logging.DEBUG)
    user(pg)
    sub(pg)
    notify.enqueue(pg, kind="k", title="T", body="B", user_id=U, dedupe_key="d1")
    notify.dispatch(pg, S(), Rec(notify.PushGone()), Rec(RuntimeError("to asha@example.com")))
    pg.execute("insert into admins (email) values ('boss@example.com')")
    notify.alert_admins(pg, kind="k", title="T", body="B", dedupe_key="d2")
    notify.dispatch(pg, S(), Rec(), Rec(RuntimeError("boss@example.com")))
    text = caplog.text
    assert "SECRET-ENDPOINT-TOKEN" not in text and "asha@example.com" not in text
    assert "boss@example.com" not in text and "authkey" not in text


def test_default_mail_send_uses_smtp_settings(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            seen.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            seen["tls"] = True

        def login(self, u, p):
            seen["login"] = (u, p)

        def send_message(self, m):
            seen["msg"] = m

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    notify.mail_send("to@example.com", "Subj", "Body\n\nhttps://hisaab.example.com/orders", S(smtp_from="hi@hisaab.example.com"))
    assert (seen["host"], seen["port"], seen["timeout"]) == ("mailpit", 1025, 10)
    assert "tls" not in seen and "login" not in seen
    m = seen["msg"]
    assert m["To"] == "to@example.com" and m["Subject"] == "Subj" and m["From"] == "hi@hisaab.example.com"
    assert m.get_content().strip() == "Body\n\nhttps://hisaab.example.com/orders"
    seen.clear()
    notify.mail_send("to@example.com", "S", "B", S(smtp_user="u", smtp_password="p", smtp_starttls=True))
    assert seen["tls"] and seen["login"] == ("u", "p")


def test_default_push_send_maps_gone(monkeypatch):
    import pywebpush

    class R:
        status_code = 410

    def boom(**kw):
        raise pywebpush.WebPushException("gone", response=R())

    monkeypatch.setattr(notify, "webpush", boom)
    with pytest.raises(notify.PushGone):
        notify.push_send({"endpoint": EP, "keys": {"p256dh": "a", "auth": "b"}}, {"title": "t"}, S())


def test_subscription_requires_https(pg):
    with pytest.raises(ValueError):
        notify.save_subscription(pg, U, "http://push.example.com/x", "p", "a")
    sub(pg)
    notify.save_subscription(pg, U, EP, "p2", "a2")
    assert pg.execute("select p256dh from push_subscriptions").fetchall() == [("p2",)]
    notify.delete_subscription(pg, U, EP)
    assert pg.execute("select count(*) from push_subscriptions").fetchone()[0] == 0


def test_format_inr_and_mask():
    assert notify.format_inr(123456) == "₹1,23,456"
    assert notify.format_inr(1234567.0) == "₹12,34,567"
    assert notify.format_inr(99.5) == "₹99.50"
    assert notify.format_inr(100) == "₹100"
    assert notify.mask_email("asha@example.com") == "a***@example.com"
