"use client";

import { useState, type FormEvent } from "react";
import { relativeTime } from "@/components/connect/copy";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/TextField";
import { getInvites, isForbidden, problem, revokeInvite, sendInvite } from "@/lib/admin";
import {
  Badge,
  ConfirmSheet,
  ListSkeleton,
  LoadError,
  Notice,
  SectionLabel,
  useAdminLoad,
  type PanelProps,
} from "./parts";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Invites tab: invite by email, the invite list (pending ones can be revoked), the waitlist. */
export function InvitesPanel({ onForbidden }: PanelProps) {
  const data = useAdminLoad("invites", getInvites, onForbidden);
  const [email, setEmail] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // "form" or the row's email
  const [notice, setNotice] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  async function run(key: string, action: () => Promise<unknown>, fallback: string): Promise<boolean> {
    setBusy(key);
    setNotice(null);
    setSent(null);
    try {
      await action();
      data.reload();
      return true;
    } catch (e) {
      if (isForbidden(e)) onForbidden();
      else if (key === "form") setFieldError(problem(e, fallback));
      else setNotice(problem(e, fallback));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const value = email.trim().toLowerCase();
    if (!EMAIL.test(value)) {
      setFieldError("Enter an email address like name@example.com.");
      return;
    }
    setFieldError(null);
    if (await run("form", () => sendInvite(value), "Couldn't send the invite. Try again.")) {
      setEmail("");
      setSent(value);
    }
  }

  const invite = (address: string) =>
    run(address, () => sendInvite(address), `Couldn't invite ${address}.`).then((ok) => ok && setSent(address));
  const [revoking, setRevoking] = useState<string | null>(null);

  return (
    <div>
      <ConfirmSheet
        open={revoking !== null}
        title="Revoke this invite?"
        description={`${revoking ?? ""} won't be able to sign up with this address. You can invite them again later.`}
        confirmLabel="Revoke invite"
        onConfirm={async () => {
          if (!revoking) return;
          await revokeInvite(revoking);
          setSent(null);
          data.reload();
        }}
        onClose={() => setRevoking(null)}
        onForbidden={onForbidden}
      />
      <Card as="section" aria-label="Invite someone">
        <form noValidate onSubmit={onSubmit} className="flex flex-col gap-3">
          <TextField
            label="Email"
            type="email"
            inputMode="email"
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              if (fieldError) setFieldError(null);
            }}
            error={fieldError}
            hint="They get an email and can sign up with this address."
          />
          <Button type="submit" loading={busy === "form"} fullWidth>
            Invite
          </Button>
        </form>
        {sent ? (
          <p role="status" className="mt-3 text-label font-medium text-accent">
            Invite sent to {sent}.
          </p>
        ) : null}
      </Card>

      {notice ? <Notice className="mt-3">{notice}</Notice> : null}

      {data.status === "loading" ? (
        <div className="mt-6">
          <ListSkeleton label="Invites" />
        </div>
      ) : data.status === "error" ? (
        <div className="mt-6">
          <LoadError what="invites" onRetry={data.reload} />
        </div>
      ) : (
        <>
          <SectionLabel aside={data.data.invites.length || undefined}>Invites</SectionLabel>
          {data.data.invites.length === 0 ? (
            <Card>
              <p className="text-body text-ink-muted">No invites yet.</p>
            </Card>
          ) : (
            <Card padding="none">
              <ul aria-label="Invites">
                {data.data.invites.map((inv) => {
                  const joined = Boolean(inv.accepted_at);
                  return (
                    <li
                      key={inv.email}
                      className={`flex min-h-14 items-center gap-3 border-b border-line-soft py-2 pl-4 last:border-b-0 ${joined ? "pr-4" : "pr-1.5"}`}
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium" title={inv.email}>
                          {inv.email}
                        </p>
                        <p className="mt-0.5 flex items-center gap-2 text-label text-ink-muted" suppressHydrationWarning>
                          <Badge tone={joined ? "ok" : "neutral"}>{joined ? "Joined" : "Pending"}</Badge>
                          <span className="truncate">
                            {joined && inv.accepted_at
                              ? relativeTime(inv.accepted_at)
                              : `Invited ${relativeTime(inv.invited_at)}`}
                          </span>
                        </p>
                      </div>
                      {joined ? null : (
                        <Button
                          variant="ghost"
                          className="px-3 text-danger hover:bg-danger-soft active:bg-danger-soft"
                          disabled={busy !== null}
                          onClick={() => setRevoking(inv.email)}
                          aria-label={`Revoke invite for ${inv.email}`}
                        >
                          Revoke
                        </Button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Card>
          )}

          <SectionLabel aside={data.data.waitlist.length || undefined}>Waitlist</SectionLabel>
          {data.data.waitlist.length === 0 ? (
            <Card>
              <p className="text-body text-ink-muted">Nobody is waiting. People who try to sign up without an invite land here.</p>
            </Card>
          ) : (
            <Card padding="none">
              <ul aria-label="Waitlist">
                {data.data.waitlist.map((w) => (
                  <li
                    key={w.email}
                    className={`flex min-h-14 items-center gap-3 border-b border-line-soft py-2 pl-4 last:border-b-0 ${w.invited_at ? "pr-4" : "pr-1.5"}`}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium" title={w.email}>
                        {w.email}
                      </p>
                      <p className="mt-0.5 flex items-center gap-2 text-label text-ink-muted" suppressHydrationWarning>
                        {w.invited_at ? <Badge tone="neutral">Invited</Badge> : null}
                        <span className="truncate">
                          {w.invited_at ? relativeTime(w.invited_at) : `Joined the list ${relativeTime(w.joined_at)}`}
                        </span>
                      </p>
                    </div>
                    {w.invited_at ? null : (
                      <Button
                        variant="ghost"
                        className="px-3"
                        loading={busy === w.email}
                        disabled={busy !== null && busy !== w.email}
                        onClick={() => void invite(w.email)}
                        aria-label={`Invite ${w.email}`}
                      >
                        Invite
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
