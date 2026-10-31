// Screenshot pass for the design check (not part of the regular suite: run it by name through
// scripts/e2e_local.sh tests/zz-design-shots.spec.ts). Captures every screen and state at 390x844 (and the busiest at
// 375x812) into design-final/, and writes design-final/audit.json (tap targets, overflow, contrast).
// Each step is soft: a state that cannot be reached is recorded in audit.json under "missed", the pass goes on.

import * as fs from "node:fs";
import * as path from "node:path";
import { devices, expect, test, type Browser, type Locator, type Page } from "@playwright/test";
import {
  accessToken, api, invite, restartWorker, schedulerOnce, setAddress, setConnectionStatus, setFakeAppLocation,
  setOrdersPage, sql, until, userId, insertFinishedJobs,
} from "../helpers/stack";

test.describe.configure({ mode: "serial" });

const OUT = path.resolve(__dirname, "..", "..", "design-final");
const PASSWORD = "e2e-Passw0rd!";
const OTP = "123456";
const PHONE = "9876543210";
const ADMIN = (process.env.E2E_ADMIN_EMAIL || "admin@e2e.test").toLowerCase();
const BASE = process.env.E2E_BASE_URL || "http://localhost:3110";
const ADDRESS = "12 Shanti Society, Gorwa, Vadodara, Gujarat";
const LAT = 22.33;
const LON = 73.15;
const APPS = ["Zepto", "Blinkit", "Swiggy", "Zomato"] as const;
const LIST = ["Amul Taza Milk 500ml ×2", "Aashirvaad Atta 5kg", "Farm Eggs 6 pcs", "Lay's Classic", "Britannia Brown Bread", "milk"];

fs.mkdirSync(OUT, { recursive: true });
const audit: { shots: Record<string, unknown>; missed: string[] } = { shots: {}, missed: [] };
function saveAudit(): void {
  // Playwright starts a fresh worker after a failed test: merge with what an earlier worker wrote.
  const file = path.join(OUT, "audit.json");
  try {
    const old = JSON.parse(fs.readFileSync(file, "utf-8")) as typeof audit;
    audit.shots = { ...old.shots, ...audit.shots };
    audit.missed = Array.from(new Set([...old.missed, ...audit.missed]));
  } catch {
    // first write
  }
  fs.writeFileSync(file, JSON.stringify(audit, null, 1));
}

async function settle(page: Page): Promise<void> {
  await page.waitForTimeout(500);
  await page.evaluate(() => document.fonts?.ready).catch(() => undefined);
}

const AUDIT_JS = () => {
  const out: { small: string[]; contrast: string[]; overflowX: boolean; vw: number; sw: number } = {
    small: [], contrast: [], overflowX: false, vw: 0, sw: 0,
  };
  const de = document.documentElement;
  out.vw = de.clientWidth;
  out.sw = de.scrollWidth;
  out.overflowX = de.scrollWidth > de.clientWidth;
  const parse = (s: string): [number, number, number, number] | null => {
    let m = /^rgba?\(([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.%]+))?\)/.exec(s);
    if (m) {
      const a = m[4] === undefined ? 1 : m[4].endsWith("%") ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
      return [+m[1]!, +m[2]!, +m[3]!, a];
    }
    m = /^color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+))?\)/.exec(s);
    if (m) return [+m[1]! * 255, +m[2]! * 255, +m[3]! * 255, m[4] === undefined ? 1 : +m[4]];
    return null;
  };
  const lum = (c: number[]) => {
    const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c[0]!) + 0.7152 * f(c[1]!) + 0.0722 * f(c[2]!);
  };
  const bgOf = (el: Element): number[] | null => {
    let layers: number[][] = [];
    let e: Element | null = el;
    while (e) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage !== "none") return null;
      const c = parse(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
      e = e.parentElement;
    }
    let base = [255, 255, 255];
    for (const c of layers.reverse()) base = base.map((b, i) => b * (1 - c[3]!) + c[i]! * c[3]!);
    return base;
  };
  const name = (el: Element) => {
    const t = (el.getAttribute("aria-label") || (el as HTMLElement).innerText || "").trim().replace(/\s+/g, " ").slice(0, 40);
    return `${el.tagName.toLowerCase()}"${t}"`;
  };
  const visible = (el: Element) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0";
  };
  document.querySelectorAll("a[href],button,input,select,textarea,[role=tab],[role=radio],[role=button]").forEach((el) => {
    if (!visible(el)) return;
    if ((el as HTMLInputElement).type === "hidden") return;
    const r = el.getBoundingClientRect();
    if (r.height < 43.5 || r.width < 43.5) {
      // inline links inside a sentence are exempt from the box rule
      const inline = el.tagName === "A" && getComputedStyle(el).display === "inline";
      if (!inline) out.small.push(`${name(el)} ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  });
  const seen = new Set<string>();
  document.querySelectorAll("body *").forEach((el) => {
    if (!visible(el)) return;
    const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && (n.textContent || "").trim().length > 0);
    if (!own) return;
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const bg = bgOf(el);
    if (!fg || !bg) return;
    const f = fg[3] < 1 ? fg.slice(0, 3).map((v, i) => v * fg[3]! + bg[i]! * (1 - fg[3]!)) : fg;
    const L1 = lum(f), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const px = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = px >= 24 || (px >= 18.66 && bold);
    const need = large ? 3 : 4.5;
    if (ratio < need) {
      const key = `${name(el)}|${ratio.toFixed(2)}`;
      if (!seen.has(key)) { seen.add(key); out.contrast.push(`${name(el)} ${ratio.toFixed(2)}:1 (${px}px)`); }
    }
  });
  return out;
};

/** Screenshot (full page when no sheet is open, else the viewport) and audit it. */
async function shot(page: Page, name: string, opts: { full?: boolean } = {}): Promise<void> {
  await settle(page);
  const w = page.viewportSize()?.width ?? 390;
  const dialog = (await page.locator("dialog[open], [role=dialog]").count()) > 0;
  const file = `${name}-${w}.png`;
  await page.screenshot({ path: path.join(OUT, file), fullPage: opts.full ?? !dialog });
  try {
    audit.shots[file] = await page.evaluate(AUDIT_JS);
  } catch (e) {
    audit.shots[file] = { error: String(e).slice(0, 100) };
  }
  saveAudit();
}

/** A soft step: failures are recorded, not thrown. */
async function soft(what: string, fn: () => Promise<void>, page?: Page): Promise<boolean> {
  try {
    await fn();
    return true;
  } catch (e) {
    audit.missed.push(`${what}: ${String(e).split("\n")[0]!.slice(0, 160)}`);
    saveAudit();
    if (page) await page.screenshot({ path: path.join(OUT, `_miss-${what.replace(/\W+/g, "-")}.png`) }).catch(() => undefined);
    return false;
  }
}

function uniqueEmail(tag: string): string {
  return `ds-${tag}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}@example.com`;
}
const h1 = (page: Page, name: string): Locator => page.getByRole("heading", { level: 1, name, exact: true });

async function signUp(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page).toHaveURL(/\/spend$/);
  await expect(h1(page, "Spending")).toBeVisible();
}
async function signIn(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Sign in" }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/spend$/);
}
async function newMember(page: Page, tag: string): Promise<string> {
  const email = uniqueEmail(tag);
  invite(email);
  await signUp(page, email);
  return email;
}
async function adminIn(page: Page): Promise<void> {
  if (!userId(ADMIN)) await signUp(page, ADMIN);
  else await signIn(page, ADMIN);
}

/** Connect one app, shooting the consent, phone, OTP, syncing and done steps when `prefix` is given. */
async function connect(page: Page, app: string, prefix?: string, phone = PHONE): Promise<void> {
  await page.goto("/accounts");
  await expect(h1(page, "Your apps")).toBeVisible();
  const row = page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: app });
  const btn = page.getByRole("button", { name: `Connect ${app}`, exact: true });
  if (!(await btn.isVisible())) return;
  await btn.click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: `Before you connect ${app}` })).toBeVisible();
  if (prefix) await shot(page, `${prefix}-1-consent`);
  await sheet.getByRole("button", { name: "I understand" }).click();
  await expect(sheet.getByRole("heading", { name: `Connect ${app}`, exact: true })).toBeVisible();
  if (prefix) await shot(page, `${prefix}-2-phone`);
  await sheet.getByLabel("Phone number").fill(phone);
  if (prefix) await shot(page, `${prefix}-2b-phone-filled`);
  await sheet.getByRole("button", { name: "Send OTP" }).click();
  if (phone.endsWith("0000")) {
    await expect(sheet.getByRole("heading", { name: `${app} has a question` })).toBeVisible({ timeout: 60_000 });
    if (prefix) await shot(page, `${prefix}-3-question`);
    await sheet.getByLabel("Which city do you order from?").fill("Bengaluru");
    await sheet.getByRole("button", { name: "Send answer" }).click();
  }
  await expect(sheet.getByRole("heading", { name: `Enter ${app}'s OTP` })).toBeVisible({ timeout: 60_000 });
  if (prefix) await shot(page, `${prefix}-3-otp`);
  const field = sheet.getByLabel("Verification code");
  if (prefix === "accounts-connect-zepto-wrong") {
    await field.pressSequentially("000000");
    await expect(sheet.getByRole("alert")).toBeVisible({ timeout: 60_000 });
    await shot(page, `${prefix}-4-wrong-otp`);
    await sheet.getByRole("button", { name: "Try again" }).click();
    await expect(sheet.getByRole("heading", { name: `Enter ${app}'s OTP` })).toBeVisible({ timeout: 60_000 });
  }
  await sheet.getByLabel("Verification code").pressSequentially(OTP);
  if (prefix) {
    await page.waitForTimeout(700);
    await shot(page, `${prefix}-4-syncing`);
  }
  await expect(sheet.getByRole("heading", { name: `${app} connected` })).toBeVisible({ timeout: 90_000 });
  if (prefix) await shot(page, `${prefix}-5-connected`);
  await sheet.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.reload();
  await expect(row).toContainText("Synced");
}

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

async function newPhone(browser: Browser, state?: Awaited<ReturnType<Page["context"]>["storageState"]> extends infer S ? S : never, narrow = true) {
  const { defaultBrowserType: _e, ...iphone } = devices["iPhone 14"];
  return browser.newContext({
    ...iphone, baseURL: BASE, locale: "en-IN",
    ...(narrow ? { viewport: { width: 375, height: 812 } } : {}),
    ...(state ? { storageState: state as never } : {}),
  });
}
/** Re-shoot the current URL on a 375x812 phone of the same session. */
async function narrowShot(page: Page, name: string, prep?: (p: Page) => Promise<void>): Promise<void> {
  const browser = page.context().browser()!;
  const ctx = await newPhone(browser, await page.context().storageState());
  try {
    const p = await ctx.newPage();
    await p.goto(new URL(page.url()).pathname + new URL(page.url()).search);
    await p.waitForLoadState("networkidle").catch(() => undefined);
    if (prep) await prep(p);
    await shot(p, name);
  } finally {
    await ctx.close();
  }
}

const S: { user: string; user2: string; groceryId: string; foodId: string } = { user: "", user2: "", groceryId: "", foodId: "" };

test("logged-out screens", async ({ page }) => {
  test.setTimeout(300_000);
  await page.goto("/login");
  await expect(page.getByRole("tab", { name: "Sign in" })).toBeVisible();
  await shot(page, "auth-01-login");
  await narrowShot(page, "auth-01-login");
  await soft("login wrong password", async () => {
    await page.getByLabel("Email", { exact: true }).fill("nobody@example.com");
    await page.getByLabel("Password", { exact: true }).fill("wrong-pass-123");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await shot(page, "auth-02-login-error");
  }, page);
  await soft("signup", async () => {
    await page.goto("/login");
    await page.getByRole("tab", { name: "Create account" }).click();
    await shot(page, "auth-03-signup");
    await page.getByLabel("Email", { exact: true }).fill(uniqueEmail("uninvited"));
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Create account", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("invite-only");
    await shot(page, "auth-04-invite-only");
    await narrowShot(page, "auth-04-invite-only");
  }, page);
  await soft("privacy public", async () => {
    await page.goto("/privacy");
    await expect(page.getByText(/We never touch payment/)).toBeVisible();
    await shot(page, "privacy-01-public");
    await narrowShot(page, "privacy-01-public");
  }, page);
  await soft("not found", async () => {
    await page.goto("/does-not-exist");
    await shot(page, "error-01-not-found");
  }, page);
});

test("admin teaches the recipes and connects the apps", async ({ page }) => {
  test.setTimeout(900_000);
  await adminIn(page);
  for (const app of APPS) await soft(`admin connect ${app}`, () => connect(page, app), page);
  setAddress(ADMIN, ADDRESS, LAT, LON);

  await page.goto("/admin");
  await expect(h1(page, "Admin")).toBeVisible();
  await page.getByRole("tab", { name: "Sites" }).click();

  async function learn(group: string, name: string, approve?: string): Promise<void> {
    void group;
    const btn = page.getByRole("button", { name: `Learn ${name}`, exact: true });
    const li = page.getByRole("listitem").filter({ has: btn });
    await btn.click();
    const done = li.getByRole("status").filter({ hasText: /Recipe updated|Learn finished/ });
    if (approve) {
      const asking = li.getByRole("list", { name: "Requested POST requests" });
      await expect(asking.or(done)).toBeVisible({ timeout: 120_000 });
      if (await asking.isVisible()) {
        if (name.startsWith("Instamart")) await shot(page, "admin-sites-02-learn-asking-approval");
        await li.getByRole("button", { name: "Approve", exact: true }).click();
      }
    }
    await expect(done).toBeVisible({ timeout: 120_000 });
  }
  await soft("learn zepto", () => learn("Grocery prices", "Zepto grocery prices"), page);
  await soft("learn blinkit", () => learn("Grocery prices", "Blinkit grocery prices"), page);
  await soft("learn instamart", () => learn("Grocery prices", "Instamart grocery prices", "^https://fake"), page);
  await soft("learn swiggy", () => learn("Restaurants and menus", "Swiggy restaurants and menus"), page);
  await soft("learn zomato", () => learn("Restaurants and menus", "Zomato restaurants and menus", "^https://fake"), page);

  // The orders recipe and the Zepto cart recipe (with the cart permissions the admin reviews).
  await soft("cart recipe", async () => {
    const cartCard = page.getByRole("list", { name: "Cart filling", exact: true }).getByRole("listitem", { name: "Zepto cart filling", exact: true });
    await cartCard.getByRole("button", { name: "Learn Zepto cart", exact: true }).click();
    const asking = cartCard.getByRole("list", { name: "Requested POST requests" });
    await expect(asking).toBeVisible({ timeout: 120_000 });
    await shot(page, "admin-sites-03-cart-learn-asking");
    await cartCard.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(cartCard.getByRole("status").filter({ hasText: "Waiting for your approval in Approvals" })).toBeVisible({ timeout: 120_000 });
    await page.getByRole("tab", { name: "Approvals", exact: true }).click();
    const approval = page.getByRole("listitem", { name: /^Zepto cart,/ });
    await expect(approval.getByRole("region", { name: "Cart permissions" })).toBeVisible();
    await shot(page, "admin-approvals-01-cart-permissions");
    await narrowShot(page, "admin-approvals-01-cart-permissions", async (p) => {
      await p.getByRole("tab", { name: "Approvals", exact: true }).click();
      await p.waitForTimeout(600);
    });
    await approval.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(approval).toHaveCount(0);
  }, page);
});

test("a new user: empty states, connect flow, Spend, Orders, Ask", async ({ page }) => {
  test.setTimeout(900_000);
  S.user = await newMember(page, "a");

  // empty states before any app is connected
  await soft("spend empty", async () => { await page.goto("/spend"); await expect(h1(page, "Spending")).toBeVisible(); await page.waitForTimeout(800); await shot(page, "empty-01-spend-no-apps"); await narrowShot(page, "empty-01-spend-no-apps"); }, page);
  await soft("orders empty", async () => { await page.goto("/orders"); await expect(h1(page, "Orders")).toBeVisible(); await page.waitForTimeout(800); await shot(page, "empty-02-orders-no-apps"); }, page);
  await soft("ask empty", async () => { await page.goto("/ask"); await expect(h1(page, "Ask")).toBeVisible(); await shot(page, "ask-01-empty"); await narrowShot(page, "ask-01-empty"); }, page);
  await soft("cart empty", async () => { await page.goto("/cart"); await expect(h1(page, "Cart")).toBeVisible(); await shot(page, "empty-03-cart-no-apps"); }, page);
  await soft("accounts none", async () => { await page.goto("/accounts"); await expect(h1(page, "Your apps")).toBeVisible(); await shot(page, "accounts-01-none-connected"); await narrowShot(page, "accounts-01-none-connected"); }, page);

  // connect flow, with a wrong OTP once
  await soft("connect zepto flow", () => connect(page, "Zepto", "accounts-connect-zepto-wrong"), page);
  await soft("connect zomato question", () => connect(page, "Zomato", "accounts-connect-zomato-question", "9876540000"), page);
  await soft("connect swiggy", () => connect(page, "Swiggy", "accounts-connect-swiggy"), page);
  await soft("connect blinkit", () => connect(page, "Blinkit"), page);
  setAddress(S.user, ADDRESS, LAT, LON);

  await soft("narrow connect", async () => {
    await page.goto("/accounts");
    await narrowShot(page, "accounts-02-connected-all", async () => undefined);
    await shot(page, "accounts-02-connected-all");
  }, page);

  await soft("address picker", async () => {
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Delivery address" }).getByRole("button").click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await shot(page, "accounts-address-01-picker");
    const typeInstead = page.getByRole("dialog").getByRole("button", { name: /type|Type/ }).first();
    if (await typeInstead.isVisible()) { await typeInstead.click(); await shot(page, "accounts-address-02-type"); }
    await page.keyboard.press("Escape");
  }, page);

  await soft("refresh limits", async () => {
    await page.goto("/accounts");
    const refresh = page.getByRole("button", { name: /^Refresh now/ }).first();
    await expect(refresh).toBeVisible();
    await shot(page, "accounts-03-refresh-3-left");
    // use the three refreshes of Zepto through the API, then reload: "No refreshes left today"
    const token = await accessToken(S.user, PASSWORD);
    for (let i = 0; i < 3; i++) {
      const r = await api(token, "POST", "/connections/blinkit/refresh");
      if (r.status === 202) await until("refresh", () => sql<[number]>("select count(*)::int from jobs where user_id=%s::uuid and site='blinkit' and type='sync' and status in ('queued','running')", [userId(S.user)!])[0]?.[0] === 0, 60_000);
    }
    await page.reload();
    await expect(page.getByRole("button", { name: "No refreshes left today" }).first()).toBeVisible();
    await shot(page, "accounts-04-refresh-none-left");
    await page.keyboard.press("Escape");
  }, page);

  // Spend with data, Orders, order detail
  await soft("spend", async () => {
    await page.goto("/spend");
    await expect(h1(page, "Spending")).toBeVisible();
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    await shot(page, "spend-01-month");
    await narrowShot(page, "spend-01-month", async (p) => { await p.waitForTimeout(1200); });
  }, page);
  await soft("orders", async () => {
    await page.goto("/orders");
    await expect(h1(page, "Orders")).toBeVisible();
    await page.waitForTimeout(1200);
    await shot(page, "orders-01-list");
    const first = page.getByRole("list").getByRole("link").first();
    if (await first.isVisible()) { await first.click(); await page.waitForTimeout(1000); await shot(page, "orders-02-detail"); }
  }, page);

  // Ask: question, answer, error
  await soft("ask answer", async () => {
    await page.goto("/ask");
    await page.getByLabel("Ask a question about your spending").fill("How much have I spent on each app?");
    await shot(page, "ask-02-question-typed");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByRole("log", { name: "Conversation" }).getByRole("table")).toBeVisible({ timeout: 60_000 });
    await shot(page, "ask-03-answer-table");
    await narrowShot(page, "ask-03-answer-table", async (p) => {
      await p.getByLabel("Ask a question about your spending").fill("How much have I spent on each app?");
      await p.getByRole("button", { name: "Send", exact: true }).click();
      await expect(p.getByRole("log", { name: "Conversation" }).getByRole("table")).toBeVisible({ timeout: 60_000 });
    });
  }, page);
  await soft("ask error", async () => {
    await page.route("**/ask", (r) => r.fulfill({ status: 502, body: '{"detail":"x"}', contentType: "application/json" }));
    await page.getByLabel("Ask a question about your spending").fill("What did I spend on dairy?");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.locator("[data-ask-error]")).toBeVisible({ timeout: 30_000 });
    await shot(page, "ask-04-error-retry");
    await page.unroute("**/ask");
  }, page);
});

test("cart: groceries, compare, pick, split, food", async ({ page }) => {
  test.setTimeout(900_000);
  await signIn(page, S.user);
  await soft("cart list", async () => {
    await page.goto("/cart");
    await expect(page.getByText("Delivering to")).toBeVisible();
    await shot(page, "cart-01-groceries-empty-list");
    await addItems(page, LIST);
    await shot(page, "cart-02-groceries-list");
    await narrowShot(page, "cart-02-groceries-list-narrow-reload");
  }, page);
  await soft("usual sheet", async () => {
    await page.reload();
    await page.getByRole("button", { name: "Start from my usual", exact: true }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "Your usual" })).toBeVisible();
    await shot(page, "cart-03-usual-sheet");
    await page.getByRole("dialog").getByRole("button", { name: /^Add \d+ items?$/ }).click();
    await page.getByRole("list", { name: "Items" }).first().waitFor();
  }, page);
  await soft("compare", async () => {
    await page.goto("/cart");
    await addItems(page, LIST);
    const button = page.getByRole("button", { name: "Compare prices", exact: true });
    await button.click();
    await expect(page).toHaveURL(/\/cart\/[0-9a-f-]{36}$/);
    S.groceryId = new URL(page.url()).pathname.split("/").pop()!;
    await page.waitForTimeout(350);
    await shot(page, "cart-04-compare-in-progress");
    await expect(h1(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
    await shot(page, "cart-05-results-ranked");
    await narrowShot(page, "cart-05-results-ranked");
  }, page);
  await soft("item table split", async () => {
    await page.getByRole("button", { name: "See item prices" }).click();
    await expect(page.getByRole("table")).toBeVisible();
    await shot(page, "cart-06-item-table-and-split-line");
    await narrowShot(page, "cart-06-item-table-and-split-line", async (p) => {
      await p.getByRole("button", { name: "See item prices" }).click();
    });
  }, page);
  await soft("pick sheet", async () => {
    await page.getByRole("link", { name: "Edit list", exact: true }).click();
    const rows = page.getByRole("list", { name: "Items" }).getByRole("listitem");
    while ((await rows.count()) > 0) await rows.first().getByRole("button", { name: /^Remove / }).click();
    await addItems(page, ["milk", "Aashirvaad Atta 5kg"]);
    await page.getByRole("button", { name: "Compare prices", exact: true }).click();
    await expect(h1(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
    await shot(page, "cart-07-results-with-to-pick");
    await page.getByRole("article", { name: "Zepto", exact: true }).getByRole("button", { name: /1 to pick/ }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "Which one did you mean?" })).toBeVisible();
    await shot(page, "cart-08-pick-sheet");
    await narrowShot(page, "cart-08-pick-sheet-narrow-results");
    await page.getByRole("dialog").getByRole("radio", { name: /Amul Taaza Toned Milk 1 L/ }).check();
    await shot(page, "cart-09-pick-sheet-chosen");
    await page.getByRole("dialog").getByRole("button", { name: "Use this", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await shot(page, "cart-10-results-after-pick");
  }, page);

  // Food
  await soft("food", async () => {
    await page.goto("/cart?mode=food");
    await expect(page.getByRole("heading", { name: "Restaurants you order from" })).toBeVisible();
    await shot(page, "food-01-search-usual");
    await narrowShot(page, "food-01-search-usual");
    await page.getByRole("button", { name: /Meghana Foods/ }).click();
    const pair = page.getByRole("button", { name: /Meghana Foods/ }).filter({ has: page.getByRole("img", { name: "On Swiggy and Zomato" }) });
    await expect(pair).toBeVisible({ timeout: 90_000 });
    await shot(page, "food-02-search-results");
    await pair.click();
    await expect(page.getByRole("heading", { name: "Meghana Foods", level: 2 })).toBeVisible();
    const dish = (n: RegExp) => page.getByRole("list", { name: "Menu" }).getByRole("listitem").filter({ hasText: n });
    const half = dish(/Chicken Biryani/).filter({ hasText: /Half/ });
    await half.getByRole("button", { name: /^Add / }).click();
    await half.getByRole("button", { name: /^More / }).click();
    await dish(/Paneer Biryani/).getByRole("button", { name: /^Add / }).click();
    await shot(page, "food-03-menu-with-dishes");
    await narrowShot(page, "food-03-menu-narrow-fresh");
    await page.getByRole("button", { name: /^Compare \d+ dishes?$/ }).click();
    await expect(h1(page, "Meghana Foods")).toBeVisible({ timeout: 90_000 });
    S.foodId = new URL(page.url()).pathname.split("/").pop()!;
    await shot(page, "food-04-results");
    await page.getByRole("button", { name: "See item prices" }).click();
    await shot(page, "food-05-results-item-table");
    await narrowShot(page, "food-04-results", async (p) => { await p.getByRole("button", { name: "See item prices" }).click().catch(() => undefined); });
  }, page);
  await soft("food pair sheet", async () => {
    await page.goto("/cart?mode=food");
    const go = page.getByRole("button", { name: "Search", exact: true });
    await fillUntilEnabled(page.getByLabel("Search a restaurant"), go, "Truffles");
    await go.click();
    const results = page.getByRole("region", { name: "Results" });
    await expect(results).toBeVisible({ timeout: 90_000 });
    await results.getByRole("button", { name: /Truffles/ }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "Same restaurant?" })).toBeVisible();
    await shot(page, "food-06-pair-sheet-same-restaurant");
    await page.getByRole("dialog").getByRole("button", { name: "Yes, same restaurant", exact: true }).click();
    await page.getByRole("list", { name: "Menu" }).waitFor({ timeout: 90_000 });
    await page.getByRole("button", { name: "Change restaurant" }).click();
    await fillUntilEnabled(page.getByLabel("Search a restaurant"), go, "Theobroma");
    await go.click();
    await expect(results).toBeVisible({ timeout: 90_000 });
    await shot(page, "food-07-search-only-on-one-app");
    await fillUntilEnabled(page.getByLabel("Search a restaurant"), go, "Burger Singh");
    await go.click();
    await page.waitForTimeout(1500);
    await shot(page, "food-08-search-burger-singh");
  }, page);

  await soft("no horizontal scroll check", async () => { await page.goto("/cart"); await shot(page, "cart-11-groceries-after"); }, page);
});

test("fill: confirm, progress, question, result, re-price, interrupted", async ({ page }) => {
  test.setTimeout(900_000);
  await signIn(page, S.user);
  // the cart recipe is approved; fill needs Zepto connected and the place set
  setAddress(S.user, ADDRESS, LAT, LON);
  const fillBtn = page.getByRole("button", { name: "Fill cart on Zepto", exact: true });
  let id = "";
  await soft("fill list", async () => {
    await page.goto("/cart");
    await addItems(page, LIST);
    await page.getByRole("button", { name: "Compare prices", exact: true }).click();
    await expect(h1(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
    id = new URL(page.url()).pathname.split("/").pop()!;
    await expect(fillBtn).toBeEnabled();
    await shot(page, "fill-01-result-with-fill-button");
  }, page);
  await soft("fill flow", async () => {
    await fillBtn.click();
    const sheet = page.getByRole("dialog");
    await expect(sheet.getByRole("heading", { name: "Fill your Zepto cart?" })).toBeVisible();
    await shot(page, "fill-02-confirm");
    await narrowShot(page, "fill-02-confirm-narrow-results");
    await sheet.getByRole("button", { name: "Add 6 items", exact: true }).click();
    await page.waitForTimeout(500);
    await shot(page, "fill-03-progress");
    const question = sheet.getByRole("region", { name: "Your choice" });
    await expect(question.getByText("Which milk should we add?")).toBeVisible({ timeout: 90_000 });
    await shot(page, "fill-04-question");
    await question.getByRole("radio", { name: /^Amul Taaza Toned Milk 1 L/ }).check();
    await question.getByRole("button", { name: "Add this", exact: true }).click();
    await expect(sheet.getByRole("heading", { name: /^Added 5 of 6 items/ })).toBeVisible({ timeout: 90_000 });
    await shot(page, "fill-05-result-partial");
    await sheet.getByRole("button", { name: "Done", exact: true }).click();
    await shot(page, "fill-06-result-card-filled");
  }, page);
  await soft("fill interrupted", async () => {
    const fid = sql<[string]>("select id from cart_fills where request_id=%s::uuid", [id])[0]![0];
    sql("update cart_fills set status='interrupted' where id=%s::uuid", [fid]);
    await page.reload();
    await page.getByRole("button", { name: "See what was added", exact: true }).click();
    await shot(page, "fill-07-interrupted");
    await page.keyboard.press("Escape");
    sql("update cart_fills set status='partial' where id=%s::uuid", [fid]);
  }, page);
  await soft("fill reprice", async () => {
    setFakeAppLocation(S.user, "zepto", { lat: 22.31, lon: 73.17, area: "Alkapuri, Vadodara", locked: true });
    await page.goto("/cart");
    await addItems(page, ["Aashirvaad Atta 5kg", "Britannia Brown Bread"]);
    await page.getByRole("button", { name: "Compare prices", exact: true }).click();
    await expect(h1(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
    await fillBtn.click();
    const sheet = page.getByRole("dialog");
    await sheet.getByRole("button", { name: "Add 2 items", exact: true }).click();
    await expect(sheet.getByRole("heading", { name: "Zepto is set to a different location" })).toBeVisible({ timeout: 90_000 });
    await shot(page, "fill-08-reprice");
    await narrowShot(page, "fill-08-reprice-narrow-results");
    await sheet.getByRole("button", { name: "Fill at these prices", exact: true }).click();
    await expect(sheet.getByRole("heading", { name: /^Added 2 items/ })).toBeVisible({ timeout: 90_000 });
    await shot(page, "fill-09-result-all-added");
    await sheet.getByRole("button", { name: "Done", exact: true }).click();
  }, page);
  await soft("fill not available / paused", async () => {
    const r = await api(await accessToken(S.user, PASSWORD), "POST", "/cart/checks", { items: [{ text: "Aashirvaad Atta 5kg", qty: 1 }] });
    await until("check", () => sql<[string]>("select status from cart_requests where id=%s::uuid", [r.body.request_id])[0]?.[0] === "done", 90_000);
    schedulerOnce({ MONTHLY_BUDGET_INR: "0.01" });
    await page.goto(`/cart/${r.body.request_id}`);
    await expect(page.getByText(/Filling carts is paused until/)).toBeVisible({ timeout: 60_000 });
    await shot(page, "cart-12-fill-paused-budget");
    schedulerOnce({});
  }, page);
  await soft("food no fill", async () => {
    await page.goto(`/cart/${S.foodId}`);
    await expect(h1(page, "Meghana Foods")).toBeVisible({ timeout: 60_000 });
    await shot(page, "food-09-result-reload");
  }, page);
});

test("needs login, re-login banner, cool-down", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page, S.user);
  await soft("needs login", async () => {
    setConnectionStatus(S.user, "blinkit", "needs_login");
    await page.goto("/accounts");
    await expect(page.getByRole("region", { name: "Delivery apps" })).toContainText("Needs login");
    await shot(page, "accounts-05-needs-login");
    await page.goto("/cart");
    await shot(page, "cart-13-needs-login-app-row");
    await page.goto("/spend");
    await shot(page, "spend-02-needs-login");
  }, page);
  await soft("relogin banner", async () => {
    setOrdersPage(S.user, "zepto", "https://fake.zepto.test/expired");
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Delivery apps" }).getByRole("listitem").filter({ hasText: "Zepto" }).getByRole("button", { name: /^Refresh now/ }).click();
    const banner = page.getByRole("status").filter({ hasText: "Zepto logged you out." });
    await expect(banner).toBeVisible({ timeout: 90_000 });
    await shot(page, "accounts-06-relogin-banner");
    await narrowShot(page, "accounts-06-relogin-banner");
    await page.goto("/spend");
    await expect(page.getByRole("status").filter({ hasText: "Zepto logged you out." })).toBeVisible();
    await shot(page, "spend-03-relogin-banner");
    await page.goto("/ask");
    await shot(page, "ask-05-relogin-banner");
    await page.goto("/cart");
    await shot(page, "cart-14-relogin-banner");
    await page.goto("/accounts?reconnect=zepto");
    await expect(page.getByRole("dialog")).toBeVisible();
    await shot(page, "accounts-07-reconnect-sheet");
    await page.keyboard.press("Escape");
    setOrdersPage(S.user, "zepto", "https://fake.zepto.test/orders");
    setConnectionStatus(S.user, "zepto", "connected");
    setConnectionStatus(S.user, "blinkit", "connected");
  }, page);
  await soft("worker logged out compare", async () => {
    await restartWorker({ FAKE_LOGGED_OUT_SITES: "blinkit" });
    try {
      await page.goto("/cart");
      await addItems(page, ["Amul Taza Milk 500ml"]);
      await page.getByRole("button", { name: "Compare prices", exact: true }).click();
      await expect(h1(page, "Cheapest cart")).toBeVisible({ timeout: 90_000 });
      await expect(page.getByRole("article", { name: "Blinkit" })).toContainText("logged you out");
      await shot(page, "cart-15-result-app-logged-out");
    } finally {
      await restartWorker({});
    }
    setConnectionStatus(S.user, "blinkit", "connected");
  }, page);
  await soft("cooldown on user side", async () => {
    sql("insert into site_backoff (site, level, until, last_kind, events) values ('zepto', 1, now() + interval '20 minutes', 'rate_limited', 3) on conflict (site) do update set until = excluded.until, level=1, last_kind='rate_limited'");
    await page.goto("/accounts");
    await page.waitForTimeout(800);
    await shot(page, "accounts-08-site-cooling-down");
  }, page);
});

test("settings, privacy, disconnect, delete", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page, S.user);
  await soft("settings", async () => {
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Notifications" }).scrollIntoViewIfNeeded();
    await shot(page, "settings-01-accounts-full");
    await page.getByRole("region", { name: "Notifications" }).screenshot({ path: path.join(OUT, "settings-02-notifications-card-390.png") });
    await page.getByRole("region", { name: "Your data" }).screenshot({ path: path.join(OUT, "settings-03-your-data-card-390.png") });
    await page.getByRole("region", { name: "Your account" }).screenshot({ path: path.join(OUT, "settings-04-account-card-390.png") });
  }, page);
  await soft("privacy signed in", async () => {
    await page.goto("/privacy");
    await shot(page, "privacy-02-signed-in");
  }, page);
  await soft("disconnect sheet", async () => {
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Disconnect Swiggy" }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "Disconnect Swiggy?" })).toBeVisible();
    await shot(page, "settings-05-disconnect-sheet");
    await narrowShot(page, "settings-05-disconnect-sheet", async (p) => {
      await p.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Disconnect Swiggy" }).click();
      await p.waitForTimeout(600);
    });
    await page.getByRole("dialog").getByRole("button", { name: "Disconnect Swiggy" }).click();
    await expect(page.getByText(/Swiggy disconnected\. \d+ orders? deleted\./)).toBeVisible();
    await shot(page, "settings-06-disconnected-confirmation");
  }, page);
  await soft("delete sheet", async () => {
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Delete account" }).click();
    const sheet = page.getByRole("dialog");
    await shot(page, "settings-07-delete-sheet");
    await narrowShot(page, "settings-07-delete-sheet", async (p) => {
      await p.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Delete account" }).click();
      await p.waitForTimeout(600);
    });
    await sheet.getByLabel("Type DELETE to confirm").fill("DELETE");
    await shot(page, "settings-08-delete-sheet-armed");
    await page.keyboard.press("Escape");
  }, page);
});

test("a second user: delete account end state", async ({ page }) => {
  test.setTimeout(300_000);
  S.user2 = await newMember(page, "b");
  await soft("delete end", async () => {
    await page.goto("/accounts");
    await page.getByRole("region", { name: "Your data" }).getByRole("button", { name: "Delete account" }).click().catch(async () => { await page.getByRole("button", { name: "Delete account" }).click(); });
    const sheet = page.getByRole("dialog");
    await sheet.getByLabel("Type DELETE to confirm").fill("DELETE");
    await sheet.getByRole("button", { name: "Delete my account" }).click();
    await expect(page.getByText("Your account and all its data are deleted.")).toBeVisible({ timeout: 90_000 });
    await shot(page, "settings-09-delete-nothing-remains");
  }, page);
  await soft("deleted login", async () => {
    await page.goto("/login?deleted=1");
    await shot(page, "auth-05-login-after-delete");
  }, page);
});

test("admin screens with data", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page, ADMIN);
  sql("insert into site_backoff (site, level, until, last_kind, events) values ('zepto', 1, now() + interval '20 minutes', 'rate_limited', 3) on conflict (site) do update set until = excluded.until, level=1, last_kind='rate_limited'");
  sql("insert into recipes (site, kind, recipe) select 'blinkit', 'orders', '{}'::jsonb where not exists (select 1 from recipes where site='blinkit' and kind='orders')");
  const throwaway = insertFinishedJobs("blinkit", 5, 3, "RuntimeError: boom");
  try { schedulerOnce({}); } catch { audit.missed.push("scheduler tick failed (admin auto-pause group may be absent)"); }
  const openTab = async (p: Page, tab: string) => {
    await p.goto("/admin");
    await expect(h1(p, "Admin")).toBeVisible();
    await p.getByRole("tab", { name: tab, exact: true }).click();
    await expect(p.locator('[aria-busy="true"]')).toHaveCount(0);
    await p.waitForTimeout(700);
  };
  for (const [tab, slug] of [["Costs", "costs"], ["Sites", "sites"], ["Fees", "fees"], ["Approvals", "approvals-empty"], ["Invites", "invites"]] as const) {
    await soft(`admin ${tab}`, async () => {
      await openTab(page, tab);
      await shot(page, `admin-${slug}-01`);
      await narrowShot(page, `admin-${slug}-01`, (p) => openTab(p, tab));
    }, page);
  }
  await soft("admin sites cooldown, paused, speed groups", async () => {
    await openTab(page, "Sites");
    await expect(page.getByText(/Cooling down until/).first()).toBeVisible();
    await shot(page, "admin-sites-04-cooldown-and-auto-paused");
  }, page);
  await soft("admin sites cart group", async () => {
    await openTab(page, "Sites");
    await page.getByRole("list", { name: "Cart filling", exact: true }).scrollIntoViewIfNeeded();
    await shot(page, "admin-sites-05-cart-filling-group");
  }, page);
  await soft("admin invites waitlist", async () => {
    await openTab(page, "Invites");
    await page.getByLabel(/email/i).first().fill("friend@example.com").catch(() => undefined);
    await shot(page, "admin-invites-02-form-filled");
  }, page);
  sql("delete from jobs where user_id = %s::uuid", [throwaway]);
  sql("delete from site_backoff where site = 'zepto'");
  void Promise.resolve(userId);
});
