"""The server-test fixtures drop and recreate schemas: they must refuse a database that is not local."""
import pytest
from helpers import destructive_db_allowed


@pytest.mark.parametrize("url", ["postgresql://postgres:postgres@localhost:54329/postgres",
                                 "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
                                 "postgresql://postgres@[::1]:5432/postgres",
                                 "host=localhost port=54329 user=postgres dbname=postgres"])
def test_local_database_is_allowed(url):
    assert destructive_db_allowed(url, {})


@pytest.mark.parametrize("url", ["postgresql://postgres:pw@db.abcd.supabase.co:5432/postgres",
                                 "postgresql://postgres:pw@10.0.0.5:5432/postgres",
                                 "postgresql://postgres:pw@localhost.evil.example:5432/postgres",
                                 "host=db.example.com dbname=postgres"])
def test_remote_database_is_refused(url):
    assert not destructive_db_allowed(url, {})
    assert not destructive_db_allowed(url, {"ALLOW_DESTRUCTIVE_TEST_DB": "0"})


def test_remote_database_allowed_with_explicit_opt_in():
    url = "postgresql://postgres:pw@db.abcd.supabase.co:5432/postgres"
    assert destructive_db_allowed(url, {"ALLOW_DESTRUCTIVE_TEST_DB": "1"})


def test_guard_refuses_remote_hostaddr():
    assert not destructive_db_allowed("postgresql://u:p@localhost/db?hostaddr=10.0.0.5", {})


def test_guard_allows_local_hostaddr():
    assert destructive_db_allowed("postgresql://u:p@localhost/db?hostaddr=127.0.0.1", {})


def test_guard_uses_pghost_when_url_has_no_host(monkeypatch):
    monkeypatch.setenv("PGHOST", "db.example.com")
    assert not destructive_db_allowed("postgresql:///postgres", {})
    monkeypatch.setenv("PGHOST", "localhost")
    assert destructive_db_allowed("postgresql:///postgres", {})
