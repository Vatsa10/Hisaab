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
from pglast import ast, enums

import insights
import llm

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

# Cast targets: plain scalars only. Casting text to regclass/regproc/a row type would look things up in the catalog.
ALLOWED_CAST_TYPES = frozenset({
    "int2", "int4", "int8", "integer", "bigint", "smallint", "numeric", "decimal", "real", "float4", "float8",
    "text", "varchar", "char", "bpchar", "bool", "boolean", "date", "time", "timestamp", "timestamptz", "interval",
})
# Column names that are also function names, where `x.name` cannot become a dangerous call: name() is a cast to the
# `name` type that takes text, never a row.
NOTATION_SAFE = frozenset({"name"})
# Keyword "functions" without parentheses: only the date/time ones. current_user, session_user, current_role, user,
# current_schema and current_catalog would reveal role, schema and database names.
ALLOWED_SQL_VALUE_FUNCTIONS = frozenset(op.value for op in enums.SQLValueFunctionOp if op.name.startswith(
    ("SVFOP_CURRENT_DATE", "SVFOP_CURRENT_TIME", "SVFOP_LOCALTIME")))  # incl. CURRENT_TIMESTAMP, LOCALTIMESTAMP, _N

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
and norm_name. Casts only to plain types (::numeric, ::int, ::bigint, ::float8, ::text, ::date, ::timestamp,
::interval). Refer to columns as column or alias.column (aliases must not be function names such as sum or count);
never select a whole row (write o.* or list the columns)."""

ANALYST = insights.analyst_prompt(SCHEMA)
RUN_SQL = insights.sql_tool(f"Run one read-only PostgreSQL SELECT query over your own orders; returns up to "
                            f"{MAX_ROWS} rows.")

FAKE_QUERY = "select site, sum(amount) as amount from monthly_spend group by site order by site"

SCOPE_CHANGED = "the query was refused"  # generic on purpose: nothing from the database


def default_llm():
    return llm.client()


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


def _inferred_name(val) -> str | None:
    """The output column name Postgres gives an unaliased select-list entry, where it is certain (a column, a function
    call, or a cast of one of those); None otherwise. Under-approximating only makes the check stricter."""
    if isinstance(val, ast.ColumnRef) and isinstance(val.fields[-1], ast.String):
        return val.fields[-1].sval
    if isinstance(val, ast.FuncCall):
        return _names(val.funcname)[-1]
    if isinstance(val, ast.TypeCast):
        return _inferred_name(val.arg)
    return None


def _output_names(nodes) -> set[str]:
    """Every column name the query itself defines: select-list aliases and the names Postgres infers, CTE and
    FROM-alias column lists, and VALUES's column1..N."""
    names = set()
    for n in nodes:
        if isinstance(n, ast.SelectStmt):
            for t in n.targetList or ():
                name = t.name or _inferred_name(t.val)
                if name:
                    names.add(name)
            width = max((len(row) for row in n.valuesLists or ()), default=0)
            names.update(f"column{i}" for i in range(1, width + 1))
        elif isinstance(n, ast.CommonTableExpr):
            names.update(_names(n.aliascolnames))
        elif isinstance(n, ast.Alias):
            names.update(_names(n.colnames))
    return names


def _range_names(nodes) -> set[str]:
    """Names a FROM item is known by in the query: any node's alias (tables, subqueries, VALUES, JOINs, and the
    refused table functions), else the relation or CTE name."""
    names = set()
    for n in nodes:
        alias = getattr(n, "alias", None)
        if isinstance(alias, ast.Alias):
            names.add(alias.aliasname)
        elif isinstance(n, ast.RangeVar):
            names.add(n.relname)
        if isinstance(n, ast.CommonTableExpr):
            names.add(n.ctename)
    return names


def _stars_in_select_lists(nodes) -> set[int]:
    """ids of `*` / `t.*` ColumnRefs that are a whole select-list entry (expanded into columns, never a row value)."""
    return {id(t.val) for n in nodes if isinstance(n, ast.SelectStmt) for t in n.targetList or ()
            if isinstance(t.val, ast.ColumnRef) and isinstance(t.val.fields[-1], ast.A_Star)}


def _select_list_names(sel) -> set[str]:
    """The output column names of one SELECT (of its leftmost branch for UNION / INTERSECT / EXCEPT)."""
    while sel.op != enums.SetOperation.SETOP_NONE:
        sel = sel.larg
    return {t.name or _inferred_name(t.val) for t in sel.targetList or ()} - {None}


def _output_column_refs(nodes) -> set[int]:
    """ids of bare names in ORDER BY / GROUP BY that match their SELECT's output names. Postgres resolves those to
    the output column (ORDER BY prefers it; GROUP BY takes an input column first, never a whole row), so they are
    not whole-row references even when a FROM item has the same name."""
    ids = set()
    for n in nodes:
        if not isinstance(n, ast.SelectStmt):
            continue
        outputs = _select_list_names(n)
        items = [s.node for s in n.sortClause or ()] + list(n.groupClause or ())
        for item in items:
            if (isinstance(item, ast.ColumnRef) and len(item.fields) == 1 and isinstance(item.fields[0], ast.String)
                    and item.fields[0].sval in outputs):
                ids.add(id(item))
    return ids


def check_query(query: str, known_columns=frozenset()) -> tuple[str | None, set[str], set[str]]:
    """(refusal reason or None, CTE names, qualified field names) for one analyst query, from Postgres's own parse
    tree. known_columns are the real column names of ALLOWED_TABLES. The caller must still refuse a qualified field
    that is also a function name (scoped_sql does): Postgres reads `x.f` as f(x) when x has no column f."""
    try:
        stmts = pglast.parse_sql(query)
    except pglast.parser.ParseError as e:
        return f"syntax error: {e}", set(), set()
    if len(stmts) != 1:
        return "exactly one SELECT statement is allowed", set(), set()
    root = stmts[0].stmt
    if not isinstance(root, ast.SelectStmt):
        return "only a SELECT (optionally WITH ... SELECT) is allowed", set(), set()
    nodes = list(_walk(root))
    ctes = {n.ctename for n in nodes if isinstance(n, ast.CommonTableExpr)}
    known = set(known_columns) | _output_names(nodes)
    fields: set[str] = set()
    exempt = _stars_in_select_lists(nodes) | _output_column_refs(nodes)
    refusal = _check_nodes(nodes, ctes, known, _range_names(nodes), exempt, fields)
    return refusal, ctes, fields


def _check_nodes(nodes, ctes, known, ranges, exempt, fields) -> str | None:
    for n in nodes:
        kind = type(n).__name__
        if isinstance(n, ast.SelectStmt):
            if n.intoClause is not None:
                return "SELECT INTO is not allowed"
            if n.lockingClause:
                return "row locks (FOR UPDATE / FOR SHARE) are not allowed"
        if kind not in ALLOWED_NODES:
            return f"{kind} is not allowed in an analyst query"
        if isinstance(n, ast.FuncCall):
            parts = _names(n.funcname)
            name = parts[-1] if parts else ""
            ok = (len(parts) == 1 and name in ALLOWED_FUNCTIONS) or (
                len(parts) == 2 and parts[0] == "pg_catalog" and name in ALLOWED_FUNCTIONS - PUBLIC_FUNCTIONS)
            if not ok:
                return f"function {'.'.join(parts)}() is not allowed"
        elif isinstance(n, ast.RangeVar):
            qualified = n.catalogname or n.schemaname
            if n.relname in ALLOWED_TABLES and n.schemaname in (None, "public") and not n.catalogname:
                continue
            if not qualified and n.relname in ctes:
                continue
            return f"table {'.'.join(p for p in (n.catalogname, n.schemaname, n.relname) if p)} is not allowed"
        elif isinstance(n, ast.ColumnRef):
            refusal = _check_column_ref(n, known, ranges, exempt, fields)
            if refusal:
                return refusal
        elif isinstance(n, ast.SQLValueFunction) and n.op not in ALLOWED_SQL_VALUE_FUNCTIONS:
            return f"{enums.SQLValueFunctionOp(n.op).name.removeprefix('SVFOP_').lower()} is not allowed"
        elif isinstance(n, ast.A_Indirection):
            if not all(isinstance(i, ast.A_Indices) for i in n.indirection or ()):
                return "field selection like (x).f is not allowed; use alias.column"
        elif isinstance(n, ast.TypeCast):
            refusal = _check_cast(n.typeName)
            if refusal:
                return refusal
        elif isinstance(n, ast.ResTarget) and n.indirection:
            return "assignment targets are not allowed"
        elif isinstance(n, ast.JoinExpr) and n.join_using_alias is not None:
            return "JOIN ... USING (...) AS alias is not allowed"
        elif isinstance(n, ast.A_Expr) and len(_names(n.name)) > 1:
            return "schema-qualified operators are not allowed"
        elif isinstance(n, ast.CommonTableExpr) and not isinstance(n.ctequery, ast.SelectStmt):
            return "WITH may only contain SELECT queries"
    return None


def _check_column_ref(n, known, ranges, exempt, fields) -> str | None:
    """Postgres reads `x.f` as the call f(x) when x has no column f, and a bare FROM-item name as its whole row; so a
    qualified name must end in a real or query-defined column, and a bare name must not be a FROM item (except an
    ORDER BY / GROUP BY name that is one of its SELECT's output columns). exempt holds ids of the allowed `*` / `t.*`
    select-list entries and of those ORDER BY / GROUP BY names."""
    parts = n.fields
    if isinstance(parts[-1], ast.A_Star):
        if id(n) not in exempt:
            return "a whole row (t.* or *) may only be a select-list entry"
        return None if len(parts) <= 2 else "three-part names are not allowed; use alias.column"
    names = [p.sval for p in parts]
    if len(names) == 1:
        if names[0] in ranges and id(n) not in exempt:  # could be the whole row of that FROM item
            return (f"{names[0]!r} names a table or alias in this query, so it could mean the whole row; "
                    f"write alias.column")
        return None
    if len(names) > 2:
        return "three-part names are not allowed; use alias.column"
    if names[1] not in known:
        return f"{names[0]}.{names[1]} is not a known column"
    fields.add(names[1])
    return None


def _check_cast(tn) -> str | None:
    parts = _names(tn.names)
    plain = (parts and parts[-1] in ALLOWED_CAST_TYPES and len(parts) <= 2
             and (len(parts) == 1 or parts[0] == "pg_catalog") and not (tn.setof or tn.pct_type or tn.arrayBounds))
    if not plain:
        return f"cast to {'.'.join(parts) or '?'} is not allowed; cast to a plain number, text, date or time type"
    return None


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
    conn.execute("begin read only")
    try:
        _enter_scope(conn, uid)
        refusal, ctes, fields = check_query(body, _known_columns(conn))
        if refusal:
            return {"error": refusal}
        # `x.f` is the call f(x) when x has no column f: no qualified field may share a name with a function.
        callable_fields = {r[0] for r in conn.execute(
            "select distinct proname::text from pg_proc where pronamespace in "
            "('pg_catalog'::regnamespace, 'public'::regnamespace) and proname = any(%s)", (sorted(fields),))}
        callable_fields -= NOTATION_SAFE
        if callable_fields:
            return {"error": f"{sorted(callable_fields)[0]!r} is also a function name; give the column another alias"}
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


def _known_columns(conn) -> set[str]:
    """Real column names of the allowlisted tables and views, read from the catalog (one small query per call, so a
    migration that adds a column needs no restart)."""
    return {r[0] for r in conn.execute(
        "select attname::text from pg_attribute where attrelid = any(%s::regclass[]) and attnum > 0 "
        "and not attisdropped", (sorted(ALLOWED_TABLES),))}


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
