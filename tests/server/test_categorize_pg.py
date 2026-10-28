import math

import db
from helpers import A, order
from server import categorize_pg, store


def _seed(pg):
    store.upsert_orders(pg, A, "zepto", [order("z1", items=[{"name": "Amul Milk 500ml!"}, {"name": "Lays Chips"}])])
    store.upsert_orders(pg, A, "blinkit", [order("b1", items=[{"name": "amul  milk 500ml"}])])
    store.upsert_orders(pg, A, "swiggy", [order("s1", items=[{"name": "Biryani"}])])
    store.upsert_orders(pg, A, "swiggy", [order("s2", items=[{"name": "Soap"}])], sub_platform="instamart")
    store.upsert_orders(pg, A, "zomato", [order("m1", items=[{"name": "Paneer Tikka"}])])


def _classify(names):
    return {n: "dairy" for n in names}


def test_only_grocery_items_are_categorized(pg):
    _seed(pg)
    seen = []

    def classify(names):
        seen.extend(names)
        return _classify(names)

    categorize_pg.categorize_new_items(pg, classify)
    assert sorted(db.norm_name(n) for n in seen) == ["amul milk 500ml", "lays chips", "soap"]
    rows = {r[0] for r in pg.execute("select name_norm from item_categories")}
    assert rows == {"amul milk 500ml", "lays chips", "soap"}


def test_writes_normalized_names_once(pg):
    _seed(pg)
    assert categorize_pg.categorize_new_items(pg, _classify) == 3
    assert categorize_pg.uncategorized_names(pg) == []
    assert categorize_pg.categorize_new_items(pg, _classify) == 0


def test_unknown_category_is_stored_as_other(pg):
    _seed(pg)

    def classify(names):
        return {n: ("bogus" if "soap" in n.lower() else "snacks") for n in names}

    assert categorize_pg.categorize_new_items(pg, classify) == 3
    assert pg.execute("select count(*) from item_categories where category='bogus'").fetchone()[0] == 0
    assert pg.execute("select category from item_categories where name_norm='soap'").fetchone()[0] == "other"


def test_unknown_category_is_ignored(pg):
    """Kept under its old name: an invalid category never reaches the table as-is."""
    _seed(pg)
    categorize_pg.categorize_new_items(pg, lambda names: {n: "bogus" for n in names})
    assert pg.execute("select count(*) from item_categories where category='bogus'").fetchone()[0] == 0


def test_omitted_names_are_not_resent(pg):
    _seed(pg)
    calls = []

    def classify(names):
        calls.append(list(names))
        return {n: "dairy" for n in names if "soap" not in n.lower()}

    assert categorize_pg.categorize_new_items(pg, classify) == 3
    assert pg.execute("select category from item_categories where name_norm='soap'").fetchone()[0] == "other"
    assert categorize_pg.uncategorized_names(pg) == []
    assert categorize_pg.categorize_new_items(pg, classify) == 0
    assert len(calls) == 1


def test_batches_by_size(pg):
    store.upsert_orders(pg, A, "zepto", [order("z1", items=[{"name": f"item {i}"} for i in range(7)])])
    calls = []

    def classify(names):
        calls.append(len(names))
        return _classify(names)

    assert categorize_pg.categorize_new_items(pg, classify, batch=3) == 7
    assert len(calls) == math.ceil(7 / 3)
