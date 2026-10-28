"""Notification outbox: rows go to a user (Web Push, else email) or to a bare email address.

Senders are injected into dispatch(); nothing here logs push endpoints, keys, secrets or full email addresses.
"""
import json
import logging
import smtplib
from email.message import EmailMessage

from pywebpush import WebPushException, webpush

log = logging.getLogger("notify")

MAX_ATTEMPTS = 5


class PushGone(Exception):
    """The push service says the subscription no longer exists (HTTP 404/410)."""


def mask_email(e: str) -> str:
    local, _, domain = (e or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def format_inr(amount: float) -> str:
    """Indian digit grouping: 123456 -> ₹1,23,456. Two decimals below ₹100, whole rupees otherwise."""
    if abs(amount) < 100:
        return f"₹{amount:.2f}"
    n = str(int(round(abs(amount))))
    head, tail = n[:-3], n[-3:]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{','.join(groups + [tail])}"


def enqueue(conn, *, kind: str, title: str, body: str, url: str = "/", user_id: str | None = None,
            email: str | None = None, dedupe_key: str) -> str | None:
    row = conn.execute(
        "INSERT INTO notifications (user_id, email, kind, title, body, url, dedupe_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (user_id, email.lower() if email else None, kind, title, body, url, dedupe_key)).fetchone()
    return str(row[0]) if row else None


def alert_admins(conn, *, kind: str, title: str, body: str, dedupe_key: str) -> int:
    n = 0
    for (email,) in conn.execute("SELECT email FROM admins ORDER BY email").fetchall():
        if enqueue(conn, kind=kind, title=title, body=body, email=email, dedupe_key=f"{dedupe_key}:{email}"):
            n += 1
    return n


def save_subscription(conn, user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
    """Upsert the caller's subscription. An endpoint already held by another user is left untouched: it never
    moves to the caller (nor are its keys replaced)."""
    if not endpoint.startswith("https://"):
        raise ValueError("push endpoint must be https")
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
        "auth = excluded.auth WHERE push_subscriptions.user_id = excluded.user_id",
        (endpoint, user_id, p256dh, auth))


def delete_subscription(conn, user_id: str, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = %s AND user_id = %s", (endpoint, user_id))


def push_send(sub: dict, payload: dict, s) -> None:
    try:
        webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=s.vapid_private_key,
                vapid_claims={"sub": s.vapid_subject}, ttl=86400)
    except WebPushException as e:
        if getattr(e.response, "status_code", None) in (404, 410):
            raise PushGone() from None
        raise


def mail_send(to: str, subject: str, text: str, s) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(text)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
        if s.smtp_starttls:
            smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


def _send_user_row(conn, s, r, push, mail) -> str:
    """Returns the channel used. Push to every subscription; any success wins, else email."""
    if s.vapid_private_key:
        payload = {"title": r["title"], "body": r["body"], "url": r["url"]}
        subs = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
                            (r["user_id"],)).fetchall()
        ok = False
        for endpoint, p256dh, auth in subs:
            try:
                push({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}, payload, s)
            except PushGone:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
            except Exception as e:
                log.warning("push failed (%s)", type(e).__name__)
            else:
                conn.execute("UPDATE push_subscriptions SET last_ok_at = now() WHERE endpoint = %s", (endpoint,))
                ok = True
        if ok:
            return "push"
    got = conn.execute("SELECT email FROM auth.users WHERE id = %s", (r["user_id"],)).fetchone()
    if not got or not got[0]:
        raise LookupError("no email for user")
    mail(got[0], r["title"], f"{r['body']}\n\n{s.app_url}{r['url']}", s)
    return "email"


def dispatch(conn, s, push_send=None, mail_send=None, limit: int = 50) -> int:
    push = push_send or globals()["push_send"]
    mail = mail_send or globals()["mail_send"]
    sent = 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT id, user_id, email, title, body, url FROM notifications WHERE status = 'pending' "
            "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED", (limit,)).fetchall()
        for id_, user_id, email, title, body, url in rows:
            r = {"user_id": user_id, "title": title, "body": body, "url": url}
            try:
                with conn.transaction():  # savepoint: a half-done send never leaves partial changes behind
                    if user_id:
                        channel = _send_user_row(conn, s, r, push, mail)
                    else:
                        mail(email, title, f"{body}\n\n{s.app_url}{url}", s)
                        channel = "email"
            except Exception as e:
                log.warning("notification %s failed (%s)", id_, type(e).__name__)
                conn.execute(
                    "UPDATE notifications SET attempts = attempts + 1, error = %s, "
                    "status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END WHERE id = %s",
                    (type(e).__name__, MAX_ATTEMPTS, id_))
            else:
                conn.execute("UPDATE notifications SET status = 'sent', channel = %s, sent_at = now(), "
                             "error = NULL WHERE id = %s", (channel, id_))
                sent += 1
    return sent
