# Plan 5 of 7 — Cheapest Cart (Groceries and Food) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Cart tab has two modes, and both price live at the user's saved address on the user's own logged-in
session for each connected app, so the prices are what this user would pay:
- **Groceries:** the user types a grocery list, or starts from "what I usually buy", and it is priced on Zepto,
  Blinkit and Instamart.
- **Food:** the user picks a restaurant (by search, or from "restaurants you order from") and dishes (from its menu
  or "your usual dishes"), and the same order is priced on Swiggy vs Zomato.

Products, dishes and restaurants are matched carefully; when a match isn't confident, the user is asked. Each app's
fees are added: from the app's own responses when it returns them, otherwise from admin-maintained fee rules. The
result is the ranked "Cheapest cart" screen (mockup A), with the item table on demand (mockup B). Search is
read-only and replays recipes the admin has learned. Results are stored per user under RLS. Each compare counts as one
cart check against the 10/day limit and the budget pause. Apps the user hasn't connected show "Connect <App> to
compare".

**Architecture:**
- **Starting a check.** `POST /cart/checks` (groceries) or `POST /cart/food/checks` consumes one cart check, stores a
  `cart_requests` row (`mode` `grocery` | `food`), writes `cart_results` rows for apps that can't be priced
  (`not_connected`, `needs_login`, `unavailable`, `not_listed`), and enqueues one `search` job per priceable app. All
  of this happens in one transaction.
- **Running a search job.** The worker treats a `search` job like a sync of that user's app:
  - It takes the site's profile lock (`one_running_job_per_profile`; Instamart uses the `swiggy` profile).
  - It restores the user's encrypted Chrome profile from the vault and uses their sticky proxy session
    (`connections.proxy_session_id`) and their saved lat/lon.
  - It replays the admin-learned recipe under the read-only `Guard`, with media blocked.
  - It matches the results (`server/match.py`; restaurants in `server/restaurant_match.py`) and saves the profile
    back.
  - A logged-out state marks the connection `needs_login` and notifies the user.
- **Summary.** The last job to finish builds the summary (`server/fees.py`, `server/cart_rank.py`) into
  `cart_requests.summary`. The PWA follows `cart_requests` / `cart_results` over Realtime and renders the summary's
  prepared strings.
- **Food lookups.** Restaurant search and menu fetch are `search` jobs with `op` `restaurants` / `menu`. They replay
  the recipe kinds `restaurants` / `menu` and fill per-user Postgres caches with a 30-minute TTL. A food check whose
  menus are fresh in the cache is priced inline by the API.
- **Admin.** Admins learn the `search`, `restaurants` and `menu` recipes with the Plan 4 learn job on their own
  connected accounts, and they maintain fee rules in the admin view.
- **Fake mode.** `WORKER_FAKE=1` fakes search, restaurants, menus and learning, so everything runs locally end to end.

**Tech Stack:** as Plans 3–4 (Python 3.12, FastAPI, psycopg 3, Playwright-over-CDP Chrome, OpenAI SDK; Next.js 15 +
TypeScript + Tailwind 3.4, Vitest, Playwright e2e, local Supabase). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-expense-agent-product-design.md` (binding: "Cheapest cart (new)", the
"Cart result is 'ranked app totals'" bullet under Screens, "Cart checks: up to 10 per user per day", "Learning is
supervised by the admin", "Location during cart filling", data model `cart_requests` / `cart_results`, Known risks).
The owner has amended it twice: the compare also covers food, and every compare runs on the user's own logged-in
session (overriding the spec's "pooled proxy" for search). Mockup:
`.superpowers/brainstorm/24332-1793102961/content/cart-compare.html` (A = ranked totals, B = item table).

**Precondition:** Plans 3 and 4 are merged and `bash scripts/e2e_local.sh` is green. This plan uses:
- From Plan 4:
  - `limits.consume_cart_check / LimitExceeded / BudgetPaused / to_http`, `usage.record / today`,
    `budget.is_paused`, `config.SITE_NAMES`.
  - `recipe_admin.{make_approver, deny_new_posts, apply_learned, mark_drift, pause_site, is_paused, admin_learner, is_admin}`
    and `notify.{enqueue, alert_admins, dispatch}`.
  - The worker's `learn` job and the metering in `run_job`'s `finally`, `fake_sites.learn`, `scheduler.tick`,
    `server/admin_api.py` and `server/admin.py`.
  - In the PWA: `GET /me/limits` (`cart_checks_left`), `web/lib/me.ts`, and the admin page with `SitesPanel`.
- From Plans 1–3:
  - `worker._ensure_proxy_session`, `vault.restore_profile / save_profile`, `browser.SessionExpired`.
  - The `norm_name()` SQL, `merchant_spend`, `web/components/ui/*`, `lib/api.ts`, `lib/jobs.ts` `watchJob`,
    `lib/format.ts`, and the Cart tab placeholder.

## Global Constraints

- **Apps priced.**
  - Grocery mode: Zepto, Blinkit and Instamart only (`search.SEARCH_SITES = ("zepto", "blinkit", "instamart")`).
  - Food mode: Swiggy and Zomato only (`food_search.FOOD_SITES = ("swiggy", "zomato")`), through restaurant search and
    menu fetch.
- **Connected apps only.**
  - A site is priced only if the user's connection for its `db_site` (Instamart → `swiggy`) is `connected` or
    `syncing`, or is `error` with a `last_synced_at`.
  - A site that is `needs_login` gets the card "{App} logged you out. Log in again to compare."; any other site gets
    "Connect {App} to compare." (Instamart: "Connect Swiggy to compare Instamart."). Both cards link to `/accounts`.
  - A compare needs at least one priceable connected app in its mode. Without one, it is a 409 and is not counted.
- **Own session.** Every compare job (grocery search, restaurant search, menu fetch) runs on the user's own identity:
  - their vault-restored Chrome profile, their sticky proxy session (`connections.proxy_session_id`), and their saved
    `profiles.lat/lon`;
  - the site's profile lock (`one_running_job_per_profile`), so a compare waits behind a running sync for that profile
    instead of failing;
  - after a successful job the profile is saved back to the vault. A logged-out state marks the connection
    `needs_login` and enqueues the Plan 4 `needs_login` notification.
- **Strictly read-only toward delivery apps.**
  - Compare jobs use the same read-only `Guard` as sync (`login_mode` off), plus only the recipe's admin-approved
    read POSTs. Media is blocked (`block_media=s.sync_block_media`, Plan 4 R12). Jobs never log in, never click, and
    never select, create or edit an address.
  - Nothing in Plan 5 creates a `fill_cart` job, exposes a fill route, or enables a "Fill cart" button.
  - Never anything matching checkout, payment, address, coupon or wallet.
- **Personal prices.**
  - Prices include the user's memberships (Swiggy One, Zomato Gold, Zepto/Blinkit passes) and their store's stock.
    The UI says "Your prices, with your memberships and your store's stock."
  - Fees the app's own responses return (observed) always win over `fee_rules` estimates. Membership waivers in
    `fee_rules` apply only when the response says the user is a member.
- **Food prices.** Food is the listed menu price plus fees and GST. Offers and coupons are never applied, and every
  food card shows "Offers in the app may lower this." Restaurant open/closed and dish availability are shown.
- **"Usual".**
  - Grocery usual = items bought 3+ times in the last 90 IST days from Blinkit, Zepto or Swiggy rows with
    `sub_platform='instamart'`.
  - "Restaurants you order from" comes from past Swiggy (food) and Zomato orders. "Your usual dishes" = past order
    items at that restaurant.
  - Cancelled, refunded and failed orders never count.
- **Cart checks.**
  - Up to 10 per user per IST day, for groceries and food together, enforced only through Plan 4's
    `limits.consume_cart_check`.
  - At 100 % of the budget, checks are refused with `BudgetPaused` (503).
  - A refused check (409/429/503) never consumes one.
  - Food lookups (a restaurant search or menu fetch not attached to a check) are capped at `FOOD_LOOKUPS_PER_DAY=30`.
    Cache hits are free, and lookups are refused during the budget pause (R18).
- **Recipes.** `search`, `restaurants` and `menu` recipes are learned only by an admin `learn` job on the admin's own
  connected account. Users only replay. A new read-POST entry is used only after the admin approves it (Plan 4 R9).
- **No guessing.** An item, dish or restaurant pairing without a confident match is shown as "Pick" or "Check this is
  the same restaurant". It is never silently priced.
- **Tunables** live only in `server/config.py`, read from env and set explicitly in `docker-compose.yml`:
  `SPLIT_MIN_SAVING_INR=20`, `CART_MAX_ITEMS=30`, `FOOD_MAX_ITEMS=20`, `CART_STALE_S=1500`, `MATCH_JUDGE=1`,
  `MATCH_MODEL=""` (empty = `agent.model()`), `MENU_CACHE_TTL_S=1800`, `FOOD_LOOKUPS_PER_DAY=30`.
- **Logging.** Never log addresses, coordinates, list item texts, dish, product or restaurant names together with a
  user id. Compare jobs log counts and the site only.
- **Cost.** Each compare job's proxy bytes, AI tokens and worker seconds are metered to the user who asked (Plan 4
  `usage`).
- **UI.**
  - Follows Plan 3's Global Constraints: background `#f6f7f9`, white cards with radius 14px, accent `#0a7c55`, app
    dots Zepto `#5b21b6`, Blinkit `#f8cb46`, Instamart and Swiggy `#fc8019`, Zomato `#e23744`, ₹ with Indian grouping,
    no horizontal scroll at 375px, safe-area padding.
  - Also follows `cart-compare.html` A (and B for the table), for both modes.
  - Frontend tasks invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`
    before writing UI (Plan 3 ruling R3). The mockups and these constraints win any conflict.
  - After its code review, each frontend task gets a separate design review (impeccable critique/audit + ui-ux-pro-max +
    design-taste-frontend-v1, Playwright screenshots at 390×844 vs the mockups); findings enter its fix loop (Plan 3 R5).
  - Plan 3 design-review carry-overs: cheapest card `ring-2 ring-accent`; "n/a" uses `text-ink-muted` (not `#c0c4cc`).
- **New tables.**
  - RLS on. `authenticated` gets only `select` on own rows of `cart_requests` / `cart_results`, and nothing on
    `fee_rules`, `restaurant_cache` or `menu_cache`. `anon` gets nothing.
  - The PWA never uses the service-role key.
- **Testing.**
  - Each parallel implementer uses its own test database `ea_tN` on `127.0.0.1:54329` (Plan 3 ruling R4), e.g.
    `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/ea_t2 pytest -q`.
  - Every existing test and the Plan 3 and Plan 4 e2e suites keep passing.
- **Git.** All work is committed on `main`. Never push, and no AI attribution in commit messages. No deploy.

## Review Focus

- **Pack sizes.** The user writes "Amul Taaza 1L" and the app sells only 500 ml packs. It must price 2 packs, not one
  500 ml pack at half the price. → T2 `test_buys_two_half_litre_packs_for_one_litre`.
- **One-word items.** "milk" matches milk and Milk Bikis biscuits. It must become "Pick", never a guess. →
  T2 `test_ambiguous_dimensions_ask_user`.
- **Incomplete apps.** An app that is missing an item, failed, logged out or not connected must never be shown as
  "Cheapest" ahead of a complete cart. → T3 `test_incomplete_cart_never_outranks_complete`,
  `test_non_ok_cards_last_with_notes_and_actions`.
- **Double tap.** A double tap on "Compare", or a tap while a check is running: the second gets 409 and does not use
  one of the 10 daily checks. → T6 `test_second_check_while_running_is_409_and_not_counted`.
- **Everything fails.** Every app fails (bot block, drift): the user is not charged a check, and the screen says so
  instead of spinning. → T6 `test_all_sites_failed_refunds_check`, T9 `test_stale_request_is_finalized_failed`.
- **Busy profile.** A compare is requested while that app's daily sync runs on the same profile. It must wait and then
  run, not fail; a queued compare must run before a queued sync. → T1 `test_search_waits_behind_running_sync`,
  `test_queued_search_claimed_before_queued_sync`.
- **Logged out mid-compare.** The session has expired when the compare runs. That app returns "Log in again", the
  connection becomes `needs_login` with one notification, and the other apps are still compared. →
  T9 `test_logged_out_search_marks_needs_login_and_notifies`.
- **Same-name restaurants.** Behrouz Biryani exists in Gorwa and in Alkapuri, 4 km apart. Each app's listing must
  pair with the nearby branch, and the far one stays a separate "Only on Zomato" card. →
  T2 `test_same_name_far_apart_is_not_paired`, T9 `test_fake_behrouz_pairs_nearby_branch`.
- **Dish variants.** "Chicken Biryani" from past orders, while the menu has Half and Full. The dish must become
  "Pick", and a Half query must never be priced as Full (or veg as non-veg). →
  T2 `test_dish_with_variants_asks_user`, `test_variant_and_veg_must_match`.
- **Closed on one app.** That card shows "Closed on Zomato right now." with no total and is never "Cheapest", while
  the other app is still compared. → T3 `test_closed_restaurant_card_last_with_note`,
  T12 `test_closed_menu_priced_as_closed`.

## Spec rulings (made by this plan; record in the ledger)

- **R1 Own session (owner decision, replaces the pooled-proxy/fresh-profile approach).**
  - There is one `search` job per priceable app, enqueued with the site's normal profile name, so the Plan 1/2
    `one_running_job_per_profile` lock applies.
  - The worker restores the user's vault profile for that site and uses their sticky proxy session via
    `_ensure_proxy_session` and their `profiles.lat/lon`. It saves the profile back after success.
  - `VaultError` or `browser.SessionExpired` → result `needs_login`, connection `needs_login` (if not already), and
    the Plan 4 `needs_login` notification (dedupe `needs_login:<job id>`).
- **R2 Search recipes (kind `search`)** bind the query text and the user's lat/lon (or a store id resolved from
  lat/lon) into the request, and map the products plus an optional `fee_map` of fees and membership.
  - A recipe without a location binding is rejected: the price must belong to the user's saved address even if the
    session last browsed elsewhere. Injecting lat/lon into a read request never saves an address.
  - GET is preferred. A POST is accepted only through the admin-approved read allowlist.
  - Learning runs on the admin's own connected account for that site (Instamart → the admin's swiggy connection). The
    Plan 4 "connect it on your own account first" gate applies.
  - Account-specific values go through Plan 4's `sanitize_for_sharing`, and flagged recipes stay pending.
- **R3 Fee rules are data** in `fee_rules(site, rules jsonb, verified, …)` and are edited by the admin.
  - Grocery rules: delivery tiers by subtotal, flat handling, a small-cart fee, surge, and an optional
    `membership {name, waives}`.
  - Food rules (`"kind": "food"`): delivery tiers by distance, packaging per dish unit, a platform fee, a small-order
    fee, surge, GST (`food_pct` 5 on items + packaging, `fees_pct` 18 on delivery + platform + small-order + surge),
    and an optional membership.
  - Precedence per component: observed fee from the app's response > membership waiver (only when the response says
    member) > rule.
  - "Fees estimated" is shown when any component came from an unverified rule, when surge is possible and wasn't
    observed, or when membership is defined but unknown.
  - Seeds are unverified placeholders, chosen to match the mockup.
- **R4 Product matching.**
  - Score = `0.6 × coverage + 0.15 × precision + 0.25 × size`. `SURE = 0.8`, `NEAR = 0.5`, candidates shown at
    ≥ `0.3`.
  - The judge (`MATCH_JUDGE=1`) is asked only about NEAR candidates when no SURE candidate is in stock: at most one
    call per item, with up to 3 names.
  - Accepted candidates that span ml vs g → Pick.
  - Automatic swaps happen only within the same brand, the same dimension and 0.8–1.25× the size, and are always
    noted.
- **R5 Ranking.**
  - Complete carts rank before incomplete ones, ordered by `(unresolved, total, site order)`. Non-ok cards come last,
    without a total.
  - A grocery split is "worth it" only when it saves ≥ `SPLIT_MIN_SAVING_INR = 20` after both orders' fees.
  - Food never offers a split: one meal is one delivery.
- **R6 Limit accounting.**
  - A grocery check or a food compare consumes 1 at start. The consume, the request and the jobs share one
    transaction, so a 409 rolls the consume back.
  - "Pick one" choices recompute without consuming; "Edit list" and "Check again" consume.
  - A check where no app returned prices is refunded.
- **R7 The summary is computed in Python** (`cart_rank.build_summary`), including all copy, and stored in
  `cart_requests.summary`. The PWA renders the strings and does no price math. Realtime publishes `cart_requests` and
  `cart_results`.
- **R8 Stuck checks.** The scheduler finalizes `searching` requests older than `CART_STALE_S = 1500`, which is Plan 2's
  `MAX_JOB_S` (1200) plus 300 s, so a compare waiting behind a full-length sync is not timed out. Missing sites become
  `failed` / `timeout`.
- **R9 Data minimisation.**
  - Requests store the area label and lat/lon (Plan 6 needs them), never the full address.
  - Each item keeps at most 5 candidates.
- **R10 The "Fill cart on <app>" button is disabled with "Coming soon"** in both modes. Plan 6 decides food filling.
- **R11 Drift** (any of `search`, `restaurants` or `menu`) follows Plan 4 R8:
  - Mark the kind unhealthy.
  - Enqueue an automatic learn (`kind` `search` or `food`; new POST entries denied) for `admin_learner(conn, site)`.
    With no connected admin, or if that learn fails or ends pending, pause the kind and email the admins.
  - An unhealthy or paused kind makes that app "unavailable" for new checks.
- **R12 Food uses the same tables and pipeline.**
  - `cart_requests.mode = 'food'` and `restaurant jsonb`; sites `swiggy` / `zomato`.
  - Result statuses also include `closed` and `not_listed`.
  - The dishes picked from one app's menu are matched on the other app's menu.
- **R13 Restaurant pairing** (`restaurant_match.score`):
  - Score = `0.6 × name Jaccard + 0.4 × place`.
    - place = by distance: ≤ 0.25 km → 1, ≤ 1 km → 0.7, ≤ 2 km → 0.3, otherwise 0.
    - Without coordinates, place = locality-token Jaccard ≥ 0.5 → 0.7, else 0; unknown → 0.5.
    - More than 3 km apart → never paired.
  - Thresholds are the product ones: ≥ SURE pairs automatically. NEAR ≤ score < SURE asks the user, showing both
    listings. Below that, the listings stay separate as "Only on {App}".
- **R14 Dish matching** reuses `match.score` with food traits:
  - Variant (half/full/regular/small/medium/large), serves-N and veg/non-veg must match when both sides know them.
  - Mismatched add-ons cap the score below SURE.
  - An unknown query variant against several accepted variants → Pick.
  - No judge and no automatic swaps for food, because both menus are the same restaurant's, which keeps food checks
    synchronous when cached.
- **R15 Food recipes** are the kinds `restaurants` and `menu`; the `recipes` / `recipe_approvals` kind checks are
  extended.
  - One admin learn with `kind=food` learns both and applies each through `apply_learned`.
  - `restaurants` must bind location. `menu` may be bound by the restaurant id alone (an id already names one outlet).
- **R16 Interactive jobs first.** `jobs.claim` orders queued candidates by
  `CASE type WHEN 'connect' THEN 0 WHEN 'search' THEN 0 ELSE 1 END, created_at`.
  - A user is waiting on screen for connects and compares, while syncs and learns are background work.
  - A running job is never pre-empted: a compare waits for a running sync on the same profile.
- **R17 Caches.** `restaurant_cache` and `menu_cache` are kept **per user**, because prices are personal: membership
  pricing, and one user's session data is never served to another.
  - Keys: `(user_id, site, geo_key, query_norm)` and `(user_id, site, restaurant_id)`; `geo_key` = lat/lon to 2
    decimals.
  - The TTL is `MENU_CACHE_TTL_S = 1800` (30 min).
    - A menu (hundreds of dishes, often several hundred KB through the proxy) is reused across one ordering session:
      search → pick dishes → compare → edit → re-compare.
    - Within 30 minutes, prices rarely change. Availability and open/closed status change within the hour, so a
      longer TTL would mislead.
    - The result header shows the menu's fetch time, so the age is visible.
  - Rows older than 24 h are purged by the scheduler.
- **R18 Food lookups vs checks.** The owner's "a food compare counts as one cart check" is kept. Restaurant searches
  and menu fetches outside a compare cost proxy GB, so they are capped at `FOOD_LOOKUPS_PER_DAY = 30` (counted as
  enqueued lookup `search` jobs today, IST; cache hits are free) and refused while the budget is paused. This protects
  against the anti-goal of costs running away.

---

## Wave 0 (alone, first)

### Task 1: Schema, RLS, fee seeds, caches and interactive-first job claiming

**Files:**
- Create: `supabase/migrations/0008_cheapest_cart.sql`, `tests/server/test_schema_plan5.py`
- Modify:
  - `tests/server/conftest.py`: `TABLES` += `cart_requests`, `cart_results`, `restaurant_cache`, `menu_cache`; new
    fixture `fee_rules_snapshot`, which saves all `fee_rules` rows and restores them after the test.
  - `server/jobs.py` (`claim` ordering, R16), `tests/server/test_jobs.py`.

**Interfaces (Produces):**
- `cart_requests`:
  - Columns: `id uuid pk default gen_random_uuid()`, `user_id uuid not null`,
    `mode text not null default 'grocery' check in ('grocery','food')`,
    `items jsonb not null check (jsonb_typeof(items)='array')`, `restaurant jsonb` (food: `{name, locality,
    listings: {site: {restaurant_id, name, locality, lat, lon, distance_km}}, confirmed: bool, declined: [site]}`),
    `sites text[] not null`, `status text not null default 'searching' check in ('searching','done','failed')`,
    `area text not null`, `lat double precision not null`, `lon double precision not null`,
    `choices jsonb not null default '{}'`, `summary jsonb`, `error text`, `created_at timestamptz not null default now()`,
    `priced_at timestamptz`.
  - Index `cart_requests_user_created (user_id, created_at desc)`.
- `cart_results`:
  - Columns: `request_id uuid not null references cart_requests on delete cascade`, `user_id uuid not null`,
    `site text not null check in ('zepto','blinkit','instamart','swiggy','zomato')`,
    `status text not null check in ('ok','failed','unserviceable','unavailable','not_connected','needs_login','closed','not_listed')`,
    `items jsonb not null default '[]'`, `meta jsonb not null default '{}'` (observed `fee_info`, `distance_km`,
    `is_open`, `menu_fetched_at`), `subtotal numeric(10,2)`, `fees jsonb`, `total numeric(10,2)`,
    `fees_estimated boolean`, `error text`, `priced_at timestamptz not null default now()`.
  - Primary key `(request_id, site)`.
- `fee_rules`: `site text pk check in (the five sites)`, `rules jsonb not null`, `version int not null default 1`,
  `verified boolean not null default false`, `source text not null default 'seed'`,
  `updated_at timestamptz not null default now()`, `updated_by uuid`.
- Caches:
  - `restaurant_cache(user_id uuid, site text, geo_key text, query_norm text, results jsonb not null, fetched_at timestamptz not null default now())`,
    primary key `(user_id, site, geo_key, query_norm)`.
  - `menu_cache(user_id uuid, site text, restaurant_id text, menu jsonb not null, fetched_at timestamptz not null default now())`,
    primary key `(user_id, site, restaurant_id)`.
- Kind checks:
  `alter table recipes drop constraint recipes_kind_check, add constraint recipes_kind_check check (kind in ('orders','search','cart','restaurants','menu'))`.
  The same extension applies to `recipe_approvals.kind`, under its existing constraint name.
- Seeds (all unverified; `"surge": {"possible": true}, "source_url": null, "note": "Seed values; verify against the app before launch."`):
  - zepto: `{"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 99, "fee": 0}], "handling": 15, "small_cart": {"below": 99, "fee": 25}, "membership": {"name": "Zepto Pass", "waives": ["delivery", "small_cart"]}}`
  - blinkit: `{"delivery": [{"min_subtotal": 0, "fee": 25}, {"min_subtotal": 199, "fee": 0}], "handling": 9, "small_cart": {"below": 99, "fee": 20}}`
  - instamart: `{"delivery": [{"min_subtotal": 0, "fee": 35}, {"min_subtotal": 199, "fee": 0}], "handling": 10, "small_cart": {"below": 99, "fee": 15}, "membership": {"name": "Swiggy One", "waives": ["delivery"]}}`
  - swiggy: `{"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": 7, "fee": 45}, {"max_km": null, "fee": 65}], "packaging_per_item": 10, "platform": 20, "small_order": {"below": 149, "fee": 20}, "gst": {"food_pct": 5, "fees_pct": 18}, "membership": {"name": "Swiggy One", "waives": ["delivery"]}}`
  - zomato: `{"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 40}, {"max_km": 7, "fee": 55}, {"max_km": null, "fee": 75}], "packaging_per_item": 20, "platform": 10, "small_order": {"below": 149, "fee": 25}, "gst": {"food_pct": 5, "fees_pct": 18}, "membership": {"name": "Zomato Gold", "waives": ["delivery"]}}`
- RLS:
  - Enabled on all five new tables. Policy `own_rows` (`user_id = auth.uid()`) for select on `cart_requests` and
    `cart_results`.
  - `revoke all … from anon, authenticated` on all five. `grant select on cart_requests, cart_results to authenticated`.
    `grant all` on all five to `service_role`.
  - Realtime: add `cart_requests` and `cart_results` to `supabase_realtime`, in 0001's guarded `do $$` style.
- `jobs.claim(conn, worker_id, types)` keeps its signature. The candidate query orders by
  `CASE j.type WHEN 'connect' THEN 0 WHEN 'search' THEN 0 ELSE 1 END, j.created_at`, and the profile check is
  unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_cart_tables_rls_own_rows(pg, as_user): ...          # A's request + result visible to A, invisible to B
def test_users_cannot_write_cart_tables(pg, as_user): ...    # insert/update/delete as authenticated → InsufficientPrivilege
def test_fee_rules_and_caches_hidden_from_users(pg, as_user): ...   # select → InsufficientPrivilege on all three
def test_fee_rule_seeds(_applied):                           # read with a fresh connection before any truncate
    rows = dict(psycopg.connect(_applied).execute("select site, rules->>'platform' from fee_rules where rules->>'kind'='food'").fetchall())
    assert rows == {"swiggy": "20", "zomato": "10"}
    assert psycopg.connect(_applied).execute("select count(*) from fee_rules").fetchone()[0] == 5
def test_recipe_kinds_include_food(pg): ...                  # insert recipes kind 'menu' ok; kind 'x' → CheckViolation
def test_results_cascade_and_checks(pg): ...                 # status 'needs_login'/'closed' ok; site 'amazon' → CheckViolation
# tests/server/test_jobs.py
def test_search_waits_behind_running_sync(pg):
    jobs.enqueue(pg, A, "sync", "swiggy"); jobs.claim(pg, "w1", ("sync",))
    jobs.enqueue(pg, A, "search", "instamart", {"request_id": "r"})
    assert jobs.claim(pg, "w2", ("search",)) is None          # same profile 'swiggy' is busy: stays queued
    assert pg.execute("select status from jobs where type='search'").fetchone() == ("queued",)
def test_queued_search_claimed_before_queued_sync(pg):
    jobs.enqueue(pg, A, "sync", "zepto"); jobs.enqueue(pg, B, "search", "zepto", {"request_id": "r"})
    assert jobs.claim(pg, "w", ("sync", "search")).type == "search"
```

- [ ] **Step 2: Run** `pytest tests/server/test_schema_plan5.py tests/server/test_jobs.py -v` → FAIL.
- [ ] **Step 3: Implement** the migration, the conftest changes and the claim ordering.
- [ ] **Step 4: Verify on real local Supabase:** `npx supabase db reset` applies 0001–0008, and the realtime
  publication lists `cart_requests` and `cart_results`. Then `pytest -q` → green, with `TEST_DATABASE_URL` both set
  and unset.
- [ ] **Step 5: Commit** — `git commit -m "feat(db): cart requests, results, fee rules and food caches with RLS; interactive jobs claimed first"`

---

## Wave A (parallel after Task 1 is merged; disjoint files)

### Task 2: Matching — products, dishes and restaurants

**Files:** Create `server/match.py`, `server/restaurant_match.py`, `tests/server/test_match.py`, `tests/server/test_restaurant_match.py` (pure; no database)

**Interfaces (Produces) — products (`server/match.py`):**
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
- `Product(product_id, name, brand, size: Size | None, size_label: str | None, price: float, mrp: float | None, in_stock: bool)`.
  `from_raw(d: dict) -> Product | None` takes `search.extract_products` rows; the size comes from `pack_size`, else
  from the name. Rows missing a price, or with price ≤ 0, give None.
- Constants: `SURE = 0.8`, `NEAR = 0.5`, `SHOW = 0.3`, `MAX_CANDIDATES = 5`, `JUDGE_TOP = 3`.
- `score(q, p) -> float`:
  - `0.6 × |Qt∩Pt|/|Qt| + 0.15 × |Qt∩Pt|/|Pt| + 0.25 × size_score`, where `Pt = tokens(name) ∪ tokens(brand)`.
  - `size_score`: 1 if the query has no size; same dimension and equal → 1; ratio within 0.5–2× → 0.6; otherwise
    0.2; different dimension → 0.
- `Judge = Callable[[str, list[str]], list[bool]]`. `llm_judge(model: str) -> Judge` makes one OpenAI call with JSON
  output `{"same": [bool, …]}`. The response passes through `metering.count_openai`, and any error → all False.
- `match_item(q, products, judge=None) -> dict` returns
  `{"idx", "status", "confidence", "product", "packs", "line_total", "swap_from", "candidates"}`.
  - `status` is one of `matched | swapped | pick | out_of_stock | not_found`; `confidence` is
    `sure | confirmed | None`.
  - `candidates` holds ≤5 dicts of `{product_id, name, brand, size_label, price, mrp, in_stock, packs, line_total, score}`
    with score ≥ SHOW.
  - The algorithm (R4):
    1. Accepted = in-stock candidates with score ≥ SURE. If there are none, the judge confirms from the top
       `JUDGE_TOP` in-stock NEAR candidates.
    2. More than one unit dimension → `pick`.
    3. Packs:
       - With a query size: same dimension only; `packs = ceil(wanted / size − 1e-9)`; keep the candidate only if
         `packs × size ≤ 1.5 × wanted`.
       - Without a query size: the reference is the best accepted candidate's size; keep candidates within
         0.8–1.25× of it; `packs = qty`.
    4. The minimum `packs × price` wins (ties → higher score, then lower id) → `matched`.
    5. A SURE candidate out of stock → the cheapest in-stock candidate with the same brand, the same dimension and
       0.8–1.25× its size → `swapped`; otherwise `out_of_stock`.
    6. Otherwise, any candidate ≥ SHOW → `pick`; else `not_found`.

**Interfaces (Produces) — dishes (`server/match.py`, R14):**
- `dish_traits(name) -> tuple[str, str | None, int | None, frozenset[str]]` returns `(base_name, variant, serves, addons)`:
  - `variant` is one of `half | full | regular | small | medium | large`, from "(Half)", "- Full", "Half Plate" and
    similar.
  - `serves` comes from "Serves 2" / "for 2".
  - `addons` come from "with extra cheese", "+ Coke", "extra …".
- `Dish(dish_id, name, variant, serves, veg: bool | None, addons, price: float, packaging: float | None, in_stock: bool)`;
  `dish_from_raw(d) -> Dish | None` takes `food_search.extract_menu` dish rows.
- `DishQuery(idx, text, qty, tokens, variant, serves, veg, addons)`;
  `parse_dish_query(idx, text, qty, variant=None, veg=None) -> DishQuery`. Explicit `variant` / `veg` (known when the
  dish was picked from a menu) override the parsed values.
- `match_dish(q: DishQuery, dishes: list[Dish]) -> dict` has the same keys as `match_item`: `packs = qty`,
  `line_total = qty × price`, `swap_from` always None, statuses `matched | pick | out_of_stock | not_found`.
  - Dishes are excluded when veg, variant or serves differ and both sides are known.
  - A dish with an unknown variant, or mismatched add-ons, scores at most `SURE − 0.01`.
  - An unknown query variant with accepted dishes in more than one variant → `pick`.

**Interfaces (Produces) — restaurants (`server/restaurant_match.py`, R13):**
- `Listing(site, restaurant_id, name, locality, lat, lon, distance_km, is_open: bool | None, url)`;
  `listing_from_raw(site, d) -> Listing | None`.
- `name_tokens(name)` = `match.tokens(name)` minus `{"restaurant", "restaurants", "pvt", "ltd"}`.
- `haversine_km(a_lat, a_lon, b_lat, b_lon) -> float`. `score(a: Listing, b: Listing) -> float` follows R13; pairs
  more than 3 km apart score 0.
- `pair_results(by_site: dict[str, list[Listing]], user_lat, user_lon, access: dict[str, str]) -> list[dict]`.
  - Assignment: greedy by descending score, each listing used once.
  - Each pair is
    `{key, name, locality, distance_km, listings: {site: Listing-dict|None}, confidence: "sure"|"ask"|"single", note}`.
  - Notes: `"Check this is the same restaurant"` (ask), `"Only on {App}"` (single, when the other app is priceable),
    `"Connect {App} to compare"` or `"Log in to {App} again to compare"` (other app not priceable, per `access`).
  - Sorted by distance, then name; at most 10.

- [ ] **Step 1: Write the failing tests**

```python
def P(name, price=20.0, brand=None, size=None, in_stock=True, pid=None): ...   # Product via from_raw
def test_parse_size_units_and_multipacks():
    assert match.parse_size("Amul Taaza Toned Milk 1 L") == Size(1000, "ml")
    assert match.parse_size("Coke 6 x 300 ml") == Size(1800, "ml")
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
def test_never_buys_more_than_one_and_a_half_times(): ...   # want 500 g, only 1 kg → pick
def test_ambiguous_dimensions_ask_user():
    m = match.match_item(match.parse_query(0, "milk", 1), [P("Amul Taaza Toned Milk 1 L", 60), P("Milk Bikis Biscuits 200 g", 30)])
    assert m["status"] == "pick" and len(m["candidates"]) == 2
def test_no_size_picks_cheapest_equivalent(): ...           # "bread": 400 g ₹50 / 400 g ₹45 / 800 g ₹80 → ₹45
def test_swap_same_brand_when_out_of_stock(): ...           # Lay's Classic OOS → Magic Masala, swap_from set
def test_out_of_stock_without_same_brand_substitute(): ...
def test_near_match_without_judge_asks_user(): ...
def test_near_match_confirmed_by_judge(): ...               # judge → [True] → confidence "confirmed"
def test_judge_called_once_with_top_three(): ...
def test_judge_not_called_when_sure_in_stock(): ...
def test_not_found_and_candidate_cap(): ...
def test_llm_judge_failure_is_all_false(monkeypatch): ...
# dishes
def test_dish_traits():
    assert match.dish_traits("Chicken Biryani - Half")[1] == "half"
    assert match.dish_traits("Paneer Tikka (Serves 2)")[2] == 2
    assert match.dish_traits("Burger with Extra Cheese")[3] == frozenset({"cheese"})
def test_variant_and_veg_must_match():
    q = match.parse_dish_query(0, "Chicken Biryani (Half)", 2, variant="half", veg=False)
    m = match.match_dish(q, [D("Chicken Biryani - Full", 395, veg=False), D("Chicken Biryani - Half", 235, veg=False), D("Veg Biryani (Half)", 120, veg=True)])
    assert (m["status"], m["product"]["name"], m["line_total"]) == ("matched", "Chicken Biryani - Half", 470.0)
def test_dish_with_variants_asks_user(): ...                # "Chicken Biryani" vs Half + Full → pick with both candidates
def test_addon_mismatch_is_not_sure(): ...
def test_unavailable_dish_is_out_of_stock(): ...
# tests/server/test_restaurant_match.py
def test_same_restaurant_same_place_is_sure(): ...          # "Meghana Foods" both apps, 0.1 km → ≥ SURE
def test_name_variant_same_place_asks(): ...                # "Truffles" vs "Truffles Cafe", same coords → NEAR ≤ s < SURE
def test_same_name_far_apart_is_not_paired(): ...           # Behrouz Gorwa vs Alkapuri 4.1 km → 0; Gorwa↔Gorwa paired
def test_locality_used_without_coordinates(): ...
def test_pair_results_notes(): ...                          # single → "Only on Swiggy"; zomato needs_login → "Log in to Zomato again to compare"
```

- [ ] **Step 2: Run** `pytest tests/server/test_match.py tests/server/test_restaurant_match.py -v` → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): product, dish and restaurant matching with pick-one instead of guessing"`

---

### Task 3: Fee rules and ranked summaries (groceries and food)

**Files:** Create `server/fees.py`, `server/cart_rank.py`, `tests/server/test_fees.py`, `tests/server/test_cart_rank.py` (pure)

**Interfaces (Produces):**
- `fees.validate(rules: dict) -> list[str]` returns errors, and dispatches on `rules.get("kind", "grocery")`.
  - Grocery keys: `delivery` (non-empty list, the first `min_subtotal` is 0, strictly ascending), `handling`,
    `small_cart` (null or `{below 0..2000, fee}`), `surge` (`{possible: bool, fee?}`), `membership`
    (null or `{name: str ≤40, waives: subset of the component names}`), `source_url`, `note`.
  - Food keys: `kind`, `delivery_by_km` (ascending `max_km`, last null), `packaging_per_item`, `platform`,
    `small_order`, `surge`, `gst {food_pct 0..28, fees_pct 0..28}`, `membership`, `source_url`, `note`.
  - Every fee is from 0 to 500. Unknown keys are rejected.
- `fees.compute(rules, subtotal: float, verified: bool, observed: dict | None = None) -> dict` returns
  `{"delivery", "handling", "small_cart", "surge", "total", "estimated", "member"}`:
  - Delivery comes from the tier with the largest `min_subtotal ≤ subtotal`. The small-cart fee applies when
    `subtotal < below`.
  - `observed` may carry any component plus `member: bool | str`. Each component takes, in order: the observed value,
    then 0 if `member` is truthy and the component is in `membership.waives`, then the rule.
  - `member` is the membership name when applied, else None.
  - `estimated` follows R3. A subtotal of 0 gives all zeros.
- `fees.compute_food(rules, subtotal: float, units: int, distance_km: float | None, verified: bool, observed: dict | None = None) -> dict`
  returns `{"delivery", "packaging", "platform", "small_order", "surge", "gst", "total", "estimated", "member"}`:
  - Delivery is the first tier with `max_km ≥ distance`; an unknown distance → the last tier, estimated.
  - Packaging = observed (the sum from the menu), else `packaging_per_item × units`, estimated.
  - `gst = round(food_pct% × (subtotal + packaging) + fees_pct% × (delivery + platform + small_order + surge), 2)`.
  - The same precedence as `compute` applies.
- `cart_rank.inr(x) -> str`: `"₹15"`, `"₹1,23,456"`, `"₹49.50"`.
- `cart_rank.area_label(address) -> str`:
  - Drops a trailing `India` and pincodes. ≥3 parts → `"{parts[-3]}, {parts[-2]}"`; 1–2 parts → `parts[0]`.
  - A `lat,lon` address or None → `"your address"`.
- `cart_rank.build_summary(request: dict, results: list[dict], fee_rules: dict[str, tuple[dict, bool]], split_min_saving: float) -> dict`:
  - Inputs: `request = {id, mode, items: [{text, qty, variant?, veg?}], area, restaurant, choices: {"<idx>": {"<site>": id}}}`;
    `results = [{site, status, items, meta, priced_at, error}]`.
  - Grocery fees use `fees.compute(observed=meta.fee_info)`; food fees use
    `fees.compute_food(units=Σ priced qty, distance_km=meta.distance_km, observed=meta.fee_info)`.
  - A choice replaces the cell with the chosen candidate (`chosen`). Priced statuses are `matched`, `swapped` and
    `chosen`; unresolved = out_of_stock + not_found + pick.
  - It returns **the summary shape that the API, the worker and the PWA all use:**

```text
{request_id, mode, status: "done"|"failed", title ("Cheapest cart" | restaurant name), count_label ("7 items" | "3 dishes"),
 area, priced_at (ISO; food: oldest menu_fetched_at used), price_note: "Your prices, with your memberships and your store's stock.",
 offers_note: "Offers in the app may lower this." | null (food only), restaurant: {name, locality} | null, best_site,
 cards: [{site, name, rank, status: "ok"|"failed"|"unserviceable"|"unavailable"|"not_connected"|"needs_login"|"closed"|"not_listed",
          cheapest, subtotal, fees: {…components, total}|null, fees_estimated, total, total_label ("₹412" | "—"),
          delta_label ("+₹37" | "Same total" | null),
          fee_line: grocery "Items ₹397 + delivery ₹0 + handling ₹15" [+ " + small-cart ₹X"] [+ " + surge ₹X"];
                    food "Items ₹700 + delivery ₹30 + packaging ₹40 + platform ₹20 + GST ₹46" [+ small-order, surge],
          member_note ("Zepto Pass applied") | null, fee_hint ("Free delivery above ₹199") | null,
          stock_line (grocery "all 7 in stock" | "1 out of stock · 1 to pick"; food "all 3 available" | "1 not available") | null,
          swaps: [str], to_pick: int,
          note: "Couldn't price {App} right now." | "{App} doesn't deliver to this address." | "{App} prices are unavailable right now." |
                "Connect {App} to compare." (Instamart: "Connect Swiggy to compare Instamart.") |
                "{App} logged you out. Log in again to compare." | "Closed on {App} right now." | "Not on {App}." |
                "Couldn't confirm this restaurant on {App}." | null,
          action: {label: "Connect"|"Log in again", href: "/accounts"} | null}],
 table: {columns: [ok sites in card order], rows: [{idx, label, cells: {site: {status, name, price_label ("₹136"|"n/a"|"Pick"|"Out of stock"|"Not available"), cheapest}}}],
         totals: {site: total_label}},
 picks: [{idx, site, label, candidates: [{product_id, name, size_label, price_label, packs}]}],
 split: {worth_it, sites, saving, line} | null}
```

- Ranking (R5):
  - ok cards sort by `(unresolved, total, site order)`. Non-ok cards follow in the status order `closed, not_listed,
    needs_login, not_connected, unserviceable, failed, unavailable`, then site order.
  - `cheapest` goes only to rank 1, and only if it priced ≥1 item.
  - `delta_label` is shown only when the unresolved counts are equal.
  - `status` is `done` if any card is ok.
- Split (grocery only, with ≥2 ok cards): every item goes to the cheaper cell. A pair counts only if it covers the best
  single app's priced items and both sides get ≥1 item. Lines:
  - No valid pair → `"No item is cheaper on another app, so one order on {Best} is best."`
  - Worth it → `"Splitting between {A} and {B} saves {₹} even after the second order's fees."`
  - Not worth it, extra fees > 0 → `"Splitting across apps saves {₹items} on items but adds {₹fees} in fees. Not worth it."`
  - Otherwise → `"Splitting across apps saves only {₹}. Not worth a second order."`
  - Food → `split: null`.

- [ ] **Step 1: Write the failing tests**

```python
RULES = {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 199, "fee": 0}], "handling": 15,
         "small_cart": {"below": 99, "fee": 25}, "surge": {"possible": False},
         "membership": {"name": "Zepto Pass", "waives": ["delivery", "small_cart"]}}
def test_compute_tiers_and_small_cart():
    assert fees.compute(RULES, 397, True, {"member": False}) == {"delivery": 0, "handling": 15, "small_cart": 0, "surge": 0, "total": 15, "estimated": False, "member": None}
    assert fees.compute(RULES, 80, True, {"member": False})["total"] == 70
    assert fees.compute(RULES, 199, True)["delivery"] == 0 and fees.compute(RULES, 198.99, True)["delivery"] == 30
def test_membership_waives_only_when_member_observed():
    assert fees.compute(RULES, 80, True, {"member": True})["total"] == 15
    assert fees.compute(RULES, 80, True, {"member": True})["member"] == "Zepto Pass"
    assert fees.compute(RULES, 80, True)["estimated"] is True       # membership defined, unknown
def test_observed_fee_beats_rule(): ...                       # observed delivery 12 → delivery 12 while subtotal 80
def test_estimated_when_unverified_or_surge_possible(): ...
def test_compute_food_exact():
    SW = {"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": 7, "fee": 45}, {"max_km": None, "fee": 65}],
          "packaging_per_item": 10, "platform": 20, "small_order": {"below": 149, "fee": 20}, "surge": {"possible": False},
          "gst": {"food_pct": 5, "fees_pct": 18}}
    f = fees.compute_food(SW, 700, 3, 0.15, True, {"packaging": 40})
    assert (f["delivery"], f["packaging"], f["platform"], f["small_order"], f["gst"], f["total"]) == (30, 40, 20, 0, 46.0, 136.0)
    assert fees.compute_food(SW, 700, 3, None, True, {"packaging": 40})["delivery"] == 65    # unknown distance → last tier
    assert fees.compute_food(SW, 700, 3, 0.15, True)["packaging"] == 30                      # rule fallback, estimated
def test_validate_rejects_bad_rules(): ...                    # grocery first tier 10; food gst 40; unknown key "tip"
def test_inr_and_area_label(): ...
def test_ranks_cheapest_complete_first_with_deltas(): ...     # zepto "₹412" cheapest, blinkit "+₹37", instamart "+₹49"
def test_incomplete_cart_never_outranks_complete(): ...
def test_non_ok_cards_last_with_notes_and_actions(): ...      # needs_login note + action "Log in again"; instamart not_connected → "Connect Swiggy to compare Instamart."
def test_nothing_priced_has_no_cheapest(): ...
def test_swap_note_member_note_and_stock_line(): ...
def test_item_table_highlights_cheapest_per_row_and_ties(): ...
def test_choice_replaces_pick_cell(): ...
def test_split_lines(): ...                                   # worth it / not worth it / no cheaper item, exact strings
def test_food_summary_cards():
    ...   # swiggy items 700 + observed packaging 40, zomato 720 → total_labels ["₹836", "₹878"], delta "+₹42",
          # fee_line "Items ₹700 + delivery ₹30 + packaging ₹40 + platform ₹20 + GST ₹46", offers_note set,
          # title "Meghana Foods", count_label "3 dishes", split None
def test_closed_restaurant_card_last_with_note(): ...         # zomato closed → "Closed on Zomato right now.", no total, swiggy cheapest
def test_declined_pair_note(): ...                            # restaurant.declined ["zomato"] → "Couldn't confirm this restaurant on Zomato."
```

- [ ] **Step 2: Run** `pytest tests/server/test_fees.py tests/server/test_cart_rank.py -v` → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): grocery and food fee rules with observed fees and memberships; ranked summaries"`

---

### Task 4: Grocery search recipes in the engine — replay and supervised learning

**Files:**
- Create: `search.py`, `search_learn.py`, `tests/test_search.py`, `tests/test_search_learn.py`, and the synthetic
  fixtures `tests/fixtures/search_get.json` (nested widgets, prices in paise, a banner row, `inventory` counts, a
  `user.pass_active` flag, `delivery_fee` in the response) and `tests/fixtures/search_store.json`.

**Interfaces (Produces):**
- Search recipe (`recipes.kind = 'search'`):

```text
{kind: "search", site, home_url, search_page_url ("…{q}…"), endpoint, method: "GET"|"POST", query: {}, body: obj|null,
 headers: {}, query_param, location_inputs: [{in: "query"|"header"|"body", name, value: "lat"|"lon"}],
 store: null | {lookup: {method, url, body}, location_inputs: [...], store_id_path, inputs: [{in, name}]},
 field_map: {products_path, product_id, name, brand, pack_size, price, mrp, in_stock, price_divisor: 1|100},
 fee_map: null | {source: "search"|"store", delivery, handling, small_cart, surge, member, divisor: 1|100},
 read_post_allowlist: [{url_regex, why}]}
```

- `search.SEARCH_SITES = ("zepto", "blinkit", "instamart")`. The starting points for learning are
  `SEARCH_PAGES = {"zepto": "https://www.zepto.com/search?query={q}", "blinkit": "https://blinkit.com/s/?q={q}", "instamart": "https://www.swiggy.com/instamart/search?custom_back=true&query={q}"}`,
  and each recipe stores its own `search_page_url`.
- `search.SearchError(Exception)`, `search.SearchDrift(SearchError)`, `search.Unserviceable(SearchError)`.
  Logged-out states raise `browser.SessionExpired`: the existing `fetch_in_page` raises it on 401/403 or a login URL,
  and `ensure_logged_in` raises it after navigation.
- `search.to_stock(v) -> bool`:
  - None → True; a bool as is; a number → `> 0`.
  - A string is out of stock if it is in `{"out_of_stock", "sold_out", "unavailable", "oos", "false", "0"}` or
    contains "out of stock" / "sold out".
- `search.extract_products(data, field_map) -> list[dict]` returns
  `{product_id, name, brand, pack_size, price, mrp, in_stock}`. Rows without an id, a name or a price > 0 are dropped.
- `search.extract_fee_info(data, fee_map) -> dict`: only the keys whose path resolves. `member` → bool or a name.
- `search.build_request(recipe, text, location, store_id=None) -> dict`;
  `search.resolve_store(page, recipe, location) -> str | None`.
- `search.search_products(site, queries, location, recipe, profile_dir, proxy_server=None, block_media=True, max_results=20) -> dict`
  returns `{"products": {query: [...]}, "fee_info": {...}}`:
  - It runs `browser.open_site(site, allowlist=recipe["read_post_allowlist"], profile_dir=profile_dir, location=location, proxy_server=proxy_server, block_media=block_media)`
    on the **caller-supplied profile** (the user's restored one). It never passes `login_mode` and never clicks.
  - It calls `goto(home_url)` and `sync.ensure_logged_in(page)`, then resolves the store (no store → `Unserviceable`).
  - Per query, the fast path is `fetch_in_page(build_request(...))`; its fallback is `goto(search_page_url)` plus the
    last captured response of the endpoint within 8 s.
  - Both failing on the first query → `SearchDrift`. A random 0.8–1.6 s pause between queries. It logs only the site
    and counts.
- `search_learn` keeps `SEARCH_PROBES = ("milk", "bread")`, `PRODUCTISH_KEY`, `candidates`, `find_query_param`,
  `find_location_inputs`, `find_store_binding` and `check_search_recipe` (which needs a location; the endpoint path
  must not match `guard.BLOCKED_URL`; allowlist regexes must be narrow; a POST must be covered).
- New `search_learn.find_fee_map(captured, lat, lon) -> dict | None`: LLM-proposed, kept only if each path resolves to
  a number (or a bool / str for `member`).
- `search_learn.learn_search(site, location, profile_dir, proxy_server=None, approve=None, preapproved=(), llm=None) -> dict`:
  1. Open the site read-only on `profile_dir`, which is **the admin's restored, logged-in profile**, and check that
     it is logged in.
  2. Probe each search page and collect the captured responses.
  3. A blocked POST carrying the probe → propose
     `{"url_regex": "^https://<host><path>$", "why": "product search (read-only)"}` (skipped when preapproved).
     `approve` must return True before the entry joins `guard.allowlist` and the page is reloaded. A rejection →
     `sync.RecipeError("POST allowlist rejected")`.
  4. The LLM writes the `field_map` and an optional `fee_map`, with up to 3 tries and error feedback. The recipe must
     yield ≥3 products. OpenAI calls pass through `metering.count_openai`.
  5. Run `check_search_recipe`.
  6. Validate by replay on the same profile with the second probe: fewer than 3 products → `RecipeError`.
  7. Return the recipe without saving it (sanitising is `apply_learned`'s job).

- [ ] **Step 1: Write the failing tests** (browser edges faked like `tests/server/helpers.fake_login_env`)

```python
def test_extract_products_maps_fields_and_divisor(): ...    # fixture → 3 products, 2900 paise → 29.0, banner dropped
def test_extract_fee_info_reads_member_and_delivery(): ...  # {"delivery": 0.0, "member": True}; missing paths absent
def test_to_stock_values():
    assert [search.to_stock(v) for v in (None, 0, 3, "OUT_OF_STOCK", "Sold out", "available")] == [True, False, True, False, False, True]
def test_build_request_get_with_location_header(): ...
def test_build_request_post_body_path_and_store(): ...
def test_search_products_fast_path(monkeypatch): ...        # 2 queries → products keys == queries; fee_info present
def test_search_uses_given_profile_read_only_with_media_blocked(monkeypatch): ...  # open_site profile_dir is the caller's; no login_mode
def test_search_logged_out_raises_session_expired(monkeypatch): ...   # ensure_logged_in fails → browser.SessionExpired propagates
def test_search_falls_back_to_search_page_capture(monkeypatch): ...
def test_search_drift_when_both_paths_fail(monkeypatch): ...
def test_unserviceable_when_store_lookup_empty(monkeypatch): ...
def test_find_location_inputs_query_header_body(): ...
def test_find_store_binding(): ...
def test_find_query_param_get_and_post(): ...
def test_find_fee_map_drops_unresolvable_paths(): ...
def test_check_rejects_recipe_without_location(): ...
def test_check_rejects_blocked_path_and_broad_regex(): ...
def test_learn_post_search_needs_approval(monkeypatch): ...  # approve → False: RecipeError, allowlist unchanged
def test_learn_preapproved_post_does_not_ask(monkeypatch): ...
def test_learn_requires_logged_in_profile(monkeypatch): ...  # login page → SessionExpired
def test_learn_validates_by_replay_with_second_probe(monkeypatch): ...
def test_learn_retries_field_map_with_error_feedback(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/test_search.py tests/test_search_learn.py -v` → FAIL.
- [ ] **Step 3: Implement** both modules. Reuse `recipes.get_path / to_number / first_request / apply_param`,
  `browser.fetch_in_page / goto`, `sync.ensure_logged_in` and `agent.captured_for`. No change to `guard.py`.
- [ ] **Step 4: Run** `pytest -q` → green (CLI tests untouched).
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): grocery search recipes with location binding, observed fees and supervised learning"`

---

### Task 5: PWA — the grocery list screen and cart client

**Files:**
- Create: `web/lib/cart.ts`, `web/components/cart/{ListEditor,ItemRow,UsualSheet,RecentChecks,AppAccessRow}.tsx`,
  `web/__tests__/cart-list.test.tsx`, `web/__tests__/cart-lib.test.ts`
- Modify: `web/app/(app)/cart/page.tsx` (replaces the Plan 3 placeholder)

**Interfaces:**
- Consumes: the T6 routes and errors (contract only); T3's summary shape; `getLimits()`; `apiFetch` / `ApiError`.
- Produces (`web/lib/cart.ts`):
  - Types `CartItem {text: string; qty: number; variant?: string; veg?: boolean}` and `CartSummary`, `CartCard`,
    `CartTable`, `CartPick`, `CartSplit`, which mirror T3's shape field for field (including `mode`, `title`,
    `count_label`, `price_note`, `offers_note`, `restaurant`, `member_note`, `action` and every card status).
  - `parseLine(s): CartItem` handles `"×2"`, `"x 2"`, `"* 2"`, `"2 x Bread"`, and defaults to qty 1.
  - `getUsual()`, `getAccess(mode)` → `{[site]: "ok"|"not_connected"|"needs_login"|"unavailable"}`,
    `startCheck(items)`, `setChoice(requestId, idx, site, id)`, `getRequest(id)`, `getResults(id)`,
    `watchCartRequest(id, onChange)` (Realtime on `cart_requests` `id=eq.` and `cart_results` `request_id=eq.`;
    returns an unsubscribe function), and `recentChecks(limit = 3)` (both modes).
- The `/cart` screen in Groceries mode (T13 adds the mode switch):
  - Title "Cart" and subtitle "Compare your prices on Zepto, Blinkit and Instamart."
  - The address row "Delivering to {area}". With no address: "Add your delivery address" linking to `/accounts`, and
    Compare disabled.
  - `AppAccessRow` shows the three app dots with "Connected", "Connect" or "Log in again" (each non-connected one
    links to `/accounts`). With none connected: "Connect Blinkit, Zepto or Instamart to compare." and Compare disabled.
  - An input with placeholder "Add an item, e.g. Amul Taaza 1L ×2"; rows with a qty stepper (1–20, 44px targets) and
    remove.
  - "Start from my usual" opens a sheet of usual items, all checked, with "×{times} in 90 days" and "Add {n} items".
    With none: "We'll suggest items once you've ordered groceries a few times."
  - The primary "Compare prices" button, with "{n} checks left today" under it. API `detail` is shown verbatim. On
    202 → `/cart/{request_id}`.
  - `?from=<id>` prefills the list. "Recent checks" shows 3 rows ("{count_label} · {best app} {total_label} · {ago}").
    Max 30 items.

- [ ] **Step 1:** Invoke `impeccable:impeccable`, `ui-ux-pro-max:ui-ux-pro-max` and `design-taste-frontend-v1`. Then
  write the failing tests:
  - `parseLine handles ×2, x 2 and 2 x`
  - `adds, steps and removes items`
  - `usual sheet adds checked items`
  - `empty usual shows the hint`
  - `compare posts the list and navigates`
  - `429 detail is shown and nothing navigates`
  - `no address disables compare with the link`
  - `no connected grocery app disables compare and links to accounts`
  - `needs_login app shows log in again`
  - `edit list prefills from a request`
  - `checks left line comes from limits`
  - `watchCartRequest subscribes to both tables and unsubscribes`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm --dir web test`, `pnpm --dir web lint`, `pnpm --dir web build` → pass; `/cart` at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): grocery list with usual items, connected-app status and live check start"`

---

## Wave B (after Wave A is merged; parallel, disjoint files)

### Task 6: Cart store and API (groceries) plus admin routes

**Files:**
- Create: `server/cart.py`, `server/cart_api.py` (`APIRouter(prefix="/cart")`), `tests/server/test_cart_store.py`, `tests/server/test_cart_api.py`
- Modify:
  - `server/config.py`: `Settings` gains `split_min_saving_inr: float = 20`, `cart_max_items: int = 30`,
    `food_max_items: int = 20`, `cart_stale_s: int = 1500`, `match_judge: bool = True`, `match_model: str = ""`,
    `menu_cache_ttl_s: int = 1800`, `food_lookups_per_day: int = 30`.
  - `server/api.py`: include the router.
  - `server/admin.py`: `site_health` also returns rows of kind `search` (the grocery sites) and of kinds
    `restaurants` / `menu` (swiggy, zomato), with 7-day `search` job outcomes per `op`. Every row carries
    `admin_connected` for the site's `db_site`.
  - `server/admin_api.py`, `docker-compose.yml` (the new env, set explicitly), `tests/server/test_config.py`.

**Interfaces:**
- Consumes: T1; T3 `build_summary / area_label / fees.validate`; `search.SEARCH_SITES`; Plan 4 `limits.*`,
  `usage.*`, `recipe_admin.is_paused / is_admin`.
- Produces (`server/cart.py`):
  - Exceptions: `NoAddress`, `NoConnectedApp(mode)`, `CheckRunning`, `Unavailable`, `NotFound`, `NotReady`,
    `BadChoice(ValueError)`.
  - `CONNECTED = ("connected", "syncing")`. `site_access(conn, user_id, sites, kinds_by_site) -> dict[str, str]`
    returns `"ok" | "not_connected" | "needs_login" | "unavailable"`:
    - Connection by `db_site`: `CONNECTED`, or `error` with `last_synced_at` → connected; `needs_login` →
      `needs_login`; otherwise `not_connected`.
    - A connected site whose recipe kinds aren't all healthy and unpaused → `unavailable`.
  - `normalize_items(items, max_items) -> list[dict]` merges equal `norm_name` lines (qty summed, capped at 20).
  - `usual_items(conn, user_id, now, days=90, min_times=3, limit=30) -> list[{text, qty, times}]`.
  - `create_check(conn, user_id, mode, items, sites, access, now, s, restaurant=None) -> dict` is shared with T12 and
    returns `{request_id, sites, cart_checks_left, enqueue: [sites]}`:
    1. Guards: no profile lat/lon → `NoAddress`; no site `"ok"` and no site merely `"unavailable"` → `NoConnectedApp`;
       no `"ok"` site → `Unavailable`; an active `search` job with a `request_id` for the user → `CheckRunning`.
    2. In one transaction: `limits.consume_cart_check`; insert the request (area, lat/lon, mode, restaurant); insert
       result rows for the non-ok sites (status = the access value); return the ok sites.
    3. The caller enqueues the jobs inside the same transaction. `jobs.Conflict` → `CheckRunning`, and the rollback
       restores the check.
  - `start_check(conn, user_id, items, now, s) -> dict` (groceries): `site_access` over `SEARCH_SITES` with kind
    `search`, then `create_check`, then one `search` job per ok site with payload `{"op": "products", "request_id"}`
    and the default profile.
  - `get_request`, `fee_rules(conn)`, `put_fee_rules(conn, site, rules, verified, admin_user_id) -> int`,
    `record_site_result(conn, request_id, site, status, items=(), meta=None, error=None)`.
  - `finalize_if_complete(conn, request_id, s) -> bool`: locks the row; a `failed` search job without a row →
    `failed` / `worker_lost`; once every site has a row, it builds the summary, writes the per-result fees and totals,
    and sets `status`, `summary` and `priced_at`. A failed summary refunds the check.
  - `finalize_stale(conn, s, now) -> int` (`cart_stale_s`).
  - `set_choice(conn, user_id, request_id, idx, site, choice_id, s) -> dict` never consumes a check.
- Produces (routes):
  - `GET /cart/usual` → `{items}`. `GET /cart/access?mode=grocery|food` → `{access: {site: status}}`.
  - `POST /cart/checks`:
    - Body: `{items: [{text 1..80, qty 1..20}] (1..CART_MAX_ITEMS)}` → 202 `{request_id, sites, cart_checks_left}`.
    - Errors: 422; 409 `"Add your delivery address first."`; 409
      `"Connect Blinkit, Zepto or Instamart to compare."`; 409 `"A cart check is already running."`; 503
      `"Cart checks are unavailable right now."`; and `limits.to_http` (429 `"You can check 10 carts a day. Try again tomorrow."`
      + `Retry-After`; 503 `"Cart checks are paused until 1 November."`-style).
  - `POST /cart/checks/{id}/choices`: body `{idx, site, product_id}` → 200 `{summary}`. Errors: 404, 409
    `"Prices are still loading."`, 422 `"That isn't one of the options."`.
  - Admin (`require_admin`):
    - `GET /admin/fees` (all five sites) and `PUT /admin/fees/{site}` with body `{rules, verified}` → `{version}`
      (422 `{detail: [errors]}`).
    - `POST /admin/learn/{site}?kind=orders|search|food`. `search` requires a site in `SEARCH_SITES`, `food` one in
      `("swiggy", "zomato")`; otherwise 400. Every kind requires the admin's own connection for the `db_site` to be
      `connected` (409 `"Connect {App} on your own account first."`) and an address. The job is a `learn` with
      payload `{"reason": "manual", "kind": kind}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_site_access_statuses(pg): ...                        # zepto connected → ok; blinkit needs_login; instamart via swiggy error+synced → ok; unhealthy recipe → unavailable
def test_normalize_merges_duplicate_lines(): ...
def test_usual_items_three_times_in_90_days_grocery_only(pg): ...
def test_start_check_enqueues_only_connected_sites(pg): ...   # zepto + instamart connected → 2 jobs (profiles zepto, swiggy); blinkit row not_connected; usage.cart_checks == 1
def test_no_connected_app_is_409_and_not_counted(pg): ...
def test_needs_login_site_gets_row_without_job(pg): ...
def test_finalize_waits_for_every_site_then_builds_summary(pg): ...
def test_finalize_treats_failed_job_without_result_as_failed(pg): ...
def test_all_sites_failed_refunds_check(pg): ...
def test_finalize_is_idempotent_under_race(pg, pg2): ...
def test_finalize_stale_uses_1500s(pg): ...                   # 20 min old → untouched; 26 min → failed/timeout
def test_set_choice_recomputes_without_consuming(pg): ...
def test_put_fee_rules_validates_food_and_grocery(pg, fee_rules_snapshot): ...
# test_cart_api.py — local fixture admin_token: helpers.add_user(pg, ADMIN, "admin@x.test"), admin.add_admin(pg, "admin@x.test"), auth_header(ADMIN)
def test_check_needs_address(client, token_a, pg): ...
def test_check_needs_connected_app(client, token_a, pg): ...  # 409 "Connect Blinkit, Zepto or Instamart to compare."
def test_second_check_while_running_is_409_and_not_counted(client, token_a, pg): ...
def test_eleventh_check_is_429(client, token_a, pg): ...
def test_check_paused_budget_is_503(client, token_a, pg): ...
def test_check_validates_items(client, token_a): ...
def test_access_endpoint(client, token_a, pg): ...
def test_choice_endpoint_scoped_to_owner(client, token_b, pg): ...
def test_no_fill_cart_route_or_job(client): ...
def test_admin_fees_get_put(client, admin_token, pg, fee_rules_snapshot): ...
def test_admin_learn_search_requires_admin_connection(client, admin_token, pg): ...   # instamart needs admin's swiggy connection
def test_admin_learn_food_kind(client, admin_token, pg): ...  # payload kind food; zepto with kind food → 400
def test_admin_sites_lists_search_and_food_rows(client, admin_token, pg): ...
def test_settings_cart_tunables_from_env(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_cart_store.py tests/server/test_cart_api.py tests/server/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green; `docker compose config -q` → 0.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): cart checks on connected apps with daily limit, choices and admin fee rules"`

### Task 7: PWA — ranked results, item table and pick-one (both modes)

**Files:**
- Create: `web/app/(app)/cart/[id]/page.tsx`, `web/components/cart/{SearchingState,ResultHeader,AppCard,ItemTable,SplitLine,PickSheet}.tsx`, `web/__tests__/cart-result.test.tsx`

**Interfaces:**
- Consumes: T5 `lib/cart.ts`; T3 summary strings; Plan 3 `lib/jobs.ts` for the user's running jobs (RLS).
- Produces, matching mockup A and then B:
  - **Searching state:**
    - "Checking your prices…", then one row per priced site (dot, name, and "Checking…" / "✓ Done" /
      "Couldn't check"), driven by `cart_results`.
    - When the user has a running `sync` or `connect` job on that site's profile: "Waiting for your {App} sync to
      finish…".
    - After 120 s: "Taking longer than usual. You can leave this screen. Your result will be here."
    - `failed` → "We couldn't get prices right now. This check wasn't counted." with "Try again".
  - **Header:** `title` (h1), then the muted line `"{count_label} · priced live at {area} · {ago} ago"` (refreshed
    every 60 s), then `price_note` in small muted text. Food shows the restaurant locality under the title. Older than
    30 min: "Prices may have changed." plus "Check again".
  - **Cards in order:**
    - Each card has a dot, the name and `total_label`. The cheapest card gets `ring-2 ring-accent` and a "Cheapest"
      pill.
    - Under the total: `fee_line`, a muted "fees estimated" tag, `member_note` (green), `fee_hint`, `stock_line`, swap
      pills (`.warn`), and "{n} to pick ›".
    - Food ok cards also show `offers_note` (muted).
    - Non-ok cards show `note`, plus an `action` button linking to `href` when present ("Connect" / "Log in again").
    - The cheapest card has the button "Fill cart on {App}", `disabled`, `aria-disabled="true"`, with "Coming soon".
      It never calls anything. Other ok cards show `delta_label`.
  - **"See item prices"** toggles `ItemTable` (mockup B): items × app columns ("Insta" under 360px), `price_label`
    cells with the cheapest green and bold, muted "n/a" / "Out of stock" / "Not available" (`text-ink-muted`), "Pick"
    as a button, and a "Total + fees" row. `SplitLine` renders `split.line` only when it is non-null.
  - **"Edit list"** → `/cart?from={id}` (food: `/cart?mode=food&from={id}`).
  - **`PickSheet`:** "Which one did you mean?" (food: "Which dish did you mean?"), radio candidates, "Use this" →
    `setChoice`. Errors are shown in the sheet.

- [ ] **Step 1:** Invoke the three design skills. Then write the failing tests, using fixture summaries built from
  T3's shape:
  - `cards render in order with cheapest ring and pill`
  - `fill button is disabled with coming soon and calls nothing`
  - `other cards show delta labels`
  - `swap note, member note and fees estimated render`
  - `not connected card links to accounts`
  - `needs login card shows log in again`
  - `food cards show offers note and restaurant title`
  - `closed card shows its note and no total`
  - `table toggles with highlighted cells and totals row`
  - `split line hidden for food`
  - `pick sheet posts the choice and updates totals`
  - `searching state shows waiting for sync`
  - `failed request offers try again and says not counted`
  - `stale prices offer check again`
  - `edit list links back with mode and from`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass; `/cart/[id]` at 375×812 and 390×844 has no horizontal scroll with the table open (7-item grocery and 3-dish food fixtures).
- [ ] **Step 5: Commit** — `git commit -m "feat(web): ranked results for groceries and food with item table and pick-one"`

### Task 8: PWA — admin fee rules and search/food learning

**Files:**
- Create: `web/components/admin/FeesPanel.tsx`, `web/__tests__/admin-fees.test.tsx`
- Modify: `web/app/(app)/admin/page.tsx` (segment "Fees" after "Sites"), `web/components/admin/SitesPanel.tsx`, `web/lib/admin.ts`

**Interfaces:**
- Consumes: T6 `GET/PUT /admin/fees`, `POST /admin/learn/{site}?kind=search|food`, and the `/admin/sites` rows.
- Produces:
  - **`SitesPanel`** groups rows under "Order history", "Grocery prices" and "Restaurants & menus".
    - Learn calls the matching kind and is disabled with "Connect {App} on your own account first" unless
      `admin_connected`.
    - A running learn shows `approval_needed` with Approve / Reject (Plan 4 component).
  - **`FeesPanel`** has one card per site, with a badge "Verified" or "Unverified — check {App}'s fee page".
    - Grocery form: delivery tiers (first minimum fixed at 0), handling, a small-cart toggle, surge, and membership
      (name + waived fees as checkboxes).
    - Food form: distance tiers ("Up to {km} km → ₹{fee}", last tier "Beyond"), packaging per dish, platform fee, a
      small-order toggle, surge, GST food % and GST fees %, and membership.
    - Both forms: a source URL, a "These match the app today" checkbox, and Save → PUT. 422 errors are listed, and the
      version is shown after saving.
    - Numeric inputs use `inputmode="decimal"`.

- [ ] **Step 1:** Invoke the three design skills, then write the failing tests:
  - `learn disabled without admin connection`
  - `restaurants and menus row learns kind food`
  - `grocery fees save`
  - `food fees save with gst`
  - `first tier minimum is fixed at zero`
  - `422 errors are listed`
  - `unverified badge shows`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass; `/admin` Fees at 390×844 has no horizontal scroll.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): admin fee rules for groceries and food and supervised search learning"`

### Task 11: Food search recipes in the engine — restaurants, menus and supervised learning

**Files:**
- Create: `food_search.py`, `food_learn.py`, `tests/test_food_search.py`, `tests/test_food_learn.py`, and the
  synthetic fixtures `tests/fixtures/restaurants_search.json` and `tests/fixtures/menu_nested.json` (categories →
  items, variants, veg flags, per-dish packaging, `isOpen`).

**Interfaces (Produces):**
- Recipes:

```text
restaurants: {kind: "restaurants", site, search_page_url, endpoint, method, query, body, headers, query_param,
              location_inputs, field_map: {restaurants_path, restaurant_id, name, locality, lat, lon, distance_km, is_open, url},
              fee_map: null | {...}, read_post_allowlist}
menu:        {kind: "menu", site, page_url_template ("…{restaurant_id}…") | null, endpoint, method, query, body, headers,
              restaurant_param, location_inputs (may be empty), field_map: {dishes_path, dish_id, name, variant, price, veg,
              in_stock, packaging, restaurant_open, price_divisor}, fee_map: null | {delivery, packaging, platform, small_order,
              surge, member, distance_km, divisor}, read_post_allowlist}
```

- `food_search.FOOD_SITES = ("swiggy", "zomato")`. The learning starting points are
  `FOOD_PAGES = {"swiggy": "https://www.swiggy.com/search?query={q}", "zomato": "https://www.zomato.com/{city}/delivery?q={q}"}`
  (`{city}` comes from `browser.city_slug(location)`), and each recipe stores its own URL. `RESTAURANT_PROBE = "pizza"`.
- `food_search.collect(data, path) -> list`: dotted paths with a `*` fan-out over lists, for nested menu categories.
  `recipes.get_path` is unchanged.
- `food_search.extract_restaurants(data, field_map) -> list[dict]` (`listing_from_raw` shape);
  `food_search.extract_menu(data, field_map) -> {"is_open": bool | None, "dishes": [{dish_id, name, variant, price, veg, in_stock, packaging}]}`.
- `food_search.search_restaurants(site, query, location, recipe, profile_dir, proxy_server=None, block_media=True, max_results=20) -> {"restaurants": [...], "fee_info": {...}}`
  and `food_search.fetch_menu(site, restaurant_id, location, recipe, profile_dir, proxy_server=None, url=None, block_media=True) -> {"is_open", "dishes", "fee_info"}`.
  - Both run on the caller's (the user's) profile, read-only, with the same fast-path → page-capture fallback and the
    same errors as `search` (`SearchDrift`, `Unserviceable`, `browser.SessionExpired`).
- `food_learn.learn_food(site, location, profile_dir, proxy_server=None, approve=None, preapproved=(), llm=None) -> {"restaurants": dict, "menu": dict}`:
  - Runs on the admin's logged-in profile.
  - Steps: probe search → candidates with restaurant-ish keys → LLM field map (≥3 restaurants) → open the first open
    restaurant's page → capture its menu → LLM field map (≥5 dishes with name and price; `dishes_path` may use `*`) →
    optional `fee_map`s.
  - It reuses `search_learn.find_query_param / find_location_inputs` and its POST approval flow.
  - `check_food_recipe(recipe) -> str | None`: `restaurants` must bind a location; `menu` must have
    `restaurant_param` or `page_url_template`.
  - It validates by replaying both on the same profile with a second restaurant.

- [ ] **Step 1: Write the failing tests**

```python
def test_collect_fans_out_with_star(): ...                  # "data.cards.*.items.*" over 2 categories → 5 dishes
def test_extract_menu_variants_veg_packaging_and_open(): ...
def test_extract_restaurants_maps_locality_and_coords(): ...
def test_search_restaurants_fast_path_on_user_profile(monkeypatch): ...
def test_fetch_menu_by_param_and_by_page_template(monkeypatch): ...
def test_food_logged_out_raises_session_expired(monkeypatch): ...
def test_menu_drift_when_both_paths_fail(monkeypatch): ...
def test_check_food_recipe_rules(): ...                     # restaurants without location → error; menu without param/template → error
def test_learn_food_returns_both_recipes(monkeypatch): ...  # stub llm; restaurants + menu validated by replay
def test_learn_food_post_needs_approval(monkeypatch): ...
```

- [ ] **Step 2: Run** `pytest tests/test_food_search.py tests/test_food_learn.py -v` → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(engine): restaurant search and menu recipes with supervised learning"`

### Task 13: PWA — food mode (restaurant, dishes, compare)

**Files:**
- Create: `web/lib/food.ts`, `web/components/cart/food/{FoodFlow,RestaurantSearch,RestaurantPairCard,SameRestaurantSheet,MenuPicker,DishRow}.tsx`, `web/__tests__/food.test.tsx`
- Modify: `web/app/(app)/cart/page.tsx` (adds the segmented control "Groceries | Food" at the top; `?mode=food`
  selects Food; Groceries stays the default)

**Interfaces:**
- Consumes: the T12 routes (contract only; T12 lands in Wave C); T5 `lib/cart.ts` (`getAccess("food")`, types);
  Plan 3 `watchJob`.
- Produces (`web/lib/food.ts`):
  - `getUsualRestaurants()`.
  - `searchRestaurants(q)` → `{status: "ready", results} | {status: "searching", job_ids}`.
  - `loadMenus(listings)` → ready `{restaurant, menu, usual}` | searching.
  - `startFoodCheck(body)` → `{request_id, cart_checks_left}`.
  - `waitForJobs(jobIds)` resolves when all are done or failed (via `watchJob`).
- The Food flow:
  1. **Restaurant.**
     - `AppAccessRow` for Swiggy and Zomato. With neither connected: "Connect Swiggy or Zomato to compare." and the
       flow is disabled.
     - "Restaurants you order from": up to 10 rows (name, app dots, "{n} orders"); tapping one searches its name.
     - A search field ("Search a restaurant", submitted on Enter or the button, never per keystroke); while searching,
       "Searching Swiggy and Zomato…".
     - Results are `RestaurantPairCard`s: name, locality, distance, app dots for the listings present, and `note`.
     - Tapping an "ask" pair opens `SameRestaurantSheet`, with both listings side by side (name, locality, distance,
       open/closed) and "Yes, same restaurant" / "No, different". "No" keeps only the tapped app's listing and records
       the other as declined.
     - 429 / 503 / 409 `detail` is shown verbatim.
  2. **Dishes.**
     - A header with the restaurant and "Closed on {App} right now." per closed app.
     - "Your usual dishes" with add buttons, then the full menu from the primary listing: Swiggy when it is present
       and open, else Zomato.
     - The menu has a filter field and rows with a veg/non-veg dot, the name, the variant and `price_label`;
       unavailable rows are greyed "Not available". Selected dishes get a qty stepper.
     - A sticky bottom bar (safe-area aware): "Compare {n} dishes" plus "{n} checks left today".
  3. **Compare** → `startFoodCheck({restaurant: {name, swiggy_id?, zomato_id?, confirmed, declined}, items: [{text, qty, variant, veg}]})`
     → `/cart/{id}`. `?from=<id>` restores the restaurant and dishes.

- [ ] **Step 1:** Invoke the three design skills, then write the failing tests:
  - `mode switch shows food flow and keeps groceries default`
  - `no connected food app disables the flow`
  - `usual restaurant tap searches by name`
  - `searching waits for jobs then shows pairs`
  - `ask pair opens the sheet and records the answer`
  - `only on one app note renders`
  - `menu shows veg dots, variants and not available`
  - `closed on one app is shown in the header`
  - `compare posts restaurant and dishes then navigates`
  - `lookup limit detail is shown`
- [ ] **Step 2: Run** `pnpm --dir web test` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, lint and build → pass; Food at 390×844 has no horizontal scroll (menu with 40 dishes, bottom bar visible).
- [ ] **Step 5: Commit** — `git commit -m "feat(web): food mode with restaurant pairing, menu picking and compare"`

---

## Wave C (after Wave B is merged)

### Task 12: Food store and API

**Files:**
- Create: `server/food_cart.py`, `server/food_api.py` (`APIRouter(prefix="/cart/food")`), `tests/server/test_food_cart.py`, `tests/server/test_food_api.py`
- Modify: `server/api.py` (include the router)

**Interfaces:**
- Consumes: T2 `match.parse_dish_query / dish_from_raw / match_dish` and `restaurant_match.*`; T3; T6
  `cart.site_access / create_check / record_site_result / finalize_if_complete / NoAddress / NoConnectedApp / CheckRunning / Unavailable`;
  `food_search.FOOD_SITES`; Plan 4 `limits.LimitExceeded / BudgetPaused / to_http`, `budget.is_paused`.
- Produces (`server/food_cart.py`):
  - `usual_restaurants(conn, user_id, limit=10) -> list[{name, sites, orders, last_order_at}]`: non-cancelled
    orders of Swiggy (`sub_platform` null) and Zomato, grouped by `norm_name(merchant)`, ordered by orders desc, then
    recency.
  - `usual_dishes(conn, user_id, restaurant_name, limit=15) -> list[{text, qty, times}]`: order items at that
    `norm_name(merchant)` on the food sites.
  - `geo_key(lat, lon) -> str` (`f"{lat:.2f},{lon:.2f}"`).
  - Cache helpers: `get_restaurants / put_restaurants(conn, user_id, site, geo_key, query, results)` and
    `get_menu / put_menu(conn, user_id, site, restaurant_id, menu)`, with the TTL `s.menu_cache_ttl_s`;
    `purge_cache(conn, older_than_s=86400) -> int`.
  - `lookups_left(conn, user_id, now, s) -> int`: today's (IST) `search` jobs with `op` in `('restaurants', 'menu')`
    and no `request_id`.
  - `lookup_restaurants(conn, user_id, query, now, s) -> dict`:
    - `site_access` with kind `restaurants`. Cached ok sites are used as is.
    - The rest: `budget.is_paused` → `BudgetPaused("cart", …)`; `lookups_left` < number needed → `LimitExceeded("food_lookup", 30, …, "You can search restaurants 30 times a day. Try again tomorrow.")`.
    - Then one `search` job per missing ok site, with payload `{"op": "restaurants", "query"}`.
    - Returns `{"status": "ready", "results": pair_results(...), "access"}` or `{"status": "searching", "job_ids", "access"}`.
  - `lookup_menus(conn, user_id, listings: dict[str, str], now, s) -> dict`: the same pattern with kind `menu` and
    payload `{"op": "menu", "restaurant_id", "url"}`. When ready, it returns `restaurant`, the primary menu (dish rows
    with `price_label`) and `usual_dishes`.
  - `price_menu(site, menu: dict, items: list[dict]) -> tuple[str, list[dict], dict]` → `(status, matched_items, meta)`:
    `is_open is False` → `"closed"`; otherwise it runs `match_dish` per item;
    `meta = {fee_info (observed packaging = Σ dish packaging × qty when all present), is_open, menu_fetched_at}`.
  - `start_food_check(conn, user_id, restaurant, items, now, s) -> dict`:
    1. Validates: ≥1 listing id. Both ids present → the pair must score ≥ SURE from cached listings, or be
       `confirmed`; otherwise `ValueError("Confirm it's the same restaurant first.")`.
    2. A site without an id gets `not_listed`, recorded as declined when the user declined it.
    3. The listing snapshots (with `distance_km` from the cache or by haversine) are copied into `restaurant`.
    4. `create_check(mode="food")`. For each ok site, a fresh cached menu → `price_menu` and `record_site_result`
       inline; otherwise enqueue `search` with payload `{"op": "menu", "restaurant_id", "url", "request_id"}`. Then
       `finalize_if_complete`.
- Produces (routes):
  - `GET /cart/food/usual` → `{restaurants}`.
  - `GET /cart/food/restaurants?q=` (2..60) → 200 ready | 202 searching.
  - `POST /cart/food/menus` with body `{name, swiggy_id?, zomato_id?}` → 200 ready | 202 searching.
  - `POST /cart/food/checks` with body
    `{restaurant: {name, swiggy_id?, zomato_id?, confirmed: bool, declined: [site]}, items: [{text 1..80, qty 1..20, variant?, veg?}] (1..FOOD_MAX_ITEMS)}`
    → 202 `{request_id, sites, cart_checks_left}`.
  - Errors: 409 `"Connect Swiggy or Zomato to compare."`; 422 `"Confirm it's the same restaurant first."`; and the
    same 409/429/503 strings as T6 (lookups: 429 with the lookup message).

- [ ] **Step 1: Write the failing tests**

```python
def test_usual_restaurants_from_food_orders(pg): ...          # swiggy food + zomato grouped; instamart and cancelled excluded
def test_usual_dishes_at_restaurant(pg): ...
def test_lookup_uses_fresh_cache_without_jobs(pg): ...        # cached 10 min ago → ready, 0 jobs, lookups_left unchanged
def test_lookup_expired_cache_enqueues_and_counts(pg): ...    # 31 min old → 202 searching, lookups_left − 1 per site
def test_cache_is_per_user(pg): ...                           # B never gets A's cached menu
def test_lookup_limit_and_budget_pause(pg): ...
def test_lookup_only_connected_food_apps(pg): ...             # zomato needs_login → access shows it, no zomato job
def test_food_check_inline_when_menus_fresh(pg, fee_rules_snapshot): ...   # status done immediately; summary total_labels ["₹836", "₹878"]
def test_food_check_enqueues_menu_jobs_when_stale(pg): ...
def test_food_check_counts_one_cart_check(pg): ...
def test_unconfirmed_ask_pair_is_rejected(pg): ...
def test_declined_site_is_not_listed_with_note(pg): ...
def test_closed_menu_priced_as_closed(pg): ...
def test_purge_cache_older_than_a_day(pg): ...
# test_food_api.py
def test_food_routes_auth_and_validation(client, token_a, pg): ...   # 422 for 21 dishes; 409 with no food app connected
def test_food_check_route_returns_202(client, token_a, pg): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_food_cart.py tests/server/test_food_api.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(server): food compare with per-user restaurant and menu caches, pairing and lookup cap"`

---

## Wave D (after Wave C is merged)

### Task 9: Worker — search jobs on the user's session, learning, fakes and stale checks

**Files:**
- Modify: `server/worker.py`, `server/fake_sites.py`, `server/scheduler.py` (the tick calls
  `cart.finalize_stale(conn, s, now)` and `food_cart.purge_cache(conn)`; the result gains `"carts_finalized"`)
- Test: `tests/server/test_worker_plan5.py`, `tests/server/test_fake_sites.py`, `tests/server/test_scheduler.py`

**Interfaces:**
- Consumes: T2 matching; T4 `search.search_products` and `search_learn.learn_search`; T11
  `food_search.search_restaurants / fetch_menu` and `food_learn.learn_food`; T6 `cart.*`; T12
  `food_cart.put_restaurants / put_menu / price_menu`; Plan 4 `recipe_admin.*`, `notify.enqueue / alert_admins / dispatch`;
  Plans 1–2 `vault`, `_ensure_proxy_session`, `Relay`.
- Produces:
  - `worker.JOB_TYPES = ("connect", "sync", "learn", "search")`.
  - The `search` job runs through `run_job`'s normal path, exactly like a `sync` of that site:
    - Vault restore (`VaultError` → result `needs_login` and connection `needs_login`), `_ensure_proxy_session`,
      `Relay` with the user's session, `_location(conn, user_id)`, heartbeat and metering.
    - Then `_do_search(job, conn, s, profile_dir, proxy_server)` dispatches on `payload.op`:
      - **`products`**:
        1. A missing request → failed `request_gone`.
        2. A recipe that isn't healthy or is paused → `unavailable`.
        3. `search_products(..., block_media=s.sync_block_media)`.
        4. `match_item` per item, with a judge when `s.match_judge` and not in fake mode.
        5. `record_site_result(..., "ok", items, meta={"fee_info"})`.
      - **`restaurants`**: `search_restaurants`, then `put_restaurants`; progress `stage="found", count`.
      - **`menu`**: `fetch_menu`, then `put_menu`. With a `request_id`: `price_menu`, then `record_site_result`.
    - Success → `save_profile()`. It never changes the connection status, except to `needs_login`.
    - `browser.SessionExpired` →
      - result `needs_login` when there is a `request_id`;
      - connection `needs_login` when not already;
      - `notify.enqueue(kind="needs_login", title=f"{App} logged you out", body="Log in again to keep comparing prices and syncing orders.", url="/accounts", user_id=…, dedupe_key=f"needs_login:{job.id}")`.
    - `Unserviceable` → `unserviceable`. `SearchDrift` → `failed` / `recipe_drift`, plus R11 for that op's kind:
      `admin_learner(conn, db_site)` gets an automatic `learn` with payload
      `{"reason": "drift", "auto": true, "kind": "search"|"food"}`; with no learner, `pause_site` +
      `alert_admins(kind="site_paused", title=f"{App} prices paused", body="Its price recipe stopped working and no admin account is connected to re-learn it.", dedupe_key=f"site_paused:{kind}:{site}:{today}")`.
    - Any other error → `failed`.
    - `finally`: `finalize_if_complete` when there is a `request_id`.
  - Learn jobs with `payload.kind` `"search"` / `"food"` follow Plan 4's learn path on the admin's own restored
    profile. Approver as in Plan 4, and preapproved = the current allowlists.
    - `learn_search` → `apply_learned(…, "search", …)`.
    - `learn_food` → `apply_learned` for `restaurants`, then for `menu`.
    - `auto` failure or pending → pause and alert.
  - Fake mode (`WORKER_FAKE=1`; no browser, vault or proxy):
    - Connection rules still apply: only connected apps get jobs, and `needs_login` comes from the connection row.
    - A runtime logout is simulated by `fake_sites.LOGGED_OUT_SITES`, read from env `FAKE_LOGGED_OUT_SITES`
      (comma-separated, local only): those sites raise `browser.SessionExpired`.
    - `fake_sites.search(site, queries, **_) -> {"products", "fee_info": {}}` serves the grocery `FAKE_CATALOG`:
      - The 12 `GROCERIES` plus Amul Taaza Toned Milk 1 L (60), Milk Bikis Biscuits 200 g (30), and Lay's Classic
        Salted Chips 52 g and Lay's Magic Masala Chips 52 g (20 each).
      - Base prices: Amul Taza Milk 500ml 29, Farm Eggs 6 pcs 54, Banana Robusta 1kg 60, Onion 1kg 40, Tomato 500g 30,
        Britannia Brown Bread (400 g) 50, Amul Butter 100g 58, Maggi Noodles 4 pack 56, Coriander Bunch 10,
        Aashirvaad Atta 5kg 260, Curd 400g 35, Potato 1kg 35.
      - Site price = `round_half_up(base × {"zepto": 0.92, "blinkit": 1.00, "instamart": 1.06})`; MRP = ×1.1;
        `product_id = f"{site}-{slug}"`.
      - Instamart has Lay's Classic out of stock; Blinkit has no Coriander Bunch. A query returns the entries that
        share a `match.tokens` token.
    - `fake_sites.search_restaurants(site, query, **_)`:
      - Listings: Meghana Foods (both apps, 22.3310, 73.1510, Gorwa); "Truffles" (Swiggy) vs "Truffles Cafe" (Zomato)
        (22.3290, 73.1490, Gorwa); Behrouz Biryani (Swiggy at 22.3320, 73.1480, Gorwa; Zomato in both Gorwa at the
        same coordinates and Alkapuri at 22.3100, 73.1800); Burger Singh (both apps, 22.3305, 73.1505; Zomato
        `is_open` false); Theobroma (Swiggy only, 22.3295, 73.1495).
      - The query matches on any shared name token.
    - `fake_sites.fetch_menu(site, restaurant_id, **_)`:
      - Meghana Foods on Swiggy: "Chicken Biryani (Half)" 220 (packaging 15), "Chicken Biryani (Full)" 380 (20),
        "Paneer Biryani" 260 (10, veg), "Veg Biryani" 200 (10, veg), "Chicken 65" 240 (10). All except the two veg
        dishes are non-veg.
      - Meghana Foods on Zomato (no packaging field): "Chicken Biryani - Half" 235, "Chicken Biryani - Full" 395,
        "Paneer Biryani" 250, "Veg Biryani" 205, "Chicken 65" 245.
      - Other restaurants: three deterministic dishes each.
    - `fake_sites.learn(site, approve, preapproved=(), kind="orders", **_)`:
      - `kind="search"` → GET recipes for zepto and blinkit; POST for instamart with
        `^https://fake\.instamart\.test/api/search$`, which needs approval.
      - `kind="food"` → `{"restaurants", "menu"}`, GET for swiggy; zomato restaurants POST with
        `^https://fake\.zomato\.test/api/search$`, which needs approval.

- [ ] **Step 1: Write the failing tests**

```python
def test_search_job_restores_and_saves_user_profile(pg, monkeypatch): ...  # vault restore + save called for the user's profile name; search got that profile_dir
def test_search_job_uses_users_sticky_proxy_session(pg, monkeypatch): ...  # proxy_url_for got connections.proxy_session_id
def test_search_job_writes_matched_result_and_finalizes(pg, monkeypatch): ...
def test_search_job_records_usage(pg, monkeypatch): ...      # proxy_mb > 0 from the relay, ai_tokens from the judge, syncs 0
def test_search_job_passes_block_media(pg, monkeypatch): ...
def test_logged_out_search_marks_needs_login_and_notifies(pg, monkeypatch): ...  # result needs_login; connection needs_login; one notification row; profile not saved
def test_vault_error_is_needs_login(pg, monkeypatch): ...
def test_unserviceable_result(pg, monkeypatch): ...
def test_search_drift_enqueues_auto_learn_for_connected_admin(pg, monkeypatch): ...
def test_search_drift_without_admin_pauses_and_alerts(pg, monkeypatch): ...
def test_restaurants_op_fills_user_cache(pg, monkeypatch): ...
def test_menu_op_with_request_prices_and_finalizes(pg, monkeypatch): ...
def test_learn_search_and_food_jobs_apply_recipes(pg, monkeypatch): ...      # food → recipes restaurants v1 and menu v1
def test_learn_food_waits_for_post_approval(pg, pg2, monkeypatch): ...
def test_stale_request_is_finalized_failed(pg): ...          # 26 min old → failed, refunded; 20 min old → untouched
# test_fake_sites.py
def test_fake_cart_ranks_zepto_blinkit_instamart(pg, fake_mode): ...
    # all three connected; list [("Amul Taza Milk 500ml", 2), ("Aashirvaad Atta 5kg", 1), ("Farm Eggs 6 pcs", 1),
    # ("Lay's Classic", 1), ("Britannia Brown Bread", 1)] → total_labels ["₹422", "₹451", "₹479"], deltas [None, "+₹29", "+₹57"],
    # instamart swap "Lay's Classic Salted Chips 52 g swapped for Lay's Magic Masala Chips 52 g",
    # split.line "No item is cheaper on another app, so one order on Zepto is best."
def test_fake_only_connected_apps_priced(pg, fake_mode): ... # only zepto connected → blinkit "Connect Blinkit to compare.", instamart "Connect Swiggy to compare Instamart."
def test_fake_runtime_logout(pg, fake_mode, monkeypatch): ...  # FAKE_LOGGED_OUT_SITES=blinkit → blinkit needs_login card + notification
def test_fake_milk_is_pick_one(pg, fake_mode): ...
def test_fake_food_meghana(pg, fake_mode): ...               # "Chicken Biryani (Half)" ×2 + "Paneer Biryani" → ["₹836", "₹878"], "+₹42", Paneer cheapest on Zomato
def test_fake_behrouz_pairs_nearby_branch(pg, fake_mode): ...  # Gorwa↔Gorwa sure; Alkapuri separate "Only on Zomato"
def test_fake_truffles_asks_and_burger_singh_closed_on_zomato(pg, fake_mode): ...
def test_fake_learn_asks_approval_for_post_recipes(): ...
```

- [ ] **Step 2: Run** `pytest tests/server/test_worker_plan5.py tests/server/test_fake_sites.py tests/server/test_scheduler.py -v` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(worker): compare jobs on the user's own session, supervised search and food learning, fakes"`

---

## Wave E

### Task 10: End-to-end — groceries and food locally on an iPhone viewport

**Files:**
- Create: `e2e/tests/plan5.spec.ts`
- Modify:
  - `e2e/helpers/stack.ts`: `setAddress(email, address, lat, lon)` and `setConnectionStatus(email, site, status)`,
    both via `$E2E_PY` + `psycopg` against `DATABASE_URL`.
  - `scripts/e2e_local.sh`: exports `MATCH_JUDGE=0` to the worker.
  - `docs/e2e-local.md`.

**Interfaces:** `plan5.spec.ts` is serial and uses the iPhone 14 profile. The address used is
"12 Shanti Society, Gorwa, Vadodara, Gujarat" (22.3300, 73.1500).
1. `admin learns price recipes`:
   - The admin connects fake Zepto, Blinkit, Swiggy and Zomato (`123456`), and gets an address.
   - Grocery prices: Learn Zepto and Blinkit → Healthy v1; Learn Instamart → approve
     `^https://fake\.instamart\.test/api/search$` → Healthy.
   - Restaurants & menus: Learn Swiggy → Healthy; Learn Zomato → approve `^https://fake\.zomato\.test/api/search$` →
     Healthy.
2. `connect prompts for unconnected apps`:
   - User B connects only fake Zepto and has the address. `/cart` shows Blinkit and Instamart as "Connect".
   - Compare one item → the Blinkit card reads "Connect Blinkit to compare." with a "Connect" button to `/accounts`,
     and Instamart reads "Connect Swiggy to compare Instamart.".
   - `setConnectionStatus(B, "zepto", "needs_login")` → Compare is disabled with
     "Connect Blinkit, Zepto or Instamart to compare.", and the Zepto row shows "Log in again".
3. `usual items come from past orders`: user A connects fake Zepto, Blinkit, Swiggy and Zomato and has the address.
   "Start from my usual" lists items with "×N in 90 days" (N ≥ 3).
4. `cheapest cart ranks apps with fees`:
   - Use T9's 5-item list. The header reads "5 items · priced live at Gorwa, Vadodara" with the price note.
   - Zepto "₹422" has the ring and "Cheapest"; Blinkit shows "₹451 +₹29"; Instamart shows "₹479 +₹57" with the swap
     pill. "fees estimated" is visible.
   - "Fill cart on Zepto" is disabled with "Coming soon".
5. `item table and split line`: the totals row is "₹422 ₹451 ₹479", and the line reads "No item is cheaper on another
   app, so one order on Zepto is best."
6. `pick one`: Edit list → "milk" + "Aashirvaad Atta 5kg" → every card shows "1 to pick". On Zepto, choose "Amul Taaza
   Toned Milk 1 L" → "₹309", ranked first; `/me/limits` is unchanged by the choice.
7. `food compare`:
   - Cart → Food. "Restaurants you order from" lists Meghana Foods; tap it → a pair card with both app dots.
   - Add "Chicken Biryani (Half)" ×2 and "Paneer Biryani" from the menu → "Compare 3 dishes".
   - The title is "Meghana Foods", with "3 dishes · priced live at Gorwa, Vadodara".
   - Swiggy shows "₹836" with "Cheapest" and the fee line
     "Items ₹700 + delivery ₹30 + packaging ₹40 + platform ₹20 + GST ₹46"; Zomato shows "₹878 +₹42". "Offers in the
     app may lower this." is shown, and "Fill cart on Swiggy" is disabled.
   - The table highlights Paneer Biryani on Zomato. There is no split line. `/me/limits` dropped by exactly 1.
8. `restaurant pairing edge cases`:
   - "Behrouz" → a Gorwa pair card plus a separate Alkapuri card "Only on Zomato".
   - "Truffles" → "Check this is the same restaurant" → the sheet shows both → "Yes, same restaurant".
   - "Burger Singh" → the menu header shows "Closed on Zomato right now.".
   - "Theobroma" → "Only on Swiggy".
9. `logged out at compare time`: restart the worker with `FAKE_LOGGED_OUT_SITES=blinkit`, then compare groceries →
   the Blinkit card reads "Blinkit logged you out. Log in again to compare."; Mailpit receives "Blinkit logged you
   out"; Accounts shows Blinkit "Needs login". Restart the worker without it.
10. `daily limit`: run the remaining checks through the API up to 10 → "You can check 10 carts a day. Try again
    tomorrow." and "0 checks left today".
11. `budget pause`: a third invited user with a connection and an address; `schedulerOnce({MONTHLY_BUDGET_INR: "0.01"})`
    → Compare shows `/Cart checks are paused until 1 \w+\./`, and a restaurant search shows the same. Then
    `schedulerOnce({})`.
12. `no horizontal scroll` on `/cart` (both modes) and `/cart/[id]` (both modes, table open).

- [ ] **Step 1: Write the spec** (it fails until the stack runs).
- [ ] **Step 2: Implement** the helpers, the script (including a `restartWorker(env)` helper in `stack.ts`) and the docs.
- [ ] **Step 3: Run** `bash scripts/e2e_local.sh` → `product.spec.ts`, `plan4.spec.ts` and `plan5.spec.ts` all green; `pytest -q` and `pnpm --dir web test` still green.
- [ ] **Step 4: Commit** — `git commit -m "test(e2e): groceries and food compare on users' own sessions, connect prompts, pairing and limits"`

---

## Self-review

- **Spec and amendment coverage:**
  - Grocery list and "usual" (3+ times in 90 days): T5, T6.
  - Search per grocery app at the user's address, learned once per site, mapping name, brand, pack size, price, MRP,
    stock and id: T4, T9.
  - Product matching with the small-model check and pick-one: T2, T7.
  - Fees (delivery, handling, small-cart, surge; "fees estimated"): R3, T1, T3, T8.
  - Ranked cards, "+₹X", table, split line, Edit list, Fill disabled: T3, T7.
  - `cart_*` tables with RLS: T1. 10 checks a day and the budget pause: T6, T12.
  - Supervised learning, POST approval and drift: T4, T6, T8, T9, T11.
  - Location: saved lat/lon injected into every request and stored for Plan 6: R2, T6.
  - Known risks: proxy GB metered, media blocked, a paused kind → unavailable: T9.
  - Amendment 1 (food):
    - Two modes in one tab: T5, T13.
    - Restaurants you order from and usual dishes: T12, T13.
    - Restaurant pairing with ask / "Only on": R13, T2, T13.
    - Dish rules (half/full, serves, veg, add-ons): R14, T2.
    - Food fees with GST and the offers note: R3, T1, T3, T8.
    - Restaurants and menu recipe kinds and learning: R15, T11, T9.
    - Menu cache TTL: R17, T12.
    - Open/closed and availability shown: T3, T13.
    - Food fakes with exact totals: T9. Food e2e: T10.
    - A food compare is one check: T12.
  - Amendment 2 (own session):
    - Vault profile, sticky proxy, saved address: R1, T9.
    - Connected apps only, with "Connect / Log in again": T3, T5, T6, T7, T13.
    - Profile lock and ordering: R16, T1.
    - Personal prices: observed fees, memberships, "your prices" note: R3, T3, T4, T11.
    - Vault save and logout handling with notification: T9.
    - Read-only guard: T4, T11.
    - Per-user cost metering: T9.
    - Admin-only learning on the admin's own accounts: R2, T6, T9.
    - Fake connected / unconnected / needs_login and the e2e: T9, T10.
- **Type consistency:** these names match between producer and consumer tasks: `match.match_item / match_dish`,
  `restaurant_match.pair_results`, `fees.compute / compute_food`, `cart_rank.build_summary`,
  `cart.site_access / create_check / record_site_result / finalize_if_complete / finalize_stale / set_choice`,
  `food_cart.price_menu / put_menu / put_restaurants`, `search.search_products` → `{"products", "fee_info"}`,
  `food_search.search_restaurants / fetch_menu`, `search_learn.learn_search`, `food_learn.learn_food`. The summary
  shape is written once (T3) and mirrored in `lib/cart.ts` (T5).
- **Ordering** (task numbers are stable IDs; waves set the order):
  - Wave 0: T1.
  - Wave A: T2–T5, pure or contract-only with disjoint files.
  - Wave B: T6, T7, T8, T11 and T13, with disjoint files. T13 edits `cart/page.tsx` after T5 created it, and
    `server/admin*.py` are only in T6.
  - Wave C: T12, which needs T2, T3 and T6.
  - Wave D: T9, the only task touching `server/worker.py`, `server/fake_sites.py` and `server/scheduler.py`.
  - Wave E: T10.
