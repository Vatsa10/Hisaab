"""One site-profile module: every site -> Chrome profile / stored site mapping comes from browser.SITES."""
from pathlib import Path

import browser
from server import jobs, sites


def test_profile_mapping_single_source():
    assert jobs.SITE_PROFILE is sites.SITE_PROFILE and sites.profile_for("instamart") == "swiggy"
    server_dir = Path(__file__).resolve().parents[2] / "server"
    hits = [p for p in server_dir.glob("*.py")
            if p.name != "sites.py" and '"instamart": "swiggy"' in p.read_text("utf-8")]
    assert hits == []


def test_site_profile_is_derived_from_browser_sites():
    assert sites.SITE_PROFILE == {s: c["profile"] for s, c in browser.SITES.items() if c["profile"] != s}
    assert all(sites.profile_for(s) == s for s in ("zepto", "blinkit", "swiggy", "zomato"))
    assert sites.profile_for("unknown") == "unknown"


def test_db_site():
    assert sites.db_site("instamart") == "swiggy"
    assert [sites.db_site(s) for s in ("zepto", "blinkit", "swiggy", "zomato")] == \
        ["zepto", "blinkit", "swiggy", "zomato"]


def test_same_profile_groups_swiggy_and_instamart():
    assert sites.same_profile("swiggy") == ("instamart", "swiggy")
    assert sites.same_profile("instamart") == ("instamart", "swiggy")
    assert sites.same_profile("zepto") == ("zepto",) and sites.same_profile("zomato") == ("zomato",)


def test_connectable():
    assert sites.CONNECTABLE == ("zepto", "blinkit", "swiggy", "zomato")
    assert all(s in browser.SITES for s in sites.CONNECTABLE)
