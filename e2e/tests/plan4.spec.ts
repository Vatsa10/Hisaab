// Plan 4 end to end on this machine, as iPhone 14 users and one admin see it: invite-only sign-up and the waitlist,
// the first-sync email, the three-refreshes-a-day limit, cost per user, an admin learn with a live approval, the
// budget pause of sign-ups, a second sync and a compare that never ask for a login again, and deleting the account.
//
// The stack is scripts/e2e_local.sh's: fake sites (WORKER_FAKE=1, OTP 123456), Mailpit for every email, and the
// admin account named by E2E_ADMIN_EMAIL (made an admin by `cli.py add-admin` before the run). The tests share
// state and run in order. No real phone number, OTP or personal data is used anywhere.

import { devices, expect, test, type Browser, type Locator, type Page } from "@playwright/test";
import { waitForMail, mailSubjects } from "../helpers/mailpit";
import {
  accessToken,
  api,
  invite,
  lockAuthUser,
  passwordGrant,
  schedulerOnce,
  sql,
  until,
  userId,
  vaultRoundTrip,
} from "../helpers/stack";

test.describe.configure({ mode: "serial" });

const PASSWORD = "e2e-Passw0rd!";
const FAKE_OTP = "123456";
const PHONE = "9876543210";
const ADMIN_EMAIL = (process.env.E2E_ADMIN_EMAIL || "admin@e2e.test").toLowerCase();
const REFRESH_LIMIT = 3;

// State handed from one test to the next.
const state = { member: "", connector: "", adminReady: false, restoreUser: "", restoreSyncJob: "" };

function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `p4-${tag}-${Date.now().toString(36)}-${rand}@example.com`;
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

/** Another phone (own cookies and storage) in the same browser. */
async function newPhone(browser: Browser): Promise<Page> {
  const { defaultBrowserType: _engine, ...iphone } = devices["iPhone 14"];
  const context = await browser.newContext({ ...iphone, baseURL: test.info().project.use.baseURL, locale: "en-IN" });
  return context.newPage();
}

// ---- auth ------------------------------------------------------------------------------------------

async function openSignUp(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account", exact: true }).click();
}

async function signUp(page: Page, email: string): Promise<void> {
  await openSignUp(page, email);
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

async function adminSession(page: Page): Promise<void> {
  if (!state.adminReady) {
    await signUp(page, ADMIN_EMAIL);
    state.adminReady = true;
  } else {
    await signIn(page, ADMIN_EMAIL);
  }
}

// ---- connecting an app -------------------------------------------------------------------------------

async function connectZepto(page: Page): Promise<void> {
  await page.goto("/accounts");
  await expect(mainHeading(page, "Your apps")).toBeVisible();
  await page.getByRole("button", { name: "Connect Zepto", exact: true }).click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Before you connect Zepto" })).toBeVisible();
  await sheet.getByRole("button", { name: "I understand" }).click();
  await expect(sheet.getByRole("heading", { name: "Connect Zepto", exact: true })).toBeVisible();
  await sheet.getByLabel("Phone number").fill(PHONE);
  await sheet.getByRole("button", { name: "Send OTP" }).click();
  await expect(sheet.getByRole("heading", { name: "Enter Zepto's OTP" })).toBeVisible();
  const field = sheet.getByLabel("Verification code");
  await expect(field).toBeVisible();
  await field.pressSequentially(FAKE_OTP);
  await expect(sheet.getByRole("heading", { name: "Zepto connected" })).toBeVisible({ timeout: 90_000 });
  await sheet.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.reload(); // the list is read fresh: never rely on a second session's live updates
  await expect(page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: "Zepto" }))
    .toContainText("✓ Synced");
}

function doneJobs(user: string, type: string, where = ""): number {
  const rows = sql<[number]>(
    `select count(*)::int from jobs where user_id = %s::uuid and site = 'zepto' and type = %s and status = 'done' ${where}`,
    [user, type],
  );
  return rows[0]?.[0] ?? 0;
}

// ---- the tests -----------------------------------------------------------------------------------------

test("invite-only sign-up joins the waitlist", async ({ page }) => {
  state.member = uniqueEmail("member");
  await openSignUp(page, state.member);
  await expect(page.getByRole("status")).toContainText("invite-only");
  await expect(page.getByRole("status")).toContainText(state.member);
  await expect(page).toHaveURL(/\/login/);
  expect(userId(state.member), "an uninvited address must not get an account").toBeUndefined();
  const waiting = sql<[string]>("select email from waitlist where email = %s", [state.member]);
  expect(waiting.length, "the address joins the waitlist").toBe(1);
});

test("admin invites from the waitlist", async ({ page, browser }) => {
  await adminSession(page);
  await page.goto("/admin");
  await expect(mainHeading(page, "Admin")).toBeVisible();
  await page.getByRole("tab", { name: "Invites" }).click();
  const waitlist = page.getByRole("list", { name: "Waitlist" });
  await expect(waitlist).toContainText(state.member);
  await page.getByRole("button", { name: `Invite ${state.member}`, exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Invite sent to" })).toContainText(state.member);

  schedulerOnce(); // the scheduler's tick sends the queued invite email
  await waitForMail(state.member, /You're invited to Hisaab/);

  const theirs = await newPhone(browser);
  try {
    await signUp(theirs, state.member);
  } finally {
    await theirs.context().close();
  }
  expect(userId(state.member)).toBeTruthy();
});

test("first sync sends an email", async ({ page }) => {
  // A user who signed up in this very page: its Realtime channels deliver the connect progress live.
  state.connector = await newMember(page, "connector");
  await connectZepto(page);
  await waitForMail(state.connector, /Zepto is connected/);
});

test("refresh limit: three a day, then none left and a 429", async ({ page }) => {
  const uid = userId(state.connector)!;
  await signIn(page, state.connector);
  await page.goto("/accounts");
  for (let used = 0; used < REFRESH_LIMIT; used++) {
    const left = REFRESH_LIMIT - used;
    const button = page.getByRole("button", { name: `Refresh now · ${left} left today`, exact: true });
    await button.click();
    await until(`refresh ${used + 1} to finish`, () => doneJobs(uid, "sync", "and payload->>'trigger' = 'manual'") >= used + 1, 90_000);
    await page.reload();
    await expect(mainHeading(page, "Your apps")).toBeVisible();
  }
  const none = page.getByRole("button", { name: "No refreshes left today", exact: true });
  await expect(none).toBeVisible();
  await expect(none).toBeDisabled();

  const token = await accessToken(state.connector, PASSWORD);
  const fourth = await api(token, "POST", "/connections/zepto/refresh");
  expect(fourth.status).toBe(429);
  expect(fourth.body.detail).toBe("You can refresh Zepto 3 times a day. Try again tomorrow.");
  const limits = await api(token, "GET", "/me/limits");
  expect(limits.body.refresh_left.zepto).toBe(0);
});

test("admin sees cost per user", async ({ page }) => {
  const uid = userId(state.connector)!;
  await until("usage to be recorded for four syncs", () => (sql<[number]>(
    "select coalesce(sum(syncs), 0)::int from usage where user_id = %s::uuid", [uid])[0]?.[0] ?? 0) >= 4);
  await adminSession(page);

  const token = await accessToken(ADMIN_EMAIL, PASSWORD);
  const costs = await api(token, "GET", "/admin/costs");
  expect(costs.status).toBe(200);
  const row = costs.body.users.find((u: { user_id: string }) => u.user_id === uid);
  expect(row, "the member has a cost row").toBeTruthy();
  expect(row.worker_minutes, "worker minutes").toBeGreaterThan(0);
  expect(row.syncs).toBe(4); // the connect and three refreshes

  await page.goto("/admin");
  await expect(mainHeading(page, "Admin")).toBeVisible();
  const rows = page.getByRole("list", { name: "Cost per user" }).getByRole("listitem");
  const mine = rows.filter({ hasText: /4 syncs/ });
  await expect(mine).toHaveCount(1);
  await expect(mine).toContainText("₹");
  await expect(mine).toContainText(/\d+(\.\d+)? min/);
});

test("admin learn with approval", async ({ page }) => {
  await adminSession(page);
  await connectZepto(page); // the admin's own Zepto: a learn runs on it

  await page.goto("/admin");
  await page.getByRole("tab", { name: "Sites" }).click();
  await page.getByRole("button", { name: "Learn Zepto", exact: true }).click();

  const request = page.getByRole("list", { name: "Requested POST requests" });
  await expect(request).toContainText("^https://fake\\.zepto\\.test/api/orders$");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Recipe updated to v1." })).toBeVisible();

  await page.reload();
  await page.getByRole("tab", { name: "Sites" }).click();
  const zepto = page.getByRole("listitem", { name: "Zepto", exact: true });
  await expect(zepto).toContainText("Recipe v1");
  await expect(zepto).toContainText("Healthy");
  const recipe = sql<[number, boolean]>("select version, healthy from recipes where site = 'zepto' and kind = 'orders'");
  expect(recipe[0]).toEqual([1, true]);

  // Compares need the grocery-price recipe too (GET only, so it publishes without a question): learned through the
  // API on the admin's saved address, so the next test can compare as an ordinary user.
  const token = await accessToken(ADMIN_EMAIL, PASSWORD);
  await choosePlace(token, userId(ADMIN_EMAIL)!);
  const learn = await api(token, "POST", "/admin/learn/zepto?kind=search");
  expect(learn.status).toBe(202);
  await until("the grocery-price learn to finish", () =>
    sql<[string]>("select status from jobs where id = %s::uuid", [learn.body.job_id])[0]?.[0] === "done");
  expect(sql<[boolean]>("select healthy from recipes where site = 'zepto' and kind = 'search'")[0]?.[0]).toBe(true);
});

/** Make the user's delivery place their Zepto "Home" saved address (what the fake app lists after a connect). */
async function choosePlace(token: string, uid: string): Promise<void> {
  const addr = sql<[string]>(
    "select address_id from app_addresses where user_id = %s::uuid and site = 'zepto' order by address_id limit 1", [uid]);
  expect(addr.length, "the connect saved Zepto's addresses").toBeGreaterThan(0);
  const put = await api(token, "PUT", "/profile", { choice: { site: "zepto", address_id: addr[0]![0] } });
  expect(put.status).toBe(200);
}

test("a second sync and a compare ask for no login: the session comes back from the vault", async ({ page }) => {
  // The real store behind a job's restore, round-tripped on a throwaway file vault (fake jobs skip the vault).
  expect(vaultRoundTrip()).toBe("vault-ok");

  state.restoreUser = await newMember(page, "restore");
  const uid = userId(state.restoreUser)!;
  await connectZepto(page); // the one and only login: the OTP
  expect(doneJobs(uid, "connect")).toBe(1);

  // Second sync: Refresh now, no OTP field anywhere, the job ends done without a prompt.
  await page.goto("/accounts");
  await page.getByRole("button", { name: "Refresh now · 3 left today", exact: true }).click();
  await until("the second sync to finish", () => doneJobs(uid, "sync") >= 1, 90_000);
  await expect(page.getByLabel("Verification code")).toHaveCount(0);
  await expect(page.getByText(/Enter Zepto's OTP|Zepto has a question/)).toHaveCount(0);
  const sync = sql<[string, string | null, string | null]>(
    "select id, progress->>'stage', error from jobs where user_id = %s::uuid and type = 'sync' and status = 'done'", [uid]);
  expect(sync.length).toBe(1);
  expect(["otp_needed", "otp_received", "question_needed", "answer_received"]).not.toContain(sync[0]![1]);
  expect(sync[0]![2], "the sync ended without an error").toBeNull();
  state.restoreSyncJob = sync[0]![0];

  // A compare on the same session.
  const token = await accessToken(state.restoreUser, PASSWORD);
  await choosePlace(token, uid);
  const check = await api(token, "POST", "/cart/checks", { items: [{ text: "milk", qty: 1 }] });
  expect(check.status, JSON.stringify(check.body)).toBe(202);
  const requestId: string = check.body.request_id;
  await until("the compare to be priced", () =>
    sql<[string]>("select status from cart_requests where id = %s::uuid", [requestId])[0]?.[0] === "done", 90_000);
  const result = sql<[string]>("select status from cart_results where request_id = %s::uuid and site = 'zepto'", [requestId]);
  expect(result[0]?.[0]).toBe("ok");
  const search = sql<[string, string | null, string | null]>(
    "select status, progress->>'stage', error from jobs where user_id = %s::uuid and type = 'search'", [uid]);
  expect(search.length).toBe(1);
  expect(search[0]![0]).toBe("done");
  expect(["otp_needed", "question_needed"]).not.toContain(search[0]![1]);

  // Still one login in the whole history, the connection still connected.
  expect(sql<[number]>("select count(*)::int from jobs where user_id = %s::uuid and type = 'connect'", [uid])[0]![0]).toBe(1);
  expect(sql<[string]>("select status from connections where user_id = %s::uuid and site = 'zepto'", [uid])[0]![0]).toBe("connected");
  await page.reload();
  await expect(page.getByRole("button", { name: /^Refresh now/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect Zepto", exact: true })).toHaveCount(0);
});

test("deleting the account removes the sign-in and the API can still lock auth.users", async ({ page }) => {
  const email = await newMember(page, "delete");
  const uid = userId(email)!;
  await connectZepto(page);
  expect(sql<[number]>("select count(*)::int from orders where user_id = %s::uuid", [uid])[0]![0]).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Delete account" }).click();
  await page.getByLabel("Type DELETE to confirm").fill("DELETE");
  await page.getByRole("button", { name: "Delete my account", exact: true }).click();
  await expect(page).toHaveURL(/\/login\?deleted=1/);

  // The Supabase Auth user is gone, so signing in again fails, in the API of Auth and in the app.
  expect(userId(email), "the auth user is removed").toBeUndefined();
  const grant = await passwordGrant(email, PASSWORD);
  expect(grant.token).toBeNull();
  expect(grant.status).toBeGreaterThanOrEqual(400);
  expect(grant.status).toBeLessThan(500);
  await page.getByRole("tab", { name: "Sign in" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "don't match" })).toBeVisible();
  await expect(page).toHaveURL(/\/login/);

  // Nothing of theirs is left.
  for (const table of ["orders", "connections", "jobs", "consents", "profiles", "usage"]) {
    const n = sql<[number]>(`select count(*)::int from ${table} where user_id = %s::uuid`, [uid])[0]![0];
    expect(n, `${table} rows left`).toBe(0);
  }
  expect(sql<[number]>("select count(*)::int from invites where email = %s", [email])[0]![0]).toBe(0);

  // The API's database role can take both user-row locks (delete uses FOR UPDATE, enqueue FOR KEY SHARE).
  const apiRole = new URL(process.env.DATABASE_URL!).username;
  expect(lockAuthUser(userId(state.member)!)).toBe(apiRole);
});

test("budget pause stops sign-ups and the next tick restores them", async ({ page }) => {
  const late = uniqueEmail("late");
  invite(late);
  schedulerOnce({ MONTHLY_BUDGET_INR: "0.01" });
  expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("paused");
  await waitForMail(ADMIN_EMAIL, /Budget reached/);

  await openSignUp(page, late);
  await expect(page.getByRole("status")).toContainText("Sign-ups are paused");
  expect(userId(late), "no account while the budget is paused").toBeUndefined();

  schedulerOnce({});
  expect(sql<[string]>("select level from budget_state order by month desc limit 1")[0]![0]).toBe("ok");
  const token = await accessToken(ADMIN_EMAIL, PASSWORD);
  expect((await api(token, "GET", "/me/limits")).body.budget_paused).toBe(false);
  await signUp(page, late);
  expect((await mailSubjects(ADMIN_EMAIL)).some((s) => /Budget reached/.test(s))).toBe(true);
});

test("no horizontal scroll on /admin", async ({ page }) => {
  await adminSession(page);
  await page.goto("/admin");
  await expect(mainHeading(page, "Admin")).toBeVisible();
  for (const tab of ["Costs", "Sites", "Fees", "Invites", "Approvals"]) {
    await page.getByRole("tab", { name: tab, exact: true }).click();
    await expect(page.getByRole("tab", { name: tab, exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    await expectNoHorizontalScroll(page, `/admin ${tab}`);
  }
});
