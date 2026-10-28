"""LLM item categorization, cached by normalized item name."""
import json
import logging

import db
import llm

log = logging.getLogger("categorize")

CATEGORIES = ["groceries", "dairy", "snacks", "household", "personal care", "food delivery", "other"]
BATCH = 50

# Structured output (output_config.format): the reply is guaranteed to match this JSON schema.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name", "category"],
        "properties": {"name": {"type": "string"}, "category": {"type": "string", "enum": CATEGORIES}},
    }}},
}
PROMPT = ("Categorize Indian quick-commerce grocery order items. Echo each name exactly. "
          "Milk, curd, paneer, butter, cheese -> dairy. Ready-to-eat restaurant meals -> food delivery.")


def classify(names: list[str], client=None) -> list[dict]:
    """[{"name", "category"}] for a batch of item names (low effort: a classification)."""
    resp = llm.create("categorize", effort="low", max_tokens=8000, system=PROMPT, output_schema=SCHEMA,
                      messages=[{"role": "user", "content": json.dumps(names, ensure_ascii=False)}], client=client)
    return llm.json_reply(resp)["items"]


def uncategorized(con) -> list[str]:
    # Restaurant orders (Zomato, Swiggy food) are "food delivery" in the report; no LLM call needed.
    rows = con.execute(
        """SELECT DISTINCT i.name FROM order_items i JOIN orders o USING (site, order_id)
           WHERE NOT (o.site = 'zomato' OR (o.site = 'swiggy' AND IFNULL(o.sub_platform, '') != 'instamart'))""")
    cached = {r[0] for r in con.execute("SELECT name_norm FROM item_categories")}
    out: dict[str, str] = {}
    for (name,) in rows:
        n = db.norm_name(name)
        if n and n not in cached:
            out.setdefault(n, name)
    return list(out.values())


def categorize(con) -> int:
    names = uncategorized(con)
    if not names:
        print("Nothing to categorize.")
        return 0
    client = llm.client()
    done = 0
    for i in range(0, len(names), BATCH):
        batch = names[i:i + BATCH]
        result = classify(batch, client)
        with con:
            con.executemany("INSERT OR REPLACE INTO item_categories (name_norm, category) VALUES (?, ?)",
                            [(db.norm_name(r["name"]), r["category"]) for r in result])
        done += len(result)
        log.info("categorized %d/%d", min(i + BATCH, len(names)), len(names))
    print(f"Categorized {done} item names.")
    return done
