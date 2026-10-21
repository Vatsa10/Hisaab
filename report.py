"""Spend reports: per platform, category, month, top merchants. Printed and exported to CSV."""
import csv
from pathlib import Path

import db

REPORTS = Path(__file__).parent / "reports"
SPENT = "IFNULL(o.status, '') NOT IN ('cancelled', 'refunded', 'failed')"
PLATFORM = "o.site || CASE WHEN o.sub_platform IS NOT NULL THEN '/' || o.sub_platform ELSE '' END"

QUERIES = {
    "platform": f"""SELECT {PLATFORM} AS key, COUNT(*) AS orders, ROUND(SUM(o.total_amount), 2) AS amount
                    FROM orders o WHERE {SPENT} {{month}} GROUP BY key ORDER BY amount DESC""",
    "month": f"""SELECT substr(o.placed_at, 1, 7) AS key, COUNT(*) AS orders, ROUND(SUM(o.total_amount), 2) AS amount
                 FROM orders o WHERE {SPENT} {{month}} GROUP BY key ORDER BY key""",
    "merchant": f"""SELECT IFNULL(o.merchant, '(unknown)') AS key, COUNT(*) AS orders,
                    ROUND(SUM(o.total_amount), 2) AS amount
                    FROM orders o WHERE {SPENT} {{month}} GROUP BY key ORDER BY amount DESC LIMIT 15""",
    # Category spend is item-level, so it may not add up to order totals (fees, discounts). Order lists that
    # carry no item prices (Zepto, Blinkit) get the order total split across their items by quantity.
    "category": f"""SELECT CASE WHEN o.site = 'zomato' OR (o.site = 'swiggy' AND IFNULL(o.sub_platform, '') != 'instamart')
                           THEN 'food delivery' ELSE IFNULL(c.category, 'uncategorized') END AS key,
                           COUNT(DISTINCT o.site || o.order_id) AS orders,
                           ROUND(SUM(COALESCE(i.price, o.total_amount * IFNULL(i.qty, 1) /
                                 (SELECT SUM(IFNULL(j.qty, 1)) FROM order_items j
                                  WHERE j.site = i.site AND j.order_id = i.order_id))), 2) AS amount
                    FROM order_items i JOIN orders o USING (site, order_id)
                    LEFT JOIN item_categories c ON c.name_norm = norm(i.name)
                    WHERE {SPENT} {{month}} GROUP BY key ORDER BY amount DESC""",
}


def build(con, month: str | None) -> dict[str, list[tuple]]:
    con.create_function("norm", 1, db.norm_name, deterministic=True)
    where = "AND substr(o.placed_at, 1, 7) = :month" if month else ""
    out = {}
    for section, sql in QUERIES.items():
        out[section] = [tuple(r) for r in con.execute(sql.format(month=where), {"month": month})]
    return out


def report(con, month: str | None) -> Path:
    data = build(con, month)
    for section, rows in data.items():
        note = " (items without prices: order total split by quantity)" if section == "category" else ""
        print(f"\n== Spend per {section} {'(' + month + ')' if month else ''}{note}")
        print(f"{'':<36}{'orders':>8}{'amount':>14}")
        for key, orders, amount in rows:
            print(f"{str(key)[:35]:<36}{orders:>8}{(amount or 0):>14,.2f}")
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"report_{month or 'all'}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "key", "orders", "amount"])
        for section, rows in data.items():
            w.writerows((section, *r) for r in rows)
    print(f"\nCSV: {path}")
    return path
