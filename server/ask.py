"""Ask: the insights analyst over the caller's own rows in Postgres.

The model's SQL is untrusted: a user can ask it to run anything. scoped_sql layers four controls:
1. An allowlist over the parse tree (pglast, Postgres's own parser): one SELECT, only known node types, only analyst
   functions (so never set_config(), which could rewrite the role or the JWT claims RLS reads), only the order tables
   and spend views (or CTEs defined in the query), no INTO and no row locks.
2. The connection's session user is expected to be the dedicated `ask_reader` login (see migration 0006 and
   server/api.py, which refuses a superuser or RLS-bypassing session): it can only become `authenticated`.
3. The query runs as role `authenticated` with the caller's JWT claims, so row-level security shows only their
   rows, in a read-only transaction with a statement timeout, always rolled back; at most MAX_ROWS rows come back.
4. The role and claims are checked again after the query; database error text is returned only for errors raised
   before execution (parse / plan), never for errors raised while the query ran.
"""
import json
import logging
import uuid
from decimal import Decimal

import pglast
import psycopg
from pglast import ast

import agent
import insights

log = logging.getLogger("server.ask")

MAX_ROWS = 200
MAX_QUERY_CHARS = 10_000
STATEMENT_TIMEOUT = "5s"

ALLOWED_TABLES = frozenset({"orders", "order_items", "item_categories", "monthly_spend", "category_spend",
                            "merchant_spend"})

ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "sum", "count", "avg", "min", "max", "string_agg", "array_agg", "bool_and", "bool_or",
    # date / time (timezone is what `x AT TIME ZONE 'Asia/Kolkata'` parses to)
    "date_trunc", "date_part", "extract", "to_char", "to_date", "to_timestamp", "now", "age", "make_date",
    "justify_days", "justify_hours", "justify_interval", "timezone",
    # strings
    "lower", "upper", "trim", "btrim", "ltrim", "rtrim", "length", "substr", "substring", "replace", "split_part",
    "concat", "concat_ws", "left", "right", "position", "strpos", "initcap", "regexp_replace", "regexp_match",
    # math
    "round", "floor", "ceil", "ceiling", "abs", "greatest", "least", "coalesce", "nullif", "div", "mod", "power",
    "sqrt",
    # window and ordered-set
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "ntile", "percent_rank",
    "cume_dist", "percentile_cont", "percentile_disc",
    # ours (migration 0005)
    "norm_name",
})
PUBLIC_FUNCTIONS = frozenset({"norm_name"})  # called unqualified only; never pg_catalog.norm_name

# Parse-tree node types an analyst SELECT needs. Anything else (XML, JSON constructors, ROWS FROM, TABLESAMPLE,
# parameters, named arguments, any non-SELECT statement...) is refused.
ALLOWED_NODES = frozenset({
    "SelectStmt", "ResTarget", "ColumnRef", "A_Const", "A_Star", "A_Expr", "BoolExpr", "FuncCall", "RangeVar",
    "JoinExpr", "RangeSubselect", "SubLink", "WithClause", "CommonTableExpr", "SortBy", "TypeCast", "TypeName",
    "CaseExpr", "CaseWhen", "CoalesceExpr", "MinMaxExpr", "NullTest", "BooleanTest", "WindowDef", "GroupingSet",
    "SQLValueFunction", "A_ArrayExpr", "A_Indices", "A_Indirection", "RowExpr", "CollateClause", "Alias",
    "String", "Integer", "Float", "Boolean",
})
REFUSED_PLAN_NODES = {"ModifyTable", "LockRows"}  # backstop behind the parse-tree check

SCHEMA = """PostgreSQL. You see only your own rows (amounts in INR; placed_at is ISO-8601 local time text, so
substr(placed_at, 1, 7) = 'YYYY-MM'). Tables:
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
item_categories.name_norm = norm_name(order_items.name).
Only one plain SELECT (WITH allowed) over these tables and views. Functions allowed: aggregates (sum, count, avg,
min, max, string_agg, array_agg, bool_and, bool_or), date_trunc, date_part, extract, to_char, to_date, to_timestamp,
now, current_date, age, make_date, AT TIME ZONE, string functions (lower, upper, trim, length, substr, substring,
replace, split_part, concat, left, right, position, strpos, initcap, regexp_replace, regexp_match), math (round,
floor, ceil, abs, greatest, least, coalesce, nullif, div, mod, power, sqrt), window functions (row_number, rank,
dense_rank, lag, lead, first_value, last_value, ntile, percent_rank, cume_dist, percentile_cont, percentile_disc)
and norm_name. Casts (::numeric, ::date, ::int) are fine."""

ANALYST = insights.analyst_prompt(SCHEMA)
RUN_SQL = insights.sql_tool(f"Run one read-only PostgreSQL SELECT query over your own orders; returns up to "
                            f"{MAX_ROWS} rows.")

FAKE_QUERY = "select site, sum(amount) as amount from monthly_spend group by site order by site"

SCOPE_CHANGED = "the query was refused"  # generic on purpose: nothing from the database


def default_llm():
    return agent.llm()


def answer(conn, user_id: str, question: str, llm=None) -> str:
    """Run the insights analyst loop with run_sql bound to scoped_sql for this user."""
    return insights.ask(question, sql_runner=lambda query: scoped_sql(conn, user_id, query),
                        system=ANALYST, tool=RUN_SQL, client=llm or default_llm(), sql_log_level=logging.DEBUG)


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


def reader_problem(conn) -> str | None:
    """Why this connection must not run Ask SQL, or None. Its session user must not be, or belong to, a superuser
    or a role that bypasses row-level security (it should be ask_reader, which can only become authenticated)."""
    bad = conn.execute(
        "select exists (select 1 from pg_roles r where (r.rolsuper or r.rolbypassrls)"
        " and pg_has_role(session_user, r.oid, 'MEMBER'))").fetchone()[0]
    if bad:
        return "the Ask database role is a superuser or bypasses row-level security; use the ask_reader login"
    if not conn.execute("select pg_has_role(session_user, 'authenticated', 'MEMBER')").fetchone()[0]:
        return "the Ask database role cannot become authenticated"
    return None


# --- the parse-tree allowlist --------------------------------------------------------------------------------

def _walk(node):
    if isinstance(node, ast.Node):
        yield node
        for field in node:
            yield from _walk(getattr(node, field))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _names(parts) -> list[str]:
    return [p.sval for p in parts or ()]


def check_query(query: str) -> tuple[str | None, set[str]]:
    """(refusal reason or None, CTE names) for one analyst query, from Postgres's own parse tree."""
    try:
        stmts = pglast.parse_sql(query)
    except pglast.parser.ParseError as e:
        return f"syntax error: {e}", set()
    if len(stmts) != 1:
        return "exactly one SELECT statement is allowed", set()
    root = stmts[0].stmt
    if not isinstance(root, ast.SelectStmt):
        return "only a SELECT (optionally WITH ... SELECT) is allowed", set()
    nodes = list(_walk(root))
    ctes = {n.ctename for n in nodes if isinstance(n, ast.CommonTableExpr)}
    for n in nodes:
        kind = type(n).__name__
        if isinstance(n, ast.SelectStmt):
            if n.intoClause is not None:
                return "SELECT INTO is not allowed", ctes
            if n.lockingClause:
                return "row locks (FOR UPDATE / FOR SHARE) are not allowed", ctes
        if kind not in ALLOWED_NODES:
            return f"{kind} is not allowed in an analyst query", ctes
        if isinstance(n, ast.FuncCall):
            parts = _names(n.funcname)
            name = parts[-1] if parts else ""
            ok = (len(parts) == 1 and name in ALLOWED_FUNCTIONS) or (
                len(parts) == 2 and parts[0] == "pg_catalog" and name in ALLOWED_FUNCTIONS - PUBLIC_FUNCTIONS)
            if not ok:
                return f"function {'.'.join(parts)}() is not allowed", ctes
        elif isinstance(n, ast.RangeVar):
            qualified = n.catalogname or n.schemaname
            if n.relname in ALLOWED_TABLES and n.schemaname in (None, "public") and not n.catalogname:
                continue
            if not qualified and n.relname in ctes:
                continue
            return f"table {'.'.join(p for p in (n.catalogname, n.schemaname, n.relname) if p)} is not allowed", ctes
        elif isinstance(n, ast.A_Expr) and len(_names(n.name)) > 1:
            return "schema-qualified operators are not allowed", ctes
        elif isinstance(n, ast.CommonTableExpr) and not isinstance(n.ctequery, ast.SelectStmt):
            return "WITH may only contain SELECT queries", ctes
    return None, ctes


# --- running a query ---------------------------------------------------------------------------------------

def scoped_sql(conn, user_id: str, query: str) -> dict:
    """Run one read-only analyst SELECT as `user_id`. Returns {columns, rows, truncated} or {error}.

    conn must be an idle autocommit connection: the query gets its own read-only transaction, which is always
    rolled back, so no role, claim or setting outlives the call."""
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("scoped_sql needs an idle autocommit connection: each query runs in its own read-only "
                         "transaction")
    uid = str(uuid.UUID(str(user_id)))
    body = (query or "").strip()
    if len(body) > MAX_QUERY_CHARS:
        return {"error": f"query is longer than {MAX_QUERY_CHARS} characters"}
    refusal, ctes = check_query(body)
    if refusal:
        return {"error": refusal}
    conn.execute("begin read only")
    try:
        _enter_scope(conn, uid)
        # A CTE name that is also a real relation would reach that relation wherever the CTE is out of scope.
        shadowing = [r[0] for r in conn.execute(
            "select n from unnest(%s::text[]) n where to_regclass(quote_ident(n)) is not null", (sorted(ctes),))]
        shadowing = [n for n in shadowing if n not in ALLOWED_TABLES]
        if shadowing:
            return {"error": f"rename the CTE {shadowing[0]!r}: it shadows a database table"}
        try:  # planning only: nothing has executed, so the database's message is safe to return
            plan = conn.execute("explain (verbose, format json) " + body).fetchone()[0]
        except psycopg.Error as e:
            return {"error": _plan_error(e)}
        if any(n.get("Node Type") in REFUSED_PLAN_NODES for n in _plan_nodes(plan)):
            return {"error": "only plain reads are allowed (no data changes or row locks)"}
        try:
            # A server-side cursor: at most MAX_ROWS + 1 rows ever leave the database.
            with conn.cursor(name="ask_sql") as cur:
                cur.execute(body)
                columns = [d.name for d in cur.description or []]
                rows = cur.fetchmany(MAX_ROWS + 1)
        except psycopg.Error as e:
            # Raised while the query ran: its text may carry values the query computed, so only the error class.
            return {"error": f"the query failed while running ({type(e).__name__})"}
        if not _in_scope(conn, uid):  # the statement must not have changed who it ran as
            return {"error": SCOPE_CHANGED}
        return {"columns": columns, "rows": [tuple(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS}
    finally:
        conn.execute("rollback")


def _enter_scope(conn, uid: str) -> None:
    """Inside an open read-only transaction: become `authenticated` with uid's claims, a statement timeout and a
    fixed search path. Every setting is LOCAL, so the rollback undoes it."""
    conn.execute("set local role authenticated")
    conn.execute("select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)",
                 (uid, json.dumps({"sub": uid, "role": "authenticated"})))
    conn.execute(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'")
    conn.execute("set local search_path = pg_catalog, public")
    if not _in_scope(conn, uid):
        raise RuntimeError("scoped_sql could not enter the caller's read-only scope")


def _in_scope(conn, uid: str) -> bool:
    row = conn.execute("select current_user::text, current_setting('transaction_read_only'), "
                       "current_setting('request.jwt.claim.sub', true), current_setting('request.jwt.claims', true)"
                       ).fetchone()
    try:
        claims = json.loads(row[3] or "null")
    except ValueError:
        return False
    return row[:3] == ("authenticated", "on", uid) and claims == {"sub": uid, "role": "authenticated"}


def _plan_error(e: psycopg.Error) -> str:
    msg = (getattr(e.diag, "message_primary", None) or type(e).__name__).strip()
    return msg[:300]


def _plan_nodes(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _plan_nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _plan_nodes(v)
