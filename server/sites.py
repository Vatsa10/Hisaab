"""Site/profile grouping (minimal; Plan 7 Task 3 owns the full module). Derived from browser.SITES."""
import browser

SITE_PROFILE: dict[str, str] = {s: c["profile"] for s, c in browser.SITES.items() if c["profile"] != s}


def profile_for(site: str) -> str:
    return SITE_PROFILE.get(site, site)


def db_site(site: str) -> str:
    return browser.SITES.get(site, {}).get("db_site", site)


def same_profile(site: str) -> tuple[str, ...]:
    p = profile_for(site)
    return tuple(sorted(s for s in browser.SITES if profile_for(s) == p) or [site])
