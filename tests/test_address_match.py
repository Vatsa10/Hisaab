"""The scored same-place matcher, against the vectors the web mirror also runs."""
import json
from pathlib import Path

import pytest

from server import address_match as m

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "address_pairs.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vectors(v):
    assert m.same_place(v["a"], v["b"]) is v["same"]
    assert m.same_place(v["b"], v["a"]) is v["same"]


def test_unit_key_forms():
    assert m.parse("Flat 1204, B Wing, Shanti Soc")["unit"] == "b1204"
    assert m.parse("B-1204 Shanti Society")["unit"] == "b1204"
    assert m.parse("flat 1204 tower b")["unit"] == "b1204"
    assert m.parse("Shanti Society, 390016")["pin"] == "390016"


def test_group_one_app_per_group_and_default_leads():
    a = {"site": "zepto", "address_id": "z", "line": "Shanti Society", "area": "Gorwa", "city": "Vadodara",
         "lat": 22.331, "lon": 73.151, "is_default": False}
    b = dict(a, site="swiggy", address_id="s", is_default=True)
    c = dict(a, address_id="z2")  # a second zepto row of the same place: never in the same group
    g = m.group([b, a, c])
    assert [[r["address_id"] for r in grp] for grp in g] == [["s", "z"], ["z2"]]
