"""Month-wise splits and an LLM analyst that answers questions from the orders database (read-only SQL)."""
import csv
import json
import logging
import re
import sqlite3

import db
import llm
from report import REPORTS, SPENT

log = logging.getLogger("insights")

PLATFORM = "o.site || CASE WHEN o.sub_platform IS NOT NULL THEN '/' || o.sub_platform ELSE '' END"
FOOD = "(o.site = 'zomato' OR (o.site = 'swiggy' AND IFNULL(o.sub_platform, '') != 'instamart'))"


def monthly(con, last: int | None = None) -> tuple[list[str], list[tuple]]:
    """Month x platform spend pivot (cancelled/refunded excluded). Columns: month, <platforms...>, orders, total."""
    platforms = [r[0] for r in con.execute(f"SELECT DISTINCT {PLATFORM} FROM orders o ORDER BY 1")]
    cols = ", ".join(f"ROUND(SUM(CASE WHEN {PLATFORM} = ? THEN o.total_amount ELSE 0 END), 2)" for _ in platforms)
    rows = con.execute(
        f"""SELECT substr(o.placed_at, 1, 7) AS month, {cols}, COUNT(*), ROUND(SUM(o.total_amount), 2)
            FROM orders o WHERE {SPENT} GROUP BY month ORDER BY month DESC {'LIMIT ?' if last else ''}""",
        [*platforms, *([last] if last else [])]).fetchall()
    return ["month", *platforms, "orders", "total"], [tuple(r) for r in reversed(rows)]


def monthly_categories(con, last: int | None = None) -> tuple[list[str], list[tuple]]:
    """Month x category pivot. Items without prices get their order total split by quantity (as in report)."""
    con.create_function("norm", 1, db.norm_name, deterministic=True)
    item_amount = """COALESCE(i.price, o.total_amount * IFNULL(i.qty, 1) /
                     (SELECT SUM(IFNULL(j.qty, 1)) FROM order_items j WHERE j.site = i.site AND j.order_id = i.order_id))"""
    rows = con.execute(
        f"""SELECT substr(o.placed_at, 1, 7) AS month,
                   CASE WHEN {FOOD} THEN 'food delivery' ELSE IFNULL(c.category, 'uncategorized') END AS cat,
                   ROUND(SUM({item_amount}), 2)
            FROM order_items i JOIN orders o USING (site, order_id)
            LEFT JOIN item_categories c ON c.name_norm = norm(i.name)
            WHERE {SPENT} GROUP BY month, cat""").fetchall()
    cats = sorted({r[1] for r in rows})
    by_month: dict[str, dict] = {}
    for month, cat, amount in rows:
        by_month.setdefault(month, {})[cat] = amount
    months = sorted(by_month)[-last:] if last else sorted(by_month)
    return ["month", *cats], [(m, *(by_month[m].get(c, 0.0) for c in cats)) for m in months]


def print_table(title: str, header: list[str], rows: list[tuple]) -> None:
    widths = [max(len(str(h)), *(len(f"{r[i]:,.0f}" if isinstance(r[i], float) else str(r[i])) for r in rows))
              for i, h in enumerate(header)] if rows else [len(h) for h in header]
    fmt = lambda v, w: f"{v:>{w},.0f}" if isinstance(v, float) else f"{str(v):>{w}}"  # noqa: E731
    print(f"\n== {title}")
    print("  ".join(f"{h:>{w}}" for h, w in zip(header, widths, strict=True)))
    for r in rows:
        print("  ".join(fmt(v, w) for v, w in zip(r, widths, strict=True)))


def months(con, last: int | None = None):
    """Print both month-wise splits and export them to reports/monthly_*.csv."""
    REPORTS.mkdir(exist_ok=True)
    for name, (header, rows) in (("platform", monthly(con, last)), ("category", monthly_categories(con, last))):
        print_table(f"Monthly spend by {name}", header, rows)
        path = REPORTS / f"monthly_{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([header, *rows])
    print(f"\nCSV: {REPORTS / 'monthly_platform.csv'}, {REPORTS / 'monthly_category.csv'}")


# --- the analyst agent ---------------------------------------------------------

SCHEMA = """SQLite tables (amounts in INR; placed_at is ISO-8601 local time, so substr(placed_at,1,7) = 'YYYY-MM'):
orders(site TEXT -- blinkit|zepto|swiggy|zomato, order_id TEXT, sub_platform TEXT, placed_at TEXT,
       total_amount REAL -- what was paid, currency TEXT, status TEXT -- delivered|cancelled|refunded|failed|...,
       merchant TEXT -- restaurant/store name, may be NULL, raw_json TEXT -- avoid)
order_items(site, order_id, idx, name TEXT, qty REAL, price REAL -- line total, NULL when the site gives none)
item_categories(name_norm TEXT -- lower-cased item name with punctuation removed, category TEXT)
Rules: exclude status IN ('cancelled','refunded','failed') from spend. Zomato and Swiggy orders are food
delivery. Join categories with item_categories.name_norm = norm(order_items.name) (norm() is available)."""


def analyst_prompt(schema: str) -> str:
    """The analyst's system prompt over a given schema description (SQLite here, Postgres in server.ask)."""
    return f"""You are a personal-finance analyst for the user's own food and grocery delivery orders.
Answer from the database only: call run_sql (read-only SELECT queries) as often as needed, then answer.
Give month-wise splits as compact tables (month | amount | orders | change vs previous month), name the
months, platforms, restaurants and categories that drive the spend, and end with 2-3 concrete, numeric
observations or suggestions. Use ₹ and round to whole rupees. Never invent numbers.
{schema}"""


def sql_tool(description: str) -> dict:
    """The run_sql tool definition handed to the model (strict: its input always matches the schema)."""
    return llm.tool("run_sql", description, {"query": {"type": "string"}}, ["query"])


ANALYST = analyst_prompt(SCHEMA)
RUN_SQL = sql_tool("Run one read-only SQLite SELECT query; returns up to 200 rows.")

READ_ONLY = re.compile(r"^\s*(select|with)\b", re.I)


def run_sql(query: str) -> dict:
    """Read-only by construction: SELECT/WITH only, on a connection opened in read-only mode."""
    if not READ_ONLY.match(query) or ";" in query.strip().rstrip(";"):
        return {"error": "only a single SELECT/WITH query is allowed"}
    con = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=30)
    con.create_function("norm", 1, db.norm_name, deterministic=True)
    try:
        cur = con.execute(query)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(200)
        return {"columns": cols, "rows": rows, "truncated": len(rows) == 200}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        con.close()


# Tokens kept free under llm.INPUT_CAP for the closing "answer now" turn.
ANSWER_RESERVE = 2_000
MIN_RESULT_BYTES = 1_000  # below this much room a tool result is not worth sending: answer instead
RESULT_MAX_BYTES = 30_000
BUDGET_SPENT = "result omitted: the token budget for this question is spent. Answer with what you have."
ANSWER_NOW = "Answer now with what you have."


def _fit(content: str, room: int) -> str:
    """content cut to at most room UTF-8 bytes (an upper bound on its tokens), marked when trimmed."""
    data = content.encode("utf-8")
    if len(data) <= room:
        return content
    note = "\n[result trimmed to stay under the token cap]"
    return data[:max(room - len(note), 0)].decode("utf-8", errors="ignore") + note


def ask(question: str, max_calls: int = 12, *, sql_runner=None, system: str = ANALYST, tool: dict = RUN_SQL,
        client=None, sql_log_level: int = logging.INFO) -> str:
    """The analyst loop. By default it reads the local SQLite database (CLI); server.ask passes its own
    sql_runner (query -> result dict), system prompt, tool definition, LLM client and a quieter SQL log level.

    The history is append-only (thinking blocks go back untouched), so the prompt is kept under llm.INPUT_CAP by
    trimming each new tool result to the room left; once the room is spent the model is asked to answer."""
    sql_runner = sql_runner or run_sql
    client = client or llm.client()
    messages: list[dict] = [{"role": "user", "content": question}]
    spent = False
    for _ in range(max_calls):
        resp = llm.create("ask", effort="medium", max_tokens=16_000, system=system, messages=messages, tools=[tool],
                          client=client)
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            return llm.text(resp)
        room = llm.INPUT_CAP - llm.prompt_size(resp) - ANSWER_RESERVE
        per_call = room // len(calls)
        spent = per_call < MIN_RESULT_BYTES
        results = []
        for b in calls:
            if spent:
                content = BUDGET_SPENT
            else:
                query = b.input.get("query", "") if isinstance(b.input, dict) else ""
                result = sql_runner(query)
                log.log(sql_log_level, "sql: %s -> %s", query[:120].replace("\n", " "),
                        "error" if "error" in result else len(result["rows"]))
                content = _fit(json.dumps(result, ensure_ascii=False, default=str)[:RESULT_MAX_BYTES], per_call)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
        messages.append({"role": "user", "content": results})
        if spent:
            break
    messages.append({"role": "user", "content": ANSWER_NOW})
    resp = llm.create("ask", effort="medium", max_tokens=16_000, system=system, messages=messages, tools=[tool],
                      tool_choice={"type": "none"}, client=client)
    return llm.text(resp)


INSIGHTS_QUESTION = ("Give me a month-wise split of my spending: per month the total, per-platform amounts and "
                     "order counts, and the change versus the previous month. Then the top 5 restaurants/stores, "
                     "the category split, my most expensive months and orders, and what changed recently.")
