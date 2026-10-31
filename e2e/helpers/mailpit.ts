// Mailpit (scripts/e2e_local.sh starts it): the API and worker send their email here, never anywhere real.

const BASE = process.env.E2E_MAILPIT_URL || "http://127.0.0.1:8025";

export interface MailSummary {
  ID: string;
  Subject: string;
  To: { Address: string }[];
}

interface SearchReply {
  messages?: MailSummary[];
}

/** Mail to `to` whose subject matches, polled until it arrives (the notification is sent by the worker or scheduler). */
export async function waitForMail(to: string, subject: RegExp, timeoutMs = 30000): Promise<MailSummary> {
  const deadline = Date.now() + timeoutMs;
  const url = `${BASE}/api/v1/search?query=${encodeURIComponent(`to:"${to}"`)}`;
  let seen: string[] = [];
  for (;;) {
    try {
      const res = await fetch(url);
      if (res.ok) {
        const body = (await res.json()) as SearchReply;
        const messages = body.messages ?? [];
        seen = messages.map((m) => m.Subject);
        const hit = messages.find((m) => subject.test(m.Subject));
        if (hit) return hit;
      }
    } catch {
      // Mailpit not answering yet: keep polling until the deadline.
    }
    if (Date.now() > deadline) {
      throw new Error(`no mail to ${to} matching ${subject} in ${timeoutMs} ms (subjects seen: ${JSON.stringify(seen)})`);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

/** Mail to `to` right now (no waiting), subjects only. */
export async function mailSubjects(to: string): Promise<string[]> {
  const res = await fetch(`${BASE}/api/v1/search?query=${encodeURIComponent(`to:"${to}"`)}`);
  const body = (await res.json()) as SearchReply;
  return (body.messages ?? []).map((m) => m.Subject);
}

export async function clearMail(): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/messages`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Mailpit refused to clear the mailbox (${res.status})`);
}
