"""Saved delivery addresses: normalising an app's saved-address list (engine, no database)."""
import contextlib
import logging
from types import SimpleNamespace

import pytest

import addresses
from server import fake_sites

RECIPE = {
    "endpoint": "https://www.example-app.test/api/user/addresses",
    "method": "GET",
    "query": {"v": "2"},
    "headers": {},
    "field_map": {
        "list_path": "data.addresses",
        "address_id": "id",
        "label": "annotation",
        "line": "address_line",
        "area": "area",
        "city": "city",
        "lat": "lat",
        "lon": "lng",
        "is_default": "is_default",
    },
}

LINE = "Flat 12, Lakeview Residency, 27th Main Road"
RESPONSE = {"data": {"addresses": [
    {"id": 101, "annotation": "HOME", "address_line": f"  {LINE}  ", "area": "HSR Layout", "city": "Bengaluru",
     "lat": "12.9116", "lng": "77.6474", "is_default": "true"},
    {"id": "w-7", "annotation": "Work", "address_line": "Tower B, 4th floor", "area": "Koramangala",
     "city": "Bengaluru", "lat": 12.9352, "lng": 77.6245, "is_default": False},
    # no coordinates, but an area and city: still placeable by name
    {"id": "o-1", "annotation": "", "address_line": "Near the temple", "area": "Indiranagar", "city": "Bengaluru",
     "lat": None, "lng": None},
    # no coordinates and nothing to place it by: dropped
    {"id": "x-1", "annotation": "Other", "address_line": "Somewhere", "area": "", "city": None},
    # impossible coordinates are treated as missing, and nothing else places it: dropped
    {"id": "x-2", "annotation": "Other", "address_line": "Nowhere", "lat": 0, "lng": 0},
    # no id: dropped
    {"annotation": "Home", "address_line": "No id", "area": "HSR Layout", "city": "Bengaluru", "lat": 12.9, "lng": 77.6},
    "not a dict",
]}}


def _page():
    return SimpleNamespace(url="https://www.example-app.test/")


def test_fetch_saved_normalises_and_drops_unplaceable(monkeypatch):
    seen = []

    def fake_fetch(page, req):
        seen.append(req)
        return RESPONSE

    monkeypatch.setattr(addresses, "fetch_in_page", fake_fetch)
    rows = addresses.fetch_saved("swiggy", _page(), RECIPE)
    assert seen[0]["method"] == "GET" and seen[0]["url"].startswith(RECIPE["endpoint"])
    assert rows == [
        {"address_id": "101", "label": "Home", "line": LINE, "area": "HSR Layout", "city": "Bengaluru",
         "lat": 12.9116, "lon": 77.6474, "is_default": True},
        {"address_id": "w-7", "label": "Work", "line": "Tower B, 4th floor", "area": "Koramangala",
         "city": "Bengaluru", "lat": 12.9352, "lon": 77.6245, "is_default": False},
        {"address_id": "o-1", "label": None, "line": "Near the temple", "area": "Indiranagar", "city": "Bengaluru",
         "lat": None, "lon": None, "is_default": False},
    ]


def test_fetch_saved_logs_counts_only(monkeypatch, caplog):
    monkeypatch.setattr(addresses, "fetch_in_page", lambda page, req: RESPONSE)
    caplog.set_level(logging.DEBUG)
    addresses.fetch_saved("swiggy", _page(), RECIPE)
    assert "3" in caplog.text
    for secret in ("Lakeview", "HSR", "Koramangala", "12.91", "77.64", "temple"):
        assert secret not in caplog.text


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_a_get_recipe_is_ever_replayed(monkeypatch, method):
    called = []
    monkeypatch.setattr(addresses, "fetch_in_page", lambda page, req: called.append(req) or RESPONSE)
    with pytest.raises(ValueError, match="GET"):
        addresses.fetch_saved("swiggy", _page(), RECIPE | {"method": method})
    assert called == []


def test_a_response_without_the_list_gives_no_rows(monkeypatch):
    monkeypatch.setattr(addresses, "fetch_in_page", lambda page, req: {"data": {}})
    assert addresses.fetch_saved("zepto", _page(), RECIPE) == []


def test_duplicate_ids_are_kept_once(monkeypatch):
    first = RESPONSE["data"]["addresses"][0]
    monkeypatch.setattr(addresses, "fetch_in_page",
                        lambda page, req: {"data": {"addresses": [first, dict(first, annotation="Work")]}})
    rows = addresses.fetch_saved("zepto", _page(), RECIPE)
    assert [r["address_id"] for r in rows] == ["101"]


def test_fetch_for_site_opens_the_home_page_read_only(monkeypatch):
    """The replay navigates only to the site's home page (never an address page) under the read-only guard."""
    opened, went = [], []

    @contextlib.contextmanager
    def fake_open(site, **k):
        opened.append((site, k))
        yield None, _page(), SimpleNamespace(login_mode=False), SimpleNamespace(items=[])

    monkeypatch.setattr(addresses, "open_site", fake_open)
    monkeypatch.setattr(addresses, "goto", lambda page, url: went.append(url))
    monkeypatch.setattr(addresses, "fetch_in_page", lambda page, req: RESPONSE)
    rows = addresses.fetch_for_site("zepto", RECIPE, profile_dir="p", location={}, proxy_server=None)
    assert len(rows) == 3
    assert opened[0][0] == "zepto" and opened[0][1].get("login_mode", False) is False
    assert opened[0][1].get("allowlist") in (None, [])
    assert went == ["https://www.zepto.com/"]


def test_distance_and_line_normalisation():
    assert addresses.distance_m((12.9116, 77.6474), (12.9116, 77.6474)) == 0
    assert 100 < addresses.distance_m((12.9116, 77.6474), (12.9126, 77.6474)) < 120  # ~111 m per 0.001 deg lat
    assert addresses.norm_line("Flat 12,  Lakeview Residency!") == addresses.norm_line("flat 12 lakeview residency")


@pytest.mark.parametrize("site", ["zepto", "blinkit", "swiggy", "instamart", "zomato"])
def test_fake_mode_emits_two_saved_addresses_per_site(site):
    rows = fake_sites.saved_addresses(site)
    assert len(rows) == 2
    assert sum(r["is_default"] for r in rows) == 1
    for r in rows:
        assert set(r) == {"address_id", "label", "line", "area", "city", "lat", "lon", "is_default"}
        assert r["lat"] is not None and r["lon"] is not None
    assert len({r["address_id"] for r in rows}) == 2


def test_fake_home_is_the_same_place_on_every_app():
    homes = [next(r for r in fake_sites.saved_addresses(s) if r["label"] == "Home")
             for s in ("zepto", "blinkit", "swiggy", "zomato")]
    assert len({h["address_id"] for h in homes}) == 4  # each app has its own id
    for h in homes[1:]:
        assert addresses.distance_m((homes[0]["lat"], homes[0]["lon"]), (h["lat"], h["lon"])) < 150
