"""Categorise grocery item names into the shared Postgres item_categories cache."""
import logging
from typing import Callable

import categorize
import db
from categorize import CATEGORIES, PROMPT  # noqa: F401  (PROMPT: the shared classifier prompt)

log = logging.getLogger("categorize_pg")


def uncategorized_names(conn, limit: int = 200) -> list[str]:
    """Distinct grocery item names (blinkit, zepto, swiggy Instamart) not yet in item_categories."""
    rows = conn.execute(
        """SELECT DISTINCT i.name FROM order_items i JOIN orders o USING (user_id, site, order_id)
           WHERE o.site IN ('blinkit', 'zepto') OR (o.site = 'swiggy' AND o.sub_platform = 'instamart')
           ORDER BY i.name""").fetchall()
    cached = {r[0] for r in conn.execute("SELECT name_norm FROM item_categories")}
    out: dict[str, str] = {}
    for (name,) in rows:
        n = db.norm_name(name)
        if n and n not in cached:
            out.setdefault(n, name)
    return list(out.values())[:limit]


def llm_classify(names: list[str]) -> dict[str, str]:
    """The default classifier: Claude Haiku 5.5 through llm.py (structured output)."""
    return {r["name"]: r["category"] for r in categorize.classify(names)}


def categorize_new_items(conn, classify: Callable[[list[str]], dict[str, str]] | None = None,
                         batch: int = 50) -> int:
    classify = classify or llm_classify
    names = uncategorized_names(conn)
    written = 0
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        wanted = {db.norm_name(n) for n in chunk}
        result = classify(chunk)
        rows = {}
        for name, cat in result.items():
            n = db.norm_name(name)
            if n in wanted and cat in CATEGORIES:
                rows[n] = cat
        if rows:
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO item_categories (name_norm, category) VALUES (%s, %s) "
                    "ON CONFLICT (name_norm) DO UPDATE SET category = excluded.category", list(rows.items()))
        written += len(rows)
        log.info("categorized %d/%d", min(i + batch, len(names)), len(names))
    return written
