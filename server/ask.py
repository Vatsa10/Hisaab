"""Ask: the insights analyst over the caller's own rows in Postgres.

Every query the model writes goes through scoped_sql, which runs it as role `authenticated` with the caller's JWT
claims (so row-level security shows only their rows), in a read-only transaction that is always rolled back, with a
statement timeout and a row cap. The model's SQL is untrusted (a user can ask it to run anything), so the plan is
inspected before execution: a SELECT can call functions, and set_config() could rewrite the claims or the role
mid-statement, and a few functions run SQL text at execution time where no static check could see it.
"""
import json
import re
import uuid
from decimal import Decimal

import psycopg

import agent
import insights

MAX_ROWS = 200
MAX_QUERY_CHARS = 10_000
STATEMENT_TIMEOUT = "5s"

SCHEMA = """PostgreSQL. You see only your own rows (amounts in INR; placed_at is ISO-8601 local time text, so
left(placed_at, 7) = 'YYYY-MM'). Tables:
orders(site text -- blinkit|zepto|swiggy|zomato, order_id text, sub_platform text -- 'instamart' for Swiggy Instamart,
       placed_at text, total_amount double precision -- what was paid, currency text,
       status text -- delivered|cancelled|refunded|failed|..., merchant text -- restaurant/store name, may be NULL)
order_items(site, order_id, idx int, name text, qty double precision,
            price double precision -- line total, NULL when the site gives none)
item_categories(name_norm text -- lower-cased item name, punctuation turned into spaces, category text)
Views (already exclude cancelled, refunded and failed orders -- prefer them for spend):
monthly_spend(month text 'YYYY-MM', site, sub_platform, orders bigint, amount numeric)
category_spend(month text 'YYYY-MM', category text -- 'food delivery' for Zomato/Swiggy food, else the item category
               or 'uncategorized', amount numeric)
merchant_spend(merchant, site, orders bigint, amount numeric, last_order_at text)
Rules: when using the tables directly, exclude status IN ('cancelled','refunded','failed') from spend. Zomato and
Swiggy orders (except sub_platform 'instamart') are food delivery. Join categories with
item_categories.name_norm = norm_name(order_items.name). Only plain SELECT/WITH queries are allowed; functions
named pg_*, set_config and the xml functions are refused."""

ANALYST = insights.analyst_prompt(SCHEMA)
RUN_SQL = insights.sql_tool(f"Run one read-only PostgreSQL SELECT query over your own orders; returns up to "
                            f"{MAX_ROWS} rows.")

FAKE_QUERY = "select site, sum(amount) as amount from monthly_spend group by site order by site"

# Functions the model may not call, whatever the arguments. set_config rewrites the role or the JWT claims RLS reads;
# query_to_xml & co., ts_stat and ts_rewrite execute SQL text at run time; pg_* covers sleeping, signalling backends,
# reading server files, advisory locks and NOTIFY; lo_* reads and writes server files; the rest touch sequences.
DENIED = re.compile(r"^(set_config|ts_stat|ts_rewrite|nextval|setval|currval|lastval)$|xml|^pg_|^lo_|^txid_"
                    r"|^dblink|^binary_upgrade_")
# Functions outside pg_catalog that the views and RLS policies use (read-only helpers).
ALLOWED_QUALIFIED = {("auth", "uid"), ("auth", "jwt"), ("auth", "role"), ("auth", "email")}
PUBLIC_ALLOWED = {"norm_name"}
REFUSED_NODES = {"ModifyTable", "LockRows"}

# One pass over deparsed SQL: string literals and double-quoted identifiers are consumed whole, so a quote inside one
# can never hide what follows it.
_TOKEN = re.compile(r"""(?P<str>'(?:[^']|'')*')|(?P<qid>"(?:[^"]|"")*")|(?P<id>[A-Za-z_][\w$]*)|(?P<ws>\s+)|(?P<other>.)""",
                    re.S)


def default_llm():
    return agent.llm()


def answer(conn, user_id: str, question: str, llm=None) -> str:
    """Run the insights analyst loop with run_sql bound to scoped_sql for this user."""
    return insights.ask(question, sql_runner=lambda query: scoped_sql(conn, user_id, query),
                        system=ANALYST, tool=RUN_SQL, client=llm or default_llm())


def fake_answer(conn, user_id: str) -> str:
    """ASK_FAKE (local end-to-end runs only): a deterministic answer from scoped_sql, no LLM."""
    result = scoped_sql(conn, user_id, FAKE_QUERY)
    if "error" in result:
        return f"Could not answer: {result['error']}"
    return markdown_table(result["columns"], result["rows"])


def markdown_table(columns: list[str], rows: list[tuple]) -> str:
    def cell(v) -> str:
        if isinstance(v, (Decimal, float)):
            v = int(v) if v == int(v) else f"{v:.2f}"
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def scoped_sql(conn, user_id: str, query: str) -> dict:
    """Run one read-only SELECT/WITH as `user_id`. Returns {columns, rows, truncated} or {error}.

    conn must be an idle autocommit connection: the query gets its own read-only transaction, which is always
    rolled back, so no role, claim or setting outlives the call."""
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("scoped_sql needs an idle autocommit connection: each query runs in its own read-only "
                         "transaction")
    uid = str(uuid.UUID(str(user_id)))
    body = (query or "").strip().rstrip(";").rstrip()
    if not insights.READ_ONLY.match(body) or ";" in body:
        return {"error": "only a single SELECT/WITH query is allowed"}
    if len(body) > MAX_QUERY_CHARS:
        return {"error": f"query is longer than {MAX_QUERY_CHARS} characters"}
    conn.execute("begin read only")
    try:
        conn.execute("set local role authenticated")
        conn.execute("select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)",
                     (uid, json.dumps({"sub": uid, "role": "authenticated"})))
        conn.execute(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.execute("set local search_path = pg_catalog, public")
        if not _in_scope(conn, uid):
            raise RuntimeError("scoped_sql could not enter the caller's read-only scope")
        try:
            plan = conn.execute("explain (verbose, format json) " + body).fetchone()[0]
            refusal = _refusal(conn, plan)
            if refusal:
                return {"error": refusal}
            # A server-side cursor: at most MAX_ROWS + 1 rows ever leave the database.
            with conn.cursor(name="ask_sql") as cur:
                cur.execute(body)
                columns = [d.name for d in cur.description or []]
                rows = cur.fetchmany(MAX_ROWS + 1)
            if not _in_scope(conn, uid):  # belt and braces: the statement must not have changed who it ran as
                return {"error": "the query changed its own role or claims"}
        except psycopg.Error as e:
            return {"error": str(e).strip() or type(e).__name__}
        return {"columns": columns, "rows": [tuple(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS}
    finally:
        conn.execute("rollback")


def _in_scope(conn, uid: str) -> bool:
    row = conn.execute("select current_user::text, current_setting('transaction_read_only'), "
                       "current_setting('request.jwt.claim.sub', true)").fetchone()
    return row == ("authenticated", "on", uid)


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _nodes(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _nodes(v)


def _ident(kind: str, text: str) -> str:
    return text[1:-1].replace('""', '"') if kind == "qid" else text


def function_calls(text: str) -> set[tuple[str | None, str]]:
    """(schema or None, name) of every `name(` / `schema.name(` in deparsed SQL, outside string literals."""
    toks = [(m.lastgroup, m.group()) for m in _TOKEN.finditer(text) if m.lastgroup != "ws"]
    calls = set()
    for i, (kind, tok) in enumerate(toks):
        if tok != "(" or i == 0 or toks[i - 1][0] not in ("id", "qid"):
            continue
        name = _ident(*toks[i - 1])
        schema = None
        if i >= 3 and toks[i - 2][1] == "." and toks[i - 3][0] in ("id", "qid"):
            schema = _ident(*toks[i - 3])
        calls.add((schema, name))
    return calls


def _refusal(conn, plan) -> str | None:
    """Why the planned query may not run, or None. Works on EXPLAIN VERBOSE output, where views and inlined
    functions are expanded and names are normalised (U& escapes, quoting) by Postgres itself."""
    calls = set()
    for node in _nodes(plan):
        if node.get("Node Type") in REFUSED_NODES:
            return "only plain reads are allowed (no data changes or row locks)"
        if "Function Name" in node:
            calls.add((node.get("Schema"), node["Function Name"]))
    for s in _strings(plan):
        calls |= function_calls(s)
    unqualified = set()
    for schema, name in calls:
        if schema in (None, "pg_catalog"):
            if DENIED.search(name.lower()):
                return f"function {name}() is not allowed"
            if schema is None:
                unqualified.add(name)
        elif schema == "public":
            if name not in PUBLIC_ALLOWED:
                return f"function {schema}.{name}() is not allowed"
        elif (schema, name) not in ALLOWED_QUALIFIED:
            return f"function {schema}.{name}() is not allowed"
    if unqualified:  # with search_path pg_catalog, public an unqualified call may also be a public function
        public = {r[0] for r in conn.execute(
            "select distinct proname::text from pg_proc where pronamespace = 'public'::regnamespace "
            "and proname = any(%s)", (sorted(unqualified),))}
        if public - PUBLIC_ALLOWED:
            return f"function {sorted(public - PUBLIC_ALLOWED)[0]}() is not allowed"
    return None
