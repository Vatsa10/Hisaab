"""The one place that maps a site to its Chrome profile and its stored site, derived from browser.SITES."""
import browser

# Sites whose Chrome profile is another site's (they share its login, so never run a browser at the same time).
SITE_PROFILE: dict[str, str] = {site: cfg["profile"] for site, cfg in browser.SITES.items() if cfg["profile"] != site}

# Sites a user connects directly (Instamart rides on the Swiggy connection).
CONNECTABLE = ("zepto", "blinkit", "swiggy", "zomato")


def profile_for(site: str) -> str:
    """The Chrome profile (and vault blob name) a site's jobs use."""
    return SITE_PROFILE.get(site, site)


def db_site(site: str) -> str:
    """The site its orders and connection are stored under."""
    return browser.SITES.get(site, {}).get("db_site", site)


def same_profile(site: str) -> tuple[str, ...]:
    """Every site sharing this site's profile, sorted (itself included)."""
    profile = profile_for(site)
    return tuple(sorted({s for s in browser.SITES if profile_for(s) == profile} | {site}))
