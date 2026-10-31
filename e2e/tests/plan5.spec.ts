// Plan 5 end to end on this machine, as iPhone 14 users and one admin see it: the admin teaches the price recipes,
// compares run on each user's own connected apps at each app's own saved address, the cheapest cart and its fees,
// pick-one, a food compare across Swiggy and Zomato with restaurant pairing, a logout noticed at compare time, the
// daily limit and the budget pause.
//
// The stack is scripts/e2e_local.sh's: fake sites (WORKER_FAKE=1, OTP 123456, MATCH_JUDGE=0), Mailpit for every
// email, and the admin account named by E2E_ADMIN_EMAIL. The tests share state and run in order. No real phone
// number, OTP, address or personal data is used anywhere: the place is a made-up society in Gorwa, Vadodara.

import { expect, test, type Locator, type Page } from "@playwright/test";
import { mailSubjects, waitForMail } from "../helpers/mailpit";
import {
  accessToken,
  api,
  invite,
  restartWorker,
  schedulerOnce,
  setAddress,
  setConnectionStatus,
  sql,
  until,
  userId,
} from "../helpers/stack";

test.describe.configure({ mode: "serial" });

const PASSWORD = "e2e-Passw0rd!";
const FAKE_OTP = "123456";
const PHONE = "9876543210";
const ADMIN_EMAIL = (process.env.E2E_ADMIN_EMAIL || "admin@e2e.test").toLowerCase();
const ADDRESS = "12 Shanti Society, Gorwa, Vadodara, Gujarat";
const LAT = 22.33;
const LON = 73.15;
const AREA = "Gorwa, Vadodara";
const CHECK_LIMIT = 10;

type App = "Zepto" | "Blinkit" | "Swiggy" | "Zomato";
const ALL_APPS: App[] = ["Zepto", "Blinkit", "Swiggy", "Zomato"];

// The five-item list of the plan: Zepto is cheapest, Blinkit a little more, Instamart has Lay's Classic out of stock.
const LIST = [
  "Amul Taza Milk 500ml ×2",
  "Aashirvaad Atta 5kg",
  "Farm Eggs 6 pcs",
  "Lay's Classic",
  "Britannia Brown Bread",
];

// State handed from one test to the next.
const state = { adminReady: false, userB: "", userA: "", userC: "", groceryId: "", foodId: "", limitsAfterFood: -1 };

function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `p5-${tag}-${Date.now().toString(36)}-${rand}@example.com`;
}

function mainHeading(page: Page, name: string): Locator {
  return page.getByRole("heading", { level: 1, name, exact: true });
}

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

// ---- auth ------------------------------------------------------------------------------------------

async function signUp(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page).toHaveURL(/\/spend$/);
  await expect(mainHeading(page, "Spending")).toBeVisible();
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

/** A user who is invited (CLI) and signed up through the UI. */
async function newMember(page: Page, tag: string): Promise<string> {
  const email = uniqueEmail(tag);
  invite(email);
  await signUp(page, email);
  return email;
}

/** The admin: signs up the first time (the account may exist already from Plan 4's spec), signs in otherwise. */
async function adminSession(page: Page): Promise<void> {
  if (!state.adminReady && !userId(ADMIN_EMAIL)) await signUp(page, ADMIN_EMAIL);
  else await signIn(page, ADMIN_EMAIL);
  state.adminReady = true;
}

// ---- connecting apps -------------------------------------------------------------------------------

/** Accounts -> Connect <app> -> consent -> phone -> OTP, then the row reads "✓ Synced". A connected app is left alone. */
async function connectApp(page: Page, app: App): Promise<void> {
  await page.goto("/accounts");
  await expect(mainHeading(page, "Your apps")).toBeVisible();
  const row = page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: app });
  await expect(row).toHaveCount(1);
  const connect = page.getByRole("button", { name: `Connect ${app}`, exact: true });
  if (!(await connect.isVisible())) {
    await expect(row).toContainText("✓ Synced");
    return;
  }
  await connect.click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: `Before you connect ${app}` })).toBeVisible();
  await sheet.getByRole("button", { name: "I understand" }).click();
  await expect(sheet.getByRole("heading", { name: `Connect ${app}`, exact: true })).toBeVisible();
  await sheet.getByLabel("Phone number").fill(PHONE);
  await sheet.getByRole("button", { name: "Send OTP" }).click();
  await expect(sheet.getByRole("heading", { name: `Enter ${app}'s OTP` })).toBeVisible();
  const field = sheet.getByLabel("Verification code");
  await expect(field).toBeVisible();
  await field.pressSequentially(FAKE_OTP);
  await expect(sheet.getByRole("heading", { name: `${app} connected` })).toBeVisible({ timeout: 90_000 });
  await sheet.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.reload(); // read fresh: never rely on a second session's live updates
  await expect(row).toContainText("✓ Synced");
}

// ---- the cart screen ---------------------------------------------------------------------------------

/** Fill a field and wait for its button to wake up: a fill before the page has hydrated is lost, so type again. */
async function fillUntilEnabled(field: Locator, button: Locator, text: string): Promise<void> {
  await expect(async () => {
    await field.fill(text);
    await expect(button).toBeEnabled({ timeout: 1_000 });
  }).toPass({ timeout: 20_000 });
}

async function addItems(page: Page, items: string[]): Promise<void> {
  const field = page.getByLabel("New item");
  for (const text of items) {
    const add = page.getByRole("button", { name: "Add item", exact: true });
    await fillUntilEnabled(field, add, text);
    await add.click();
    await expect(field).toHaveValue("");
  }
}

/** Compare prices -> the result page, waited until the ranked cards are on screen. Returns the request id. */
async function compare(page: Page): Promise<string> {
  const button = page.getByRole("button", { name: "Compare prices", exact: true });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(page).toHaveURL(/\/cart\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split("/").pop()!;
  await expect(mainHeading(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
  return id;
}

function card(page: Page, name: string): Locator {
  return page.getByRole("article", { name, exact: true });
}

async function checksLeft(email: string): Promise<number> {
  const token = await accessToken(email, PASSWORD);
  return (await api(token, "GET", "/me/limits")).body.cart_checks_left as number;
}

async function requestDone(id: string): Promise<void> {
  await until("the check to finish", () =>
    ["done", "failed"].includes(sql<[string]>("select status from cart_requests where id = %s::uuid", [id])[0]?.[0] ?? ""),
  90_000);
}

// ---- the tests -------------------------------------------------------------------------------------

test("admin learns price recipes", async ({ page }) => {
  test.setTimeout(900_000);
  await adminSession(page);
  for (const app of ALL_APPS) await connectApp(page, app);
  setAddress(ADMIN_EMAIL, ADDRESS, LAT, LON); // after the connects: a sync would put the fake apps' own addresses back

  await page.goto("/admin");
  await expect(mainHeading(page, "Admin")).toBeVisible();
  await page.getByRole("tab", { name: "Sites" }).click();

  /** Learn on the card named `name`; a POST request asks for the admin's approval first. */
  async function learn(name: string, approve?: string): Promise<void> {
    const cardLi = page.getByRole("listitem", { name, exact: true });
    await cardLi.getByRole("button", { name: `Learn ${name}`, exact: true }).click();
    const done = cardLi.getByRole("status").filter({ hasText: /Recipe updated|Learn finished/ });
    if (approve) {
      // The first run on a database asks for the yes. A later run finds the request already on the allowlist and
      // finishes without asking: both are right, so wait for whichever comes first.
      const asking = cardLi.getByRole("list", { name: "Requested POST requests" });
      await expect(asking.or(done)).toBeVisible({ timeout: 120_000 });
      if (await asking.isVisible()) {
        await expect(asking).toContainText(approve);
        await cardLi.getByRole("button", { name: "Approve", exact: true }).click();
      }
    }
    await expect(done).toBeVisible({ timeout: 120_000 });
  }

  // Grocery prices: Zepto and Blinkit are plain GETs, Instamart posts a search and needs the admin's yes.
  await learn("Zepto grocery prices");
  await learn("Blinkit grocery prices");
  await learn("Instamart grocery prices", "^https://fake\\.instamart\\.test/api/search$");
  // Restaurants and menus: Swiggy is GET only, Zomato posts its search.
  await learn("Swiggy restaurants and menus");
  await learn("Zomato restaurants and menus", "^https://fake\\.zomato\\.test/api/search$");

  await page.reload();
  await page.getByRole("tab", { name: "Sites" }).click();
  for (const name of [
    "Zepto grocery prices",
    "Blinkit grocery prices",
    "Instamart grocery prices",
    "Swiggy restaurants and menus",
    "Zomato restaurants and menus",
  ]) {
    const cardLi = page.getByRole("listitem", { name, exact: true });
    await expect(cardLi).toContainText(/Recipe v\d+/);
    await expect(cardLi).toContainText("Healthy");
  }
  const healthy = sql<[string, string]>(
    "select site, kind from recipes where healthy and not paused and kind in ('search','restaurants','menu') order by site, kind",
  ).map((r) => `${r[0]}:${r[1]}`);
  expect(healthy).toEqual(expect.arrayContaining([
    "zepto:search", "blinkit:search", "instamart:search", "swiggy:restaurants", "swiggy:menu", "zomato:restaurants", "zomato:menu",
  ]));
});

test("connect prompts for unconnected apps", async ({ page }) => {
  test.setTimeout(300_000);
  state.userB = await newMember(page, "b");
  await connectApp(page, "Zepto");
  setAddress(state.userB, ADDRESS, LAT, LON);

  await page.goto("/cart");
  const apps = page.getByRole("list", { name: "Apps we compare" });
  await expect(apps.getByRole("listitem").filter({ hasText: "Zepto" })).toContainText("Connected");
  await expect(apps.getByRole("link", { name: "Connect Blinkit to compare" })).toContainText("Connect");
  await expect(apps.getByRole("link", { name: "Connect Swiggy to compare Instamart" })).toContainText("Connect");

  await addItems(page, ["Amul Taza Milk 500ml"]);
  await compare(page);
  await expect(card(page, "Zepto")).toContainText("₹");
  const blinkit = card(page, "Blinkit");
  await expect(blinkit).toContainText("Connect Blinkit to compare.");
  await expect(blinkit.getByRole("link", { name: "Connect", exact: true })).toHaveAttribute("href", "/accounts");
  await expect(card(page, "Instamart")).toContainText("Connect Swiggy to compare Instamart.");

  // Zepto logs them out: nothing is left to compare on, and the screen says what to do.
  setConnectionStatus(state.userB, "zepto", "needs_login");
  await page.goto("/cart");
  await addItems(page, ["Amul Taza Milk 500ml"]);
  await expect(page.getByRole("button", { name: "Compare prices", exact: true })).toBeDisabled();
  await expect(page.getByText("Connect Blinkit, Zepto or Instamart to compare.")).toBeVisible();
  const relogin = apps.getByRole("link", { name: "Zepto logged you out. Log in again to compare." });
  await expect(relogin).toContainText("Log in again");
  await expectNoHorizontalScroll(page, "/cart with no app to compare");
});

test("usual items come from past orders", async ({ page }) => {
  test.setTimeout(600_000);
  state.userA = await newMember(page, "a");
  for (const app of ALL_APPS) await connectApp(page, app);
  setAddress(state.userA, ADDRESS, LAT, LON);

  // The address picker showed the fake apps' saved addresses live after the connects.
  await page.goto("/accounts");
  await expect(page.getByText("Home").first()).toBeVisible();

  await page.goto("/cart");
  await expect(page.getByText("Delivering to")).toBeVisible();
  await expect(page.getByText(AREA)).toBeVisible();
  await page.getByRole("button", { name: "Start from my usual", exact: true }).click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Your usual" })).toBeVisible();
  const times = sheet.getByText(/^×\d+ in 90 days$/);
  await expect(times.first()).toBeVisible();
  const counts = (await times.allTextContents()).map((t) => Number(/\d+/.exec(t)![0]));
  expect(counts.length).toBeGreaterThan(0);
  for (const n of counts) expect(n).toBeGreaterThanOrEqual(3);
  await sheet.getByRole("button", { name: /^Add \d+ items?$/ }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Items" }).getByRole("listitem")).toHaveCount(counts.length);
});

test("cheapest cart ranks apps with fees", async ({ page }) => {
  await signIn(page, state.userA);
  await page.goto("/cart");
  await addItems(page, LIST);
  state.groceryId = await compare(page);

  await expect(page.getByText(`5 items · priced live at ${AREA}`)).toBeVisible();
  await expect(page.getByText("Your prices, with your memberships and your store's stock.")).toBeVisible();

  const zepto = card(page, "Zepto");
  await expect(zepto).toContainText("₹422");
  await expect(zepto).toContainText("Cheapest");
  await expect(zepto).toHaveClass(/ring-2/);
  const blinkit = card(page, "Blinkit");
  await expect(blinkit).toContainText("₹451");
  await expect(blinkit).toContainText("+₹29");
  // Lay's Classic is out of stock on Instamart and match.py never swaps it for a sibling that drops "classic": the
  // plan's swap to ₹479 does not happen. The card shows a Pick, and a total of ₹458 without that item.
  const instamart = card(page, "Instamart");
  await expect(instamart).toContainText("₹458");
  await expect(instamart).toContainText("1 to pick");
  await expect(page.getByText("fees estimated").first()).toBeVisible();

  // The Fill button of the cheapest app is shown but not tappable: no cart recipe has been learned in this run.
  const fill = page.getByRole("button", { name: "Fill cart on Zepto", exact: true });
  await expect(fill).toBeVisible();
  await expect(fill).toBeDisabled();
});

test("item table and split line", async ({ page }) => {
  await signIn(page, state.userA);
  await page.goto(`/cart/${state.groceryId}`);
  await expect(mainHeading(page, "Cheapest cart")).toBeVisible({ timeout: 60_000 });
  await page.getByRole("button", { name: "See item prices" }).click();
  const totals = page.getByRole("row", { name: /^Total \+ fees/ });
  await expect(totals).toBeVisible();
  const cells = await totals.getByRole("cell").allTextContents();
  expect(cells).toEqual(["₹422", "₹451", "₹458"]);
  await expect(page.getByTestId("split-line")).toHaveText(
    "No item is cheaper on another app, so one order on Zepto is best.",
  );
});

test("pick one", async ({ page }) => {
  await signIn(page, state.userA);
  await page.goto(`/cart/${state.groceryId}`);
  await expect(mainHeading(page, "Cheapest cart")).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: "Edit list", exact: true }).click();
  await expect(page).toHaveURL(/\/cart\?from=/);
  const rows = page.getByRole("list", { name: "Items" }).getByRole("listitem");
  await expect(rows).toHaveCount(LIST.length);
  while ((await rows.count()) > 0) await rows.first().getByRole("button", { name: /^Remove / }).click();
  await addItems(page, ["milk", "Aashirvaad Atta 5kg"]);
  const id = await compare(page);
  for (const app of ["Zepto", "Blinkit", "Instamart"]) await expect(card(page, app)).toContainText("1 to pick");

  const before = await checksLeft(state.userA);
  await card(page, "Zepto").getByRole("button", { name: /1 to pick/ }).click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Which one did you mean?" })).toBeVisible();
  await sheet.getByRole("radio", { name: /Amul Taaza Toned Milk 1 L/ }).check();
  await sheet.getByRole("button", { name: "Use this", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(card(page, "Zepto")).toContainText("₹309");
  await expect(page.getByRole("article").first()).toHaveAccessibleName("Zepto");
  expect(await checksLeft(state.userA), "choosing a product is not a new check").toBe(before);
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
});

test("food compare", async ({ page }) => {
  test.setTimeout(300_000);
  await signIn(page, state.userA);
  await page.goto("/cart");
  await page.getByRole("link", { name: "Food", exact: true }).click();
  await expect(page).toHaveURL(/mode=food/);
  await expect(page.getByRole("heading", { name: "Restaurants you order from" })).toBeVisible();
  const usual = page.getByRole("button", { name: /Meghana Foods/ });
  await expect(usual).toBeVisible();
  await usual.click();

  const pair = page.getByRole("button", { name: /Meghana Foods/ }).filter({ has: page.getByRole("img", { name: "On Swiggy and Zomato" }) });
  await expect(pair).toBeVisible({ timeout: 90_000 });
  await pair.click();
  await expect(page.getByRole("heading", { name: "Meghana Foods", level: 2 })).toBeVisible();

  const dish = (name: RegExp) => page.getByRole("list", { name: "Menu" }).getByRole("listitem").filter({ hasText: name });
  const half = dish(/Chicken Biryani/).filter({ hasText: /Half/ });
  await half.getByRole("button", { name: /^Add / }).click();
  await half.getByRole("button", { name: /^More / }).click();
  const paneer = dish(/Paneer Biryani/);
  await paneer.getByRole("button", { name: /^Add / }).click();

  const before = await checksLeft(state.userA);
  const button = page.getByRole("button", { name: /^Compare \d+ dishes?$/ });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(page).toHaveURL(/\/cart\/[0-9a-f-]{36}$/);
  state.foodId = new URL(page.url()).pathname.split("/").pop()!;
  await expect(mainHeading(page, "Meghana Foods")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(`priced live at ${AREA}`)).toBeVisible();

  const swiggy = card(page, "Swiggy");
  await expect(swiggy).toContainText("₹836");
  await expect(swiggy).toContainText("Cheapest");
  await expect(swiggy).toContainText("Items ₹700 + delivery ₹30 + packaging ₹40 + platform ₹20 + GST ₹46");
  const zomato = card(page, "Zomato");
  await expect(zomato).toContainText("₹878");
  await expect(zomato).toContainText("+₹42");
  await expect(page.getByText("Offers in the app may lower this.").first()).toBeVisible();
  const fill = page.getByRole("button", { name: "Fill cart on Swiggy", exact: true });
  await expect(fill).toBeDisabled();

  await page.getByRole("button", { name: "See item prices" }).click();
  const table = page.getByRole("table");
  const columns = await table.getByRole("columnheader").allTextContents();
  const zomatoCol = columns.indexOf("Zomato");
  expect(zomatoCol, `columns: ${columns.join(", ")}`).toBeGreaterThan(0);
  const paneerRow = table.getByRole("row", { name: /Paneer Biryani/ });
  await expect(paneerRow.getByRole("cell").nth(zomatoCol - 1)).toHaveClass(/font-bold/);
  await expect(page.getByTestId("split-line")).toHaveCount(0);

  state.limitsAfterFood = await checksLeft(state.userA);
  expect(state.limitsAfterFood, "a food compare is exactly one check").toBe(before - 1);
});

test("restaurant pairing edge cases", async ({ page }) => {
  test.setTimeout(300_000);
  await signIn(page, state.userA);
  await page.goto("/cart?mode=food");
  const results = page.getByRole("region", { name: "Results" });

  async function search(query: string): Promise<void> {
    const go = page.getByRole("button", { name: "Search", exact: true });
    await fillUntilEnabled(page.getByLabel("Search a restaurant"), go, query);
    await go.click();
    await expect(results).toBeVisible({ timeout: 90_000 });
  }
  const resultButton = (name: RegExp) => results.getByRole("button", { name });
  // The server takes one menu lookup at a time per user: let it finish before searching again.
  const menuLoaded = () => expect(page.getByRole("list", { name: "Menu" })).toBeVisible({ timeout: 90_000 });

  // Behrouz: the Gorwa branch pairs on both apps; the Alkapuri branch exists only on Zomato.
  await search("Behrouz");
  const gorwa = resultButton(/Behrouz Biryani/).filter({ hasText: "Gorwa" });
  await expect(gorwa).toHaveCount(1);
  await expect(gorwa.getByRole("img", { name: "On Swiggy and Zomato" })).toBeVisible();
  const alkapuri = resultButton(/Behrouz Biryani/).filter({ hasText: "Alkapuri" });
  await expect(alkapuri).toHaveCount(1);
  await expect(alkapuri).toContainText("Only on Zomato");

  // Truffles / Truffles Cafe: not sure, so the app asks.
  await search("Truffles");
  const truffles = resultButton(/Truffles/);
  await expect(truffles).toContainText("Check this is the same restaurant");
  await truffles.click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Same restaurant?" })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "Swiggy listing" })).toContainText("Truffles");
  await expect(sheet.getByRole("button", { name: "Zomato listing" })).toContainText("Truffles Cafe");
  await sheet.getByRole("button", { name: "Yes, same restaurant", exact: true }).click();
  await expect(page.getByRole("region", { name: "Restaurant" })).toBeVisible();
  await menuLoaded();
  await page.getByRole("button", { name: "Change restaurant" }).click();

  // Burger Singh is closed on Zomato right now.
  await search("Burger Singh");
  await resultButton(/Burger Singh/).click();
  await expect(page.getByRole("region", { name: "Restaurant" })).toContainText("Closed on Zomato right now.");
  await menuLoaded();
  await page.getByRole("button", { name: "Change restaurant" }).click();

  // Theobroma exists only on Swiggy.
  await search("Theobroma");
  await expect(resultButton(/Theobroma/)).toContainText("Only on Swiggy");
});

test("logged out at compare time", async ({ page }) => {
  test.setTimeout(300_000);
  await restartWorker({ FAKE_LOGGED_OUT_SITES: "blinkit" });
  try {
    await signIn(page, state.userA);
    await page.goto("/cart");
    await addItems(page, ["Amul Taza Milk 500ml"]);
    await compare(page);
    await expect(card(page, "Blinkit")).toContainText("Blinkit logged you out. Log in again to compare.");
    await expect(card(page, "Zepto")).toContainText("₹");

    schedulerOnce(); // the tick sends the queued "logged you out" email
    await waitForMail(state.userA, /Blinkit logged you out/);
    await page.goto("/accounts");
    const row = page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: "Blinkit" });
    await expect(row).toContainText("Needs login");
  } finally {
    await restartWorker({});
  }
});

test("daily limit", async ({ page }) => {
  test.setTimeout(600_000);
  const token = await accessToken(state.userA, PASSWORD);
  let refused: { status: number; detail: string } | null = null;
  for (let i = 0; i < CHECK_LIMIT + 2 && !refused; i++) {
    const res = await api(token, "POST", "/cart/checks", { items: [{ text: "milk", qty: 1 }] });
    if (res.status === 202) await requestDone(res.body.request_id);
    else refused = { status: res.status, detail: res.body.detail };
  }
  expect(refused, "the daily limit never stopped the checks").not.toBeNull();
  expect(refused!.status).toBe(429);
  expect(refused!.detail).toBe(`You can check ${CHECK_LIMIT} carts a day. Try again tomorrow.`);
  expect(await checksLeft(state.userA)).toBe(0);

  await signIn(page, state.userA);
  await page.goto("/cart");
  await expect(page.getByText("0 checks left today")).toBeVisible();
  await addItems(page, ["milk"]);
  await expect(page.getByRole("button", { name: "Compare prices", exact: true })).toBeDisabled();
});

test("budget pause", async ({ page }) => {
  test.setTimeout(600_000);
  state.userC = await newMember(page, "c");
  await connectApp(page, "Swiggy"); // Instamart comes with Swiggy, so groceries and food both have an app
  setAddress(state.userC, ADDRESS, LAT, LON);
  try {
    schedulerOnce({ MONTHLY_BUDGET_INR: "0.01" });
    expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("paused");

    await page.goto("/cart");
    await addItems(page, ["milk"]);
    await page.getByRole("button", { name: "Compare prices", exact: true }).click();
    await expect(page.getByText(/Cart checks are paused until 1 \w+\./)).toBeVisible();

    await page.goto("/cart?mode=food");
    const go = page.getByRole("button", { name: "Search", exact: true });
    await fillUntilEnabled(page.getByLabel("Search a restaurant"), go, "Theobroma");
    await go.click();
    await expect(page.getByText(/Cart checks are paused until 1 \w+\./)).toBeVisible();
  } finally {
    schedulerOnce({});
  }
  expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("ok");
  expect((await mailSubjects(ADMIN_EMAIL)).length).toBeGreaterThan(0);
});

test("no horizontal scroll on /cart and a result", async ({ page }) => {
  await signIn(page, state.userA);
  await page.goto("/cart");
  await expect(page.getByRole("button", { name: "Compare prices", exact: true })).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart groceries");
  await addItems(page, LIST);
  await expectNoHorizontalScroll(page, "/cart groceries with a list");
  await page.goto("/cart?mode=food");
  await expect(page.getByRole("heading", { name: "Restaurants you order from" })).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart food");

  for (const [id, where] of [[state.groceryId, "grocery result"], [state.foodId, "food result"]] as const) {
    await page.goto(`/cart/${id}`);
    await expect(page.getByRole("button", { name: "See item prices" })).toBeVisible({ timeout: 60_000 });
    await page.getByRole("button", { name: "See item prices" }).click();
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoHorizontalScroll(page, `/cart/[id] ${where} with the table open`);
  }
});
