import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

HERE = Path(__file__).parent
MIGRATIONS = HERE.parent.parent / "supabase" / "migrations"
TABLES = ("profiles", "connections", "orders", "order_items", "item_categories",
          "recipes", "jobs", "consents")


def _url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def _schema():
    url = _url()
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("drop schema if exists public cascade")
        c.execute("create schema public")
        c.execute("drop schema if exists auth cascade")
    return url


@pytest.fixture(scope="session")
def _applied(_schema):
    with psycopg.connect(_schema, autocommit=True, options="-c client_min_messages=warning") as c:
        c.execute((HERE / "bootstrap.sql").read_text(encoding="utf-8"))
        for f in sorted(MIGRATIONS.glob("*.sql")):
            c.execute(f.read_text(encoding="utf-8"))
    return _schema


@pytest.fixture
def pg(_applied):
    with psycopg.connect(_applied, autocommit=True) as c:
        c.execute("truncate " + ", ".join(TABLES) + " cascade")
        yield c


@pytest.fixture
def pg2(_applied, pg):
    """Second independent autocommit connection (pg already truncates)."""
    with psycopg.connect(_applied, autocommit=True) as c:
        yield c


@pytest.fixture
def as_user():
    @contextmanager
    def _as_user(conn, user_id):
        conn.execute("begin")
        try:
            conn.execute("set local role authenticated")
            conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(user_id),))
            yield conn
        finally:
            conn.execute("rollback")
    return _as_user


@pytest.fixture
def client(pg, monkeypatch):
    from fastapi.testclient import TestClient

    from server.api import app
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.delenv("SUPABASE_JWKS_URL", raising=False)
    return TestClient(app)


@pytest.fixture
def token_a():
    from helpers import A, auth_header
    return auth_header(A)


@pytest.fixture
def token_b():
    from helpers import B, auth_header
    return auth_header(B)


@pytest.fixture
def pg_listen(_applied):
    """Autocommit connection dedicated to LISTEN/NOTIFY."""
    with psycopg.connect(_applied, autocommit=True) as c:
        yield c
