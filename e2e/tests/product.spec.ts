// The whole product on one machine, as an iPhone 14 user sees it:
//   PWA -> FastAPI -> jobs queue -> fake worker (WORKER_FAKE=1) -> Postgres -> Realtime -> PWA
// plus Ask answering from the user's own rows (ASK_FAKE=1: a fixed scoped_sql query, no LLM).
//
// Every test signs up its own user (local Supabase has email confirmations off); the
// `supabase db reset` at the start of scripts/e2e_local.sh removes them. Nothing here sleeps:
// every step waits for the text that Realtime or the API puts on screen.
//
// Contract with server/fake_sites.py: OTP 123456; a phone ending in 0000 is asked
// "Which city do you order from?" first; every connect finds 28 orders over six months, every
// 11th cancelled; Swiggy's every 3rd order is an Instamart grocery order.

import { devices, expect, test, type Browser, type BrowserContextOptions, type Locator, type Page } from "@playwright/test";

const PASSWORD = "e2e-Passw0rd!";
const FAKE_OTP = "123456";
const WRONG_OTP = "000000";
const FAKE_ORDER_COUNT = 28;
const FAKE_CANCELLED = 2; // orders 11 and 22
const FAKE_QUESTION = "Which city do you order from?";
const PHONE = "9876543210";
const QUESTION_PHONE = "9876540000";

const APP_NAMES = ["Zepto", "Blinkit", "Swiggy", "Zomato"] as const;
type AppName = (typeof APP_NAMES)[number];

const TABS = {
  Spend: { path: "/spend", heading: "Spending" },
  Orders: { path: "/orders", heading: "Orders" },
  Cart: { path: "/cart", heading: "Cheapest cart" },
  Ask: { path: "/ask", heading: "Ask" },
  Accounts: { path: "/accounts", heading: "Your apps" },
} as const;
type TabName = keyof typeof TABS;

/** The narrowest phone the design must fit, besides the iPhone 14's 390 px. */
const NARROW = { width: 375, height: 812 } as const;

// ---- small helpers ---------------------------------------------------------------------------

function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `e2e-${tag}-${Date.now().toString(36)}-${rand}@example.com`;
}

/** The first rupee amount in a text: "₹1,23,456" -> 123456. */
function rupees(text: string): number {
  const m = /₹\s*([\d,]+(?:\.\d+)?)/.exec(text);
  if (!m?.[1]) throw new Error(`no rupee amount in ${JSON.stringify(text)}`);
  return Number(m[1].replace(/,/g, ""));
}

/** "October 2026" for the browser's own clock, the way the Spend eyebrow writes it. */
async function browserMonthLabel(page: Page): Promise<string> {
  return page.evaluate(() => {
    const names = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
      "October", "November", "December"];
    const d = new Date();
    return `${names[d.getMonth()]} ${d.getFullYear()}`;
  });
}

function mainHeading(page: Page, name: string): Locator {
  return page.getByRole("heading", { level: 1, name, exact: true });
}

/** The page cannot scroll sideways: nothing is wider than the screen, and a sideways scroll does nothing. */
async function expectNoHorizontalScroll(page: Page, where: string): Promise<void> {
  const m = await page.evaluate(() => {
    const before = window.scrollY;
    window.scrollTo(200, before);
    const scrolledX = window.scrollX;
    window.scrollTo(0, before);
    return {
      viewport: document.documentElement.clientWidth,
      doc: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      scrolledX,
    };
  });
  expect(m.doc, `${where}: the page is ${m.doc}px wide on a ${m.viewport}px screen`).toBeLessThanOrEqual(m.viewport);
  expect(m.body, `${where}: the body is ${m.body}px wide on a ${m.viewport}px screen`).toBeLessThanOrEqual(m.viewport);
  expect(m.scrolledX, `${where}: the page scrolled sideways`).toBe(0);
}

/** Open a tab from the bottom tab bar and wait until its content has loaded. */
async function openTab(page: Page, tab: TabName): Promise<void> {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: tab, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${TABS[tab].path}(\\?.*)?$`));
  await expect(mainHeading(page, TABS[tab].heading)).toBeVisible();
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
}

/** Every tab of this page's phone, opened from the tab bar, has no horizontal scroll. */
async function expectNoHorizontalScrollOnTabs(page: Page, where: string): Promise<void> {
  for (const tab of Object.keys(TABS) as TabName[]) {
    await openTab(page, tab);
    await expectNoHorizontalScroll(page, `${where}: ${tab}`);
  }
}

/** Every tab has no horizontal scroll, on the iPhone 14 (390 px) and on a 375 px phone. */
async function expectNoHorizontalScrollOnEveryTab(page: Page, state: string): Promise<void> {
  await expectNoHorizontalScrollOnTabs(page, `${state}, 390px`);
  await onNarrowPhone(page, async (narrow) => {
    await narrow.goto("/spend");
    await expect(mainHeading(narrow, "Spending")).toBeVisible();
    await expectNoHorizontalScrollOnTabs(narrow, `${state}, 375px`);
  });
}

/** No phone number or OTP in localStorage, sessionStorage, IndexedDB names or cookies. */
async function expectNothingSensitiveStored(page: Page, secrets: string[]): Promise<void> {
  const stored = await page.evaluate(async () => {
    const dump = (s: Storage) =>
      Array.from({ length: s.length }, (_, i) => {
        const key = s.key(i) ?? "";
        return `${key}=${s.getItem(key) ?? ""}`;
      });
    const dbs = typeof indexedDB.databases === "function" ? await indexedDB.databases() : [];
    return [...dump(localStorage), ...dump(sessionStorage), ...dbs.map((d) => d.name ?? "")].join("\n");
  });
  const cookies = (await page.context().cookies()).map((c) => `${c.name}=${c.value}`).join("\n");
  for (const secret of secrets) {
    expect(stored, "a phone number or OTP was written to browser storage").not.toContain(secret);
    expect(cookies, "a phone number or OTP was written to a cookie").not.toContain(secret);
  }
}

// ---- auth --------------------------------------------------------------------------------------

async function signUp(page: Page, tag: string): Promise<string> {
  const email = uniqueEmail(tag);
  await page.goto("/login");
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page).toHaveURL(/\/spend$/);
  await expect(mainHeading(page, "Spending")).toBeVisible();
  return email;
}

async function signIn(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Sign in" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/spend$/);
  await expect(mainHeading(page, "Spending")).toBeVisible();
}

/** Another phone (its own cookies and storage) in the same browser: an iPhone 14 unless overridden. */
async function secondPhone(browser: Browser, options: BrowserContextOptions = {}) {
  // defaultBrowserType picks the engine for a project; it is not a context option.
  const { defaultBrowserType: _engine, ...iphone } = devices["iPhone 14"];
  return browser.newContext({ ...iphone, baseURL: test.info().project.use.baseURL, locale: "en-IN", ...options });
}

/**
 * Run `fn` as the same signed-in user on a 375 px phone. A fresh context, not page.setViewportSize: resizing a
 * mobile WebKit page in place leaves its layout viewport at the old height and hit-testing off (seen as the
 * fixed tab bar vanishing and "<html> intercepts pointer events").
 */
async function onNarrowPhone(page: Page, fn: (narrow: Page) => Promise<void>): Promise<void> {
  const browser = page.context().browser();
  if (!browser) throw new Error("no browser for a second context");
  const narrow = await secondPhone(browser, { viewport: NARROW, storageState: await page.context().storageState() });
  try {
    await fn(await narrow.newPage());
  } finally {
    await narrow.close();
  }
}

// ---- Accounts and the connect flow ---------------------------------------------------------------

function appRow(page: Page, app: AppName): Locator {
  return page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: app });
}

/** Accounts -> Connect <app> -> (consent) -> phone -> Send OTP. Returns the connect sheet. */
async function startConnect(page: Page, app: AppName, phone: string): Promise<Locator> {
  await page.getByRole("button", { name: `Connect ${app}`, exact: true }).click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: `Before you connect ${app}` })).toBeVisible();
  await expect(sheet).toContainText("We never place an order or pay.");
  await sheet.getByRole("button", { name: "I understand" }).click();
  await expect(sheet.getByRole("heading", { name: `Connect ${app}`, exact: true })).toBeVisible();
  await sheet.getByLabel("Phone number").fill(phone);
  await sheet.getByRole("button", { name: "Send OTP" }).click();
  return sheet;
}

/** The job reached otp_needed (seen live): the OTP field, with the attributes iOS needs for "From Messages". */
async function otpField(sheet: Locator, app: AppName): Promise<Locator> {
  await expect(sheet.getByRole("heading", { name: `Enter ${app}'s OTP` })).toBeVisible();
  const field = sheet.getByLabel("6-digit code");
  await expect(field).toBeVisible();
  await expect(field).toHaveAttribute("inputmode", "numeric");
  await expect(field).toHaveAttribute("autocomplete", "one-time-code");
  await expect(field).toHaveAttribute("maxlength", "6");
  await expect(field).toHaveAttribute("pattern", "\\d{6}");
  return field;
}

/** Type the code digit by digit; the field sends itself at the sixth. */
async function typeOtp(sheet: Locator, app: AppName, code: string): Promise<void> {
  const field = await otpField(sheet, app);
  await field.pressSequentially(code);
}

interface FirstSync {
  orders: number;
  amount: number;
  /** "Found so far" counts seen live while the first sync ran (the e2e worker pauses between fake pages). */
  live: number[];
}

/**
 * Watch the first sync (pushed over Realtime): the live "Found so far" count while it runs, then what the sheet
 * says was found. scripts/e2e_local.sh sets FAKE_PAGE_DELAY_S=0.4, so the fake fetch reports its pages over
 * about a second and the live count is always on screen for a while.
 */
async function waitForFirstSync(sheet: Locator, app: AppName): Promise<FirstSync> {
  const live: number[] = [];
  let found: RegExpExecArray | null = null;
  await expect
    .poll(
      async () => {
        const text = await sheet.innerText();
        const soFar = /Found so far\s+(\d+) orders?/.exec(text);
        if (soFar?.[1]) live.push(Number(soFar[1]));
        // "Found / 28 orders / ₹13,240 · Apr 2026 – today" once the sync is in and the first month is read.
        found = /Found\s+(\d+) orders?\s+(₹[\d,]+(?:\.\d+)?) · [A-Z][a-z]{2} \d{4} – today/.exec(text);
        return found !== null;
      },
      { message: `${app}: the first sync never finished`, timeout: 90_000, intervals: [100] },
    )
    .toBe(true);
  await expect(sheet.getByRole("heading", { name: `${app} connected` })).toBeVisible();
  const m = found as unknown as RegExpExecArray;
  const result = { orders: Number(m[1]), amount: rupees(m[2] ?? ""), live };
  expect(result.orders).toBe(FAKE_ORDER_COUNT);
  expect(result.amount).toBeGreaterThan(0);
  expect(live.length, `${app}: no live "Found so far" count was shown during the first sync`).toBeGreaterThan(0);
  for (const [i, n] of live.entries()) {
    expect(n, "a live count is a positive number of orders").toBeGreaterThan(0);
    expect(n, "a live count never passes the final one").toBeLessThanOrEqual(result.orders);
    expect(n, "a live count never goes down").toBeGreaterThanOrEqual(live[i - 1] ?? 0);
  }
  return result;
}

async function closeSheet(page: Page, sheet: Locator): Promise<void> {
  await sheet.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

async function expectSynced(page: Page, app: AppName): Promise<void> {
  await expect(appRow(page, app)).toContainText("✓ Synced");
  await expect(page.getByRole("button", { name: `Refresh ${app} now` })).toBeVisible();
}

// ---- Ask ---------------------------------------------------------------------------------------------

/**
 * Ask "how much on each app" on the open Ask screen and read the answer's table (ASK_FAKE: spend per site from
 * monthly_spend). Expects exactly one row, Swiggy, the only app this user connected; returns its amount.
 */
async function askSpendPerApp(page: Page): Promise<number> {
  const question = "How much have I spent on each app?";
  await page.getByLabel("Ask a question about your spending").fill(question);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  const log = page.getByRole("log", { name: "Conversation" });
  await expect(log).toContainText(question);
  const table = log.getByRole("table");
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader")).toHaveText(["site", "amount"]);
  const rows = table.getByRole("row").filter({ has: page.getByRole("cell") });
  await expect(rows).toHaveCount(1); // only this user's one app: row-level security held
  await expect(rows.getByRole("cell")).toHaveText(["swiggy", /^\d+(\.\d+)?$/]);
  return Number((await rows.getByRole("cell").nth(1).innerText()).trim());
}

// ---- Realtime --------------------------------------------------------------------------------------

interface RealtimeMessage {
  topic: string;
  event: string;
  payload: { status?: string; extension?: string } | undefined;
}

/** realtime-js speaks either the v1 object frame or the v2 array frame [join_ref, ref, topic, event, payload]. */
function parseFrame(raw: string | Buffer): RealtimeMessage | null {
  if (typeof raw !== "string") return null; // binary frames carry broadcasts only
  try {
    const msg: unknown = JSON.parse(raw);
    if (Array.isArray(msg)) return { topic: String(msg[2]), event: String(msg[3]), payload: msg[4] };
    const o = msg as { topic?: unknown; event?: unknown; payload?: RealtimeMessage["payload"] };
    return { topic: String(o.topic), event: String(o.event), payload: o.payload };
  } catch {
    return null;
  }
}

/**
 * Resolves `ready` once the page's Realtime channel whose topic contains `topicPart` is listening
 * to Postgres changes (the server's "Subscribed to PostgreSQL" system message). Call before goto.
 */
function watchRealtimeReady(page: Page, topicPart: string): { ready: () => boolean } {
  let ready = false;
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/realtime/")) return;
    ws.on("framereceived", ({ payload }) => {
      const msg = parseFrame(payload);
      if (
        msg?.topic.includes(topicPart) &&
        msg.event === "system" &&
        msg.payload?.extension === "postgres_changes" &&
        msg.payload.status === "ok"
      ) {
        ready = true;
      }
    });
  });
  return { ready: () => ready };
}

// ---- the tests -------------------------------------------------------------------------------------

test("sign up, connect Swiggy with an OTP, and Spend, Orders and Ask fill in", async ({ page }) => {
  const email = await signUp(page, "main");

  await test.step("Spend before any app is connected: mockup A at zero", async () => {
    await expect(page.getByText(await browserMonthLabel(page), { exact: true })).toBeVisible();
    const total = page.getByRole("region", { name: "This month so far" });
    await expect(total).toContainText("₹0");
    await expect(page.getByRole("group", { name: "Last six months" })).toHaveCount(0);
    const rows = page.getByRole("region", { name: "By app" }).getByRole("listitem");
    await expect(rows).toHaveCount(4);
    for (const app of APP_NAMES) await expect(rows.filter({ hasText: app })).toContainText("₹0");
    await expect(page.getByRole("link", { name: "Connect an app" })).toBeVisible();
  });

  await test.step("no horizontal scroll on any tab, before any data", async () => {
    await expectNoHorizontalScrollOnEveryTab(page, "empty");
  });

  await test.step("Accounts lists the four apps; Instamart comes with Swiggy", async () => {
    await openTab(page, "Spend");
    await page.getByRole("link", { name: "Connect an app" }).click();
    await expect(page).toHaveURL(/\/accounts$/);
    await expect(mainHeading(page, "Your apps")).toBeVisible();
    for (const app of APP_NAMES) {
      await expect(page.getByRole("button", { name: `Connect ${app}`, exact: true })).toBeVisible();
    }
    await expect(page.getByRole("button", { name: /Instamart/ })).toHaveCount(0);
    await expect(page.getByText("Instamart orders come in with your Swiggy account.")).toBeVisible();
  });

  let sync: FirstSync = { orders: 0, amount: 0, live: [] };
  await test.step("consent -> phone -> otp_needed -> OTP -> first sync", async () => {
    const sheet = await startConnect(page, "Swiggy", PHONE);
    await expect(appRow(page, "Swiggy")).toContainText("Syncing…");
    await typeOtp(sheet, "Swiggy", FAKE_OTP);
    sync = await waitForFirstSync(sheet, "Swiggy");
    await expectNothingSensitiveStored(page, [PHONE, `+91${PHONE}`, FAKE_OTP]);
    await closeSheet(page, sheet);
    await expectSynced(page, "Swiggy");
  });

  await test.step("Spend: non-zero totals over six bars, Instamart inside Swiggy", async () => {
    await openTab(page, "Spend");
    const bars = page.getByRole("group", { name: "Last six months" }).getByRole("button");
    await expect(bars).toHaveCount(6);
    const labels = await bars.evaluateAll((els) => els.map((el) => el.getAttribute("aria-label") ?? ""));
    const amounts = labels.map(rupees);
    const sum = amounts.reduce((a, b) => a + b, 0);
    expect(sum).toBeGreaterThan(0);
    expect(sum, "six months can't exceed everything the sync found").toBeLessThanOrEqual(sync.amount);
    expect(labels[5]).toContain(await browserMonthLabel(page));
    await expect(page.getByRole("link", { name: "Connect an app" })).toHaveCount(0);

    // The big number is the selected month's bar.
    const total = page.getByRole("region", { name: "This month so far" });
    expect(rupees(await total.innerText())).toBe(amounts[5]);

    // Instamart is never its own app row: it is a sub-label of Swiggy, in the months it has orders.
    const byApp = page.getByRole("region", { name: "By app" }).getByRole("listitem");
    let instamartMonths = 0;
    for (let i = 0; i < 6; i++) {
      await bars.nth(i).click();
      await expect(bars.nth(i)).toHaveAttribute("aria-pressed", "true");
      await expect(byApp).toHaveCount(4);
      await expect(byApp.filter({ hasText: /^Instamart/ })).toHaveCount(0);
      const swiggy = await byApp.filter({ hasText: "Swiggy" }).innerText();
      if (/Instamart\s+₹[\d,]+/.test(swiggy)) instamartMonths += 1;
      if ((amounts[i] ?? 0) > 0) expect(rupees(swiggy.replace(/Instamart\s+₹[\d,.]+/, ""))).toBe(amounts[i]);
    }
    expect(instamartMonths, "no month showed Swiggy's Instamart share").toBeGreaterThan(0);
    await bars.nth(5).click();
  });

  await test.step("Orders: every synced order, newest first, Instamart named as such", async () => {
    await openTab(page, "Orders");
    const main = page.getByRole("main");
    const orders = main.getByRole("link").filter({ hasText: /Swiggy · \d{1,2} [A-Z][a-z]{2}/ });
    await expect(orders).toHaveCount(FAKE_ORDER_COUNT);
    await expect(main.getByText("No older orders")).toBeVisible();
    await expect(main.getByText("Cancelled", { exact: true })).toHaveCount(FAKE_CANCELLED);
    const instamart = orders.filter({ has: page.getByText("Instamart", { exact: true }) });
    expect(await instamart.count()).toBeGreaterThan(0);
    await instamart.first().click();
    await expect(page).toHaveURL(/\/orders\/swiggy\/FAKE-swiggy-\d{4}$/);
  });

  await test.step("an order's detail: Instamart under Swiggy, total, items", async () => {
    await expect(mainHeading(page, "Instamart")).toBeVisible();
    const main = page.getByRole("main");
    await expect(main.getByText("Swiggy", { exact: true })).toBeVisible();
    const summary = page.getByRole("region", { name: "Summary" });
    expect(rupees(await summary.innerText())).toBeGreaterThan(0);
    await expect(summary).toContainText(/Order ID\s+FAKE-swiggy-\d{4}/);
    await expect(main.getByRole("region", { name: /^\d+ items?$/ })).toBeVisible();
    await expect(main.getByText("Swiggy doesn't list item prices, only the order total.")).toBeVisible();
    await expectNoHorizontalScroll(page, "order detail, 390px");
    const detailUrl = page.url();
    await onNarrowPhone(page, async (narrow) => {
      await narrow.goto(detailUrl);
      await expect(mainHeading(narrow, "Instamart")).toBeVisible();
      await expect(narrow.getByRole("region", { name: "Summary" })).toBeVisible();
      await expectNoHorizontalScroll(narrow, "order detail, 375px");
    });
    await main.getByRole("link", { name: "Orders", exact: true }).click();
    await expect(page).toHaveURL(/\/orders$/);
  });

  await test.step("Ask answers from the user's own orders with a table", async () => {
    await openTab(page, "Ask");
    const amount = await askSpendPerApp(page);
    expect(amount, "Ask's Swiggy total should equal what the first sync found").toBe(sync.amount);
    await expectNoHorizontalScroll(page, "Ask with a table, 390px");
    await onNarrowPhone(page, async (narrow) => {
      await narrow.goto("/ask");
      await expect(mainHeading(narrow, "Ask")).toBeVisible();
      expect(await askSpendPerApp(narrow)).toBe(sync.amount);
      await expectNoHorizontalScroll(narrow, "Ask with a table, 375px");
    });
  });

  await test.step("no horizontal scroll on any tab, with data", async () => {
    await expectNoHorizontalScrollOnEveryTab(page, "with data");
  });

  await test.step("sign out, sign back in: the data is still there", async () => {
    await openTab(page, "Accounts");
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login(\?.*)?$/);
    await signIn(page, email);
    await expect(page.getByRole("group", { name: "Last six months" }).getByRole("button")).toHaveCount(6);
    await page.goto("/accounts");
    await expectSynced(page, "Swiggy");
  });
});

test("a login that asks a question first (phone ending 0000)", async ({ page }) => {
  await signUp(page, "question");
  await openTab(page, "Accounts");
  const sheet = await startConnect(page, "Zomato", QUESTION_PHONE);

  await expect(sheet.getByRole("heading", { name: "Zomato has a question" })).toBeVisible();
  const answer = sheet.getByLabel(FAKE_QUESTION);
  await expect(answer).toBeVisible();
  await answer.fill("Bengaluru");
  await sheet.getByRole("button", { name: "Send answer" }).click();

  await typeOtp(sheet, "Zomato", FAKE_OTP);
  await waitForFirstSync(sheet, "Zomato");
  await expectNothingSensitiveStored(page, [QUESTION_PHONE, FAKE_OTP, "Bengaluru"]);
  await closeSheet(page, sheet);
  await expectSynced(page, "Zomato");
});

test("a wrong OTP says so, and trying again connects", async ({ page }) => {
  await signUp(page, "wrong-otp");
  await openTab(page, "Accounts");
  const sheet = await startConnect(page, "Zepto", PHONE);

  await typeOtp(sheet, "Zepto", WRONG_OTP);
  await expect(sheet.getByRole("alert")).toHaveText("That code didn't work");
  await expect(sheet).toContainText("Check the code from Zepto. Trying again sends a new one.");

  await sheet.getByRole("button", { name: "Try again" }).click();
  await typeOtp(sheet, "Zepto", FAKE_OTP);
  await waitForFirstSync(sheet, "Zepto");
  await closeSheet(page, sheet);
  await expectSynced(page, "Zepto");
});

test("connect flow updates live", async ({ page, browser }) => {
  const email = await signUp(page, "live");

  // The same user on a second phone, watching Accounts. It never reloads.
  const other = await secondPhone(browser);
  try {
    const watcher = await other.newPage();
    await signIn(watcher, email);
    const realtime = watchRealtimeReady(watcher, "connections-");
    await watcher.goto("/accounts");
    await expect(watcher.getByRole("button", { name: "Connect Blinkit", exact: true })).toBeVisible();
    await expect
      .poll(realtime.ready, { message: "the second phone never subscribed to connection changes", timeout: 30_000 })
      .toBe(true);
    let navigations = 0;
    watcher.on("framenavigated", (frame) => {
      if (frame === watcher.mainFrame()) navigations += 1;
    });

    await openTab(page, "Accounts");
    const sheet = await startConnect(page, "Blinkit", PHONE);
    await otpField(sheet, "Blinkit");
    // While the first phone waits for the OTP, the second already shows Blinkit syncing.
    await expect(appRow(watcher, "Blinkit")).toContainText("Syncing…");
    await expect(watcher.getByRole("button", { name: "Connect Blinkit", exact: true })).toHaveCount(0);

    await typeOtp(sheet, "Blinkit", FAKE_OTP);
    await waitForFirstSync(sheet, "Blinkit");
    await expectSynced(watcher, "Blinkit");
    expect(navigations, "the second phone reloaded instead of updating live").toBe(0);

    await closeSheet(page, sheet);
    await expectSynced(page, "Blinkit");
  } finally {
    await other.close();
  }
});
