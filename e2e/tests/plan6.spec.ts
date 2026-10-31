// Plan 6 end to end on this machine, as iPhone 14 users and one admin see it: the admin teaches the Zepto cart
// recipe and approves it, a user fills their Zepto cart from a cheapest-cart check (add-only; a question when the
// app has several answers; the cart that was there stays), a fill is one per check, a different app location is
// re-priced and confirmed before anything is added, the budget pause and the daily fill limit.
//
// The stack is scripts/e2e_local.sh's: fake sites (WORKER_FAKE=1, OTP 123456, MATCH_JUDGE=0), each user's fake
// Zepto cart kept in $FAKE_STATE_DIR, and the admin account named by E2E_ADMIN_EMAIL. The tests share state and
// run in order. No real phone number, OTP, address or personal data is used: the place is a made-up society.
//
// "No order was placed": the fake apps fill in the worker's process (no HTTP), so there is no request log to read.
// What is checked instead: the fake app's state holds a cart, a location and a lock and nothing else, the cart has
// exactly the lines that were added, and the worker log never mentions a checkout or a place-order call.

import * as fs from "node:fs";
import * as path from "node:path";
import { devices, expect, test, type Locator, type Page } from "@playwright/test";
import {
  accessToken,
  api,
  fakeStateKeys,
  invite,
  readFakeCart,
  schedulerOnce,
  setAddress,
  setConnectionStatus,
  setFakeAppLocation,
  sql,
  until,
  userId,
} from "../helpers/stack";

test.describe.configure({ mode: "serial" });

const PASSWORD = "e2e-Passw0rd!";
const FAKE_OTP = "123456";
const PHONE = "9876543210";
const ADMIN_EMAIL = (process.env.E2E_ADMIN_EMAIL || "admin@e2e.test").toLowerCase();
const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:3110";
const ADDRESS = "12 Shanti Society, Gorwa, Vadodara, Gujarat";
const LAT = 22.33;
const LON = 73.15;
const GORWA = { lat: LAT, lon: LON, area: "Gorwa, Vadodara" };
const FILLS_PER_DAY = 5;

// Plan 5's list, plus "milk": the app has several milks, so it asks which one to add.
const LIST = [
  "Amul Taza Milk 500ml ×2",
  "Aashirvaad Atta 5kg",
  "Farm Eggs 6 pcs",
  "Lay's Classic",
  "Britannia Brown Bread",
  "milk",
];

const state = { adminReady: false, user: "", requestId: "", fillId: "" };

function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `p6-${tag}-${Date.now().toString(36)}-${rand}@example.com`;
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

async function newMember(page: Page, tag: string): Promise<string> {
  const email = uniqueEmail(tag);
  invite(email);
  await signUp(page, email);
  return email;
}

async function adminSession(page: Page): Promise<void> {
  if (!state.adminReady && !userId(ADMIN_EMAIL)) await signUp(page, ADMIN_EMAIL);
  else await signIn(page, ADMIN_EMAIL);
  state.adminReady = true;
}

// ---- connecting Zepto ------------------------------------------------------------------------------

async function connectZepto(page: Page): Promise<void> {
  const app = "Zepto";
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
  await page.reload();
  await expect(row).toContainText("✓ Synced");
}

// ---- the admin's Sites tab -------------------------------------------------------------------------

/** Learn on the Sites card named `name` of the list `group`. */
async function learnPrices(page: Page, group: string, name: string, button: string): Promise<void> {
  const cardLi = page.getByRole("list", { name: group, exact: true }).getByRole("listitem", { name, exact: true });
  await cardLi.getByRole("button", { name: button, exact: true }).click();
  await expect(cardLi.getByRole("status").filter({ hasText: /Recipe updated|Learn finished/ })).toBeVisible({ timeout: 120_000 });
}

// ---- the cart screen -------------------------------------------------------------------------------

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

/** A fresh /cart with `items` checked: the request id. */
async function checkList(page: Page, items: string[]): Promise<string> {
  await page.goto("/cart");
  await addItems(page, items);
  return compare(page);
}

async function fillsLeft(email: string): Promise<number> {
  const token = await accessToken(email, PASSWORD);
  return (await api(token, "GET", "/me/limits")).body.cart_fills_left as number;
}

async function requestDone(id: string): Promise<void> {
  const status = await until("the check to finish", () => {
    const s = sql<[string]>("select status from cart_requests where id = %s::uuid", [id])[0]?.[0] ?? "";
    return ["done", "failed"].includes(s) ? s : "";
  }, 90_000);
  expect(status).toBe("done");
}

async function fillEnded(fillId: string): Promise<string> {
  return until("the fill to end", () => {
    const s = sql<[string]>("select status from cart_fills where id = %s::uuid", [fillId])[0]?.[0] ?? "";
    return ["done", "partial", "failed", "needs_confirmation"].includes(s) ? s : "";
  }, 120_000);
}

const fillButton = (page: Page) => page.getByRole("button", { name: "Fill cart on Zepto", exact: true });

// ---- the tests -------------------------------------------------------------------------------------

// So that this file also runs alone: Zepto's grocery price recipe is learned through the admin view when missing.
test.beforeAll(async ({ browser }) => {
  test.setTimeout(600_000);
  const have = sql<[number]>(
    "select count(*)::int from recipes where site = 'zepto' and kind = 'search' and healthy and not paused",
  )[0]![0];
  if (have > 0) return;
  const context = await browser.newContext({ ...devices["iPhone 14"], baseURL: BASE_URL, locale: "en-IN" });
  const page = await context.newPage();
  try {
    await adminSession(page);
    await connectZepto(page);
    await page.goto("/admin");
    await expect(mainHeading(page, "Admin")).toBeVisible();
    await page.getByRole("tab", { name: "Sites" }).click();
    await learnPrices(page, "Grocery prices", "Zepto grocery prices", "Learn Zepto grocery prices");
  } finally {
    await context.close();
  }
});

test("admin learns the Zepto cart recipe", async ({ page }) => {
  test.setTimeout(600_000);
  await adminSession(page);
  await connectZepto(page);
  await page.goto("/admin");
  await expect(mainHeading(page, "Admin")).toBeVisible();
  await page.getByRole("tab", { name: "Sites" }).click();

  const cartCard = page
    .getByRole("list", { name: "Cart filling", exact: true })
    .getByRole("listitem", { name: "Zepto cart filling", exact: true });
  await cartCard.getByRole("button", { name: "Learn Zepto cart", exact: true }).click();
  const asking = cartCard.getByRole("list", { name: "Requested POST requests" });
  await expect(asking).toBeVisible({ timeout: 120_000 });
  await expect(asking).toContainText("^https://fake\\.zepto\\.test/api/cart/add$");
  await expect(asking).toContainText("Add to cart");
  await expect(asking).toContainText("^https://fake\\.zepto\\.test/api/location/select$");
  await expect(asking).toContainText("Select location");
  await cartCard.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(
    cartCard.getByRole("status").filter({ hasText: "Waiting for your approval in Approvals" }),
  ).toBeVisible({ timeout: 120_000 });
  await page.reload();
  await page.getByRole("tab", { name: "Sites" }).click();
  await expect(cartCard).toContainText("Waiting for approval");

  await page.getByRole("tab", { name: "Approvals", exact: true }).click();
  const approval = page.getByRole("listitem", { name: /^Zepto cart,/ });
  await expect(approval).toBeVisible();
  const perms = approval.getByRole("region", { name: "Cart permissions" });
  await expect(perms).toBeVisible();
  await expect(perms).toContainText("It can never check out, pay or change addresses.");
  await approval.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(approval).toHaveCount(0);

  await page.getByRole("tab", { name: "Sites", exact: true }).click();
  await expect(cartCard).toContainText("Healthy");
  await expect(cartCard).toContainText("Recipe v1");
  const rows = sql<[boolean, boolean, number]>(
    "select healthy, paused, version from recipes where site = 'zepto' and kind = 'cart' order by version desc limit 1",
  );
  expect(rows[0]).toEqual([true, false, 1]);
});

test("fill needs a logged-in app", async ({ page }) => {
  test.setTimeout(600_000);
  state.user = await newMember(page, "u");
  await connectZepto(page);
  setAddress(state.user, ADDRESS, LAT, LON);
  const id = await checkList(page, ["Aashirvaad Atta 5kg"]);

  setConnectionStatus(state.user, "zepto", "needs_login");
  try {
    await page.goto(`/cart/${id}`);
    const reconnect = page.getByRole("link", { name: "Reconnect Zepto to fill", exact: true });
    await expect(reconnect).toBeVisible({ timeout: 60_000 });
    await expect(reconnect).toHaveAttribute("href", "/accounts");
    await reconnect.click();
    await expect(page).toHaveURL(/\/accounts$/);
  } finally {
    setConnectionStatus(state.user, "zepto", "connected");
  }
});

test("fill adds items, asks, and keeps the existing cart", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page, state.user);
  state.requestId = await checkList(page, LIST);
  await expect(fillButton(page)).toBeEnabled();
  await fillButton(page).click();

  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Fill your Zepto cart?" })).toBeVisible();
  await expect(sheet.getByText("We'll add 6 items to your Zepto cart.")).toBeVisible();
  await expect(sheet.getByText("Items already in your cart stay there.")).toBeVisible();
  await expect(sheet.getByText("1 item needs your choice while we fill.")).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart/[id] with the fill sheet open");
  await sheet.getByRole("button", { name: "Add 6 items", exact: true }).click();

  const question = sheet.getByRole("region", { name: "Your choice" });
  await expect(question.getByText("Which milk should we add?")).toBeVisible({ timeout: 90_000 });
  await expectNoHorizontalScroll(page, "/cart/[id] during a question");
  await question.getByRole("radio", { name: /^Amul Taaza Toned Milk 1 L · .* · ₹55$/ }).check();
  await question.getByRole("button", { name: "Add this", exact: true }).click();

  await expect(sheet.getByRole("heading", { name: "Added 5 of 6 items to your Zepto cart" })).toBeVisible({
    timeout: 90_000,
  });
  await expect(sheet.getByRole("region", { name: "Out of stock now" })).toContainText("Farm Eggs 6 pcs");
  await expect(sheet.getByText("Zepto cart total ₹480 · items ₹465 + fees ₹15", { exact: true })).toBeVisible();
  await expect(sheet.getByText("Includes 1 item that was already in your cart.", { exact: true })).toBeVisible();
  const open = sheet.getByRole("link", { name: "Open Zepto to pay", exact: true });
  await expect(open).toHaveAttribute("href", "https://fake.zepto.test/cart");
  await expect(sheet.getByText("We never place the order or pay.")).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart/[id] with the fill result");

  // Add-only: the butter that was in the cart is still there once, and 5 new lines came in. No order, no checkout.
  const cart = Object.values(readFakeCart(state.user, "zepto"));
  expect(cart.filter((l) => l.name === "Amul Butter 100g")).toEqual([{ name: "Amul Butter 100g", qty: 1 }]);
  expect(cart).toHaveLength(6);
  expect(cart.map((l) => l.name)).not.toContain("Farm Eggs 6 pcs");
  expect(fakeStateKeys(state.user, "zepto")).toEqual(["cart", "location", "locked"]);
  const workerLog = path.join(process.env.E2E_RUN_DIR ?? "", "logs", "worker.log");
  expect(fs.readFileSync(workerLog, "utf-8")).not.toMatch(/checkout|place[_ -]?order/i);

  const row = sql<[string, string]>("select id, status from cart_fills where request_id = %s::uuid", [state.requestId]);
  expect(row).toHaveLength(1);
  state.fillId = row[0]![0];
  expect(row[0]![1]).toBe("partial");
  await sheet.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("the same check can't be filled twice", async ({ page }) => {
  await signIn(page, state.user);
  await page.goto(`/cart/${state.requestId}`);
  const seen = page.getByRole("button", { name: "See what was added", exact: true });
  await expect(seen).toBeVisible({ timeout: 60_000 });
  await seen.click();
  await expect(
    page.getByRole("dialog").getByRole("heading", { name: "Added 5 of 6 items to your Zepto cart" }),
  ).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Done", exact: true }).click();

  const token = await accessToken(state.user, PASSWORD);
  const again = await api(token, "POST", `/cart/checks/${state.requestId}/fill`, { site: "zepto" });
  expect(again.status).toBe(409);
  expect(again.body.detail).toBe("This cart was already filled on Zepto.");
  expect(await fillsLeft(state.user)).toBe(FILLS_PER_DAY - 1);

  // A fill the worker lost halfway ("interrupted") holds its slot too: the user checks their cart in the app.
  sql("update cart_fills set status = 'interrupted' where id = %s::uuid", [state.fillId]);
  try {
    await page.reload();
    await expect(page.getByRole("button", { name: "See what was added", exact: true })).toBeVisible({ timeout: 60_000 });
    const interrupted = await api(token, "POST", `/cart/checks/${state.requestId}/fill`, { site: "zepto" });
    expect(interrupted.status).toBe(409);
    expect(interrupted.body.detail).toBe("This cart was already filled on Zepto.");
  } finally {
    sql("update cart_fills set status = 'partial' where id = %s::uuid", [state.fillId]);
  }
  expect(await fillsLeft(state.user)).toBe(FILLS_PER_DAY - 1);
});

test("location mismatch re-prices instead of filling", async ({ page }) => {
  test.setTimeout(600_000);
  setFakeAppLocation(state.user, "zepto", { lat: 22.31, lon: 73.17, area: "Alkapuri, Vadodara", locked: true });
  await signIn(page, state.user);
  await checkList(page, ["Aashirvaad Atta 5kg", "Britannia Brown Bread"]);
  await fillButton(page).click();
  const sheet = page.getByRole("dialog");
  await sheet.getByRole("button", { name: "Add 2 items", exact: true }).click();

  await expect(sheet.getByRole("heading", { name: "Zepto is set to a different location" })).toBeVisible({
    timeout: 90_000,
  });
  await expect(sheet.getByText("Zepto delivers to Alkapuri, Vadodara right now, not Gorwa, Vadodara.")).toBeVisible();
  await expect(sheet.getByText("₹239 → ₹246")).toBeVisible();
  await expect(sheet.getByText("₹46 → ₹47")).toBeVisible();
  await expect(sheet.getByText("Items there: ₹293 instead of ₹285")).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart/[id] with the re-price notice");
  expect(await fillsLeft(state.user), "a fill that only re-priced is not counted").toBe(FILLS_PER_DAY - 1);
  const before = Object.keys(readFakeCart(state.user, "zepto")).length;

  await sheet.getByRole("button", { name: "Fill at these prices", exact: true }).click();
  await expect(sheet.getByRole("heading", { name: "Added 2 items to your Zepto cart" })).toBeVisible({
    timeout: 90_000,
  });
  expect(await fillsLeft(state.user)).toBe(FILLS_PER_DAY - 2);
  expect(Object.keys(readFakeCart(state.user, "zepto"))).toHaveLength(before); // atta and bread were lines already
});

test("budget pause", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page, state.user);
  const id = await checkList(page, ["Aashirvaad Atta 5kg"]);
  try {
    schedulerOnce({ MONTHLY_BUDGET_INR: "0.01" });
    expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("paused");
    await page.goto(`/cart/${id}`);
    await expect(fillButton(page)).toBeDisabled({ timeout: 60_000 });
    await expect(page.getByText(/Filling carts is paused until 1 \w+\./)).toBeVisible();
  } finally {
    schedulerOnce({});
  }
  expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("ok");
});

test("daily fill limit", async ({ page }) => {
  test.setTimeout(900_000);
  setFakeAppLocation(state.user, "zepto", { ...GORWA, locked: false });
  const token = await accessToken(state.user, PASSWORD);

  async function newCheck(): Promise<string> {
    const res = await api(token, "POST", "/cart/checks", { items: [{ text: "Aashirvaad Atta 5kg", qty: 1 }] });
    expect(res.status, JSON.stringify(res.body)).toBe(202);
    await requestDone(res.body.request_id);
    return res.body.request_id as string;
  }

  let left = await fillsLeft(state.user);
  expect(left).toBe(FILLS_PER_DAY - 2);
  while (left > 0) {
    const check = await newCheck();
    const started = await api(token, "POST", `/cart/checks/${check}/fill`, { site: "zepto" });
    expect(started.status, JSON.stringify(started.body)).toBe(202);
    expect(await fillEnded(started.body.fill_id)).toBe("done");
    const now = await fillsLeft(state.user);
    expect(now).toBe(left - 1);
    left = now;
  }

  const last = await newCheck();
  await signIn(page, state.user);
  await page.goto(`/cart/${last}`);
  await expect(page.getByText("No fills left today")).toBeVisible({ timeout: 60_000 });
  await expect(fillButton(page)).toBeDisabled();
  const refused = await api(token, "POST", `/cart/checks/${last}/fill`, { site: "zepto" });
  expect(refused.status).toBe(429);
  expect(refused.body.detail).toBe(`You can fill ${FILLS_PER_DAY} carts a day. Try again tomorrow.`);
});

test("no horizontal scroll on /cart/[id] with the fill sheet open", async ({ page }) => {
  await signIn(page, state.user);
  await page.goto(`/cart/${state.requestId}`);
  await expect(mainHeading(page, "Cheapest cart")).toBeVisible({ timeout: 60_000 });
  await expectNoHorizontalScroll(page, "/cart/[id] with a filled card");
  await page.getByRole("button", { name: "See what was added", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByRole("heading", { name: "Added 5 of 6 items to your Zepto cart" }),
  ).toBeVisible();
  await expectNoHorizontalScroll(page, "/cart/[id] with the result sheet reopened");
});
