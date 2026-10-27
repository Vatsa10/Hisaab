# Expense Agent as a product: design

Date: 2026-10-27. Status: approved in conversation; the owner asked the builder to take it from here.

## Intent

Today `expense-agent` is a CLI that works for one person on one PC. It logs in to Blinkit, Zepto, Swiggy
and Zomato, learns each app's order-history API, syncs every order into SQLite, and reports where the
money went. The product puts this in front of ordinary people in India:

1. **Know your spending.** Connect your delivery apps once and see all your food and grocery spending in
   one place: per month, per app, per restaurant and per category. You can also ask an assistant about it.
2. **Spend less.** Give a grocery list (or start from what you usually buy). The agent compares live
   prices across Blinkit, Zepto and Instamart at your address and shows the cheapest cart. If you want,
   it fills that cart in the app; you pay there yourself.

Audience: friends first, then a public launch. It has to work for strangers from day one, with many
accounts at once, without the owner babysitting it. Primary device: iPhone.

## Goals

- Any user can sign up, connect apps with phone + OTP from their phone, and see a correct spending
  dashboard within a few minutes.
- Orders stay in sync automatically (daily) without new OTPs until an app logs the user out.
- The cheapest-cart answer is correct for the user's address at the time they asked.
- It runs reliably in production: Vercel (frontend), Azure (backend and browser workers), Supabase
  (auth and data).
- It is free for the first 4 months, with per-user cost visible so paid plans can be priced afterwards.

## Non-goals (for this version)

- Placing orders, paying, applying coupons, or editing addresses, ever.
- A native App Store app. A PWA comes first; Expo can come later on the same API.
- Apps beyond Blinkit, Zepto, Swiggy (including Instamart prices for carts) and Zomato.
- Payments and billing for the paid tier (pricing is decided later).

## Anti-goals: failure even if it technically works

- An order placed or money spent without the user's own tap in the delivery app.
- One user's data visible to another user.
- Users' delivery-app accounts getting flagged or locked because of us.
- Proxy or AI costs running away unnoticed.
- OTPs or app passwords stored anywhere.

## Decided by the owner

- **Frontend:** a PWA on Vercel that installs to the iPhone home screen. The UI must be clean and
  polished, with buttons, sections and layout working properly on iPhone.
- **Backend:** on Azure.
- **Accounts for our app:** Supabase Auth with Google sign-in plus email and password.
- **Delivery-app logins:** done on our servers. The user types their phone number and the OTP into our
  app; nothing else gets installed.
- **Network:** paid Indian residential proxies, sticky per user, accepting the cost and the
  terms-of-service risk.
- **Cart:** show the cheapest cart, and optionally fill it in the chosen app. Never place the order.
- **Price:** free for the first 4 months.
- **Budget:** ₹5,000 per month, all-in (Azure, proxies, AI, Supabase if it goes beyond the free tier).
- **Consent:** before connecting any app, the user sees and agrees to a short note: automated access
  could get their delivery-app account flagged or temporarily locked; we only read order history and
  prices; carts are filled only when they tap. Agreement is stored per user and per site
  (`consents`: user_id, site, text_version, agreed_at).
- **Screens** (mockups in `.superpowers/brainstorm/*/content/`, gitignored):
  - **Home is "dashboard first" (mockup A):**
    - Month label and title "Spending".
    - A card with this month's total and change versus last month, plus six monthly bars.
    - A card splitting spend per app, with app colour dots.
    - "Top this year": the top restaurant or store, its amount and order count.
  - **Bottom tabs:** Spend · Orders · Cart · Ask · Accounts.
  - **Cart result is "ranked app totals" (mockup A):**
    - Header: "Cheapest cart · N items · priced live at <area> · <time> ago".
    - Then one card per app, cheapest first and outlined, each showing the app's total including fees,
      a fee breakdown, stock status, and a note for any swapped item.
    - The cheapest card has a primary "Fill cart on <app>" button. Other cards show "+₹X" versus the
      cheapest.
    - A "See item prices" link opens the item-by-item table (mockup B): items as rows, apps as
      columns, cheapest per row highlighted, and a totals row. Under the table, a line says whether
      splitting the order across apps is worth a second delivery fee.
    - An "Edit list" button.
  - **Connect flow (4 steps, plus a one-time consent sheet per app before step 2):**
    1. **Accounts:** each app is shown as "✓ Synced 2h ago" or "Connect ›", with the read-only promise
       and the delivery address (editable). Tapping "Connect" for an app not consented to yet opens the
       consent sheet first ("I understand" stores the consent).
    2. **Phone:** a number field, a "Send OTP" button, and a note that the app sends the OTP and we
       store only an encrypted session, never the OTP.
    3. **OTP:** a 6-digit field with `autocomplete="one-time-code"` so it fills from iPhone Messages,
       plus Verify and a resend timer.
    4. **First sync:** a progress bar and a live count of orders and total found. The user can leave the
       screen and gets a notification when it's done, plus a "Connect <next app>" button.

## The builder's calls (made in conversation, open to the owner's veto)

- **Supabase is also the main database (Postgres), replacing SQLite.** Row-level security means a user
  can only read their own rows. Azure services write through the service role.
- **Sync frequency:** each connected app syncs once a day automatically, plus up to 3 "refresh now" taps
  per app per day.
- **Cart checks:** up to 10 per user per day.
- **Models:** the assistant uses the cheap fast model where possible. Jev handles browser steps and GPT
  is the fallback, as in the CLI.
- **Admin view:** cost per user (proxy GB, AI tokens, worker minutes) and the health of each site's
  recipe.
- **Data control:** users can disconnect an app or delete their account at any time, which wipes that
  app's sessions and orders, or everything.
- **Privacy page:** says plainly that we read order history and prices only and never touch payment.
- **Cart filling:** adds items only. It never empties the user's existing cart. It reports
  out-of-stock and swapped items. When a product match isn't confident, it asks the user instead of
  guessing.
- **Learning is supervised by the admin.** Recipes (order, search, cart) are learned by the owner on
  the owner's or test accounts, using an admin "learn" job driven from the admin view. The exploring
  agent never runs on a stranger's account.
  - POST-allowlist approvals and every cart recipe need admin approval in the admin view. This replaces
    the CLI's terminal prompt.
  - When a recipe drifts, a re-learn that adds no new POST permission may update the shared recipe
    automatically.
  - Otherwise the site is marked unhealthy and paused, and the admin is alerted.
  - A user's own sync only replays, using the fast path or the nav trace.
- **Location during cart filling:** the worker may select one of the user's already-saved addresses or
  "use current location" (geolocation from their profile). It never creates, edits or saves an address.
  If the app's selected store or location doesn't match what was priced, it re-prices first and tells
  the user instead of filling silently. The cart-mode guard allows the location-selection requests
  recorded for the site, and still blocks address create/update.
- **Budget control (₹5,000 per month):**
  - Sign-up is invite-only with a waitlist at first, and the admin opens invites.
  - The `usage` table is converted to ₹ using configured unit costs: proxy ₹/GB, AI ₹/1K tokens, and
    worker ₹/minute.
  - At 80% of the monthly budget the admin is alerted (email).
  - At 100%, new sign-ups and cart checks pause, and automatic syncs drop to every 3 days until the
    month resets. Manual "refresh now" stays within its per-day limit.
  - The budget figure and unit costs live in config, not code.
  - Expected scale at this budget: a few dozen active users. Workers scale to zero when idle.
- **Picked as sensible defaults** (no owner input needed):
  - **Frontend:** Next.js (App Router) with TypeScript.
  - **Backend:** Python, reusing the existing modules (`agent`, `guard`, `browser`, `recipes`, `sync`,
    `categorize`, `insights`), behind FastAPI.
  - **Hosting:** Azure Container Apps for the API and the workers.
  - **Job queue:** a Postgres job table claimed with `FOR UPDATE SKIP LOCKED`. No extra queue service at
    this scale.

## How it works

### Components

```
iPhone PWA (Vercel, Next.js)
   │  Supabase Auth (Google / email+password); reads its own data directly via RLS
   │  calls API with the Supabase JWT
   ▼
API (Azure Container Apps, FastAPI)  ── Supabase Postgres (users, connections, orders, items,
   │  validates JWT, enqueues jobs,        categories, recipes, jobs, usage, otp_requests)
   │  relays OTP prompts                    Supabase Realtime: job progress + "OTP needed" events
   ▼
Browser workers (Azure Container Apps, N replicas)
   - real Google Chrome under Xvfb (not headless; same "normal Chrome + CDP attach" as the CLI)
   - per job: restore the user's encrypted Chrome profile from Azure Blob, use the user's sticky
     Indian residential proxy, run login / learn / sync / cart, save the profile back
   - existing guard enforces read-only; a separate, narrower "cart mode" guard for cart filling
```

### Login over the network

1. The user taps Connect, then Swiggy, and enters their phone number. The API creates a `connect` job.
2. A worker claims the job and opens Chrome with that user's proxy, profile and geolocation (from their
   address). It runs the existing `LoginAgent`.
3. When the agent reaches the OTP field (code already detects this), it publishes `otp_needed` over
   Supabase Realtime and waits, with a 3-minute timeout.
4. The PWA shows the OTP screen. The user types the code (iOS autofill), and it goes to the API, then to
   the worker, which types it. The OTP is never written to disk or to the database. It travels as a
   short-lived message only.
5. The agent finishes and verifies the login. The worker saves the encrypted profile and runs the first
   sync, streaming "orders found" counts. The PWA shows a notification when the sync is done.
6. When the agent is stuck (a CAPTCHA, an unknown screen), its existing `ask_user` becomes a question to
   the PWA. If the question isn't answered, the job fails cleanly with "Try again".

### Sync

- A scheduler enqueues one `sync` job per connection per day. "Refresh now" enqueues one too, within the
  per-day limit.
- The worker uses the existing three-step order: the in-page API fast path, then nav-trace replay, then
  re-learning.
- If the session has expired, the connection is marked `needs_login` and the user is notified ("Swiggy
  logged you out, reconnect").
- **Recipes are shared across users per site.** The endpoint, pagination and field map are the same for
  every user; learning them once from any connected account serves everyone.
- **Per-user parts stay per connection.** For example, Zomato's orders page lives at
  `/users/<user-id>/ordering`, so each connection stores its own orders-page URL (the CLI's "hint").
- **Recipe drift:** when re-learning succeeds, it updates the shared recipe and is logged in the admin
  view.

### Cheapest cart (new)

1. **The list.** The user types items or taps "start from my usual". "Usual" means items bought 3+ times
   in the last 90 days from grocery apps (Blinkit, Zepto, Instamart), which the data already has.
2. **Search.** A worker searches each grocery app for each item at the user's address. The product
   search API is learned the same way as order history: discovery captures the search JSON once per
   site, and a recipe maps it (name, brand, pack size, price, MRP, in-stock, product id). Searching
   doesn't need a login, only location, so it can use a pooled proxy rather than the user's.
3. **Matching.** Products are normalised (brand, variant, pack size in g/ml/units), and the
   cheapest-per-unit equivalent is matched. A small model confirms near matches. When there's no
   confident match, the item is marked "pick one" in the UI instead of guessing.
4. **Totals.** Each app's total is item prices plus that app's delivery, handling and small-cart fees.
   Fee rules are learned per site from its public fee info. When a fee can't be known without a cart,
   the total is labelled "fees estimated" and becomes exact during "Fill cart".
5. **Result.** The ranked app totals screen, plus the item table on request. It also checks whether
   splitting the order across two apps beats the best single app after the second delivery fee.

### Fill cart (the only write action)

- It runs only after the user taps "Fill cart on <app>". That creates a `fill_cart` job for that app with
  the exact product ids and quantities. It needs that app to be connected (logged in).
- **Cart mode guard:**
  - It allows only the add-to-cart / update-cart requests recorded for that site during a supervised
    learn, plus GETs.
  - It blocks every navigation or request matching checkout, payment, order placement, address, coupon
    or wallet paths, and every POST not on the cart allowlist.
  - It never clicks anything whose text matches checkout, pay, place order or proceed.
  - Downloads stay denied.
- **Report back:** items added, items out of stock, and items swapped (only with the user's choice). The
  user then opens the delivery app and pays.
- The existing read-only guard and the cart-mode guard are separate code paths, chosen per job type.
  Sync, login and search jobs can never run in cart mode.

### Data model (Supabase Postgres)

| Table | Contents |
|---|---|
| `profiles` | user id (from auth), display name, delivery address text, lat/lon, created_at |
| `connections` | user_id, site, status (`connected`, `needs_login`, `syncing`, `error`), last_synced_at, orders_page_url, proxy_session_id, profile_blob_key |
| `orders` | user_id, site, order_id, sub_platform, placed_at, total_amount, currency, status, merchant, raw_json. Primary key (user_id, site, order_id). |
| `order_items` | user_id, site, order_id, idx, name, qty, price |
| `item_categories` | name_norm → category. Global and shared: categories aren't personal. |
| `recipes` | site, kind (`orders`, `search`, `cart`), recipe json, version, learned_at, healthy flag. Global. |
| `jobs` | id, user_id, type (`connect`, `sync`, `learn`, `search`, `fill_cart`), site, payload, status, progress, error, timestamps |
| `cart_requests` / `cart_results` | the list, the matched products per app, the totals |
| `usage` | user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks |
| `consents` | user_id, site, text_version, agreed_at |
| `invites` / `waitlist` | email, invited_at, accepted_at |
| `recipe_approvals` | site, kind, proposed recipe, diff vs current, status, decided_at |

- RLS: users can select only their own rows in `orders`, `order_items`, `connections`, `jobs`,
  `cart_*`, `usage` and `profiles`.
- Workers use the service key.

### Security

- **Chrome profiles:** encrypted at rest in Azure Blob (per-user key from Azure Key Vault), restored to
  the worker's temp disk for a job, and wiped after it.
- **Proxies:** each user gets a sticky session id per site, stored on the connection, so the same Indian
  IP region is used every time (one consistent "phone" per user).
- **Secrets:** the OpenAI and TypeSafe keys, proxy credentials and the Supabase service key live in
  Azure Key Vault and app settings, never in the repo.
- **Logs:** phone numbers and OTPs are masked in all logs, as in the CLI. Request logs never contain
  cookies.
- **Deletion:** account deletion removes rows, profiles and blobs. A test proves nothing remains.

## Build order

1. **Backend foundations:**
   - Supabase schema and RLS.
   - Port storage from SQLite to Postgres (`db.py`).
   - FastAPI with JWT auth.
   - The job table and the worker loop.
2. **Worker in the cloud:**
   - Container with Chrome and Xvfb.
   - Profile save/restore to Blob, and the proxy per user.
   - Run the existing login and sync as jobs.
   - OTP relay over Realtime.
3. **PWA:**
   - Auth, the Accounts and connect flow, and the dashboard (Spend), using the mockups.
   - Orders list, monthly view, and Ask (the existing `insights` agent over Postgres, read-only role).
4. **Scheduling and limits:**
   - Daily sync and refresh limits.
   - Usage metering and the admin view.
   - Notifications.
5. **Cheapest cart:**
   - Search recipes, matching, fee rules, ranked results and the item table.
6. **Fill cart:**
   - Cart-mode guard, learning cart recipes, the fill job and result reporting.
7. **Hardening:**
   - Account deletion, the privacy page, rate-limit backoff per site.
   - Session-expiry notifications and an error budget per site.

## Left to the builder

- Exact component library and styling. It must match the mockups' clean, card-based iPhone look: green
  accent, rounded cards, bottom tab bar, safe-area padding.
- Proxy vendor choice, as long as it offers sticky Indian residential sessions.
- Worker concurrency per container and autoscaling rules.
- How fee rules are represented per site.
- Notification mechanism: Web Push on an installed iOS PWA, with email as fallback.
- Test strategy beyond keeping the existing unit tests green and adding tests for new pure logic
  (matching, fee totals, guard modes, RLS).

## Known risks

- **Bot detection on Azure even with residential proxies.** Mitigation: real Chrome, sticky IP per user,
  low request rates, per-site backoff, and pausing a site in the admin view.
- **The cart feature writes to users' accounts.** Mitigation: a separate guard mode, an explicit tap per
  fill, add-only, and never checkout.
- **Instamart order history isn't on the web** (Swiggy's feed covers food only). Instamart is used for
  prices and cart filling, not history.
- **Proxy cost per user is unknown until real usage.** The usage table and admin view exist to measure
  it within the free 4 months.
