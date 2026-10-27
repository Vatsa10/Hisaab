# Plan 5 of 7 — Cheapest Cart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user types a grocery list (or starts from "what I usually buy"), and the product prices it live at their
saved address on Zepto, Blinkit and Instamart. It matches products carefully and asks the user when a match isn't
certain. It adds each app's fees, then shows the ranked "Cheapest cart" screen (mockup A) with the item table on
demand (mockup B). Search stays read-only and replays recipes the admin has learned. Results are stored per user under
RLS, and each check counts against the daily cart-check limit and the budget pause.

**Architecture:** `POST /cart/checks` consumes one cart check, stores a `cart_requests` row and enqueues one `search`
job per grocery app, all in one transaction. A worker runs each `search` job. It uses a fresh, empty Chrome profile
(never the user's), a pooled proxy session for the area, and the site's `search` recipe, either the in-page fast path
or the search-page capture. It matches products (`server/match.py`) and writes a `cart_results` row. The last job to
finish builds the summary (`server/fees.py`, `server/cart_rank.py`) into `cart_requests.summary`. The PWA follows
`cart_requests`/`cart_results` over Realtime and renders the summary's prepared strings. Admins learn `search` recipes
with the Plan 4 learn job (`kind: "search"`), and they maintain per-site fee rules in the admin view. `WORKER_FAKE=1`
fakes search and search-learning, so the whole flow runs locally end to end.

**Tech Stack:** as Plans 3–4 (Python 3.12, FastAPI, psycopg 3, Playwright-over-CDP Chrome, OpenAI SDK; Next.js 15 +
TypeScript + Tailwind 3.4, Vitest, Playwright e2e, local Supabase). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md` (binding: "Cheapest cart (new)", the
"Cart result is 'ranked app totals'" bullet under Screens, "Cart checks: up to 10 per user per day", "Learning is
supervised by the admin", "Location during cart filling", data model `cart_requests` / `cart_results`, Known risks).
Mockup: `.superpowers/brainstorm/24332-1793102961/content/cart-compare.html` (A = ranked totals, B = item table).

**Precondition:** Plans 3 and 4 are merged and `bash scripts/e2e_local.sh` is green. This plan uses:
- From Plan 4:
  - `limits.consume_cart_check / LimitExceeded / BudgetPaused / to_http`, `usage.record / today`,
    `budget.is_paused`, `config.SITE_NAMES`.
  - `recipe_admin.{make_approver, apply_learned, mark_drift, pause_site, is_paused, alert_admins via notify}`.
  - The worker's `learn` job, metering in `run_job`'s `finally`, `fake_sites.learn`, `scheduler.tick`,
    `server/admin_api.py` and `server/admin.py`.
  - In the PWA: `GET /me/limits` (`cart_checks_left`), `web/lib/me.ts`, and the admin page with `SitesPanel`.
- From Plan 3: `norm_name()` SQL, `web/components/ui/*`, `lib/api.ts`, `lib/format.ts`, and the Cart tab placeholder.

## Global Constraints

- Grocery apps priced: Zepto, Blinkit, Instamart only (`search.SEARCH_SITES = ("zepto", "blinkit", "instamart")`).
  Zomato and Swiggy food are never searched.
- "Usual" means items bought 3+ times in the last 90 days from grocery apps (Blinkit, Zepto, and Swiggy rows with
  `sub_platform='instamart'`). Cancelled, refunded and failed orders don't count.
- Cart checks:
  - Up to 10 per user per day (IST), enforced only through Plan 4's `limits.consume_cart_check`.
  - At 100 % of the budget, checks are refused with `BudgetPaused` (503).
  - A check that is refused (409/429/503) never consumes one.
- Strictly read-only toward delivery apps:
  - Search jobs never restore or save a user's Chrome profile, never log in and never click.
  - They only navigate, and call `GET` or an admin-approved read POST from the recipe's `read_post_allowlist`, under
    the existing read-only `Guard`. There is no cart-mode guard in this plan.
  - Nothing in Plan 5 creates a `fill_cart` job, exposes a fill route, or enables the "Fill cart" button.
  - Never anything matching checkout, payment, address, coupon or wallet.
- Search recipes are learned only by an admin `learn` job. A user's search only replays. A new read-POST entry is used
  only after the admin approves it in the admin view (Plan 4 R9).
- No guessing: an item without a confident match is shown as "Pick" and is never silently priced.
- New tunables live only in `server/config.py`, read from env and set explicitly in `docker-compose.yml`:
  `SPLIT_MIN_SAVING_INR=20`, `CART_MAX_ITEMS=30`, `CART_STALE_S=600`, `MATCH_JUDGE=1`, `MATCH_MODEL=""` (empty =
  `agent.model()`).
- Never log addresses, coordinates, list item texts or product names together with a user id. Searches log counts and
  site only.
- UI:
  - Follows Plan 3's Global Constraints: background `#f6f7f9`, white cards with radius 14px, accent `#0a7c55`, app dots
    Zepto `#5b21b6`, Blinkit `#f8cb46`, Instamart `#fc8019`, ₹ with Indian grouping, no horizontal scroll at 375px,
    safe-area padding.
  - Also follows `cart-compare.html` A (and B for the table).
  - Frontend tasks invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`
    before writing UI (Plan 3 ruling R3). The mockups and these constraints win any conflict.
  - After its code review, each frontend task gets a separate design review (impeccable critique/audit + ui-ux-pro-max +
    design-taste-frontend-v1, Playwright screenshots at 390×844 vs the mockups); findings enter its fix loop (Plan 3 R5).
  - Plan 3 design-review carry-overs: cheapest card `ring-2 ring-accent`; "n/a" uses `text-ink-muted` (not `#c0c4cc`).
- New tables:
  - RLS on. `authenticated` gets only `select` on own rows of `cart_requests` / `cart_results`, and nothing on
    `fee_rules`. `anon` gets nothing.
  - The PWA never uses the service-role key.
- Each parallel implementer uses its own test database `ea_tN` on `127.0.0.1:54329` (Plan 3 ruling R4), e.g.
  `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/ea_t2 pytest -q`.
- Every existing test and the Plan 3 and Plan 4 e2e suites keep passing.
- All work is committed on `main`. Never push, and no AI attribution in commit messages. No deploy.

## Review Focus

- The user writes "Amul Taaza 1L" and the app sells only 500 ml packs. It must price 2 packs, not one 500 ml pack at
  half the price. → T2 `test_buys_two_half_litre_packs_for_one_litre`.
- A one-word item ("milk") matches milk and Milk Bikis biscuits. It must become "Pick", never a guess. →
  T2 `test_ambiguous_dimensions_ask_user`.
- An app that is missing an item, failed, or doesn't deliver to the address must never be shown as "Cheapest" ahead
  of a complete cart. → T3 `test_incomplete_cart_never_outranks_complete`,
  `test_failed_and_unserviceable_cards_last_with_notes`.
- A double tap on "Compare prices", or a tap while a check is running: the second gets 409 and does not use one of the
  10 daily checks. → T6 `test_second_check_while_running_is_409_and_not_counted`.
- Every app fails (bot block, drift): the user is not charged a check, and the screen says so instead of spinning. →
  T6 `test_all_sites_failed_refunds_check`, T9 `test_stale_request_is_finalized_failed`.

## Spec rulings (made by this plan; record in the ledger)

- **R1 One `search` job per app.** Three jobs run in parallel when replicas allow.
  - Each locks its own profile name `search:<site>` (not the user's `swiggy` profile), so a running sync never
    delays a search.
  - Each runs in a fresh empty profile directory that is deleted after the job; the user's vault profile is never
    touched.
  - The proxy session is pooled per site and area: the first 12 hex characters of
    `sha256(f"search:{site}:{lat:.2f}:{lon:.2f}")`. The spec says search needs location only and can use a pooled
    proxy.
- **R2 Search recipes (kind `search`)** describe two things: where the query text and the user's lat/lon (or a
  store id resolved from lat/lon) go in the request, and how to map products.
  - A recipe without a location binding is rejected, because a price must belong to the user's address.
  - GET is preferred. A POST search API is accepted only through the admin-approved read POST allowlist, the same
    rule as orders. The brief said "GET"; real search APIs on these apps are often POST.
  - Search is learned on a fresh profile at the admin's saved address, with no delivery-app login. Plan 4's "connect
    the site on your own account first" gate therefore does not apply to `kind=search`.
- **R3 Fee rules are data in their own table `fee_rules(site, rules jsonb, verified, …)`, edited by the admin.** They
  are not part of the search recipe.
  - Fees change for business reasons, independently of the API shape. A re-learn must not wipe them.
  - The admin copies them from the app's public fee info, which is the supervised "learned from public fee info".
  - Rules: delivery tiers (by subtotal), a flat handling fee, a small-cart fee below a threshold, and surge
    (`possible` and an optional known fee).
  - The seed values are unverified placeholders. A total is labelled "fees estimated" while the rules are unverified,
    or while surge is possible: surge and rain fees appear only in a real cart, so they become exact in Plan 6's fill.
- **R4 Matching** is deterministic scoring.
  - Score = `0.6 × coverage + 0.15 × precision + 0.25 × size`. `SURE = 0.8`, `NEAR = 0.5`, candidates shown at
    ≥ `0.3`.
  - The "small model" (`MATCH_JUDGE=1`) is asked only about NEAR candidates when no SURE candidate is in stock: at most
    one call per item, with up to 3 names.
  - Accepted candidates that span two unit dimensions (ml vs g) always become "Pick".
  - A swap is automatic only within the same brand, the same dimension and a size of 0.8–1.25×, and it is always
    shown as a note.
  - Fake mode and tests run without the judge.
- **R5 Ranking.**
  - Complete carts rank before incomplete ones, ordered by `(unresolved items, total, site order)`.
  - Failed, unserviceable and unavailable apps come last, without a total.
  - Splitting is "worth it" only if it saves at least `SPLIT_MIN_SAVING_INR = 20` after both orders' fees. A second
    checkout and wait for less than that is not worth recommending.
- **R6 Limit accounting.**
  - A check consumes 1 when it starts, whatever the number of apps. The consume, the request and the jobs are created
    in one transaction, so a 409 rolls the consume back.
  - "Pick one" choices recompute without consuming. "Edit list" and "Check again" start a new check, which consumes.
  - A check where no app returned prices (`status = failed`) is refunded (`usage.cart_checks − 1` on its IST day).
- **R7 The summary is computed in Python** (`cart_rank.build_summary`), including every user-facing copy string, and
  stored in `cart_requests.summary`. The PWA renders the strings and does no price math. Realtime publishes
  `cart_requests` and `cart_results`.
- **R8 Stuck checks.** The scheduler tick finalizes `searching` requests older than `CART_STALE_S = 600`. Apps without
  a result are marked `failed` (`timeout`).
- **R9 Data minimisation.**
  - Requests store the area label and lat/lon (Plan 6 needs them for the location match), never the full address.
  - Each item keeps at most 5 candidates.
- **R10 The "Fill cart on <app>" button is rendered disabled with "Coming soon"** until Plan 6.
- **R11 Search drift** works like Plan 4 R8, with any admin user as the learner, since no account is needed.
  - The worker marks the `search` recipe unhealthy and enqueues an automatic `learn` with `kind: "search"`. New POST
    entries are denied in that job.
  - If there is no admin, or that learn fails or ends pending, the `search` recipe is paused and admins are emailed.
  - A paused or unhealthy search recipe makes that app "unavailable" for new checks.

---

## Wave 0 (alone, first)

### Task 1: Schema, RLS, fee seeds and search-job profiles

**Files:**
- Create: `supabase/migrations/0008_cheapest_cart.sql`, `tests/server/test_schema_plan5.py`
- Modify:
  - `tests/server/conftest.py`: `TABLES` += `cart_requests`, `cart_results`; new fixture `fee_rules_snapshot`, which
    saves all `fee_rules` rows and restores them after the test.
  - `server/jobs.py`, `tests/server/test_jobs.py`.

**Interfaces (Produces):**
- `cart_requests`:
  - Columns: `id uuid pk default gen_random_uuid()`, `user_id uuid not null`,
    `items jsonb not null check (jsonb_typeof(items)='array')`, `sites text[] not null`,
    `status text not null default 'searching' check in ('searching','done','failed')`, `area text not null`,
    `lat double precision not null`, `lon double precision not null`, `choices jsonb not null default '{}'`,
    `summary jsonb`, `error text`, `created_at timestamptz not null default now()`, `priced_at timestamptz`.
  - Index `cart_requests_user_created (user_id, created_at desc)`.
- `cart_results`:
  - Columns: `request_id uuid not null references cart_requests on delete cascade`, `user_id uuid not null`,
    `site text not null check in ('zepto','blinkit','instamart')`,
    `status text not null check in ('ok','failed','unserviceable','unavailable')`,
    `items jsonb not null default '[]'`, `subtotal numeric(10,2)`, `fees jsonb`, `total numeric(10,2)`,
    `fees_estimated boolean`, `error text`, `priced_at timestamptz not null default now()`.
  - Primary key `(request_id, site)`.
- `fee_rules`: `site text pk check in ('zepto','blinkit','instamart')`, `rules jsonb not null`,
  `version int not null default 1`, `verified boolean not null default false`, `source text not null default 'seed'`,
  `updated_at timestamptz not null default now()`, `updated_by uuid`.
- Seeds, unverified, with
  `"surge": {"possible": true}, "source_url": null, "note": "Seed values; verify against the app before launch."`:
  - zepto: `{"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 99, "fee": 0}], "handling": 15, "small_cart": {"below": 99, "fee": 25}}`
  - blinkit: `{"delivery": [{"min_subtotal": 0, "fee": 25}, {"min_subtotal": 199, "fee": 0}], "handling": 9, "small_cart": {"below": 99, "fee": 20}}`
  - instamart: `{"delivery": [{"min_subtotal": 0, "fee": 35}, {"min_subtotal": 199, "fee": 0}], "handling": 10, "small_cart": {"below": 99, "fee": 15}}`

  These match the mockup (Zepto handling ₹15, Blinkit free delivery above ₹199).
- RLS:
  - Enabled on all three tables. Policy `own_rows` (`user_id = auth.uid()`) for select on `cart_requests` and
    `cart_results`.
  - `revoke all … from anon, authenticated` on all three. `grant select on cart_requests, cart_results to
    authenticated`. `grant all` on all three to `service_role`.
  - Realtime: add `cart_requests` and `cart_results` to `supabase_realtime`, in 0001's guarded `do $$` block style.
- `jobs.enqueue(conn, user_id, type, site, payload=None, profile: str | None = None) -> str` (profile defaults to
  `SITE_PROFILE.get(site, site)` as today); `jobs.search_profile(site: str) -> str` returns `f"search:{site}"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cart_tables_rls_own_rows(pg, as_user): ...          # A's request + result visible to A, invisible to B
def test_users_cannot_write_cart_tables(pg, as_user): ...    # insert/update/delete as authenticated → InsufficientPrivilege
def test_fee_rules_hidden_from_users(pg, as_user): ...       # select → InsufficientPrivilege
def test_fee_rule_seeds(_applied):                           # read with a fresh connection before any truncate
    rows = dict(psycopg.connect(_applied).execute("select site, rules->>'handling' from fee_rules").fetchall())
    assert rows == {"zepto": "15", "blinkit": "9", "instamart": "10"}
def test_results_cascade_with_request(pg): ...
def test_result_status_and_site_checked(pg): ...             # site 'zomato' or status 'x' → CheckViolation
# tests/server/test_jobs.py
def test_search_jobs_lock_their_own_profile(pg):
    jobs.enqueue(pg, A, "sync", "swiggy"); jobs.claim(pg, "w1", ("sync",))
    jobs.enqueue(pg, A, "search", "instamart", {"request_id": "r"}, profile=jobs.search_profile("instamart"))
    assert jobs.claim(pg, "w2", ("search",)).site == "instamart"
```

- [ ] **Step 2: Run** `pytest tests/server/test_schema_plan5.py tests/server/test_jobs.py -v` → FAIL.
- [ ] **Step 3: Implement** the migration, the conftest changes and the `jobs.enqueue` profile parameter.
- [ ] **Step 4: Verify on real local Supabase:** `npx supabase db reset` applies 0001–0008 without error, and
  `select tablename from pg_publication_tables where pubname='supabase_realtime'` lists `cart_requests` and
  `cart_results`. Then `pytest -q` → green, with `TEST_DATABASE_URL` both set and unset.
- [ ] **Step 5: Commit** — `git commit -m "feat(db): cart requests, results and fee rules with RLS; search jobs lock their own profile"`

---

## Wave A (parallel after Task 1 is merged; disjoint files)

### Task 2: Product matching

**Files:** Create `server/match.py`, `tests/server/test_match.py` (pure; no database)

**Interfaces (Produces):**
- `Size(amount: float, unit: Literal["g", "ml", "pc"])`, a frozen dataclass. `parse_size(text: str) -> Size | None`:
  - kg→g ×1000, l/ltr/litre/liter→ml ×1000, g/gm/gms/gram(s), ml, pc/pcs/piece(s)/unit(s)/nos/"N pack"→pc.
  - Multipacks multiply: "6 x 300 ml", "300 ml x 6", "Pack of 2 (500 g each)".
- `tokens(text) -> frozenset[str]`:
  - Lower-case, non-word → space (as `db.norm_name`).
  - Drop size tokens, tokens of 1 character, and the stopwords
    `{"pack", "of", "the", "and", "fresh", "x", "pcs", "pc", "each"}`.
- `Query(idx: int, text: str, qty: int, tokens: frozenset[str], size: Size | None)`.
  `parse_query(idx, text, qty) -> Query`: a trailing `×N` / `xN` / `x N` / `* N` is stripped from the text and
  multiplies `qty`.
- `Product(product_id, name, brand, size: Size | None, size_label: str | None, price: float, mrp: float | None,
  in_stock: bool)`. `from_raw(d: dict) -> Product | None` takes `search.extract_products` rows; the size comes from
  `pack_size`, else from the name. Rows missing a price, or with price ≤ 0, give None.
- Constants: `SURE = 0.8`, `NEAR = 0.5`, `SHOW = 0.3`, `MAX_CANDIDATES = 5`, `JUDGE_TOP = 3`.
- `score(q: Query, p: Product) -> float`:
  - `0.6 × |Qt∩Pt|/|Qt| + 0.15 × |Qt∩Pt|/|Pt| + 0.25 × size_score`, where `Pt = tokens(name) ∪ tokens(brand)`.
  - `size_score`: 1 if the query has no size; same dimension and equal → 1; ratio within 0.5–2× → 0.6; otherwise
    0.2; different dimension → 0.
- `Judge = Callable[[str, list[str]], list[bool]]`. `llm_judge(model: str) -> Judge` makes one OpenAI
  chat-completions call with JSON output `{"same": [bool, …]}`: does each product name denote the same product the
  shopper asked for (brand and variant included)? The response passes through `metering.count_openai`. Any error →
  all False.
- `match_item(q: Query, products: list[Product], judge: Judge | None = None) -> dict` returns
  `{"idx", "status", "confidence", "product", "packs", "line_total", "swap_from", "candidates"}`:
  - `status` is one of `"matched" | "swapped" | "pick" | "out_of_stock" | "not_found"`; `confidence` is
    `"sure" | "confirmed" | None`.
  - `product` is the `Product` as a dict, or None.
  - `candidates` holds ≤5 dicts of `{product_id, name, brand, size_label, price, mrp, in_stock, packs, line_total, score}`
    with score ≥ `SHOW`, best score first.
  - The algorithm follows R4, in this order:

    1. Accepted = in-stock candidates with score ≥ SURE. If there are none, ask the judge (if given) about the top
       `JUDGE_TOP` in-stock candidates with score in [NEAR, SURE); the ones it confirms are accepted with confidence
       `confirmed`.
    2. If the accepted candidates span more than one unit dimension → `pick`.
    3. Packs:
       - With a query size: wanted = `size.amount × qty`; candidates must have the same dimension;
         `packs = ceil(wanted / p.size.amount − 1e-9)`; keep the candidate only if
         `packs × p.size.amount ≤ 1.5 × wanted`.
       - Without a query size: reference = the size of the best-scoring accepted candidate (ties → cheaper); keep
         candidates of the same dimension within 0.8–1.25× of the reference (or both size None); `packs = qty`.
    4. Choose the minimum `packs × price`; ties → higher score, then lower `product_id` → `matched`.
    5. If no accepted candidate is in stock but a SURE candidate exists out of stock: take the cheapest in-stock
       candidate with the same brand (case-insensitive), the same dimension and 0.8–1.25× its size → `swapped`, with
       `swap_from` = the out-of-stock name. Otherwise → `out_of_stock`.
    6. Otherwise, if any candidates ≥ SHOW exist → `pick`; else → `not_found`.

- [ ] **Step 1: Write the failing tests**

```python
def P(name, price=20.0, brand=None, size=None, in_stock=True, pid=None): ...   # helper building Product via from_raw
def test_parse_size_units_and_multipacks():
    assert match.parse_size("Amul Taaza Toned Milk 1 L") == Size(1000, "ml")
    assert match.parse_size("Aashirvaad Atta 5kg") == Size(5000, "g")
    assert match.parse_size("Coke 6 x 300 ml") == Size(1800, "ml")
    assert match.parse_size("Farm Eggs 6 pcs") == Size(6, "pc")
    assert match.parse_size("Pack of 2 (500 g each)") == Size(1000, "g")
    assert match.parse_size("Coriander Bunch") is None
def test_parse_query_reads_quantity_suffix():
    q = match.parse_query(0, "Amul Taaza 1L ×2", 1)
    assert (q.qty, q.size, q.tokens) == (2, Size(1000, "ml"), frozenset({"amul", "taaza"}))
def test_score_prefers_full_query_coverage():
    q = match.parse_query(0, "Lay's Classic", 1)
    assert match.score(q, P("Lay's Classic Salted Chips 52 g")) >= match.SURE
    assert match.NEAR <= match.score(q, P("Lay's Magic Masala Chips 52 g")) < match.SURE
def test_buys_two_half_litre_packs_for_one_litre():
    m = match.match_item(match.parse_query(0, "Amul Taaza Milk 1L", 1),
                         [P("Amul Taaza Toned Milk 500 ml", 29), P("Amul Taaza Toned Milk 1 L", 60)])
    assert (m["status"], m["product"]["name"], m["packs"], m["line_total"]) == ("matched", "Amul Taaza Toned Milk 500 ml", 2, 58.0)
def test_never_buys_more_than_one_and_a_half_times(): ...   # want 500 g, only 1 kg → not matched (pick)
def test_ambiguous_dimensions_ask_user():
    m = match.match_item(match.parse_query(0, "milk", 1), [P("Amul Taaza Toned Milk 1 L", 60), P("Milk Bikis Biscuits 200 g", 30)])
    assert m["status"] == "pick" and {c["name"] for c in m["candidates"]} == {"Amul Taaza Toned Milk 1 L", "Milk Bikis Biscuits 200 g"}
def test_no_size_picks_cheapest_equivalent(): ...           # "bread": 400 g ₹50 vs 400 g ₹45 vs 800 g ₹80 → ₹45, packs 1
def test_swap_same_brand_when_out_of_stock():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1),
                         [P("Lay's Classic Salted Chips 52 g", brand="Lay's", in_stock=False), P("Lay's Magic Masala Chips 52 g", brand="Lay's")])
    assert (m["status"], m["swap_from"], m["product"]["name"]) == ("swapped", "Lay's Classic Salted Chips 52 g", "Lay's Magic Masala Chips 52 g")
def test_out_of_stock_without_same_brand_substitute(): ...  # → "out_of_stock", product None, line_total None
def test_near_match_without_judge_asks_user(): ...          # → "pick"
def test_near_match_confirmed_by_judge(): ...               # judge → [True] → matched, confidence "confirmed"
def test_judge_called_once_with_top_three(): ...            # 5 near candidates → one call, 3 names
def test_judge_not_called_when_sure_in_stock(): ...
def test_not_found_and_candidate_cap(): ...                 # unrelated products → not_found; 9 weak → ≤5 candidates
def test_llm_judge_failure_is_all_false(monkeypatch): ...   # client raises → [False, False]
```

- [ ] **Step 2: Run** `pytest tests/server/test_match.py -v` → FAIL.
- [ ] **Step 3: Implement** `server/match.py`.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): product matching with size normalisation, swaps and pick-one"`

---

### Task 3: Fee rules and ranked totals

**Files:** Create `server/fees.py`, `server/cart_rank.py`, `tests/server/test_fees.py`, `tests/server/test_cart_rank.py` (pure)

**Interfaces (Produces):**
- `fees.validate(rules: dict) -> list[str]` returns error strings, empty when the rules are valid.
  - Allowed keys: `delivery, handling, small_cart, surge, source_url, note`.
  - `delivery`: a non-empty list whose first `min_subtotal` is 0 and which strictly ascends.
  - Every fee is a number from 0 to 500. `small_cart` is null or `{below: 0..2000, fee}`.
  - `surge` is `{possible: bool, fee?: 0..500}`. `source_url` and `note` are strings of ≤300 characters, or null.
- `fees.compute(rules, subtotal: float, verified: bool) -> dict` returns
  `{"delivery", "handling", "small_cart", "surge", "total", "estimated"}`:
  - Delivery comes from the tier with the largest `min_subtotal ≤ subtotal`. The small-cart fee applies when
    `subtotal < below`. `surge = rules.surge.fee or 0`.
  - `estimated = not verified or surge.possible`. A subtotal of 0 gives all zeros.
- `cart_rank.inr(x: float) -> str`: Indian grouping; integral values → `"₹1,23,456"`, otherwise two decimals
  `"₹49.50"`.
- `cart_rank.area_label(address: str | None) -> str`:
  - Split on commas and drop empty parts, a trailing `India`, and trailing 6-digit pincodes inside parts.
  - ≥3 parts → `f"{parts[-3]}, {parts[-2]}"`; 1–2 parts → `parts[0]`.
  - A `"lat,lon"`-only address or None → `"your address"`.
- `cart_rank.build_summary(request: dict, results: list[dict], fee_rules: dict[str, tuple[dict, bool]], split_min_saving: float) -> dict`:
  - Inputs: `request = {id, items: [{text, qty}], area, choices: {"<idx>": {"<site>": product_id}}}`;
    `results = [{site, status, items: [match_item dicts], priced_at, error}]`.
  - A choice replaces the cell with the chosen candidate, as status `chosen`.
  - Priced statuses are `matched`, `swapped` and `chosen`. Unresolved = out_of_stock + not_found + pick.
  - It returns **the summary shape that the API, the worker and the PWA all use:**

```text
{request_id, status: "done"|"failed", item_count, area, priced_at (ISO, latest result), best_site: str|null,
 cards: [{site, name (config.SITE_NAMES), rank, status: "ok"|"failed"|"unserviceable"|"unavailable", cheapest: bool,
          subtotal, fees: {delivery, handling, small_cart, surge, total}|null, fees_estimated: bool, total,
          total_label ("₹412" | "—" when nothing priced), delta_label ("+₹37" | "Same total" | null),
          fee_line ("Items ₹397 + delivery ₹0 + handling ₹15" [+ " + small-cart ₹X"] [+ " + surge ₹X"]) | null,
          fee_hint ("Free delivery above ₹199" when delivery > 0 and a 0-fee tier exists) | null,
          stock_line ("all 7 in stock" | " · "-joined "1 out of stock", "1 not found", "1 to pick") | null,
          swaps: ["Lay's Classic swapped for Lay's Magic Masala"], to_pick: int,
          note: "Couldn't price Instamart right now." | "Instamart doesn't deliver to this address." |
                "Instamart prices are unavailable right now." | null}],
 table: {columns: [ok sites in card order], rows: [{idx, label ("Amul Taaza 1L ×2" when qty > 1),
         cells: {site: {status, name, price_label ("₹136" | "n/a" | "Pick" | "Out of stock"), cheapest: bool}}}],
         totals: {site: total_label}},
 picks: [{idx, site, label, candidates: [{product_id, name, size_label, price_label, packs}]}],
 split: {worth_it: bool, sites: [a, b] | [], saving: float, line: str} | null}
```

- Ranking (R5):
  - ok cards are sorted by `(unresolved, total, SEARCH_SITES index)`; non-ok cards follow in `SEARCH_SITES` order.
  - `cheapest` and `best_site` go only to rank 1, and only if it priced ≥ 1 item.
  - `delta_label` is shown only when a card's unresolved count equals the best card's.
  - `status` is `"done"` if any card is ok, else `"failed"`.
  - "1 item" is singular.
- Split (only with ≥2 ok cards):
  - For each pair, every item goes to the cheaper priced cell (a tie goes to the better-ranked app).
  - A pair counts only if it covers every item the best single app priced and both sides get ≥1 item.
  - `split_total` = both sides' subtotals + `fees.compute` on each side; `saving = best.total − split_total`. The best
    pair is the one with the minimum `split_total`. Lines:
    - No valid pair → `"No item is cheaper on another app, so one order on {Best} is best."`
    - `saving ≥ split_min_saving` → `"Splitting between {A} and {B} saves {inr(saving)} even after the second order's fees."`
    - Otherwise, when the extra fees are > 0 → `"Splitting across apps saves {inr(item_saving)} on items but adds {inr(extra_fees)} in fees. Not worth it."`
    - Otherwise → `"Splitting across apps saves only {inr(saving)}. Not worth a second order."`

- [ ] **Step 1: Write the failing tests**

```python
RULES = {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 199, "fee": 0}], "handling": 15,
         "small_cart": {"below": 99, "fee": 25}, "surge": {"possible": False}}
def test_compute_tiers_and_small_cart():
    assert fees.compute(RULES, 397, verified=True) == {"delivery": 0, "handling": 15, "small_cart": 0, "surge": 0, "total": 15, "estimated": False}
    assert fees.compute(RULES, 80, verified=True)["total"] == 70
    assert fees.compute(RULES, 199, True)["delivery"] == 0 and fees.compute(RULES, 198.99, True)["delivery"] == 30
def test_empty_cart_has_no_fees(): ...
def test_estimated_when_unverified_or_surge_possible(): ...
def test_validate_rejects_bad_rules(): ...   # first tier 10 → "delivery must start at 0"; fee 900 → "≤ 500"; unknown key "tip"
def test_inr_and_area_label():
    assert (cart_rank.inr(15), cart_rank.inr(123456), cart_rank.inr(49.5)) == ("₹15", "₹1,23,456", "₹49.50")
    assert cart_rank.area_label("12 Shanti Society, Gorwa, Vadodara, Gujarat 390016") == "Gorwa, Vadodara"
    assert cart_rank.area_label("22.31,73.18") == "your address"
def test_ranks_cheapest_complete_first_with_deltas(): ...
    # zepto items 397 (handling 15) → card 1 "₹412", cheapest, fee_line "Items ₹397 + delivery ₹0 + handling ₹15",
    # stock_line "all 7 in stock"; blinkit "₹449" delta "+₹37"; instamart "₹461" delta "+₹49"
def test_incomplete_cart_never_outranks_complete(): ...      # cheaper app missing 1 item ranks 2nd, delta_label None, stock_line "1 not found"
def test_failed_and_unserviceable_cards_last_with_notes(): ...
def test_nothing_priced_has_no_cheapest(): ...               # all "pick" → best_site None, total_label "—"
def test_swap_note_and_stock_line(): ...
def test_item_table_highlights_cheapest_per_row_and_ties(): ...
def test_choice_replaces_pick_cell(): ...                    # choices {"0": {"zepto": "z-1"}} → cell "chosen", totals include it, picks drop it
def test_split_worth_it_line(): ...                          # saving ₹45 ≥ 20 → worth_it True, exact line
def test_split_not_worth_it_line(): ...                      # items save ₹6, extra fees ₹30 → exact "… saves ₹6 on items but adds ₹30 in fees. Not worth it."
def test_split_no_cheaper_item_line(): ...
def test_split_needs_two_ok_sites(): ...                     # → split None
def test_free_delivery_hint(): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_fees.py tests/server/test_cart_rank.py -v` → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): fee rules and ranked cart summary with split-order check"`

---

### Task 4: Search recipes in the engine — replay and supervised learning

**Files:**
- Create: `search.py`, `search_learn.py`, `tests/test_search.py`, `tests/test_search_learn.py`, and the synthetic
  fixtures `tests/fixtures/search_get.json` (nested widgets with products in paise, a banner row without a price, and
  `inventory` counts) and `tests/fixtures/search_store.json`.

**Interfaces (Produces):**
- Search recipe (`recipes.kind = 'search'`):

```text
{kind: "search", site, home_url, search_page_url ("…{q}…"), endpoint, method: "GET"|"POST", query: {}, body: obj|null,
 headers: {}, query_param (query-string name, or dotted body path for POST),
 location_inputs: [{in: "query"|"header"|"body", name, value: "lat"|"lon"}],
 store: null | {lookup: {method, url, body}, location_inputs: [...], store_id_path, inputs: [{in, name}]},
 field_map: {products_path, product_id, name, brand, pack_size, price, mrp, in_stock, price_divisor: 1|100},
 read_post_allowlist: [{url_regex, why}]}
```

- `search.SEARCH_SITES = ("zepto", "blinkit", "instamart")`. The starting points for learning, kept in each recipe
  afterwards, are
  `SEARCH_PAGES = {"zepto": "https://www.zepto.com/search?query={q}", "blinkit": "https://blinkit.com/s/?q={q}", "instamart": "https://www.swiggy.com/instamart/search?custom_back=true&query={q}"}`.
- `search.SearchError(Exception)`, `search.SearchDrift(SearchError)`, `search.Unserviceable(SearchError)`,
  `search.Blocked(SearchError)` (raised for 401/403 or a login redirect: there is no session to expire).
- `search.to_stock(v) -> bool`:
  - None → True; a bool as is; a number → `> 0`.
  - A string is out of stock if it is in `{"out_of_stock", "sold_out", "unavailable", "oos", "false", "0"}` or
    contains "out of stock" / "sold out".
- `search.extract_products(data, field_map) -> list[dict]` returns
  `{product_id: str, name: str, brand, pack_size, price: float, mrp: float | None, in_stock: bool}`. Rows without an
  id, a name or a price > 0 are dropped. Prices and MRPs are divided by `price_divisor`.
- `search.build_request(recipe, text: str, location: dict, store_id: str | None = None) -> dict`: starts from
  `recipes.first_request`, then `recipes.apply_param(query_param, text)`, then injects the location inputs and the
  store inputs (headers set directly).
- `search.resolve_store(page, recipe, location) -> str | None`: `browser.fetch_in_page` on the lookup request, then
  `get_path(store_id_path)`.
- `search.search_products(site, queries: list[str], location: dict, recipe: dict, profile_dir: Path, proxy_server: str | None = None, max_results: int = 20) -> dict[str, list[dict]]`:
  - Runs `browser.open_site(site, allowlist=recipe["read_post_allowlist"], profile_dir=profile_dir, location=location, proxy_server=proxy_server, block_media=True)`.
    It never passes `login_mode` and never clicks.
  - It calls `goto(home_url)`, then resolves the store when there is one: no store id → `Unserviceable`.
  - Per query, the fast path is `fetch_in_page(build_request(...))`. Its fallback is `goto(search_page_url)` and
    reading the last captured response of the endpoint (`agent.captured_for`) within 8 s.
  - Both paths failing on the first query → `SearchDrift`. An empty result for a later query → `[]`.
  - It sleeps a random 0.8–1.6 s between queries, and logs only the site and counts.
- `search_learn.SEARCH_PROBES = ("milk", "bread")`. `search_learn.PRODUCTISH_KEY` is a regex over
  name/title/price/mrp/stock/inventory/brand/weight/quantity/unit/variant/product.
- `search_learn.candidates(captured: list[dict], probe: str) -> list[dict]`: captured responses that hold a
  product-like collection (≥2 product-ish keys) whose item names contain the probe word, best first.
- `search_learn.find_query_param(req: dict, probe: str) -> str | None`.
- `search_learn.find_location_inputs(req: dict, lat: float, lon: float) -> list[dict]`: query, header or body values
  equal to the coordinates rounded to 3 or more decimals.
- `search_learn.find_store_binding(captured: list[dict], req: dict) -> dict | None`: a query or header value of ≥4
  characters, other than the probe or the coordinates, that appears in an earlier captured JSON response, giving that
  request as the lookup and the path as `store_id_path`.
- `search_learn.check_search_recipe(recipe) -> str | None`:
  - Required keys are present, and the endpoint path doesn't match `guard.BLOCKED_URL`.
  - Allowlist regexes are narrow: none matches `https://<host>/` or `https://example.com/x`. A POST endpoint must be
    covered by the allowlist.
  - Missing location → `"search is not tied to a location"`.
- `search_learn.learn_search(site, location, profile_dir, proxy_server=None, approve=None, preapproved=(), llm=None) -> dict`:
  1. Open the site in read-only mode on `profile_dir`.
  2. For each probe, navigate `search_page_url` and collect the captured responses.
  3. If a POST from `guard.blocked` carries the probe, propose
     `{"url_regex": "^https://<host><path>$", "why": "product search (read-only)"}`, skipped when the regex is
     already preapproved. `approve` (default `agent.confirm_allowlist`) must return True before the entry is added to
     `guard.allowlist` and the page is reloaded. A rejection → `sync.RecipeError("POST allowlist rejected")`.
  4. The LLM writes the `field_map` from the top candidate's sample. A recipe must yield ≥3 products for the probe;
     on failure the error goes back to the LLM, up to 3 tries. OpenAI calls pass through `metering.count_openai`.
  5. Run `check_search_recipe`.
  6. Validate on a **new empty** temporary profile with `search_products(site, ["bread"], …)`. Fewer than 3 products
     → `sync.RecipeError("search recipe needs the learner's session")`.
  7. Return the recipe without saving it.

- [ ] **Step 1: Write the failing tests** (browser edges faked like `tests/server/helpers.fake_login_env`)

```python
def test_extract_products_maps_fields_and_divisor(): ...    # fixture → 3 products, price 2900 paise → 29.0, banner dropped
def test_to_stock_values():
    assert [search.to_stock(v) for v in (None, 0, 3, "OUT_OF_STOCK", "Sold out", "available")] == [True, False, True, False, False, True]
def test_build_request_get_with_location_header(): ...      # q=milk in the query string; header lat/lon set
def test_build_request_post_body_path_and_store(): ...      # body "variables.query" = "milk"; store header from store_id
def test_search_products_fast_path(monkeypatch): ...        # 2 queries → 2 fetches, keys == queries
def test_search_opens_read_only_fresh_profile_with_media_blocked(monkeypatch): ...  # open_site kwargs: no login_mode, block_media True, allowlist from recipe
def test_search_falls_back_to_search_page_capture(monkeypatch): ...
def test_search_drift_when_both_paths_fail(monkeypatch): ...  # → SearchDrift
def test_unserviceable_when_store_lookup_empty(monkeypatch): ...
def test_blocked_on_403(monkeypatch): ...                   # fetch_in_page raises SessionExpired → search.Blocked
def test_find_location_inputs_query_header_body(): ...
def test_find_store_binding(): ...
def test_find_query_param_get_and_post(): ...
def test_candidates_prefers_product_collection_with_probe(): ...
def test_check_rejects_recipe_without_location(): ...
def test_check_rejects_blocked_path_and_broad_regex(): ...
def test_learn_post_search_needs_approval(monkeypatch):     # approve → False: RecipeError, guard.allowlist unchanged, no reload
    ...
def test_learn_preapproved_post_does_not_ask(monkeypatch): ...
def test_learn_validates_on_a_new_empty_profile(monkeypatch): ...  # search_products called with a profile_dir != learn's
def test_learn_retries_field_map_with_error_feedback(monkeypatch): ...  # stub llm: bad map, then good → 2 calls
```

- [ ] **Step 2: Run** `pytest tests/test_search.py tests/test_search_learn.py -v` → FAIL.
- [ ] **Step 3: Implement** `search.py` and `search_learn.py`. Reuse `recipes.get_path / to_number / first_request / apply_param`, `browser.fetch_in_page / goto`, `agent.captured_for / _collections`-style walking; no change to `guard.py`.
- [ ] **Step 4: Run** `pytest -q` → green (CLI tests untouched).
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): search recipes with location binding, read-only replay and supervised learning"`

---

### Task 5: PWA — the list screen and cart client

**Files:**
- Create: `web/lib/cart.ts`, `web/components/cart/{ListEditor,ItemRow,UsualSheet,RecentChecks}.tsx`,
  `web/__tests__/cart-list.test.tsx`, `web/__tests__/cart-lib.test.ts`
- Modify: `web/app/(app)/cart/page.tsx` (replaces the Plan 3 placeholder)

**Interfaces:**
- Consumes: the T6 routes and errors (contract only; T6 lands in Wave B); T3's summary shape; `getLimits()` from Plan 4 `me.ts`; `apiFetch` / `ApiError`.
- Produces (`web/lib/cart.ts`):
  - Types `CartItem {text: string; qty: number}` and `CartSummary`, `CartCard`, `CartTable`, `CartPick`,
    `CartSplit`, which mirror T3's shape field for field.
  - `parseLine(s: string): CartItem` handles `"Amul Taaza 1L ×2"`, `"x2"` / `"* 2"` / `"2 x Bread"` → qty 2, and
    defaults to qty 1.
  - `getUsual()`, `startCheck(items)` → `{request_id, sites, cart_checks_left}`, and
    `setChoice(requestId, idx, site, productId)` → `CartSummary`.
  - `getRequest(id)`: a Supabase select on `cart_requests` (`id, items, status, area, summary, created_at, priced_at`).
  - `getResults(id)`: `cart_results` with `site, status`.
  - `watchCartRequest(id, onChange)`: Realtime on `cart_requests` with `id=eq.<id>` and on `cart_results` with
    `request_id=eq.<id>`. It returns an unsubscribe function.
  - `recentChecks(limit = 3)`.
- The `/cart` screen:
  - Title "Cart" and subtitle "Compare live prices on Zepto, Blinkit and Instamart."
  - An address row "Delivering to {area}". With no address: "Add your delivery address" linking to `/accounts`, and
    Compare disabled.
  - An input with placeholder "Add an item, e.g. Amul Taaza 1L ×2"; Enter or the "Add" button appends `parseLine`.
  - Rows show the name, a qty stepper (1–20, 44px touch targets) and a remove button.
  - "Start from my usual" opens a `Sheet` listing the usual items, all checked, with "×{times} in 90 days" and an
    "Add {n} items" button. With no usual items: "We'll suggest items once you've ordered groceries a few times."
  - The primary "Compare prices" button is disabled when the list is empty; under it, "{n} checks left today".
    API 4xx/5xx `detail` is shown verbatim under the button. On 202 → `router.push("/cart/" + request_id)`.
  - `?from=<id>` prefills the list from that request ("Edit list").
  - "Recent checks": up to 3 rows of "{N} items · {best app} {total_label} · {relative time}", each linking to
    `/cart/{id}`.
  - Max 30 items: the input is disabled at 30, with "Up to 30 items per check."

- [ ] **Step 1:** Invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`. Then
  write the failing tests:
  - `parseLine handles ×2, x 2 and 2 x`
  - `adds, steps and removes items`
  - `usual sheet adds checked items`
  - `empty usual shows the hint`
  - `compare posts the list and navigates`
  - `429 detail is shown and nothing navigates`
  - `no address disables compare with the link`
  - `edit list prefills from a request`
  - `checks left line comes from limits`
  - `watchCartRequest subscribes to both tables and unsubscribes`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm --dir web test`, `pnpm --dir web lint`, `pnpm --dir web build` → pass; `/cart` at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): cart list with usual items and live check start"`

---

## Wave B (after Wave A is merged; parallel, disjoint files)

### Task 6: Cart store and API

**Files:**
- Create: `server/cart.py`, `server/cart_api.py` (`APIRouter(prefix="/cart")`), `tests/server/test_cart_store.py`, `tests/server/test_cart_api.py`
- Modify:
  - `server/config.py`: `Settings` gains `split_min_saving_inr: float = 20`, `cart_max_items: int = 30`,
    `cart_stale_s: int = 600`, `match_judge: bool = True` (`MATCH_JUDGE` "1"/"0") and `match_model: str = ""`.
  - `server/api.py`: include the router.
  - `server/admin.py`: `site_health` also returns `kind: "search"` rows for `SEARCH_SITES`, with 7-day `search` job
    outcomes and `needs_account: false`; orders rows get `needs_account: true`.
  - `server/admin_api.py`, `docker-compose.yml` (the new env, set explicitly), `tests/server/test_config.py`.

**Interfaces:**
- Consumes: T1 tables and `jobs.enqueue(profile=)` / `search_profile`; T3 `build_summary`, `area_label`,
  `fees.validate`, `fees.compute`; `search.SEARCH_SITES`; Plan 4 `limits.consume_cart_check / to_http`,
  `usage.record / today`, `recipe_admin.is_paused / is_admin`.
- Produces (`server/cart.py`):
  - Exceptions: `NoAddress`, `CheckRunning`, `Unavailable`, `NotFound`, `NotReady`, `BadChoice(ValueError)`.
  - `normalize_items(items: list[dict], max_items: int) -> list[dict]`: trims text; merges lines whose
    `db.norm_name` is equal (sums qty, capped at 20, keeps the first text). Raises `ValueError` when the list is empty
    or longer than `max_items`.
  - `usual_items(conn, user_id, now, days=90, min_times=3, limit=30) -> list[dict]` returns `{text, qty, times}`.
    `times` counts distinct non-cancelled orders over the last `days` IST days, from grocery apps only (Global
    Constraints). Items are keyed by `norm_name`. `text` is the most recent name and `qty` the most common quantity
    (≥1; ties → smaller). Ordered by times desc, then text.
  - `usable_sites(conn) -> list[str]`: `SEARCH_SITES` with a `search` recipe that is healthy and not paused.
  - `start_check(conn, user_id, items, now, s) -> dict` returns `{request_id, sites, cart_checks_left}`:
    1. Profile lat/lon missing → `NoAddress`.
    2. An active `search` job for the user → `CheckRunning`.
    3. No usable site → `Unavailable`.
    4. Then, in one `conn.transaction()`: `limits.consume_cart_check`; insert the request with
       `area = area_label(profile.address)` and lat/lon; insert `unavailable` result rows for the unusable sites;
       enqueue one `search` job per usable site with payload `{"request_id"}` and profile `search_profile(site)`.
       `jobs.Conflict` → `CheckRunning`, and the rollback restores the check.
  - `get_request(conn, request_id) -> dict | None`. `fee_rules(conn) -> dict[str, tuple[dict, bool]]`.
  - `put_fee_rules(conn, site, rules, verified, admin_user_id) -> int`: validates (`ValueError(errors)`) and bumps
    the version.
  - `record_site_result(conn, request_id, site, status, items=(), error=None) -> None`: upsert, with `user_id` copied
    from the request.
  - `finalize_if_complete(conn, request_id, s) -> bool`:
    - Locks the request row (`for update`) and returns False unless the status is `searching`.
    - Every site whose `search` job is `failed` without a result row gets `failed` / `worker_lost`. It returns False
      while any site lacks a row.
    - Otherwise it builds the summary, writes each result's `subtotal / fees / total / fees_estimated`, and sets
      `status`, `summary` and `priced_at`.
    - A `failed` summary refunds the check: `usage.record(conn, user_id, <IST day of created_at>, cart_checks=-1)`.
  - `finalize_stale(conn, s, now) -> int`: `searching` requests older than `s.cart_stale_s` get `failed` / `timeout`
    rows for the missing sites and are finalized.
  - `set_choice(conn, user_id, request_id, idx, site, product_id, s) -> dict` returns the new summary:
    - Errors: not the user's request → `NotFound`; status not `done` → `NotReady`; the product is not among that
      cell's candidates → `BadChoice`.
    - On success it updates `choices`, rebuilds and stores the summary. It never consumes a check.
- Produces (routes):
  - `GET /cart/usual` → `{items: [{text, qty, times}]}`.
  - `POST /cart/checks`:
    - Body: `{items: [{text: str 1..80, qty: int 1..20}] (1..CART_MAX_ITEMS)}` → 202
      `{request_id, sites, cart_checks_left}`.
    - Errors: 422 for an invalid list; 409 `"Add your delivery address first."`; 409
      `"A cart check is already running."`; 503 `"Cart checks are unavailable right now."`; and `limits.to_http` for
      the daily limit (429 `"You can check 10 carts a day. Try again tomorrow."` + `Retry-After`) and for the budget
      pause (503 `"Cart checks are paused until 1 November."`-style).
  - `POST /cart/checks/{id}/choices`: body `{idx: int, site: str, product_id: str}` → 200 `{summary}`. Errors: 404,
    409 `"Prices are still loading."`, 422 `"That product isn't one of the options."`.
  - Admin routes (in `admin_api.py`, guarded by `require_admin`):
    - `GET /admin/fees` → `[{site, rules, version, verified, source, updated_at}]`.
    - `PUT /admin/fees/{site}` with body `{rules, verified}` → 200 `{version}` (422 `{detail: [errors]}`).
    - `POST /admin/learn/{site}?kind=orders|search` (default `orders`, unchanged). For `kind=search`: the site must be
      in `SEARCH_SITES` (else 400); the admin needs a saved address (409 `"Add your delivery address first."`) but no
      connected account. The job is a `learn` with payload `{"reason": "manual", "kind": "search"}` and profile
      `search_profile(site)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_merges_duplicate_lines():
    assert cart.normalize_items([{"text": "Curd 400g", "qty": 1}, {"text": " curd 400G ", "qty": 2}], 30) == [{"text": "Curd 400g", "qty": 3}]
def test_usual_items_three_times_in_90_days_grocery_only(pg): ...   # 3 zepto orders → listed; zomato ×5 and 91-day-old excluded; cancelled not counted
def test_start_check_consumes_one_and_enqueues_per_usable_site(pg): ...   # 3 recipes → 3 search jobs with profiles search:*; usage.cart_checks == 1
def test_paused_site_is_marked_unavailable_without_job(pg): ...
def test_no_usable_site_is_unavailable_and_not_counted(pg): ...
def test_finalize_waits_for_every_site_then_builds_summary(pg): ...
def test_finalize_treats_failed_job_without_result_as_failed(pg): ...
def test_all_sites_failed_refunds_check(pg): ...              # status failed, usage.cart_checks back to 0
def test_finalize_is_idempotent_under_race(pg, pg2): ...      # two connections finalize at once → one summary write
def test_finalize_stale_marks_timeout(pg): ...
def test_set_choice_recomputes_without_consuming(pg): ...
def test_set_choice_rejects_unknown_product(pg): ...
def test_put_fee_rules_validates_and_versions(pg, fee_rules_snapshot): ...
# test_cart_api.py — local fixture admin_token: helpers.add_user(pg, ADMIN, "admin@x.test"), admin.add_admin(pg, "admin@x.test"), auth_header(ADMIN)
def test_check_needs_address(client, token_a, pg): ...        # 409 exact detail
def test_second_check_while_running_is_409_and_not_counted(client, token_a, pg): ...
def test_eleventh_check_is_429(client, token_a, pg): ...      # 10 ok (jobs finished between), 11th 429 exact detail + Retry-After
def test_check_paused_budget_is_503(client, token_a, pg): ...
def test_check_validates_items(client, token_a): ...          # 0 items, 31 items, qty 0, text 81 chars → 422
def test_usual_endpoint(client, token_a, pg): ...
def test_choice_endpoint_scoped_to_owner(client, token_b, pg): ...   # B on A's request → 404
def test_no_fill_cart_route_or_job(client): ...               # no route path contains "fill"; jobs never get type fill_cart
def test_admin_fees_get_put(client, admin_token, pg, fee_rules_snapshot): ...
def test_admin_learn_search_needs_no_connection(client, admin_token, pg): ...  # 202; payload kind search; profile search:blinkit
def test_admin_sites_lists_search_rows(client, admin_token, pg): ...
def test_settings_cart_tunables_from_env(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_cart_store.py tests/server/test_cart_api.py tests/server/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green; `docker compose config -q` → 0.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): cart checks with daily limit, usual items, choices and admin fee rules"`

### Task 7: PWA — ranked results, item table and pick-one

**Files:**
- Create: `web/app/(app)/cart/[id]/page.tsx`, `web/components/cart/{SearchingState,ResultHeader,AppCard,ItemTable,SplitLine,PickSheet}.tsx`, `web/__tests__/cart-result.test.tsx`

**Interfaces:**
- Consumes: T5 `lib/cart.ts` (`getRequest`, `getResults`, `watchCartRequest`, `setChoice`, `startCheck`, types); T3 summary strings.
- Produces, matching mockup A and then B:
  - **Searching state** (status `searching`):
    - "Checking prices…", then one row per site with an app dot, the name and "Checking…" / "✓ Done" /
      "Couldn't check", driven by `cart_results` updates.
    - After 120 s: "Taking longer than usual. You can leave this screen. Your result will be here."
    - Status `failed` → "We couldn't get prices right now. This check wasn't counted." with "Try again" (`startCheck`
      with the same items).
  - **Header:** "Cheapest cart" (h1) and a muted line `"{N} items · priced live at {area} · {relative time} ago"`,
    re-rendered every 60 s. When `priced_at` is older than 30 min: "Prices may have changed." plus "Check again".
  - **Cards in `summary.cards` order:**
    - Each card has an app dot, the name and `total_label` (bold on the cheapest).
    - The cheapest card gets an accent outline (`outline: 2px solid #0a7c55`) and a "Cheapest" pill.
    - Under the total: `fee_line`, then a muted "fees estimated" tag when `fees_estimated`, `fee_hint`, `stock_line`,
      one orange `.warn` pill per `swaps` entry, and "{n} to pick ›" (opens `PickSheet` for that site). Non-ok cards
      show `note` only.
    - The cheapest card has a full-width primary-styled button "Fill cart on {App}", rendered `disabled`,
      `aria-disabled="true"`, with the sublabel "Coming soon". It never calls the API.
    - Other ok cards show `delta_label` next to the total.
  - **"See item prices"** is a text button that toggles `ItemTable` (mockup B):
    - Item column left, app columns right, short names ("Insta" when narrower than 360px).
    - Cells show `price_label`; `cheapest` → green bold; "n/a" / "Out of stock" muted; "Pick" is a button.
    - A totals row "Total + fees" with a top border. The table fits 375px without horizontal page scroll (the table
      itself wraps item labels).
    - `SplitLine` under the table renders `split.line`.
  - **"Edit list"** is a secondary button → `/cart?from={id}`.
  - **`PickSheet(idx, site)`:** title "Which one did you mean?", the item label, then candidates as radio rows
    (name, size_label, price_label, "×{packs}" when >1). "Use this" → `setChoice` → replaces the summary in place.
    422/409 detail is shown in the sheet.
  - Errors from Supabase reads → "Couldn't load this check." with a retry.

- [ ] **Step 1:** Invoke the three design skills. Then write the failing tests, using fixture summaries built from
  T3's shape:
  - `cards render in order with cheapest outlined and pill`
  - `fill button is disabled with coming soon and calls nothing`
  - `other cards show delta labels`
  - `swap note and fees estimated render`
  - `failed card shows its note and no total`
  - `see item prices toggles the table with highlighted cells and totals row`
  - `split line renders under the table`
  - `pick sheet posts the choice and updates totals`
  - `searching state ticks sites from results`
  - `failed request offers try again and says not counted`
  - `stale prices offer check again`
  - `edit list links back with from`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass; with a 7-item fixture, `/cart/[id]` at 375×812 and 390×844 has no horizontal scroll with the table open.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): ranked cheapest-cart results with item table and pick-one"`

### Task 8: PWA — admin fee rules and search learning

**Files:**
- Create: `web/components/admin/FeesPanel.tsx`, `web/__tests__/admin-fees.test.tsx`
- Modify: `web/app/(app)/admin/page.tsx` (segment "Fees" after "Sites"), `web/components/admin/SitesPanel.tsx`, `web/lib/admin.ts`

**Interfaces:**
- Consumes: T6 `GET /admin/fees`, `PUT /admin/fees/{site}`, `POST /admin/learn/{site}?kind=search`, and the `/admin/sites` rows with `kind` and `needs_account`.
- Produces:
  - **`SitesPanel`** groups rows under "Order history" and "Prices (search)". A search row's "Learn" is never
    disabled for want of a connection. A running search learn shows `approval_needed` regexes with Approve / Reject,
    the same component Plan 4 uses.
  - **`FeesPanel`** has one card per app (dot + name):
    - A badge "Verified" (green) or "Unverified — check {App}'s fee page" (orange).
    - Delivery tiers as rows "From ₹{min} → fee ₹{fee}", with add and remove; the first row's minimum is fixed at 0.
    - Handling fee; a small-cart toggle with "below ₹" and "fee ₹".
    - "Surge or rain fees possible" toggle, plus an optional known fee.
    - Source URL; a "These match the app today" checkbox (`verified`).
    - "Save" → PUT; 422 errors are listed under the form, and the version is shown after saving.
  - Numeric inputs use `inputmode="decimal"`.

- [ ] **Step 1:** Invoke the three design skills, then write the failing tests:
  - `search rows learn without a connection`
  - `fees panel loads rules and saves edits`
  - `first tier minimum is fixed at zero`
  - `422 errors are listed`
  - `unverified badge shows`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass; `/admin` Fees at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): admin fee rules and supervised search learning"`

---

## Wave C (after Wave B is merged)

### Task 9: Worker — search jobs, search learning, fake search and stale checks

**Files:**
- Modify:
  - `server/worker.py`, `server/fake_sites.py`.
  - `server/recipe_admin.py`: `any_admin_user(conn) -> str | None`, the oldest admin with an `auth.users` row.
  - `server/scheduler.py`: the tick calls `cart.finalize_stale(conn, s, now)` after enqueueing, and the result dict
    gains `"carts_finalized"`.
- Test: `tests/server/test_worker_plan5.py`, `tests/server/test_fake_sites.py`, `tests/server/test_scheduler.py`

**Interfaces:**
- Consumes: T2 `parse_query / from_raw / match_item / llm_judge`; T4 `search.search_products / SearchDrift /
  Unserviceable / Blocked` and `search_learn.learn_search`; T6 `cart.get_request / record_site_result /
  finalize_if_complete / finalize_stale` and the Settings fields; Plan 4 `recipe_admin.*`, `notify.alert_admins`,
  `usage` metering in `run_job`.
- Produces:
  - `worker.JOB_TYPES = ("connect", "sync", "learn", "search")`.
  - `worker.search_session_id(site, lat, lon) -> str` (R1).
  - The search job is `_do_search(job, conn, s, profile_dir, proxy_server)`. It runs inside `run_job`'s metered
    `try`/`finally`, but skips vault restore and save, `_ensure_proxy_session` and every `connections` write. With
    `s.proxy_url_template` set and fake mode off, it opens its own `Relay(proxy_url_for(template, search_session_id(...)))`,
    and its bytes count toward the user's usage.
    1. A request that is gone → job failed `request_gone`.
    2. A missing, unhealthy or paused recipe → `unavailable` result.
    3. Search with location `{query: area, lat, lon}` and the fresh `profile_dir`.
    4. Match every item; the judge is `llm_judge(s.match_model or agent.model())` when `s.match_judge` and fake mode
       is off, else None.
    5. `record_site_result(..., "ok", items)`, then progress `stage="priced", matched, to_pick`.
    6. Error handling:
       - `Unserviceable` → `unserviceable`.
       - `Blocked` → `failed` / `blocked`.
       - `SearchDrift` → `failed` / `recipe_drift`, plus R11: when `mark_drift(conn, site, "search", note)`
         transitions, enqueue a `learn` for `any_admin_user` with payload
         `{"reason": "drift", "auto": true, "kind": "search"}` and profile `search_profile(site)`. With no admin,
         `pause_site(…, "search", …)` and
         `alert_admins(kind="site_paused", title=f"{App} prices paused", body="Its search recipe stopped working and no admin is set up to re-learn it.", dedupe_key=f"site_paused:search:{site}:{today}")`.
       - Any other error → `failed` with the cleaned error.
    7. Always `finalize_if_complete` in `finally`. The job is `done` when a result row was written.
  - Learn job with `payload.kind == "search"`:
    - The admin's profile address is required (`no_address`); no vault profile is restored.
    - It uses a fresh `profile_dir`, a pooled proxy, `approve` = Plan 4's approver (or `deny_new_posts` when `auto`),
      and `preapproved` = the current search recipe's allowlist regexes.
    - It calls `learn_search(...)`, then
      `apply_learned(conn, site, "search", recipe, approver.approved, job.id, reason="drift_relearn" if auto else "learned")`.
      An `auto` failure or a pending result → pause the search recipe and alert admins, as in Plan 4.
  - Fake mode (`WORKER_FAKE=1`):
    - `fake_sites.search(site, queries, **_) -> dict[str, list[dict]]` returns `extract_products`-shaped rows from
      `FAKE_CATALOG`: the 12 `GROCERIES` plus `Amul Taaza Toned Milk 1 L`, `Milk Bikis Biscuits 200 g`,
      `Lay's Classic Salted Chips 52 g` and `Lay's Magic Masala Chips 52 g`.
    - Each entry has a brand, a pack size and a base price. The base prices: Amul Taza Milk 500ml 29, Farm Eggs 6 pcs
      54, Banana Robusta 1kg 60, Onion 1kg 40, Tomato 500g 30, Britannia Brown Bread (400 g) 50, Amul Butter 100g 58,
      Maggi Noodles 4 pack 56, Coriander Bunch 10, Aashirvaad Atta 5kg 260, Curd 400g 35, Potato 1kg 35, Amul Taaza
      Toned Milk 1 L 60, Milk Bikis Biscuits 200 g 30, both Lay's 52 g 20.
    - Site price = `round_half_up(base × {"zepto": 0.92, "blinkit": 1.00, "instamart": 1.06}[site])`;
      MRP = `round_half_up(base × 1.1)`. `product_id = f"{site}-{slug(name)}"`.
    - Instamart has Lay's Classic out of stock; Blinkit has no Coriander Bunch.
    - A query returns the entries sharing ≥1 `match.tokens` token with it, sorted by name.
    - `fake_sites.learn(site, approve, preapproved=(), kind="orders", **_)`: `kind="search"` returns a valid search
      recipe for `https://fake.<site>.test/api/search`. It is GET for zepto and blinkit, and POST for instamart with
      `read_post_allowlist=[{"url_regex": "^https://fake\\.instamart\\.test/api/search$", "why": "product search (read-only)"}]`,
      which calls `approve([entry])` unless preapproved (rejection → `RecipeError("POST allowlist rejected")`).

- [ ] **Step 1: Write the failing tests**

```python
def test_search_job_writes_matched_result_and_finalizes(pg, monkeypatch): ...  # stub search_products; 1-site request → summary present, status done
def test_search_job_never_touches_connections_or_vault(pg, monkeypatch): ...    # vault restore/save and set_connection fail if called
def test_search_job_uses_fresh_profile_and_pooled_proxy(pg, monkeypatch): ...   # profile_dir under PROFILE_ROOT/<job id>; session == search_session_id
def test_search_job_records_usage(pg, monkeypatch): ...      # judge stub counts 500 tokens → usage.ai_tokens == 500; worker_seconds > 0; syncs 0; cart_checks unchanged
def test_unserviceable_and_blocked_results(pg, monkeypatch): ...
def test_search_drift_enqueues_auto_search_learn(pg, monkeypatch): ...
def test_search_drift_without_admin_pauses_and_alerts(pg, monkeypatch): ...
def test_learn_search_job_auto_applies_get_recipe(pg, monkeypatch): ...   # recipes(site, 'search') version 1
def test_learn_search_job_waits_for_post_approval(pg, pg2, monkeypatch): ...
def test_learn_search_needs_admin_address(pg): ...
def test_stale_request_is_finalized_failed(pg): ...          # scheduler.tick with request 11 min old → status failed, check refunded
# test_fake_sites.py
def test_fake_cart_ranks_zepto_blinkit_instamart(pg, fake_mode): ...
    # list [("Amul Taza Milk 500ml", 2), ("Aashirvaad Atta 5kg", 1), ("Farm Eggs 6 pcs", 1), ("Lay's Classic", 1),
    #       ("Britannia Brown Bread", 1)], seeded fee rules → total_labels ["₹422", "₹451", "₹479"],
    #       delta_labels [None, "+₹29", "+₹57"], instamart swaps == ["Lay's Classic Salted Chips 52 g swapped for Lay's Magic Masala Chips 52 g"],
    #       split.line == "No item is cheaper on another app, so one order on Zepto is best."
def test_fake_milk_is_pick_one(pg, fake_mode): ...           # ["milk", "Aashirvaad Atta 5kg"] → every card to_pick == 1
def test_fake_learn_search_instamart_asks_for_approval(): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_worker_plan5.py tests/server/test_fake_sites.py tests/server/test_scheduler.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(worker): live search jobs, supervised search learning, fake search and stale-check cleanup"`

---

## Wave D

### Task 10: End-to-end — cheapest cart locally on an iPhone viewport

**Files:**
- Create: `e2e/tests/plan5.spec.ts`
- Modify:
  - `e2e/helpers/stack.ts`: `setAddress(email, address, lat, lon)` runs `$E2E_PY` with a short `psycopg` upsert into
    `profiles` for that user, against `DATABASE_URL`.
  - `scripts/e2e_local.sh`: exports `MATCH_JUDGE=0` to the worker.
  - `docs/e2e-local.md`.

**Interfaces:** `plan5.spec.ts` is serial and uses the iPhone 14 profile. The address used is
"12 Shanti Society, Gorwa, Vadodara, Gujarat" (22.3300, 73.1500).
1. `admin learns search recipes`:
   - The admin opens Sites → "Prices (search)", then runs Learn for Zepto and Blinkit; each becomes Healthy at
     version 1.
   - Learn for Instamart shows the approval `^https://fake\.instamart\.test/api/search$` → Approve → Healthy.
2. `usual items come from past orders`:
   - An invited user connects fake Zepto with `123456`, and `setAddress` is applied.
   - On `/cart`, "Start from my usual" lists at least one item, each with "×N in 90 days" (N ≥ 3), and "Add {n} items" fills the list.
3. `cheapest cart ranks apps with fees`:
   - Type the 5-item list from T9's fake test → Compare prices → the searching state ticks 3 apps.
   - The header is "5 items · priced live at Gorwa, Vadodara".
   - The first card is Zepto "₹422", outlined, with the "Cheapest" pill. Blinkit shows "₹451 +₹29" and Instamart
     "₹479 +₹57", with the swap pill "Lay's Classic Salted Chips 52 g swapped for Lay's Magic Masala Chips 52 g".
   - "fees estimated" is visible.
   - "Fill cart on Zepto" is disabled and "Coming soon" is visible.
4. `item table and split line`:
   - "See item prices" shows 5 item rows plus "Total + fees" with "₹422 ₹451 ₹479". The Zepto cells are highlighted.
   - The line reads "No item is cheaper on another app, so one order on Zepto is best."
5. `pick one`:
   - "Edit list" → the list is prefilled. Replace it with "milk" + "Aashirvaad Atta 5kg" → Compare.
   - Every card shows "1 to pick". On Zepto, choose "Amul Taaza Toned Milk 1 L" → Zepto shows "₹309" and ranks
     first; no new check is counted (`/me/limits` unchanged).
6. `daily limit`:
   - Run the remaining checks through the API (each waited until done) up to 10.
   - Compare then shows "You can check 10 carts a day. Try again tomorrow." and the count line "0 checks left today".
7. `budget pause`:
   - A second invited user with an address runs `schedulerOnce({MONTHLY_BUDGET_INR: "0.01"})`.
   - Compare shows `/Cart checks are paused until 1 \w+\./`. Then `schedulerOnce({})` restores it.
8. `no horizontal scroll on /cart and /cart/[id] with the table open`.

- [ ] **Step 1: Write the spec** (it fails until the stack runs).
- [ ] **Step 2: Implement** the helper, script and docs changes.
- [ ] **Step 3: Run** `bash scripts/e2e_local.sh` → `product.spec.ts`, `plan4.spec.ts` and `plan5.spec.ts` all green; `pytest -q` and `pnpm --dir web test` still green.
- [ ] **Step 4: Commit** — `git commit -m "test(e2e): cheapest cart end to end with supervised search learning, pick-one and limits"`

---

## Self-review

- **Spec coverage:**
  - List entry and "start from my usual" with 3+ times in 90 days from grocery apps: T5, T6.
  - Search per grocery app at the user's address, learned once per site, mapping name, brand, pack size, price, MRP,
    in-stock and product id: T4, T9.
  - No login needed, pooled proxy: R1, T9.
  - Matching with brand, variant and size normalisation, cheapest-per-unit equivalent, a small model confirming near
    matches, and "pick one" instead of guessing: T2, T7.
  - Totals with delivery, handling, small-cart and surge fees from per-site rules, labelled "fees estimated" when
    unknowable: R3, T1, T3, T8.
  - Ranked app totals with header, outline, fee breakdown, stock, swap note, "+₹X", "See item prices" table with
    cheapest per row and totals row, split-order line, "Edit list", and the Fill button disabled until Plan 6: T3,
    T7.
  - `cart_requests` / `cart_results` with RLS: T1.
  - 10 checks a day and the budget pause: T6 (Plan 4 limits).
  - Admin-supervised learning, POST approval and drift handling: T4, T6, T8, T9.
  - Location: the search is priced at the saved lat/lon and the request stores area plus lat/lon for Plan 6's
    location check: R9, T6.
  - Known risks: low request rate, media blocked, a paused site degrades to "unavailable": T4, T9.
  - Local end to end on an iPhone viewport: T9 fake mode, T10.
- **Type consistency:** these names are identical in producer and consumer tasks: `jobs.enqueue(profile=)` /
  `search_profile`; `match.match_item` dict keys; `search.search_products` and its errors; `search_learn.learn_search`;
  `cart.start_check / record_site_result / finalize_if_complete / finalize_stale / set_choice / fee_rules`;
  `cart_rank.build_summary`. The summary shape is written once (T3) and mirrored in `lib/cart.ts` (T5).
- **Ordering:**
  - T1 runs alone first, since every server test needs 0008.
  - Wave A (T2–T5) is pure or contract-only: disjoint file sets, no imports between them. T5 mocks the T6 API.
  - Wave B (T6–T8): T6 needs T1–T4. T7 and T8 consume only API contracts. The file sets are disjoint
    (`server/admin_api.py` and `server/admin.py` are only in T6).
  - Wave C (T9) is alone: it needs T2, T4 and T6, and it is the only task touching `server/worker.py`,
    `server/scheduler.py`, `server/recipe_admin.py` and `server/fake_sites.py`.
  - T10 runs last.
