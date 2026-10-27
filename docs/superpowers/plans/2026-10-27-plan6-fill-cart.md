# Plan 6 of 7 — Fill Cart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the cheapest-cart result, the user taps "Fill cart on <app>". A worker opens the user's own logged-in
Zepto, Blinkit or Instamart (via Swiggy) session and checks that the app's location matches what was priced. It adds
the priced items on top of the existing cart, asks the user live when a match isn't certain, and reports what was
added, out of stock, skipped or only partly added. The result offers "Open <app> to pay". It never checks out and
never pays.

**Architecture:**
- **Guard.** `cart_guard.CartGuard` is used only by `fill_cart` jobs and admin cart-learn jobs. It allows
  admin-approved add and location POSTs plus GETs, and blocks seven forbidden categories over every allowlist.
- **Recipes.** Cart recipes are learned on an admin's own account and always wait for approval.
- **Fill engine.** `cart_fill.py` never clicks. It replays the add request in the page and reads the cart before and
  after. When the location is wrong, it re-prices and adds nothing.
- **Reporting.** Each fill is a `cart_fills` row followed over Realtime, with copy built in Python
  (`server/fill_summary.py`).
- **Fake mode.** `WORKER_FAKE=1` fakes fill and learning against a JSON state file for the e2e.

**Tech Stack:** as Plans 3–5 (Python 3.12, FastAPI, psycopg 3, Playwright-over-CDP Chrome, OpenAI SDK; Next.js 15 +
TypeScript + Tailwind 3.4, Vitest, Playwright e2e, local Supabase). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md`. The binding parts:
- "Fill cart (the only write action)".
- The builder's calls "Cart filling: adds items only…", "Location during cart filling" and "Learning is supervised by
  the admin" (every cart recipe needs admin approval).
- Non-goals, anti-goals, Security, and Known risks.

Mockup: `.superpowers/brainstorm/24332-1793102961/content/cart-compare.html` (A: "Fill cart on Zepto" on the cheapest
card).

**Precondition:** Plans 3–5 are merged and `bash scripts/e2e_local.sh` is green. The Produces blocks of those plans
are assumed:
- Plan 5:
  - The cart tables (`cart_requests.mode` grocery|food) and summary, and `search.*`.
  - Compares run on the user's own vault profile and sticky proxy, only for connected apps.
  - `fees.compute(rules, subtotal, verified, observed)`, `fake_sites.FAKE_CATALOG`, the `fake_mode` fixture, the e2e
    `setConnectionStatus` helper, `lib/cart.ts` and `AppCard`.
- Plan 4: `limits`, `usage`, `budget`, `recipe_admin`, `notify`, the learn job, the scheduler and the admin panels.
- Plan 3: `make_prompt`, `/jobs/{id}/answer`, `watchJob` and `components/ui`.

## Global Constraints

- **Never place an order, pay, apply a coupon or wallet, or create, edit or delete an address.**
  - The only writes are adding, or raising the quantity of, planned products, and selecting an existing location.
  - Nothing links to checkout or payment.
- **Add-only.** A fill never removes, empties or lowers an existing cart line.
  - `qty_mode="set"` sends `before_qty + packs`; `"increment"` sends `packs`; `packs ≥ 1` always.
- **The cart guard is its own code path.**
  - `connect`, `sync` and `search` never construct `CartGuard`.
  - `browser.open_site` refuses `login_mode=True` together with `cart_policy`.
  - Downloads stay denied: `open_site` sets this unconditionally.
- **Fills never click.** Only the admin's cart-learn clicks, and only elements allowed by `agent.cart_click_refusal`.
- **Cart recipes:**
  - Learned only by an admin `learn` job (`kind:"cart"`) on the admin's connected account, after the site's `search`
    recipe exists.
  - `apply_learned(kind="cart")` always ends `pending` (Plan 4 R9); only the admin's Approve publishes one.
  - Drift pauses that site's cart filling. There is never an automatic re-learn.
- **A fill needs a tap:** `fill_cart` jobs are created only by `POST /cart/checks/{id}/fill` and
  `POST /cart/fills/{id}/confirm`, as the owner.
- **Instamart** fills use the Swiggy connection, the `swiggy` profile lock (`SITE_PROFILE`) and Swiggy's sticky proxy
  session.
- **Tunables** live only in `server/config.py`, from env, set explicitly in `docker-compose.yml`:
  `CART_FILLS_PER_DAY=5`, `CART_FILL_MAX_AGE_S=1800`, `CART_LOCATION_MAX_M=500`, `FILL_CONFIRM_WINDOW_S=600`.
  - Module constant:
    `OPEN_APP_URLS = {"zepto": "https://www.zepto.com/", "blinkit": "https://blinkit.com/", "instamart": "https://www.swiggy.com/instamart"}`.
- **Logging:** never log request bodies, product names, addresses, coordinates or address ids with a user id. The guard
  logs the method, `guard.redact_url(url)` and the category; fills log the site and counts.
- **UI:**
  - Plan 3/5 look: background `#f6f7f9`, white cards radius 14px, accent `#0a7c55`, ₹ with Indian grouping, no
    horizontal scroll at 375px, safe areas, 44px touch targets.
  - Frontend tasks invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1` first
    (Plan 3 R3).
  - After code review, each gets a separate design review with Playwright screenshots at 390×844 vs the mockups, feeding
    its fix loop (Plan 3 R5). The mockups win conflicts.
- **`cart_fills`:** RLS on, `authenticated` gets `select` on its own rows only, `anon` nothing. The PWA never uses the
  service-role key.
- **Tests:**
  - Each parallel implementer uses its own test database `ea_tN` on `127.0.0.1:54329` (Plan 3 R4), e.g.
    `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/ea_t3 pytest -q`.
  - All existing tests and the Plan 3–5 e2e suites keep passing. The Plan 5 "Coming soon" assertions are updated in
    T6, T7 and T10.
- **Commits:** on `main`, never pushed, no AI attribution, no deploy.

## Review Focus

- A double tap on "Add items", or two devices filling the same check: the second gets 409, nothing is added twice, and
  nothing more is counted. → T5 `test_second_fill_same_check_is_already_filled_and_not_counted`, T7
  `test_double_fill_is_409`.
- The worker dies mid-fill: the job is never requeued (that would add items again). The fill becomes `interrupted`,
  tells the user to check the app, and can't be re-filled from that check. → T9 `test_lost_fill_job_is_not_requeued`,
  T5 `test_finalize_lost_started_fill_is_interrupted`.
- The app's API sets an absolute quantity and the user already has 2 of the product: send 3, never 1. → T2
  `test_add_value_set_mode_adds_on_top`, T3 `test_set_mode_adds_on_top_of_existing_quantity`.
- The user taps Fill and leaves while "Which milk?" waits: after 180 s that item is skipped ("no answer"), the rest are
  added, and the job doesn't fail. → T3 `test_unanswered_question_skips_item_and_continues`, T9
  `test_question_timeout_is_skip_not_failure`.
- The user changed the app's address after pricing: re-price there, add nothing, offer "Fill at these prices". → T3
  `test_location_mismatch_reprices_and_adds_nothing`, T10 step 5.

## Spec rulings (made by this plan; record in the ledger)

- **R1 Separate guard.** `cart_guard.decide_cart` is new; the read-only `guard.decide` and `Guard` are unchanged.
  - Rule order:
    1. A method outside GET/HEAD/OPTIONS/POST is blocked.
    2. A forbidden category (path, operation name or body) is blocked.
    3. A bot-challenge POST is allowed.
    4. A safe method is allowed.
    5. A POST is allowed only through the cart, location or read allowlist.
  - Forbidden beats allowlist, so even a broad approved regex can't reach checkout or removal.
  - The read guard's blanket `cart` block can't apply, since adding uses cart paths. Removal is blocked by path,
    operation name and body instead.
- **R2 Fills replay requests; they never click.** The spec's click rule is enforced where clicks exist, in the admin
  learn (`cart_click_refusal`).
- **R3 Add-only has four layers:**
  - The guard.
  - `add_value` (never below `before + 1` in set mode).
  - `check_cart_recipe` (no whole-cart add bodies).
  - A before/after cart read, which reports any dropped existing line (`existing_changed`).
- **R4 Quantity semantics are learned.** Add, then "+": a second request carrying 2 means `set`, 1 means `increment`,
  anything else rejects the recipe.
- **R5 Swaps and picks are asked during the fill** (`question_needed` + `choices`).
  - A swap happens only if chosen. No answer within 180 s (`otp_timeout_s`) skips the item.
  - Out-of-stock and not-found items are skipped and reported.
- **R6 Location:**
  - **Match:** the same store id when both sides expose one, else ≤ 500 m from the priced lat/lon.
  - **Mismatch:** select "current location" (the priced lat/lon, which is also Chrome's geolocation) or the nearest
    saved address within 500 m, via the recorded request. Never create or edit one.
  - **Still wrong:** re-price at the app's location, add nothing, end `needs_confirmation`.
  - **"Fill at these prices":** a new fill pinned there (within 600 s). If the location moved again, `location_changed`.
- **R7 Limits and budget.** Fills have their own cap of 5 per IST day (`usage.cart_fills`) and do not consume cart
  checks.
  - Why not checks: a check was already consumed to produce the result, and charging again for acting on it would make
    "10 checks a day" mislead. Fills are bounded by checks anyway (R8).
  - A fill that added nothing is refunded.
  - At 100 % budget fills get 503 like checks: they restore a profile and use the user's residential proxy, the
    costliest cart job.
- **R8 One fill per check per app** (unique over `queued, filling, done, partial, interrupted`), and only within 30
  minutes of pricing, when Plan 5 starts saying "Prices may have changed".
- **R9 `fill_cart` jobs never requeue.** A started fill whose job died → `interrupted`; one never started → `failed`
  and refunded.
- **R10 Fill copy is built in Python** (`fill_summary`, `fill_options`).
- **R11 "Open <app> to pay" goes to `OPEN_APP_URLS[site]`**, home URLs that iOS universal links open in the app, where
  the account-bound cart is. There are no checkout or pay links. Fake: `https://fake.<site>.test/cart`.
- **R12 The button is on the cheapest card only**, per the mockup. The API accepts any `ok` card.
- **R13 Cart learning:**
  - Needs the admin's connected account (Swiggy for Instamart), the search recipe and the admin's address.
  - Its agent can't ask questions (`prompt` raises `TimeoutError`).
  - The probe (2 × "milk") stays in the admin's cart, and the job says so.
- **R14 Food compares (Plan 5 `mode='food'`) are not filled.** Their cards keep a disabled button with "Filling isn't
  available for food orders." and the API answers 422.
  - Why: Swiggy and Zomato food carts hold one restaurant. Adding a dish from another restaurant replaces the
    existing cart, which breaks "never empties the user's existing cart".
  - The brief's fill scope also names only Blinkit, Zepto and Instamart.

---

## Wave 0 (parallel; disjoint files)

### Task 1: Schema for fills

**Files:** Create `supabase/migrations/0009_fill_cart.sql` and `tests/server/test_schema_plan6.py`. Modify
`tests/server/conftest.py` (`TABLES` += `cart_fills`).

**Interfaces (Produces):**
- `cart_fills` columns:
  - `id uuid pk default gen_random_uuid()`, `user_id uuid not null`.
  - `request_id uuid not null references cart_requests on delete cascade`.
  - `site text not null check in ('zepto','blinkit','instamart')`, `job_id uuid`.
  - `status text not null default 'queued' check in ('queued','filling','done','partial','failed','needs_confirmation','interrupted')`.
  - `plan jsonb not null check (jsonb_typeof(plan)='array')`, `items jsonb not null default '[]'`.
  - `priced_location jsonb not null`, `confirm_location jsonb`, `app_location jsonb`.
  - `location_selected boolean not null default false`, `repriced jsonb`, `cart jsonb`, `summary jsonb`, `error text`.
  - `counted boolean not null default true`, `confirmed_from uuid references cart_fills(id) on delete set null`.
  - `open_url text`, `created_at timestamptz not null default now()`, `started_at timestamptz`, `finished_at timestamptz`.
- Unique index `one_fill_per_check_site on (request_id, site) where status in ('queued','filling','done','partial','interrupted')`.
  Index `cart_fills_user_created (user_id, created_at desc)`.
- `alter table usage add column cart_fills int not null default 0`.
- RLS:
  - Enabled, with policy `own_rows` for select (`user_id = auth.uid()`).
  - `revoke all … from anon, authenticated`; `grant select … to authenticated`; `grant all … to service_role`.
  - Added to `supabase_realtime` in 0001's guarded `do $$` style.

- [ ] **Step 1: Write the failing tests**

```python
def test_cart_fills_rls_own_rows(pg, as_user): ...          # A's fill visible to A, invisible to B
def test_users_cannot_write_cart_fills(pg, as_user): ...    # insert/update/delete as authenticated → InsufficientPrivilege
def test_one_fill_per_check_and_site(pg):
    ins(pg, status="done"); ins(pg, status="failed")         # failed does not hold the slot
    with pytest.raises(psycopg.errors.UniqueViolation): ins(pg, status="queued")
def test_needs_confirmation_does_not_hold_the_slot(pg): ...
def test_fill_status_and_site_checked(pg): ...               # site 'zomato' / status 'paid' → CheckViolation
def test_fills_cascade_with_request(pg): ...
def test_usage_has_cart_fills(pg): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_schema_plan6.py -v` → FAIL.
- [ ] **Step 3: Implement** the migration and the conftest change.
- [ ] **Step 4: Verify:**
  - `npx supabase db reset` applies 0001–0009.
  - `pg_publication_tables` for `supabase_realtime` lists `cart_fills`.
  - `pytest -q` is green with `TEST_DATABASE_URL` set and unset.
- [ ] **Step 5: Commit** — `git commit -m "feat(db): cart fills with one fill per check and app, RLS and realtime"`

### Task 2: Cart guard, cart recipe format and the cart click policy

**Files:**
- Create: `cart_guard.py`, `cart_recipe.py`, `tests/test_cart_guard.py`, `tests/test_cart_recipe.py`.
- Modify:
  - `browser.py`: `open_site(..., cart_policy=None)`.
  - `agent.py`: `cart_click_refusal`, and a `Discovery.refuse_click(el)` hook (default
    `click_refusal(el, self.login)`) used at both existing `click_refusal` call sites.

**Interfaces (Produces):**
- `cart_guard.FORBIDDEN: dict[str, re.Pattern]` (`re.I`, searched in the URL path+query and the body's
  `operationName`). Use these patterns verbatim:

```text
checkout         check-?out
payment          payment|(?<![a-z])pay(?![a-z])|pay-?now|razorpay|juspay|cashfree|(?<![a-z])upi(?![a-z])
order_placement  place-?order|placeorder|order/(create|confirm|place|submit)|(create|confirm|submit)[-_]?order
address_write    (add|create|new|save|update|edit|delete|remove)[-_]?(user[-_]?)?address|address(es)?/(add|create|new|save|update|edit|delete|remove)
coupon           coupon|promo|voucher|apply-?offer|offers?/apply
wallet           wallet|gift-?card|(?<![a-z])money(?![a-z])|cashback|super-?cash
cart_removal     (remove|delete|clear|empty|reset|decrement)[-_/]?(from[-_])?(cart|items?|products?)|cart/(remove|delete|clear|empty|reset|decrement)
```

- `ADDRESS_PAGE = re.compile(r"address", re.I)` applies to navigations only, as category `address_write`.
- `REMOVE_BODY` is searched in POST bodies, as category `cart_removal`:
  `"(quantity|qty|count|itemCount|item_count)"\s*:\s*"?(0|-\d+)"?\s*[,}\]]` or
  `"(action|op|operation|type|event|operationName)"\s*:\s*"[^"]*(remove|delete|clear|empty|decrement|reset)[^"]*"`.
- `operation_name(body) -> str | None`; `forbidden_category(url, body, is_nav) -> str | None`.
- `@dataclass(frozen=True) CartPolicy(cart: tuple[dict, ...] = (), location: tuple = (), read: tuple = ())`, with
  entries `{url_regex, why, op, operation?}`.
- `decide_cart(method, url, is_nav, body, policy) -> tuple[bool, str]` (R1 order). Reasons:
  - `"{METHOD} never allowed"`, `"forbidden: {category}"`, `"bot-challenge token"`, `"safe method"`.
  - `"cart add"`, `"location select"`, `"read POST"`, `"POST not on cart allowlist"`.
  - A body containing `mutation` passes a cart or location entry only when the entry's `operation` equals
    `operation_name(body)`, and never passes a read entry.
- `CartGuard(policy, block_media=False)`:
  - `install(context)`.
  - `blocked: list[dict]` (`{method, url, body[:4000], reason}`, kept in memory for the learner, never logged).
  - `media_blocked: int`, handled as Plan 4 `Guard` does.
  - `allow(op: "cart"|"location"|"read", entries) -> None`.
- `open_site(..., cart_policy=None)`: `login_mode and cart_policy` → `ValueError("cart mode cannot log in")`, raised
  before Chrome starts. With a policy it installs `CartGuard(cart_policy, block_media)` instead of `Guard`.
- Cart recipe (`recipes.kind='cart'`). `*_path` values are dotted JSON-body paths, or `query:<name>`:

```text
{kind: "cart", site, home_url,
 add: {method: "POST", url, headers, body, product_id_path, qty_path, store_id_path|null, qty_mode: "set"|"increment"},
 read: {method, url, headers, body|null, field_map: {lines_path, product_id, name, qty, price, subtotal, total,
        fees: {delivery?, handling?, small_cart?, surge?, other?}, out_of_stock|null}},
 location: {current: {method, url, headers, body|null, store_id_path|null, lat_path|null, lon_path|null, area_path|null},
            select: null | {mode: "latlon", method, url, headers, body, lat_input, lon_input}
                         | {mode: "address_id", method, url, headers, body, address_id_path,
                            saved: {method, url, headers, list_path, id, lat, lon, label}}},
 cart_allowlist: [{url_regex, why, op: "cart", operation?}], location_allowlist: [{…, op: "location"}],
 read_post_allowlist: [{url_regex, why}]}
```

- `cart_recipe` functions:
  - `add_value(qty_mode, before_qty, packs) -> int` (`ValueError` for `packs < 1` or an unknown mode).
  - `build_add_request(recipe, product_id, value, store_id) -> dict`, `build_read_request(recipe)`,
    `build_current_request(recipe)`, `build_location_request(recipe, *, lat=None, lon=None, address_id=None)`.
  - `extract_cart(data, field_map) -> {"lines": [{product_id, name, qty: int, price, out_of_stock: bool}], "subtotal", "total", "fees": {name: float}}`,
    which drops lines without an id or with qty ≤ 0.
  - `policy_for(recipe, search_recipe=None) -> CartPolicy`: read entries = the recipe's `read_post_allowlist` plus the
    search recipe's.
  - `narrow(url_regex, host) -> bool`: starts `^https://`, ends `$`, has no `.*` or `.+`, and matches neither
    `https://{host}/` nor `https://{host}/x`.
  - `check_cart_recipe(recipe, site) -> str | None` returns the first of these exact errors:
    - `"missing {key}"`.
    - `"add endpoint is {category}"`.
    - `"add request carries the whole cart"`: a body array with ≥2 objects having a `product|item|sku|variant` key.
    - `"unknown qty_mode"`.
    - `"add endpoint is not on the cart allowlist"`.
    - `"allowlist regex is too broad: {regex}"`.
    - `"allowlist entry for another site"`: via `agent.site_allows_host`; `fake.<site>.test` only with
      `WORKER_FAKE=1`.
    - `"cart recipe can't read the app's location"`: no `location.current`, or neither a store id path nor both
      lat/lon paths.
    - `"location select is not on the location allowlist"`.
- `agent.cart_click_refusal(el) -> str | None` refuses:
  - Text or labels matching
    `\b(check ?out|pay\w*|place (the )?order|proceed|confirm order|remove|delete|clear|empty|coupon|apply|wallet|log ?out|sign ?out|cancel\w*|reorder|buy again|(add|new|edit|save|change|update) (new )?address)\b`.
  - A bare minus `^\s*[-−–]\s*$`.
  - An `href` in any `FORBIDDEN` category.
  - Submit buttons are allowed, because "Add" often sits in a form.

- [ ] **Step 1: Write the failing tests**

```python
SHOP = "https://shop.test"
BROAD = CartPolicy(cart=({"url_regex": r"^https://shop\.test/.*$", "why": "x", "op": "cart"},))   # forbidden must still win
ADD = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart"},),
                 location=({"url_regex": r"^https://shop\.test/api/location/select$", "why": "loc", "op": "location"},),
                 read=({"url_regex": r"^https://shop\.test/api/cart/view$", "why": "read"},))
FORBIDDEN_URLS = [("checkout", "/checkout"), ("checkout", "/api/v2/check-out/init"), ("payment", "/api/pay/init"),
    ("payment", "/razorpay/order"), ("payment", "/api/upi/collect"), ("order_placement", "/api/place-order"),
    ("order_placement", "/v1/confirm_order"), ("address_write", "/api/address/add"), ("address_write", "/api/update-address"),
    ("coupon", "/api/coupon/apply"), ("wallet", "/api/swiggy-money/use"), ("cart_removal", "/api/clear-cart"),
    ("cart_removal", "/api/remove_item")]
@pytest.mark.parametrize("category,path", FORBIDDEN_URLS)
@pytest.mark.parametrize("method,is_nav", [("GET", True), ("POST", False)])
def test_forbidden_categories_blocked_even_when_allowlisted(category, path, method, is_nav):
    assert decide_cart(method, SHOP + path, is_nav, "{}", BROAD) == (False, f"forbidden: {category}")
@pytest.mark.parametrize("body,category", [
    ('{"operationName":"PlaceOrder","query":"mutation PlaceOrder{…}"}', "order_placement"),
    ('{"product_id":"p1","quantity":0}', "cart_removal"), ('{"items":[{"id":"p1","qty":-1}]}', "cart_removal"),
    ('{"action":"REMOVE_ITEM","id":"p1"}', "cart_removal"), ('{"operationName":"ApplyCoupon"}', "coupon")])
def test_forbidden_bodies_blocked_on_allowlisted_add(body, category):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, f"forbidden: {category}")
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_non_post_writes_never_allowed(method): ...           # on the add URL → "{method} never allowed"
def test_address_page_navigation_blocked_but_address_list_xhr_allowed(): ...
def test_allowed_requests():
    assert decide_cart("POST", SHOP + "/api/cart/add", False, '{"product_id":"p1","quantity":2}', ADD) == (True, "cart add")
    assert decide_cart("POST", SHOP + "/api/location/select", False, '{"lat":22.33}', ADD) == (True, "location select")
    assert decide_cart("POST", SHOP + "/api/cart/view", False, "{}", ADD) == (True, "read POST")
    assert decide_cart("GET", SHOP + "/v1/display/banners", True, None, ADD)[0]     # "display" is not "pay"
def test_unlisted_post_blocked(): ...
def test_mutation_needs_matching_operation(): ...
def test_challenge_post_allowed_and_lookalike_blocked(): ...
def test_cart_guard_records_blocked_without_logging_body(caplog): ...
def test_open_site_refuses_login_with_cart_policy(): ...
def test_open_site_installs_cart_guard(monkeypatch, tmp_path): ...
def test_sync_login_and_search_never_pass_cart_policy(monkeypatch): ...  # recorded open_site kwargs of sync.fetch_orders, sync.login, search.search_products
@pytest.mark.parametrize("text", ["Proceed to Checkout", "Pay ₹412", "Place Order", "Proceed", "Remove", "Clear cart", "−",
                                  "Add new address", "Edit address", "Apply coupon", "Use Zepto Cash wallet", "Delete"])
def test_cart_click_refusal_blocks(text): assert agent.cart_click_refusal({"text": text, "tag": "button"})
@pytest.mark.parametrize("text", ["Add", "ADD", "+", "Use current location", "Detect my location", "Home · 12 Shanti Society, Gorwa"])
def test_cart_click_refusal_allows(text): assert agent.cart_click_refusal({"text": text, "tag": "button", "type": "submit"}) is None
def test_discovery_refuse_click_hook_default_unchanged(): ...
# tests/test_cart_recipe.py
def test_add_value_set_mode_adds_on_top(): assert cart_recipe.add_value("set", 2, 1) == 3
def test_add_value_increment_and_bad_input(): ...             # ("increment", 2, 1) → 1; packs 0 → ValueError
def test_build_add_request_body_and_query_paths(): ...
def test_extract_cart_maps_lines_and_fees(): ...              # → 2 lines, subtotal 412.0, fees {"handling": 15.0}; qty-0 line dropped
def test_check_rejects_whole_cart_add(): ...
def test_check_rejects_forbidden_add_and_broad_regex(): ...   # /api/checkout/add → "add endpoint is checkout"
def test_check_requires_location_read(): ...
def test_check_rejects_other_site_host(): ...
def test_policy_for_merges_search_read_posts(): ...
```

- [ ] **Step 2: Run** `pytest tests/test_cart_guard.py tests/test_cart_recipe.py tests/test_guard.py -v` → the new tests FAIL; `test_guard.py` still passes.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): cart-mode guard with forbidden categories, cart recipe format and cart click policy"`

---

## Wave A (parallel after Wave 0 is merged; disjoint files)

### Task 3: Fill engine

**Files:** Create `cart_fill.py`, `tests/test_cart_fill.py` and `tests/fixtures/cart_recipe.json` (a valid synthetic
recipe for `shop.test` with `qty_mode` set). The test-local `FakeShop` routes `fetch_in_page` requests to an
in-memory cart, location and store.

**Interfaces:**
- Consumes: T2 `CartPolicy` / `cart_recipe.*` / `open_site(cart_policy=)`; `browser.fetch_in_page / goto /
  SessionExpired`; Plan 5 `search.build_request / resolve_store / extract_products`.
- Produces:
  - `CartError(Exception)`, `CartDrift(CartError)`.
  - `haversine_m(lat1, lon1, lat2, lon2) -> float`.
  - `location_matches(app, target, expected_store, max_m) -> bool`: store ids equal when both are set; else
    distance ≤ max_m when both lat/lons are set; else False.
  - `OUT_OF_STOCK_TEXT = re.compile(r"out of stock|sold out|unavailable|not available|no stock", re.I)`;
    `classify_add(text) -> "ok"|"out_of_stock"`.
  - `verify(before: dict[str, int], after: dict[str, int], sent: list[dict]) -> tuple[list[dict], bool]`:
    - Each sent item's `added_qty = after − before`: `added` when ≥ packs, `partial` when 0 < x < packs, `not_added`
      when 0, unless already `out_of_stock`.
    - The bool is `existing_changed`: True when any product with `before > 0` has `after < before`.
  - `outcome(status, items, cart, app_location=None, location_selected=False, repriced=None, error=None, existing_changed=False) -> dict`.
    This is the **fill outcome shape** T5, T9 and fake mode use:

```text
{status: "done"|"partial"|"failed"|"needs_confirmation", error, existing_changed,
 items: [{idx, label, name, product_id, packs, status: "added"|"partial"|"out_of_stock"|"not_added"|"skipped", added_qty,
          skip_reason: "you_skipped"|"no_answer"|"out_of_stock"|"not_found"|null}],
 cart: {subtotal, total, fees, line_count, existing_count} | null, app_location: {lat, lon, store_id, area} | null,
 location_selected, repriced: [{idx, name, product_id, old_price, new_price|null, in_stock}] | null}
```

  - `fill_cart(site, plan, priced, recipe, search_recipe, profile_dir, ask: Callable[[str, list[dict]], str | None], on_item: Callable[[dict], None], on_stage=lambda s: None, proxy_server=None, confirm_location=None, max_location_m=500) -> dict`:
    - Plan items (built by T5): `{idx, label, action: "add"|"ask"|"skip", product_id, name, packs, price, question, choices: [{id, label, product_id, name, packs, price}], reason}`.
      `priced = {lat, lon, area}`.
    - The algorithm, in order:
      1. `open_site(site, cart_policy=policy_for(recipe, search_recipe), profile_dir, location={"query": area, lat, lon} of the target, proxy_server, block_media=True)`,
         then `goto(home_url)`.
      2. `before` = quantities by product id from the cart read. A read failure → `CartDrift("cart read failed")`;
         `SessionExpired` propagates.
      3. `on_stage("checking_location")`. `target = confirm_location or priced`. `app` comes from the current-location
         request. `expected_store = search.resolve_store(page, search_recipe, target)` when the search recipe has a
         store binding, else None.
      4. No match with `confirm_location` set → `outcome("failed", [], None, app_location=app, error="location_changed")`.
      5. No match → `on_stage("selecting_location")` and select:
         - `latlon` sends the target lat/lon.
         - `address_id` picks the nearest saved address within `max_location_m`; with none, there is no select.
         - Re-read the location; a match now → `location_selected=True`.
      6. Still no match → `on_stage("repricing")`. For each `add` item, search its name with
         `search.build_request(search_recipe, name, app, store_id=app.store_id)`, taking the same product id (else the
         same name, case-insensitive). `old_price = packs × price`; `new_price = packs × new` or None. Return
         `outcome("needs_confirmation", [], None, app_location=app, repriced=…)` (`repriced=[]` without a search
         recipe). Nothing is added.
      7. `on_stage("adding")`. Items in plan order:
         - `skip` → `skipped` with its reason.
         - `ask` → `ask(question, choices)`. A choice id → add that choice. `"skip"` → `you_skipped`; None → `no_answer`;
           an unknown id → `you_skipped`.
         - `add` → send `build_add_request(recipe, pid, add_value(qty_mode, before + sent_so_far, packs), store_id)`.
           `classify_add` gives `out_of_stock`. Any other error on the first add, with nothing yet sent →
           `CartDrift("add failed")`; later → `not_added`.
         - `on_item(result)` after each item, and a random 0.8–1.6 s sleep between adds.
      8. `after` = cart read. `verify` gives the items and `existing_changed`. `cart` comes from the after read
         (`existing_count` = lines whose product had `before > 0`).
      9. Status: every item not skipped by the user `added` → `done`; ≥1 added or partial → `partial`; else `failed`
         with `nothing_added`.
    - It never calls `agent.click_ref` or `page.click`, and logs only the site and counts.

- [ ] **Step 1: Write the failing tests**

```python
def test_adds_planned_items_and_reports_cart(monkeypatch, shop): ...   # 2 adds → done, cart.total from read, on_item ×2
def test_set_mode_adds_on_top_of_existing_quantity(monkeypatch, shop):
    shop.cart = {"p1": 2}; out = run_fill(shop, plan=[add("p1", packs=1)])
    assert shop.requests_sent[-1]["qty"] == 3 and shop.cart["p1"] == 3 and out["items"][0]["status"] == "added"
def test_increment_mode_sends_packs(monkeypatch, shop): ...
def test_existing_lines_untouched(monkeypatch, shop):
    shop.cart = {"butter": 1}; out = run_fill(shop, plan=[add("p1", 2)])
    assert shop.cart["butter"] == 1 and out["cart"]["existing_count"] == 1 and out["existing_changed"] is False
def test_existing_line_dropping_is_reported(monkeypatch, shop): ...
def test_out_of_stock_at_fill_time(monkeypatch, shop): ...             # "Item is out of stock" → out_of_stock, status partial
def test_partial_quantity_cap(monkeypatch, shop): ...                  # cap 2, packs 3 → partial, added_qty 2
def test_ask_choice_adds_chosen_product(monkeypatch, shop): ...
def test_unanswered_question_skips_item_and_continues(monkeypatch, shop):
    out = run_fill(shop, plan=[ask_item(), add("p1", 1)], ask=lambda q, c: None)
    assert [i["status"] for i in out["items"]] == ["skipped", "added"] and out["items"][0]["skip_reason"] == "no_answer"
    assert out["status"] == "done"
def test_skip_items_reported_with_reason(monkeypatch, shop): ...
def test_location_selected_when_app_elsewhere(monkeypatch, shop): ...  # 3 km away, select works → location_selected, items added
def test_location_mismatch_reprices_and_adds_nothing(monkeypatch, shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}; shop.select_works = False
    out = run_fill(shop, plan=[add("p1", 2, price=29.0)])
    assert out["status"] == "needs_confirmation" and shop.add_calls == 0
    assert out["repriced"] == [{"idx": 0, "name": "Milk", "product_id": "p1", "old_price": 58.0, "new_price": 62.0, "in_stock": True}]
def test_confirm_location_moved_again_fails(monkeypatch, shop): ...    # error "location_changed", nothing added
def test_store_id_match_wins_over_distance(): ...
def test_saved_address_selected_only_within_500m(monkeypatch, shop): ...
def test_first_add_failure_is_drift(monkeypatch, shop): ...
def test_session_expired_propagates(monkeypatch, shop): ...
def test_fill_never_clicks(monkeypatch, shop): ...                     # agent.click_ref / page.click raise if called
def test_verify_rules():
    items, changed = cart_fill.verify({"a": 1}, {"a": 1, "p": 1}, [sent("p", packs=2)])
    assert (items[0]["status"], items[0]["added_qty"], changed) == ("partial", 1, False)
```

- [ ] **Step 2: Run** `pytest tests/test_cart_fill.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): add-only cart fill with location check, re-pricing and live questions"`

### Task 4: Supervised cart learning

**Files:** Create `cart_learn.py` and `tests/test_cart_learn.py`. The browser edges are faked as in Plan 5 T4.

**Interfaces:**
- Consumes: T2 `CartGuard`, `CartPolicy`, `check_cart_recipe`, `extract_cart`, `cart_click_refusal` and
  `refuse_click`; Plan 5 `search.extract_products / resolve_store` and `search_learn.find_location_inputs`;
  `agent.snapshot / click_ref`; `metering.count_openai`.
- Produces:
  - `CART_PROBE = "milk"`, `ADD_BUTTON = re.compile(r"^\s*(add|add to cart|\+)\s*$", re.I)`,
    `PLUS_BUTTON = re.compile(r"^\s*\+\s*$")`.
  - `CartLearnAgent(agent.Discovery)`:
    - `GOAL = "Open the delivery-location picker and choose 'Use current location' / 'Detect my location', or the saved address that matches {query}. Never add, edit, save or delete an address. Finish as soon as the location is set."`
    - `refuse_click` = `cart_click_refusal`, at most 15 steps.
  - `find_value_path(data, predicate, path="") -> str | None`.
  - `propose(blocked, needles, op, why) -> list[dict]`: blocked POSTs whose URL or body holds a needle →
    `{"url_regex": "^https://<host><path>$", why, op, operation?}`, deduplicated.
  - `qty_mode_from(first, second) -> str`: `(1, 2)` → `"set"`, `(1, 1)` → `"increment"`, else
    `sync.RecipeError("couldn't tell how the cart counts quantities")`.
  - `learn_cart(site, profile_dir, location, search_recipe, approve, preapproved=(), proxy_server=None, llm=None, runs_dir=None) -> {"recipe": dict, "probe_item": str}`
    never saves. Steps:
    1. `open_site(site, cart_policy=CartPolicy(...))`, with the preapproved cart, location and read entries plus the
       search read allowlist. `goto(home_url)`.
    2. **Location.** Run `CartLearnAgent(..., prompt=_no_prompt)`, where `_no_prompt` raises `TimeoutError`.
       - Propose `op="location"` entries from `guard.blocked`, with needles = lat/lon at 3 and 4 decimals.
       - If there are any: `approve(entries)`, with False → `sync.RecipeError("location permission rejected")`.
         Otherwise `guard.allow`, then run the agent once more.
       - `location.select` comes from the now-allowed captured request: `latlon` via `find_location_inputs`, or
         `address_id` when the body holds an id from a captured saved-address list. With no POST, `select = None`.
       - `location.current` = the latest captured GET or allowed read response holding the store id (`resolve_store`
         at `location`) or the lat/lon within 0.001. If there is none →
         `sync.RecipeError("cart recipe can't read the app's location")`.
    3. **Add.** `goto(search_page_url.format(q=CART_PROBE))`. Product ids come from the captured search responses.
       - Click the first element matching `ADD_BUTTON` that `cart_click_refusal` allows. None →
         `sync.RecipeError("no Add button found")`.
       - Propose `op="cart"` entries with the product ids as needles. `approve` False →
         `sync.RecipeError("cart permission rejected")`.
       - Allow them, click Add again, then click the first allowed `PLUS_BUTTON`.
       - From the two add requests: `product_id_path` = the path equal to the id; `qty_path` = the numeric path (1 in
         the first request); `qty_mode = qty_mode_from(...)`; `store_id_path` = the path equal to the store id, or
         None.
    4. **Read.** The latest response holding the product id with quantity 2.
       - If it is a POST, propose an `op="read"` entry through `approve` (False →
         `sync.RecipeError("cart read permission rejected")`).
       - The LLM writes `read.field_map` from its sample. It is accepted when `extract_cart` yields the probe line with
         `qty == 2` and `subtotal > 0`. Otherwise the error goes back, up to 3 tries, each via `count_openai`.
    5. `check_cart_recipe(recipe, site)` error → `sync.RecipeError(error)`. Return the recipe and the probe's name.

- [ ] **Step 1: Write the failing tests**

```python
def test_learn_records_add_location_and_read(monkeypatch): ...  # scripted page/guard/capture → passes check_cart_recipe, qty_mode "set"
def test_add_permission_needs_approval(monkeypatch): ...        # approve False → RecipeError; guard cart allowlist unchanged
def test_location_permission_needs_approval(monkeypatch): ...
def test_preapproved_entries_not_asked_again(monkeypatch): ...
def test_qty_mode_from_rules(): ...                              # (1,2) set, (1,1) increment, (1,5) RecipeError
def test_learner_never_clicks_refused_elements(monkeypatch): ... # "Proceed to Checkout" listed first, then "ADD" → only ADD clicked
def test_agent_uses_cart_click_policy(): ...
def test_agent_question_fails_learn(monkeypatch): ...            # ask_user → note "ask_timeout"; input() never called
def test_missing_location_read_rejected(monkeypatch): ...
def test_whole_cart_add_rejected(monkeypatch): ...
def test_read_field_map_retries_with_feedback(monkeypatch): ...  # llm stub bad then good → 2 calls
def test_propose_dedupes_and_anchors(): ...
```

- [ ] **Step 2: Run** `pytest tests/test_cart_learn.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): supervised cart-recipe learning with live permission approvals"`

### Task 5: Fill store, plan, result copy, limits and config

**Files:**
- Create: `server/fill.py`, `server/fill_summary.py`, `tests/server/test_fill_store.py`,
  `tests/server/test_fill_summary.py`.
- Modify:
  - `server/limits.py` and `server/usage.py`.
  - `server/config.py`: Settings `cart_fills_per_day: int = 5`, `cart_fill_max_age_s: int = 1800`,
    `cart_location_max_m: float = 500`, `fill_confirm_window_s: int = 600`, plus `OPEN_APP_URLS`.
  - `docker-compose.yml`, `tests/server/test_budget_limits.py`, `tests/server/test_config.py`.

**Interfaces:**
- Consumes: T1; T3's outcome shape (contract only); Plan 5 `cart.get_request` and `cart_rank.inr`; Plan 4 `limits`,
  `usage`, `budget.is_paused` and `config.SITE_NAMES`; `jobs.enqueue`; `browser.SITES` (`db_site`).
- Produces, in limits and usage:
  - `usage.record(..., cart_fills=0)`.
  - `limits.cart_fills_left(conn, user_id, now, s) -> int`.
  - `limits.consume_cart_fill(conn, user_id, now, s) -> int`, atomic like `consume_cart_check`. It raises
    `BudgetPaused("fill", <1st of next month>, "Filling carts is paused until 1 November.")` or
    `LimitExceeded("fill", 5, retry, "You can fill 5 carts a day. Try again tomorrow.")`.
  - `limits.refund_cart_fill(conn, user_id, day) -> None`.
- Produces (`server/fill.py`). Copy uses `App = SITE_NAMES[site]` and `Conn` = "Swiggy" for Instamart, else `App`.
  - Exceptions: `NotFound`, `NotReady`, `FoodNotFillable`, `NoPrices`, `Unavailable`, `NeedsConnection(message)`,
    `Stale`, `AlreadyFilled`, `NothingToAdd`, `FillRunning`, `NotConfirmable`.
  - `db_site(site) -> str`; `cart_recipe_ready(conn, site) -> bool` (a cart recipe that is healthy and not paused).
  - `plan_items(request, result, site) -> list[dict]` (pure). Each request item's cell, from `result.items` and
    `request.choices`:
    - chosen or `matched` → `add`.
    - `swapped` → `ask`. Question `f"{swap_from} is out of stock on {App}. Add {name} ({price_label}) instead?"`;
      choices `f"Add {name} · {price_label}"` and `{"id": "skip", "label": "Skip this item"}`.
    - `pick` → `ask`. Question `f"Which {label} should we add?"`; choices are the candidates (≤5) labelled
      `f"{name} · {size_label} · {price_label}"` (plus `×{packs}` when > 1), then skip.
    - `out_of_stock` / `not_found` → `skip` with that reason.
    - `price_label = cart_rank.inr(packs × price)`.
  - `fill_options(conn, user_id, request_id, now, s) -> {fills_left, fills_limit, sites: {site: {state, label, sublabel, href, fill_id, preview: {add, ask, skip: [labels]} | null}}}`
    covers each `ok` card. A food request gives every card `unavailable` with
    `f"Fill cart on {App}"` + "Filling isn't available for food orders." (R14). States in precedence order, with
    exact copy:
    1. `filling`: "Filling your cart…".
    2. `review` (a `needs_confirmation` fill inside the window, and the newest): "See new prices".
    3. `filled` (done, partial or interrupted): "See what was added".
    4. `unavailable`: `f"Fill cart on {App}"` + `f"Not available for {App} yet"`.
    5. `paused`: `f"Fill cart on {App}"` + the `BudgetPaused` message.
    6. `connect`: `f"Connect {Conn} to fill"`, href `/accounts`.
    7. `needs_login`: `f"Reconnect {Conn} to fill"`, href `/accounts`.
    8. `stale`: `f"Fill cart on {App}"` + "Prices are over 30 minutes old. Check again first."
    9. `limit`: `f"Fill cart on {App}"` + "No fills left today".
    10. `ready`: `f"Fill cart on {App}"` + `f"{n} fills left today"` ("1 fill left today"), with `preview`.
  - `start_fill(conn, user_id, request_id, site, now, s) -> {fill_id, job_id, fills_left}`. Checks in order:
    1. Owner (`NotFound`); `done` (`NotReady`); `mode='grocery'` (`FoodNotFillable`); an `ok` card (`NoPrices`);
       `cart_recipe_ready` (`Unavailable`).
    2. Connection `connected`, else `NeedsConnection` with:
       - `f"Connect {Conn} first."`; Instamart: "Connect Swiggy first. Instamart uses your Swiggy login."
       - For `needs_login`: `f"{Conn} logged you out. Reconnect it first."`.
    3. `priced_at` age ≤ `cart_fill_max_age_s` (`Stale`); no fill holding the slot (`AlreadyFilled`); ≥1 add or ask
       (`NothingToAdd`).
    4. Then one `conn.transaction()`:
       - `consume_cart_fill`.
       - Insert the fill with `plan` and `priced_location={lat, lon, area}`.
       - `jobs.enqueue(conn, user_id, "fill_cart", site, {"fill_id": id})` with the default profile, so Instamart
         locks `swiggy`. Set `job_id`.
       - `jobs.Conflict` → `FillRunning`; UniqueViolation `one_fill_per_check_site` → `AlreadyFilled`. Both roll back
         the consume.
  - `confirm_fill(conn, user_id, fill_id, now, s)` returns the same shape:
    - The fill must be owned, `needs_confirmation`, finished within `fill_confirm_window_s`, and the newest for its
      (request, site). Otherwise `NotConfirmable`.
    - Then the recipe, connection and limit checks (staleness is not re-checked).
    - It inserts a fill with `confirm_location = app_location`, `confirmed_from` and the same plan.
  - `get_fill`.
  - `mark_started(conn, fill_id)` sets `filling` and `started_at`, with items = the plan as `pending` rows.
  - `record_item(conn, fill_id, item)` replaces by `idx`.
  - `finish_fill(conn, fill_id, outcome, open_url, s) -> dict` writes the outcome, `finished_at`, `open_url` and
    `summary = fill_summary.build(...)`. When nothing was added or partial, it refunds (IST day of `created_at`) and
    sets `counted=false`. It returns the summary.
  - `fail_fill(conn, fill_id, error, s)` = `finish_fill` with a failed outcome over the current items.
  - `finalize_lost(conn, s, now) -> int` covers `queued`/`filling` fills whose job is `failed` or missing. A started
    fill → `interrupted`, not refunded. A queued fill → `failed` / `worker_lost`, refunded.
- Produces (`server/fill_summary.py`): `build(fill, area, open_url) -> {status, title, tone: "success"|"warning"|"error", sections: [{key, heading, rows}], cart_line, existing_line, warning_line, location_line, reprice: {rows: [{label, old, new}], total_line} | null, error_line, open: {label, url} | null, pay_note, confirm_label, counted}`.
  Exact copy (`a` = items added or partial; `t` = items minus the user's own skips):
  - **Titles:**
    - done: `f"Added {a} items to your {App} cart"` ("1 item").
    - partial: `f"Added {a} of {t} items to your {App} cart"`.
    - interrupted: `f"Your {App} cart may be half filled"`.
    - failed: `f"Couldn't fill your {App} cart"`.
    - needs_confirmation: `f"{App} is set to a different location"`.
  - **Sections**, empty ones omitted:
    - "Added": `f"{name} ×{qty}"`, with ×1 omitted.
    - "Only some added": `f"{name}: {added} of {packs}"`.
    - "Out of stock now".
    - "Not added".
    - "Skipped": `f"{label} (you skipped it)"`, `(no answer)`, `(out of stock when we checked)` or `(not found)`.
  - `cart_line`: `f"{App} cart total {inr(total)} · items {inr(subtotal)} + fees {inr(total − subtotal)}"`.
  - `existing_line`: "Includes 1 item that was already in your cart." / `f"Includes {k} items that were already in your cart."`
  - `warning_line` (existing_changed): `f"Something already in your {App} cart changed while we were adding. Please check your cart."`
  - `location_line`: `f"We set {App}'s delivery location to {area}."` when `location_selected`.
  - **Reprice:**
    - Rows `{label: name, old, new | "Not available"}`.
    - `total_line`: `f"Items there: {inr(new_sum)} instead of {inr(old_sum)}"`.
    - `location_line`: `f"{App} delivers to {app_area or 'another area'} right now, not {area}."`
    - `confirm_label`: "Fill at these prices".
    - An empty `repriced` → `total_line` `f"We couldn't get {App}'s prices there."` and no `confirm_label`.
  - **`error_line`.** `N` is "Nothing was added." when `a = 0`, else "Some items were added before it stopped.":
    - `needs_login`: `f"{Conn} logged you out. Reconnect it, then try again. {N}"`.
    - `recipe_drift`: `f"Filling {App} carts stopped working. We've told the team. {N}"`.
    - `location_changed`: `f"{App}'s location changed again. Nothing was added."`
    - `nothing_added`: `f"{App} didn't accept any of the items. Nothing was added."`
    - interrupted / `worker_lost`: `f"We lost track of this fill. Check your {App} cart before filling again."`
    - Anything else: `f"Something went wrong. {N}"`.
  - **`open`:** `{label: f"Open {App} to pay", url: open_url}` when `a > 0` or the fill is interrupted.
  - **`pay_note`:** `f"We never place the order or pay. Review your cart in {App} and pay there."`

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_items_from_result(): ...       # matched → add (packs 2); swapped → ask, exact question, [swap, skip]; pick → candidates; out_of_stock → skip
def test_plan_uses_choice(): ...
def test_start_fill_consumes_one_and_enqueues(pg): ...   # job fill_cart, payload fill_id, profile "zepto"; usage.cart_fills == 1
def test_instamart_fill_locks_swiggy_profile_and_needs_swiggy(pg): ...
def test_second_fill_same_check_is_already_filled_and_not_counted(pg):
    fill.start_fill(pg, A, rid, "zepto", NOW, s)
    with pytest.raises(fill.AlreadyFilled): fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 1
def test_stale_prices_refused(pg): ...       # priced 31 min ago
def test_food_request_not_fillable(pg): ...  # start_fill → FoodNotFillable; fill_options cards unavailable with the food sublabel
def test_unavailable_without_approved_cart_recipe(pg): ...
def test_sixth_fill_is_limit(pg): ...        # exact message
def test_fill_paused_budget(pg): ...
def test_finish_refunds_when_nothing_added(pg): ...
def test_confirm_creates_pinned_fill(pg): ...
def test_confirm_after_window_refused(pg): ...
def test_fill_options_states_and_copy(pg): ...           # every state's exact label/sublabel/href and the precedence order
def test_finalize_lost_started_fill_is_interrupted(pg): ...
def test_finalize_lost_queued_fill_is_failed_and_refunded(pg): ...
def test_record_item_replaces_by_idx(pg): ...
# test_fill_summary.py
def test_done_title_sections_and_cart_line():
    s = fill_summary.build(F(status="done", items=[added("Amul Taaza Toned Milk 1 L", 1), added("Amul Taza Milk 500ml", 2)],
                             cart={"subtotal": 465, "total": 480, "fees": {"handling": 15}, "line_count": 6, "existing_count": 1}),
                           "Gorwa, Vadodara", "https://www.zepto.com/")
    assert s["title"] == "Added 2 items to your Zepto cart"
    assert s["sections"][0]["rows"] == ["Amul Taaza Toned Milk 1 L", "Amul Taza Milk 500ml ×2"]
    assert s["cart_line"] == "Zepto cart total ₹480 · items ₹465 + fees ₹15"
    assert s["existing_line"] == "Includes 1 item that was already in your cart."
    assert s["open"] == {"label": "Open Zepto to pay", "url": "https://www.zepto.com/"}
def test_partial_counts_and_skip_reasons(): ...  # "Added 5 of 6 items to your Zepto cart"; "Out of stock now" ["Farm Eggs 6 pcs"]
def test_reprice_copy(): ...                     # "Items there: ₹293 instead of ₹285", confirm_label, location_line
def test_error_lines(): ...                      # every code, with and without items added
def test_instamart_needs_login_names_swiggy(): ...
def test_settings_fill_tunables_from_env(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_fill_store.py tests/server/test_fill_summary.py tests/server/test_budget_limits.py tests/server/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green; `docker compose config -q` → 0.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): cart fills store, fill plan, result copy and daily fill limit"`

### Task 6: PWA — Fill cart button, live fill sheet and result

**Files:**
- Create: `web/lib/fill.ts`, `web/components/cart/{FillButton,FillConfirmSheet,FillSheet,FillQuestion,FillResult,RepriceNotice}.tsx`,
  `web/__tests__/fill.test.tsx`.
- Modify:
  - `web/components/cart/AppCard.tsx`: `FillButton` replaces the disabled "Coming soon" button.
  - `web/app/(app)/cart/[id]/page.tsx`: loads the fill options; `?fill=<id>` opens `FillSheet`.
  - `web/__tests__/cart-result.test.tsx`: Plan 5's `fill button is disabled with coming soon and calls nothing`
    becomes `fill button renders the server state`.

**Interfaces:**
- Consumes (contract only):
  - `GET /cart/checks/{id}/fill` (T5 `fill_options`).
  - `POST /cart/checks/{id}/fill {site}` and `POST /cart/fills/{id}/confirm` → 202 `{fill_id, job_id, fills_left}`;
    errors as `{detail}`.
  - `POST /jobs/{id}/answer {answer}`.
  - The `cart_fills` row with its T5 `summary`.
  - Job progress `stage ∈ {checking_location, selecting_location, repricing, adding, question_needed, answer_received}`,
    plus `question`, `choices: [{id, label}]`, `done`, `total`.
- Produces:
  - `lib/fill.ts`: the types `FillOptions`, `FillOption`, `FillPlanItem`, `FillItem`, `FillSummary`, `CartFill`
    (mirroring T5), and `getFillOptions`, `startFill`, `confirmFill`, `getFill`, `watchFill(id, onChange)` (Realtime
    `cart_fills` with `id=eq.<id>`; returns an unsubscribe function) and `answerFill(jobId, answer)`.
  - **`FillButton`:**
    - `ready`: primary, full width, with the sublabel.
    - `connect` / `needs_login`: a secondary link to `href`.
    - `unavailable` / `paused` / `stale` / `limit`: the primary style, `disabled` with `aria-disabled="true"`, plus
      the sublabel.
    - `filling` / `filled` / `review`: secondary, opening `FillSheet`.
  - **`FillConfirmSheet`:**
    - Title `"Fill your {App} cart?"`.
    - Lines:
      - `"We'll add {n} items to your {App} cart."` (n = add + ask; "1 item").
      - `"Items already in your cart stay there."`
      - When ask > 0: `"{k} items need your choice while we fill."` ("1 item needs").
      - When skips exist: `"Can't be added: {labels}."`
      - `"You pay in {App}. We never place the order."`
    - Primary `"Add {n} items"` → `startFill`. An error `detail` shows verbatim in the sheet; success opens
      `FillSheet`.
    - "Cancel". Footer: the sublabel.
  - **`FillSheet`:**
    - **Progress:**
      - Title `"Filling your {App} cart…"`.
      - Stage line: `"Checking {App}'s delivery location…"` / `"Setting {App}'s location to {area}…"` /
        `"Checking prices at {App}'s location…"` / `"Adding items · {done} of {total}"`.
      - One row per item: "Waiting", "Adding…", "Added", "Added ×{n}", "Only {x} of {packs} added", "Out of stock",
        "Not added", "Skipped".
      - Footnote "You can leave this screen. We'll notify you when it's done."
    - **`FillQuestion`** (stage `question_needed`):
      - The question, and the choices as 44px radio rows.
      - "Add this" (disabled until a pick) → `answerFill(jobId, id)`; "Skip item" → `answerFill(jobId, "skip")`.
      - A countdown `"{s}s left"` from 180; at 0, "Skipped — no answer".
    - **Result:**
      - The title in its tone colour (success accent, warning `#c2410c`, error `#b91c1c`), then the sections,
        `cart_line`, `existing_line`, `warning_line`, `location_line` and `error_line`.
      - The `open` link as a primary `<a target="_blank" rel="noopener noreferrer">`, then the muted `pay_note`.
      - "Done". A `needs_login` failure adds "Reconnect" → `/accounts`.
    - **`RepriceNotice`** (needs_confirmation):
      - The title, `location_line`, and rows `label · old → new` with `total_line`.
      - The primary `confirm_label` → `confirmFill`, which switches the sheet to the new fill. "Not now".
      - Hint `"Or change the location in {App}, then check again."`
    - It follows `watchFill` and `watchJob`, and refreshes the options after each change.

- [ ] **Step 1:** Invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`. Then
  write the failing tests:
  - `fill button renders the server state`
  - `confirm sheet shows counts and keeps-your-cart copy`
  - `confirm posts and opens the live sheet`
  - `409 detail is shown in the confirm sheet`
  - `progress rows follow realtime item updates`
  - `question shows choices and answers with the choice id`
  - `skip item answers skip`
  - `result renders sections, cart line and open link`
  - `open link never mentions checkout and has rel noopener`
  - `reprice notice confirms a pinned fill`
  - `needs_login failure offers reconnect`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm --dir web test`, `lint` and `build` → pass. At 375×812 and 390×844, the open sheet (8
  rows plus a question) causes no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): fill cart button, live fill sheet with questions and pay-in-app result"`

---

## Wave B (after Wave A is merged; parallel, disjoint files)

### Task 7: API — fill routes, answers, limits and admin cart learning

**Files:**
- Modify: `server/cart_api.py`, `server/api.py`, `server/admin_api.py`, `server/admin.py`.
- `tests/server/test_cart_api.py`: Plan 5's `test_no_fill_cart_route_or_job` is removed (superseded).
- Create: `tests/server/test_fill_api.py`.

**Interfaces:**
- Consumes: T5 `fill.*` and `limits.to_http`; Plan 4 `require_admin`; `store.get_recipe`.
- Produces:
  - `GET /cart/checks/{id}/fill` → 200 `fill_options` (404 when not the owner; 409 `"Prices are still loading."`).
  - `POST /cart/checks/{id}/fill` with body `{site}` → 202 `{fill_id, job_id, fills_left}`. Errors:
    - 404 `"not found"`.
    - 409 `"Prices are still loading."`
    - 422 `"Filling isn't available for food orders."`
    - 422 `"{App} has no prices in this check."`
    - 503 `"Filling carts on {App} isn't available right now."`
    - 409 with the `NeedsConnection` message.
    - 409 `"These prices are over 30 minutes old. Check again first."`
    - 409 `"This cart was already filled on {App}."`
    - 409 `"Nothing to add on {App}."`
    - 409 `"A cart is already being filled."`
    - `limits.to_http` for the 429 and 503.
  - `POST /cart/fills/{id}/confirm` → 202, same shape. Errors: 404; 409 `"These prices have expired. Check again."`;
    the same recipe, connection and limit errors.
  - `POST /jobs/{id}/answer` also accepts running `fill_cart` jobs at stage `question_needed`.
  - `GET /me/limits` gains `cart_fills_left` and `cart_fills_limit`.
  - `/admin/costs`: each user gains `cart_fills`.
  - `POST /admin/learn/{site}?kind=cart`:
    - The site must be in `SEARCH_SITES` (else 400).
    - 409 `"Connect {Conn} on your own account first."` unless the admin's connection for `db_site` is `connected`.
    - 409 `"Learn {App} prices (search) first."` without a healthy search recipe.
    - 409 `"Add your delivery address first."`
    - It enqueues `learn` for the admin with `{"reason": "manual", "kind": "cart"}` and the default profile. Conflict
      → 409 `"A learn is already running."`
  - `admin.site_health` adds `kind:"cart"` rows for `SEARCH_SITES`, each with:
    - 7-day `fill_cart` outcomes in `syncs_ok_7d` / `syncs_failed_7d`.
    - `needs_account: true`, and `admin_connected` for `db_site`.
    - `pending_approval` (a pending cart `recipe_approvals` row exists).

- [ ] **Step 1: Write the failing tests**

```python
def test_fill_flow_returns_202_and_job(client, token_a, pg): ...
def test_double_fill_is_409(client, token_a, pg): ...                 # exact detail; usage.cart_fills == 1
def test_fill_scoped_to_owner(client, token_b, pg): ...               # check and confirm → 404 for B
def test_fill_errors_exact_details(client, token_a, pg): ...
def test_fill_limit_429_and_budget_503(client, token_a, pg): ...
def test_fill_options_endpoint(client, token_a, pg): ...
def test_confirm_endpoint(client, token_a, pg): ...
def test_answer_reaches_fill_job(client, token_a, pg, pg2): ...       # → 204 + NOTIFY on otp_<id>
def test_answer_refused_for_sync_job(client, token_a, pg): ...        # → 404
def test_me_limits_has_fills(client, token_a): ...
def test_admin_learn_cart_requires_connection_search_and_address(client, admin_token, pg): ...
def test_admin_learn_cart_instamart_needs_swiggy(client, admin_token, pg): ...
def test_admin_sites_lists_cart_rows(client, admin_token, pg): ...
def test_only_tapped_routes_create_fill_jobs(client, pg): ...         # every route that can insert a fill_cart job is one of the two fill routes
```

- [ ] **Step 2: Run** `pytest tests/server/test_fill_api.py tests/server/test_cart_api.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): fill cart routes, fill answers, fill limits and admin cart learning"`

### Task 8: PWA — admin cart learning and cart approvals

**Files:** Create `web/components/admin/CartPermissions.tsx` and `web/__tests__/admin-cart.test.tsx`. Modify
`web/components/admin/{SitesPanel,ApprovalsPanel,CostsPanel}.tsx` and `web/lib/admin.ts`.

**Interfaces:**
- Consumes: T7's `/admin/sites` cart rows, `POST /admin/learn/{site}?kind=cart` and `/admin/costs` `cart_fills`; Plan
  4's approvals routes.
- Produces:
  - **`SitesPanel`**, group "Cart filling", one row per grocery app:
    - "Learn" is disabled with `"Connect {Conn} on your own account first"` unless `admin_connected`. 409 details are
      shown verbatim.
    - A running learn shows its stage. `approval_needed` lists each regex with its op label ("Add to cart",
      "Select location", "Read cart") and Approve / Reject.
    - When the job ends `learned`/`pending`, it shows its `note` and "Waiting for your approval in Approvals".
    - Rows with `pending_approval` get a "Waiting for approval" badge.
  - **`ApprovalsPanel`**: a `kind:"cart"` card renders `CartPermissions` above `RecipeDiff`:
    - Heading "Cart permissions".
    - The `cart_allowlist` and `location_allowlist` regexes with their `why`.
    - `"Approving lets the worker add items to users' carts on {App}. It can never check out, pay or change addresses."`
    - "Quantity: set" / "Quantity: add".
  - **`CostsPanel`** shows "fills" per user.

- [ ] **Step 1:** Invoke the three design skills. Then write the failing tests:
  - `cart rows learn only with a connection`
  - `cart learn approval lists op labels and approves`
  - `cart approval shows permissions and the never-checkout line`
  - `pending badge on cart row`
  - `costs show fills`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass. `/admin` Sites and Approvals at 390×844 have no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): admin cart learning and cart recipe approvals"`

### Task 9: Worker — fill jobs, cart learning, fake fill and lost-fill cleanup

**Files:** Modify `server/worker.py`, `server/jobs.py`, `server/fake_sites.py` and `server/scheduler.py`. Tests:
`tests/server/test_worker_plan6.py`, `tests/server/test_jobs.py`, `tests/server/test_fake_sites.py`,
`tests/server/test_scheduler.py`.

**Interfaces:**
- Consumes: T3 `fill_cart / CartDrift / outcome / verify`; T4 `learn_cart`; T5 `fill.*` and `OPEN_APP_URLS`; Plan 4
  `recipe_admin.*` and `notify.*`; Plan 5's learn branching on `payload.kind`.
- Produces:
  - `JOB_TYPES = ("connect", "sync", "learn", "search", "fill_cart")`.
  - `make_prompt`'s callable becomes `prompt(text, kind="otp", choices=None)`, with `choices` written to progress next
    to `question`. Existing callers are unchanged.
  - **The fill job:** `_do_fill(job, conn, s, profile_dir, proxy_server, listen_conn)`. It runs inside `run_job`'s
    metered flow, restores the user's vault profile (`VaultError` → `needs_login`, as sync does), uses the user's
    sticky proxy for `db_site`, and never logs in. Steps:
    1. The fill is missing → job failed `fill_gone`.
    2. The cart recipe is missing, unhealthy or paused → `fail_fill("cart_unavailable")`.
    3. The connection isn't `connected` → `fail_fill("needs_login")`.
    4. `mark_started`. `ask(q, choices)` = the prompt with `kind="question"`; `sync.OtpTimeout` → None.
    5. `on_item` → `record_item` + progress `stage="adding", done, total`; `on_stage` → progress `stage`.
    6. `outcome = (fake_sites.fill_cart if fake else cart_fill.fill_cart)(site, fill.plan, fill.priced_location, recipe, search_recipe, profile_dir=…, ask=…, on_item=…, on_stage=…, proxy_server=…, confirm_location=fill.confirm_location, max_location_m=s.cart_location_max_m)`.
       Fake mode adds `user_id` and `state_dir=os.environ["FAKE_STATE_DIR"]`.
    7. `finish_fill(..., open_url=OPEN_APP_URLS[site])` (fake: `f"https://fake.{site}.test/cart"`), then
       `save_profile()`.
    8. Notify:
       - done/partial → `notify.enqueue(kind="cart_filled", title=summary.title, body=f"Open {App} to review and pay.", url=f"/cart/{request_id}?fill={fill_id}", user_id=…, dedupe_key=f"cart_filled:{fill_id}")`.
       - needs_confirmation → `kind="cart_reprice"`, title `f"Check {App} prices"`, body `reprice.total_line`.
       - Then `dispatch` best-effort.
    9. Errors:
       - `SessionExpired` → `fail_fill("needs_login")`, connection `needs_login` and Plan 4's needs_login
         notification.
       - `CartDrift` → `fail_fill("recipe_drift")`. On a `mark_drift(conn, site, "cart", note)` transition:
         `pause_site(..., "cart", ...)` + `alert_admins(kind="site_paused", title=f"{App} cart filling paused", body="Its cart recipe stopped working. Learn it again from the admin view and approve it.", dedupe_key=f"site_paused:cart:{site}:{today}")`.
       - Anything else → `fail_fill(_clean_error(e, None))`.
       - Every exit leaves the row finished.
  - **Learn with `payload.kind == "cart"`:**
    - It requires an admin, the admin's `connected` connection for `db_site` (`needs_login`), a search recipe
      (`no_search_recipe`) and an address (`no_address`).
    - It restores the admin's profile and uses their proxy.
    - `approve = make_approver(listen_conn, conn, job.id, site, "cart", s.approval_timeout_s)`; `preapproved` = the
      current cart recipe's regexes.
    - It calls `learn_cart`, then `apply_learned(conn, site, "cart", recipe, approve.approved, job.id, reason="learned")`
      (always pending).
    - Progress: `stage="learned", status, approval_id, note=f"Learning added 2 × {probe_item} to your {App} cart. Remove it in {App} if you don't want it."`
    - It saves the admin's profile. A cart learn is never enqueued automatically.
  - `jobs.requeue_stale` and `_abort_on_shutdown` treat `fill_cart` like `connect` and `learn` (failed
    `worker_lost` / `worker_shutdown`).
  - `scheduler.tick` calls `fill.finalize_lost` after `finalize_stale`; its result gains `"fills_finalized"`.
  - **Fake mode:**
    - `fake_sites.learn(site, approve, preapproved=(), kind="cart")` returns a valid recipe:
      - Add on `https://fake.<site>.test/api/cart/add` with `qty_mode="set"`; current location on `…/api/location`;
        `latlon` select on `…/api/location/select`.
      - It calls `approve([add, location])` unless preapproved; a rejection raises
        `RecipeError("cart permission rejected")`.
      - `probe_item = "Amul Taaza Toned Milk 1 L"`.
    - `fake_sites.fill_cart(site, plan, priced, recipe, search_recipe, *, ask, on_item, on_stage, user_id, state_dir, confirm_location=None, max_location_m=500, **_)`
      returns T3's outcome via `cart_fill.verify` / `outcome`. State is in `state_dir/<user_id>-<site>.json`:
      `{cart: {product_id: {name, qty}}, location: {lat, lon, area}, locked}`.
      - **First touch:** the cart holds `{f"{site}-amul-butter-100g": {"name": "Amul Butter 100g", "qty": 1}}`; the
        location is `priced`; unlocked.
      - **Location:** within range → match. Unlocked → move and set `location_selected`. Locked → `needs_confirmation`
        with `round_half_up(base × site_factor × 1.03)` per pack. `confirm_location` with no match → `location_changed`.
      - **Prices:** Plan 5's site price, ×1.03 at a `confirm_location`.
      - **Fill-time stock:** `FILL_OUT_OF_STOCK = {"zepto": {"Farm Eggs 6 pcs"}, "blinkit": set(), "instamart": set()}`;
        `FAKE_MAX_QTY = 5`.
      - **Fees:** `FAKE_FEES` (the Plan 5 grocery seeds) through `fees.compute(rules, subtotal, True, {"member": False})`.
      - No `FAKE_STATE_DIR` → `RuntimeError("FAKE_STATE_DIR is not set")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_fill_job_runs_engine_and_finishes(pg, monkeypatch): ...      # stub outcome done → row done + summary title, job done, profile saved
def test_fill_job_uses_users_profile_and_sticky_proxy(pg, monkeypatch): ...  # instamart → vault (user, "swiggy"); session from connections
def test_fill_job_never_logs_in(pg, monkeypatch): ...
def test_question_timeout_is_skip_not_failure(pg, monkeypatch): ...   # prompt raises OtpTimeout → ask returns None
def test_question_progress_carries_choices(pg, monkeypatch): ...
def test_session_expired_marks_needs_login_and_refunds(pg, monkeypatch): ...
def test_cart_drift_pauses_cart_and_alerts(pg, monkeypatch): ...
def test_fill_notifies_once(pg, monkeypatch): ...
def test_lost_fill_job_is_not_requeued(pg): ...                       # stale fill_cart → failed worker_lost, not queued
def test_shutdown_aborts_fill_without_requeue(pg): ...
def test_tick_finalizes_lost_fills(pg): ...
def test_learn_cart_job_ends_pending(pg, pg2, monkeypatch): ...       # approval row kind cart, status pending; no cart recipe row
def test_learn_cart_needs_search_recipe_and_connection(pg): ...
def test_only_fill_and_cart_learn_use_cart_mode(pg, monkeypatch): ... # open_site kwargs across connect, sync, search, learn(orders), learn(cart), fill
# test_fake_sites.py — PLAN6 = Plan 5's 5-item list (milk 500ml ×2, atta, eggs, Lay's Classic, brown bread) + an ask item for "milk"
def test_fake_fill_adds_on_top_and_keeps_existing(tmp_path, fake_mode):
    out = fake_sites.fill_cart("zepto", PLAN6, PRICED, FAKE_RECIPE, None, ask=lambda q, c: "zepto-amul-taaza-toned-milk-1-l",
                               on_item=lambda i: None, on_stage=lambda s: None, user_id=A, state_dir=tmp_path)
    assert out["status"] == "partial" and [i["status"] for i in out["items"]].count("added") == 5
    assert [i["name"] for i in out["items"] if i["status"] == "out_of_stock"] == ["Farm Eggs 6 pcs"]
    assert (out["cart"]["subtotal"], out["cart"]["total"], out["cart"]["existing_count"]) == (465, 480, 1)
    assert json.loads((tmp_path / f"{A}-zepto.json").read_text())["cart"]["zepto-amul-butter-100g"]["qty"] == 1
def test_fake_locked_location_reprices(tmp_path, fake_mode): ...      # locked at (22.31, 73.17): atta 239 → 246, bread 46 → 47
def test_fake_unlocked_location_is_selected(tmp_path, fake_mode): ...
def test_fake_learn_cart_asks_approval(): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_worker_plan6.py tests/server/test_jobs.py tests/server/test_fake_sites.py tests/server/test_scheduler.py -v` → the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(worker): fill cart jobs on the user's session, supervised cart learning, fake fill and lost-fill cleanup"`

---

## Wave C

### Task 10: End-to-end — fill cart locally on an iPhone viewport

**Files:**
- Create: `e2e/tests/plan6.spec.ts`.
- Modify:
  - `e2e/helpers/stack.ts`: `userId(email)`; `setFakeAppLocation(email, site, {lat, lon, area, locked})` writes
    `$FAKE_STATE_DIR/<id>-<site>.json` and keeps the cart; `readFakeCart(email, site)`.
  - `e2e/tests/plan5.spec.ts`: "Coming soon" becomes "Not available for Zepto yet" (grocery; no cart recipe yet) and
    "Filling isn't available for food orders." (food).
  - `scripts/e2e_local.sh`: `export FAKE_STATE_DIR=$(mktemp -d)` for the worker and Playwright, removed at the end.
  - `docs/e2e-local.md`.

**Interfaces:** Serial, on the iPhone 14 profile, running after `plan5.spec.ts`. A `beforeAll` learns the search
recipes through the admin view when they are missing, so the file also runs alone. Address: "12 Shanti Society, Gorwa,
Vadodara, Gujarat" (22.3300, 73.1500).
1. **`admin learns the Zepto cart recipe`:**
   - Admin, with fake Zepto connected: Sites → "Cart filling" → Learn Zepto.
   - The approval shows `^https://fake\.zepto\.test/api/cart/add$` ("Add to cart") and
     `^https://fake\.zepto\.test/api/location/select$` ("Select location") → Approve → "Waiting for approval".
   - Approvals shows "Cart permissions" and the never-checkout line → Approve. Sites: Zepto cart filling is Healthy,
     version 1.
2. **`fill needs a logged-in app`:**
   - A new invited user with the address connects fake Zepto (`123456`) and checks "Aashirvaad Atta 5kg".
   - `setConnectionStatus(user, "zepto", "needs_login")` → the card shows "Reconnect Zepto to fill" → `/accounts`.
   - `setConnectionStatus(user, "zepto", "connected")`.
3. **`fill adds items, asks, and keeps the existing cart`:**
   - The list is Plan 5's 5 items plus "milk" → Compare → "Fill cart on Zepto".
   - The sheet shows "Fill your Zepto cart?", "We'll add 6 items to your Zepto cart.", "Items already in your cart stay
     there." and "1 item needs your choice while we fill." → "Add 6 items".
   - "Which milk should we add?" appears live. Choose the choice matching
     `/^Amul Taaza Toned Milk 1 L · .* · ₹55$/` → "Add this".
   - The result shows:
     - "Added 5 of 6 items to your Zepto cart" and "Out of stock now" with "Farm Eggs 6 pcs".
     - "Zepto cart total ₹480 · items ₹465 + fees ₹15" and "Includes 1 item that was already in your cart."
     - "Open Zepto to pay" with `href="https://fake.zepto.test/cart"`, and "We never place the order or pay."
   - `readFakeCart` still has Amul Butter 100g ×1, plus 5 new lines.
4. **`the same check can't be filled twice`:** the card shows "See what was added", which reopens the result. A direct
   POST returns 409 `"This cart was already filled on Zepto."`. `/me/limits` `cart_fills_left` is 4.
5. **`location mismatch re-prices instead of filling`:**
   - `setFakeAppLocation(user, "zepto", {lat: 22.31, lon: 73.17, area: "Alkapuri, Vadodara", locked: true})`.
   - Check "Aashirvaad Atta 5kg" and "Britannia Brown Bread" → Fill → Add.
   - The sheet shows "Zepto is set to a different location",
     "Zepto delivers to Alkapuri, Vadodara right now, not Gorwa, Vadodara.", "₹239 → ₹246", "₹46 → ₹47" and
     "Items there: ₹293 instead of ₹285".
   - `cart_fills_left` is still 4. "Fill at these prices" → "Added 2 items to your Zepto cart"; `cart_fills_left` is 3.
6. **`budget pause`:**
   - Run a check without filling, then `schedulerOnce({MONTHLY_BUDGET_INR: "0.01"})`.
   - The card's button is disabled, with `/Filling carts is paused until 1 \w+\./`. Then `schedulerOnce({})`.
7. **`daily fill limit`:**
   - `setFakeAppLocation(user, "zepto", {lat: 22.33, lon: 73.15, area: "Gorwa, Vadodara", locked: false})`.
   - Through the API, run checks and fills (each waited until done) until 5 fills are counted.
   - The next card shows "No fills left today", and a direct POST returns 429
     `"You can fill 5 carts a day. Try again tomorrow."`.
8. **`no horizontal scroll`** on `/cart/[id]` with the fill sheet open, during a question and on the result.

- [ ] **Step 1: Write the spec.** It fails until the stack runs.
- [ ] **Step 2: Implement** the helpers, the script, the docs and the plan5 assertion.
- [ ] **Step 3: Run** `bash scripts/e2e_local.sh` → `product`, `plan4`, `plan5` and `plan6` specs all green;
  `pytest -q` and `pnpm --dir web test` stay green.
- [ ] **Step 4:** Design review of the fill-sheet screenshots (Plan 3 R5); its findings enter the T6 fix loop.
- [ ] **Step 5: Commit** — `git commit -m "test(e2e): fill cart end to end with questions, location re-pricing and limits"`

---

## Self-review

- **Spec coverage:**
  - Tap-only fills: T5, T7.
  - Guard: allowlist-only POSTs and the seven forbidden categories (T2 table); separate per job type (T2, T9); no
    clicks on checkout/pay/place order/proceed (T2, T3); downloads denied (`open_site`).
  - Add-only: R3; T2, T3, T9, T10.
  - Out-of-stock, user-chosen swaps, asking instead of guessing: R5; T3, T5, T6, T10.
  - Location (select only, re-price and tell): R6; T3, T5, T6, T10.
  - Admin-approved cart recipes and drift: R13; T4, T7–T9.
  - Pay in the app: R11; T5, T6, T10.
  - Costs: R7; T5, T7, T10. RLS: T1, T7. Instamart via Swiggy: R12; T5, T9.
- **Type consistency:** names are identical across tasks:
  - `CartPolicy`, `decide_cart`, `cart_recipe.*`.
  - `fill_cart`, `outcome` and `verify`, with the outcome shape defined once (T3).
  - `learn_cart → {recipe, probe_item}`.
  - `fill.*` and the `fill_summary.build` shape, mirrored in `lib/fill.ts`.
  - `limits.consume_cart_fill / cart_fills_left / refund_cart_fill`.
- **Ordering:** files are disjoint within each wave.
  - Wave 0: T1 ∥ T2.
  - Wave A: T3 and T4 need T2; T5 needs T1; T6 is contract-only.
  - Wave B: T7 (`cart_api`, `api`, `admin_api`, `admin`) ∥ T8 (`web` admin) ∥ T9 (`worker`, `jobs`, `fake_sites`,
    `scheduler`).
  - T10 runs last.
- **Fake numbers checked:**
  - Zepto ×0.92: milk 500ml 27×2, atta 239, Lay's 18, bread 46, milk 1 L 55, butter 53. Subtotal 465 + handling 15 =
    480.
  - Reprice ×1.03: 246 and 47, so 285 → 293.
