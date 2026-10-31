// Reaches into the local stack scripts/e2e_local.sh started: the CLI, the scheduler, the database and the
// API as a signed-in user. Local only: DATABASE_URL points at the throwaway Supabase on this machine.
// Nothing here prints a password, token, phone number or OTP.

import { execFileSync } from "node:child_process";
import * as path from "node:path";

const ROOT = path.resolve(__dirname, "..", "..");

function need(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set; run this through scripts/e2e_local.sh`);
  return v;
}

function python(args: string[], extraEnv: Record<string, string> = {}): string {
  return execFileSync(need("E2E_PY"), args, {
    cwd: ROOT,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1", ...extraEnv },
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 120_000,
  });
}

/** `cli.py invite <email>`: records the invite and queues the invite email (sent by the next dispatch). */
export function invite(email: string): void {
  python(["cli.py", "invite", email]);
}

/** One scheduler tick (`python -m server.scheduler --once`): budget level, due syncs and the mail outbox. */
export function schedulerOnce(env: Record<string, string> = {}): void {
  python(["-m", "server.scheduler", "--once"], env);
}

const SQL_RUNNER = [
  "import json, os, psycopg",
  "with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as c:",
  "    cur = c.execute(os.environ['E2E_SQL'], json.loads(os.environ['E2E_ARGS']))",
  "    rows = cur.fetchall() if cur.description else []",
  "print(json.dumps(rows, default=str))",
].join("\n");

/** Run one statement against the local database; rows come back as arrays (dates and uuids as text). */
export function sql<T extends unknown[] = unknown[]>(query: string, args: unknown[] = []): T[] {
  const out = python(["-c", SQL_RUNNER], { E2E_SQL: query, E2E_ARGS: JSON.stringify(args) });
  return JSON.parse(out.trim().split("\n").pop() ?? "[]") as T[];
}

export function userId(email: string): string | undefined {
  return sql<[string]>("select id from auth.users where lower(email) = lower(%s)", [email])[0]?.[0];
}

/** Poll `fn` until it returns a truthy value (no sleeping on a guess). */
export async function until<T>(what: string, fn: () => T | Promise<T>, timeoutMs = 60_000): Promise<NonNullable<T>> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await fn();
    if (v) return v as NonNullable<T>;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 300));
  }
}

// ---- the API as a user ----------------------------------------------------------------------------------

/** Sign in at the local Supabase Auth (password grant); token is null when it refuses. */
export async function passwordGrant(email: string, password: string): Promise<{ token: string | null; status: number }> {
  const res = await fetch(`${need("E2E_SUPABASE_URL")}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: need("E2E_ANON_KEY"), "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) return { token: null, status: res.status };
  const body = (await res.json()) as { access_token?: string };
  return { token: body.access_token ?? null, status: res.status };
}

export async function accessToken(email: string, password: string): Promise<string> {
  const { token, status } = await passwordGrant(email, password);
  if (!token) throw new Error(`could not sign ${email} in for an API call (${status})`);
  return token;
}

export interface ApiReply {
  status: number;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  body: any;
}

export async function api(token: string, method: string, route: string, body?: unknown): Promise<ApiReply> {
  const res = await fetch(`${need("E2E_API_URL")}${route}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let parsed: unknown = text;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    // not JSON: keep the text
  }
  return { status: res.status, body: parsed };
}

// ---- the vault and the user-row locks --------------------------------------------------------------------

const VAULT_ROUND_TRIP = [
  "import tempfile",
  "from pathlib import Path",
  "from server import vault, config",
  "uid, prof = 'e2e-user', 'zepto'",
  "key = vault.master_key_from_env(config.DEV_PROFILE_KEY)",
  "with tempfile.TemporaryDirectory() as d:",
  "    store = vault.LocalStore(Path(d) / 'vault')",
  "    src = Path(d) / 'in'",
  "    (src / 'Default').mkdir(parents=True)",
  "    (src / 'Default' / 'Cookies').write_text('session-marker', encoding='utf-8')",
  "    vault.stamp(src, uid, prof)",
  "    vault.save_profile(store, key, uid, prof, src)",
  "    blob = store.get(vault.blob_key(uid, prof))",
  "    assert blob and b'session-marker' not in blob, 'the stored blob is not encrypted'",
  "    dst = Path(d) / 'out'",
  "    assert vault.restore_profile(store, key, uid, prof, dst) is True",
  "    assert (dst / 'Default' / 'Cookies').read_text(encoding='utf-8') == 'session-marker'",
  "    assert vault.restore_profile(store, key, 'someone-else', prof, Path(d) / 'x') is False",
  "print('vault-ok')",
].join("\n");

/** Save a profile into a throwaway file vault and restore it: the code a real sync or compare job uses. */
export function vaultRoundTrip(): string {
  return python(["-c", VAULT_ROUND_TRIP]).trim();
}

const LOCK_AUTH_USERS = [
  "import os, psycopg",
  "with psycopg.connect(os.environ['DATABASE_URL']) as c:",
  "    role = c.execute('select current_user').fetchone()[0]",
  "    c.execute('select id from auth.users where id = %s::uuid for key share', (os.environ['E2E_UID'],)).fetchall()",
  "    c.execute('select id from auth.users where id = %s::uuid for update', (os.environ['E2E_UID'],)).fetchall()",
  "    c.rollback()",
  "print(role)",
].join("\n");

/** Lock one auth.users row FOR KEY SHARE and FOR UPDATE as the role the API uses locally; returns that role. */
export function lockAuthUser(id: string): string {
  return python(["-c", LOCK_AUTH_USERS], { E2E_UID: id }).trim();
}
