"""expense-agent CLI."""
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer

import db
import recipes
from browser import SITES, SessionExpired, load_settings, location_is_detailed, resolve_location, save_location

app = typer.Typer(add_completion=False, help="Read-only order-history expense agent.")
# Instamart is not synced on its own: on the web its "My Account" opens Swiggy's single order feed
# (/dapi/order/all), so a separate Instamart recipe can only duplicate (and mislabel) Swiggy orders.
ALL = ["blinkit", "zepto", "swiggy", "zomato"]
LOGIN_ALL = ALL
INSTAMART_NOTE = ("instamart: skipped - on swiggy.com Instamart has no separate order list; its 'My Account' opens "
                  "Swiggy's one order feed, which `sync swiggy` already stores.")
_phone: str | None = None


def _ask_phone() -> str:
    """Prompt once per run; kept in memory only."""
    global _phone
    while not _phone:
        import sync
        try:
            _phone = sync.normalize_phone(typer.prompt("Mobile number for login"))
        except ValueError as e:
            typer.secho(str(e), fg="red")
    return _phone


def _setup_logging() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/agent.log", encoding="utf-8")])
    for name in ("httpx", "httpx2", "typesafe_sdk"):  # request lines are noise
        logging.getLogger(name).setLevel(logging.WARNING)


def _ensure_location(force: bool = False) -> None:
    """Ask once for the delivery location; every browser session then shares it via geolocation."""
    if location_is_detailed(load_settings().get("location")) and not force:
        return
    typer.echo("Delivery address, e.g. 'Sunrise Heights, Alkapuri, Vadodara, Gujarat'\n"
               "(society/building, area, city, state) or 'lat,lon' copied from Google Maps.")
    while True:
        text = typer.prompt("Delivery address")
        if not location_is_detailed({"query": text}):
            typer.secho("Too vague: give society/building or area, city and state, separated by commas.", fg="red")
            continue
        try:
            loc = resolve_location(text)
        except Exception as e:
            typer.secho(str(e), fg="red")
            continue
        typer.echo(f"Found: {loc.get('display', text)} ({loc['lat']:.5f}, {loc['lon']:.5f})")
        if typer.confirm("Use this location?", default=True):
            save_location(loc)
            return


def _check_site(site: str) -> str:
    if site not in SITES:
        raise typer.BadParameter(f"unknown site {site!r}; choose from {', '.join(SITES)}")
    return site


def _expired(site: str, e: Exception) -> None:
    typer.secho(f"{site}: session expired ({e}). Run: python cli.py login {site}", fg="red")


@app.command()
def login(site: str = typer.Argument("all"),
          manual: bool = typer.Option(False, help="Skip auto-fill; log in by hand in the browser."),
          dry_run: bool = typer.Option(False, help="Test only: stop once the phone input is reached (no OTP sent).")):
    """Log in (phone + OTP per site). The profile keeps the session."""
    import sync
    _ensure_location()
    phone = "9000000000" if dry_run else None if manual else _ask_phone()  # dry run never types it
    failed = False
    for s in LOGIN_ALL if site == "all" else [_check_site(site)]:
        try:
            failed |= not sync.login(s, phone, dry_run)
        except SessionExpired as e:
            typer.secho(f"{s}: still not logged in ({e}). Try again.", fg="red")
            failed = True
        except Exception as e:  # browser closed, site down...: report it and move on to the next site
            logging.exception("%s login failed", s)
            typer.secho(f"{s}: login failed: {str(e).splitlines()[0]}", fg="red")
            failed = True
    raise typer.Exit(1 if failed else 0)


@app.command()
def learn(site: str):
    """Discover the order-history API with the LLM agent and save a recipe."""
    import sync
    _ensure_location()
    try:
        sync.learn(_check_site(site))
    except SessionExpired as e:
        _expired(site, e)
        raise typer.Exit(1) from None


def _sync_one(s: str, since: str | None, con) -> bool:
    """Sync one site in this process. Returns True on failure."""
    import sync
    try:
        try:
            sync.sync_site(s, since, con)
        except SessionExpired as e:
            if not sys.stdin.isatty() or not typer.confirm(f"{s}: not logged in ({e}). Log in now?", default=True):
                raise
            sync.login(s, _ask_phone())
            sync.sync_site(s, since, con)
    except SessionExpired as e:
        _expired(s, e)
        return True
    except Exception as e:
        logging.exception("%s sync failed", s)
        typer.secho(f"{s}: sync failed: {e}", fg="red")
        return True
    return False


def _sync_worker(s: str, since: str | None) -> tuple[str, bool, str]:
    """One site in its own process (own Chrome, own profile). No stdin: it can't prompt."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "sync", s, "--workers", "1"] + (["--since", since] if since else [])
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=Path(__file__).parent, env=os.environ | {"PYTHONIOENCODING": "utf-8"})
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip() and "DeprecationWarning" not in ln
             and "trace-deprecation" not in ln and " INFO " not in ln and "BLOCKED" not in ln]
    return s, r.returncode != 0, "\n".join(lines[-3:])


@app.command("sync")
def sync_cmd(site: str = typer.Argument("all"),
             since: str = typer.Option(None, help="Stop paginating before this date (YYYY-MM-DD)."),
             workers: int = typer.Option(4, help="Sites synced in parallel by `sync all` (one Chrome each).")):
    """Fetch orders for a site (or all) into SQLite."""
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise typer.BadParameter("--since must be YYYY-MM-DD")
    if site == "instamart":
        typer.echo(INSTAMART_NOTE)
        raise typer.Exit(0)
    _ensure_location()
    sites = ALL if site == "all" else [_check_site(site)]
    con, failed = db.connect(), False
    if site == "all" and workers > 1:
        # Sites with a recipe run in parallel workers. Sites that still need learning (allowlist prompts) or
        # that fail in a worker (e.g. logged out) run here afterwards, where they can ask you.
        ready = [s for s in sites if recipes.load(s)]
        typer.echo(f"syncing {', '.join(ready)} in parallel ({min(workers, len(ready))} workers)...")
        retry = [s for s in sites if s not in ready]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for s, bad, tail in pool.map(lambda x: _sync_worker(x, since), ready):
                typer.secho(tail or f"{s}: done", fg="red" if bad else None)
                if bad:
                    retry.append(s)
        sites = retry
    for s in sites:
        failed |= _sync_one(s, since, con)
    if site == "all":
        typer.echo(INSTAMART_NOTE)
    raise typer.Exit(1 if failed else 0)


@app.command()
def categorize():
    """Categorize uncategorized items with the LLM (cached by item name)."""
    import categorize as c
    c.categorize(db.connect())


@app.command()
def report(month: str = typer.Option(None, help="YYYY-MM")):
    """Print spend tables and export CSV."""
    import report as r
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise typer.BadParameter("--month must be YYYY-MM")
    r.report(db.connect(), month)


@app.command()
def months(last: int = typer.Option(None, help="Only the last N months.")):
    """Month-wise spend split by platform and by category (tables + CSV)."""
    import insights
    insights.months(db.connect(), last)


@app.command()
def ask(question: str):
    """Ask the analyst agent anything about your orders, e.g. "how much on pizza per month in 2026?"."""
    import insights
    typer.echo(insights.ask(question))


@app.command("insights")
def insights_cmd():
    """Month-wise breakdown with trends, top restaurants and categories, written by the analyst agent."""
    import insights
    typer.echo(insights.ask(insights.INSIGHTS_QUESTION))


@app.command()
def location():
    """Change the saved delivery location."""
    _ensure_location(force=True)


@app.command()
def reset(site: str):
    """Delete a site's recipe so the next sync/learn rediscovers it."""
    p = recipes.recipe_path(_check_site(site))
    if p.exists():
        p.unlink()
        print(f"removed {p}")


# A published recipe is shared by every user: it must not carry the account it was learned on.
IDENTITY_HEADERS = {"referer", "origin", "cookie"}
_ACCOUNT_SEGMENT = re.compile(r"/(?:users|user|profile)/[^/?#]+", re.I)
_LONG_DIGITS = re.compile(r"\d{8,}")


def _strip_identity_headers(obj):
    """A copy of obj with referer/origin/cookie (any case) removed from every "headers" mapping."""
    if isinstance(obj, dict):
        return {k: ({h: v for h, v in val.items() if h.lower() not in IDENTITY_HEADERS}
                    if k.lower() == "headers" and isinstance(val, dict) else _strip_identity_headers(val))
                for k, val in obj.items()}
    if isinstance(obj, list):
        return [_strip_identity_headers(v) for v in obj]
    return obj


def _url_path(s: str) -> str | None:
    """The path of an http(s) URL or of a "/..." path, else None."""
    from urllib.parse import urlsplit
    if re.match(r"https?://", s, re.I):
        try:
            return urlsplit(s).path
        except ValueError:
            return s
    return re.split(r"[?#]", s, maxsplit=1)[0] if s.startswith("/") else None


def _account_specific(obj, where: str = "$") -> list[str]:
    """JSON paths of string values that look tied to one account: a /users/<x>, /user/<x> or /profile/<x>
    segment, or a run of 8+ digits in a URL path."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _account_specific(v, f"{where}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _account_specific(v, f"{where}[{i}]")]
    if isinstance(obj, str):
        path = _url_path(obj)
        if _ACCOUNT_SEGMENT.search(obj if path is None else path) or (path and _LONG_DIGITS.search(path)):
            return [where]
    return []


@app.command("publish-recipe")
def publish_recipe(site: str,
                   force: bool = typer.Option(False, "--force",
                                              help="Publish even if values look account-specific (after review).")):
    """Admin: publish the locally learned recipes/<site>.json to Postgres (DATABASE_URL) for the cloud worker.
    Referer/Origin/Cookie headers are dropped; values that look tied to your account are refused unless --force."""
    import psycopg

    from server import store
    recipe = recipes.load(_check_site(site))
    if recipe is None:
        typer.secho(f"no learned recipe for {site}: run `python cli.py learn {site}` first", fg="red")
        raise typer.Exit(1)
    recipe = _strip_identity_headers(recipe)
    flagged = _account_specific(recipe)
    if flagged:
        typer.secho(f"{site} recipe has values that look specific to your account (shared recipes are readable "
                    "by every user):", fg="yellow" if force else "red")
        for p in flagged:
            typer.echo(f"  {p}")
        if not force:
            typer.echo(f"Edit recipes/{site}.json to use generic URLs, or re-run with --force after review.")
            raise typer.Exit(1)
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        typer.secho("DATABASE_URL is not set", fg="red")
        raise typer.Exit(1)
    with psycopg.connect(url, autocommit=True) as conn:
        version = store.put_recipe(conn, site, recipe)
    typer.echo(f"published {site} recipe as version {version}")


if __name__ == "__main__":
    _setup_logging()
    app()
