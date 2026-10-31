// Plan 7 end to end on this machine, as an iPhone 14 user and the admin see it: the public privacy page, a session the
// app logged out (banner, email, "Reconnect"), a site that throttles (cool-down), the error budget pausing a site,
// disconnecting an app and deleting the account with a "nothing remains" proof.
//
// The stack is scripts/e2e_local.sh's: fake sites (WORKER_FAKE=1, OTP 123456), Mailpit, ERROR_BUDGET_MIN_JOBS=5.
// The tests share state and run in order. No real phone number, OTP or personal data is used.

import { devices, expect, test, type Locator, type Page } from "@playwright/test";
import { waitForMail } from "../helpers/mailpit";
import {
  accessToken,
  api,
  insertFinishedJobs,
  invite,
  passwordGrant,
  schedulerOnce,
  setOrdersPage,
  sql,
  userId,
  verifyErased,
} from "../helpers/stack";

test.describe.configure({ mode: "serial" });

const PASSWORD = "e2e-Passw0rd!";
const FAKE_OTP = "123456";
const PHONE = "9876543210";
const ADMIN_EMAIL = (process.env.E2E_ADMIN_EMAIL || "admin@e2e.test").toLowerCase();

const state = { email: "", adminReady: false };

function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `p7-${tag}-${Date.now().toString(36)}-${rand}@example.com`;
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

async function adminSession(page: Page): Promise<void> {
  if (!state.adminReady && !userId(ADMIN_EMAIL)) await signUp(page, ADMIN_EMAIL);
  else await signIn(page, ADMIN_EMAIL);
  state.adminReady = true;
}

function zeptoRow(page: Page): Locator {
  return page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: "Zepto" });
}

/** The connect sheet that is open: consent (when asked), phone, OTP, done. */
async function connectFromSheet(page: Page, app: string): Promise<void> {
  const sheet = page.getByRole("dialog");
  const consent = sheet.getByRole("button", { name: "I understand" });
  if (await consent.isVisible().catch(() => false)) await consent.click();
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
}

async function connectZepto(page: Page): Promise<void> {
  await page.goto("/accounts");
  await expect(mainHeading(page, "Your apps")).toBeVisible();
  await page.getByRole("button", { name: "Connect Zepto", exact: true }).click();
  await connectFromSheet(page, "Zepto");
  await page.reload();
  await expect(zeptoRow(page)).toContainText("✓ Synced");
}

test("privacy is public", async ({ page }) => {
  await page.goto("/privacy");
  await expect(page).toHaveURL(/\/privacy$/);
  await expect(page.getByText(/We never touch payment/)).toBeVisible();
  await page.goto("/login");
  const link = page.getByRole("link", { name: /privacy/i });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", "/privacy");
});

test("session expiry", async ({ page }) => {
  test.setTimeout(300_000);
  state.email = uniqueEmail("u");
  invite(state.email);
  await signUp(page, state.email);
  await connectZepto(page);

  setOrdersPage(state.email, "zepto", "https://fake.zepto.test/expired");
  await page.reload();
  await zeptoRow(page).getByRole("button", { name: /^Refresh now/ }).click();
  const banner = page.getByRole("status").filter({ hasText: "Zepto logged you out." });
  await expect(banner).toBeVisible({ timeout: 90_000 });
  await waitForMail(state.email, /Zepto logged you out/);

  // The banner is on every tab. (From /accounts itself the link only changes the query: see the report.)
  await page.goto("/spend");
  await page.getByRole("status").filter({ hasText: "Zepto logged you out." }).getByRole("link", { name: "Reconnect" }).click();
  await expect(page).toHaveURL(/\/accounts/);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Connect Zepto", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  const token = await accessToken(state.email, PASSWORD);
  const direct = await api(token, "POST", "/connections/zepto/refresh");
  expect(direct.status).toBe(409);
  expect(direct.body.detail).toBe("Reconnect Zepto first.");
});

test("throttle cool-down", async ({ page, browser }) => {
  test.setTimeout(300_000);
  await signIn(page, state.email);
  // A reconnect syncs from the stored orders page, which still says "expired": point it at a working one first.
  setOrdersPage(state.email, "zepto", "https://fake.zepto.test/orders");
  await page.goto("/accounts?reconnect=zepto");
  await connectFromSheet(page, "Zepto");
  await page.goto("/accounts");
  await expect(zeptoRow(page)).toContainText("✓ Synced");

  setOrdersPage(state.email, "zepto", "https://fake.zepto.test/throttle");
  await page.reload();
  await zeptoRow(page).getByRole("button", { name: /^Refresh now/ }).click();

  const token = await accessToken(state.email, PASSWORD);
  let refused: Awaited<ReturnType<typeof api>> | null = null;
  for (let i = 0; i < 180 && !refused; i++) {
    const res = await api(token, "POST", "/connections/zepto/refresh");
    if (res.status === 503) refused = res;
    else await new Promise((r) => setTimeout(r, 500));
  }
  expect(refused, "Zepto never went into a cool-down").not.toBeNull();
  expect(String(refused!.body.detail)).toMatch(/^Zepto is limiting requests/);
  expect(refused!.headers.get("retry-after")).toBeTruthy();

  const { defaultBrowserType: _engine, ...iphone } = devices["iPhone 14"];
  const admin = await browser.newContext({ ...iphone, baseURL: test.info().project.use.baseURL, locale: "en-IN" });
  const ap = await admin.newPage();
  try {
    await adminSession(ap);
    await ap.goto("/admin");
    await expect(mainHeading(ap, "Admin")).toBeVisible();
    await ap.getByRole("tab", { name: "Sites" }).click();
    await expect(ap.getByText(/Cooling down until/).first()).toBeVisible();
  } finally {
    await admin.close();
  }
  // Leave the stack as it was: no cool-down for the next tests.
  sql("delete from site_backoff where site = 'zepto'");
});

test("error budget", async ({ page }) => {
  test.setTimeout(300_000);
  // The budget only touches a site with an unpaused orders recipe; Blinkit may never have been learned on this run.
  sql(
    "insert into recipes (site, kind, recipe) select 'blinkit', 'orders', '{}'::jsonb " +
      "where not exists (select 1 from recipes where site = 'blinkit' and kind = 'orders')",
  );
  const throwaway = insertFinishedJobs("blinkit", 5, 3, "RuntimeError: boom");
  try {
    schedulerOnce({});
    await waitForMail(ADMIN_EMAIL, /Blinkit sync paused: too many failures/);

    await adminSession(page);
    await page.goto("/admin");
    await expect(mainHeading(page, "Admin")).toBeVisible();
    await page.getByRole("tab", { name: "Sites" }).click();
    await expect(page.getByText(/Paused automatically: 3 of 5 syncs failed/).first()).toBeVisible();

    await page.getByRole("button", { name: "Resume Blinkit", exact: true }).first().click();
    await expect(page.getByRole("button", { name: "Pause Blinkit", exact: true }).first()).toBeVisible();
    schedulerOnce({});
    const paused = sql<[boolean]>("select paused from recipes where site = 'blinkit' and kind = 'orders'")[0]![0];
    expect(paused, "the same old failures paused Blinkit again").toBe(false);
  } finally {
    sql("delete from jobs where user_id = %s::uuid", [throwaway]);
  }
});

test("disconnect", async ({ page }) => {
  test.setTimeout(300_000);
  await signIn(page, state.email);
  setOrdersPage(state.email, "zepto", "https://fake.zepto.test/orders");
  const count = await until_orders(state.email);
  expect(count).toBeGreaterThan(0);

  await page.goto("/accounts");
  await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Disconnect Zepto" }).click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Disconnect Zepto?" })).toBeVisible();
  await sheet.getByRole("button", { name: "Disconnect Zepto" }).click();
  await expect(page.getByText(/Zepto disconnected\. \d+ orders? deleted\./)).toBeVisible();

  await page.goto("/spend");
  await expect(page.getByRole("link", { name: "Connect an app" }).first()).toBeVisible();
  await page.goto("/accounts");
  await expect(page.getByRole("button", { name: "Connect Zepto", exact: true })).toBeVisible();
  expect(sql("select 1 from orders where user_id = %s::uuid", [userId(state.email)!])).toHaveLength(0);
});

/** The user's Zepto order count once the connect's sync has stored them (the user connected earlier). */
async function until_orders(email: string): Promise<number> {
  const deadline = Date.now() + 60_000;
  for (;;) {
    const n = sql<[number]>("select count(*)::int from orders where user_id = %s::uuid and site = 'zepto'", [
      userId(email)!,
    ])[0]![0];
    if (n > 0 || Date.now() > deadline) return n;
    await new Promise((r) => setTimeout(r, 500));
  }
}

test("delete account", async ({ page }) => {
  test.setTimeout(300_000);
  await signIn(page, state.email);
  await connectZepto(page);
  const id = userId(state.email)!;

  await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Delete account" }).click();
  const sheet = page.getByRole("dialog");
  const confirm = sheet.getByRole("button", { name: "Delete my account" });
  await expect(confirm).toBeDisabled();
  await sheet.getByLabel("Type DELETE to confirm").fill("DELETE");
  await confirm.click();
  await expect(page).toHaveURL(/\/login\?deleted=1/, { timeout: 60_000 });
  await expect(page.getByText("Your account and all its data are deleted.")).toBeVisible();

  expect(verifyErased(id, state.email)).toContain("NOTHING REMAINS");
  const again = await passwordGrant(state.email, PASSWORD);
  expect(again.token, "a deleted account signed in again").toBeNull();
});

test("no horizontal scroll", async ({ page }) => {
  await page.goto("/privacy");
  await expect(page.getByText(/We never touch payment/)).toBeVisible();
  await expectNoHorizontalScroll(page, "/privacy");

  const email = uniqueEmail("w");
  invite(email);
  await signUp(page, email);
  await connectZepto(page);
  await expect(page.getByRole("region", { name: "Your data" })).toBeVisible();
  await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Disconnect Zepto" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectNoHorizontalScroll(page, "/accounts with the data card open");
});
