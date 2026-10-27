"""Is a Postgres URL a database on this machine? Guards dev-only modes (fake worker, fake Ask, destructive tests)."""
import os

from psycopg.conninfo import conninfo_to_dict

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def is_local_db(url: str, env=None) -> bool:
    """True only when every host libpq would connect to is localhost / 127.0.0.1 / ::1 / a local socket."""
    try:
        params = conninfo_to_dict(url)
    except Exception:
        return False
    # libpq connects to hostaddr when it is given, whatever host says; with no host at all it uses PGHOST.
    target = params.get("hostaddr") or params.get("host") or (env or {}).get("PGHOST") or os.environ.get("PGHOST") or ""
    return all(h.strip().strip("[]") in LOCAL_HOSTS or h.startswith("/") for h in target.split(","))
