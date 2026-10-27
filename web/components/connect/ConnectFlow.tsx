"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Sheet } from "@/components/ui/Sheet";
import { ApiError, apiFetch } from "@/lib/api";
import type { AppInfo } from "@/lib/apps";
import { isTerminal, watchJob, type Job } from "@/lib/jobs";
import { ConsentAction, ConsentBody } from "./ConsentSheet";
import { COPY, failureMessage, maskPhone } from "./copy";
import { AlertIcon } from "./icons";
import { OtpStep } from "./OtpStep";
import { PhoneStep } from "./PhoneStep";
import { QuestionStep } from "./QuestionStep";
import { IndeterminateBar, SyncProgress } from "./SyncProgress";
import { useKeyboardInset } from "./useKeyboardInset";

type Step = "consent" | "phone" | "job";

type View =
  | { kind: "failed"; message: string }
  | { kind: "otp" }
  | { kind: "question"; question: string }
  | { kind: "working"; text: string }
  | { kind: "syncing" }
  | { kind: "synced" };

/** A 409 on resend usually means the old job is still timing out; wait for it this long. */
const BUSY_RETRIES = 8;
const BUSY_WAIT_MS = 2500;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Which screen a job's row calls for. */
export function viewForJob(job: Job | null, app: AppInfo): View {
  if (!job) return { kind: "working", text: `Opening ${app.name}…` };
  if (job.status === "failed") return { kind: "failed", message: failureMessage(job.error) };
  const stage = job.progress.stage;
  if (job.status === "done" || stage === "synced") return { kind: "synced" };
  if (stage === "otp_needed") return { kind: "otp" };
  if (stage === "question_needed") return { kind: "question", question: job.progress.question ?? "" };
  if (stage === "syncing" || job.type === "sync") return { kind: "syncing" };
  if (stage === "otp_received" || stage === "answer_received") {
    return { kind: "working", text: `Signing in to ${app.name}…` };
  }
  return { kind: "working", text: `Opening ${app.name}…` };
}

function connectProblem(e: unknown, app: AppInfo): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return `Enter the 10-digit mobile number on your ${app.name} account.`;
    if (e.status === 409) return `${app.name} is still busy with your last request. Try again in a minute.`;
    if (e.status === 401) return "Your session has ended. Sign in again.";
    if (e.status === 429) return `You've reached today's limit for ${app.name}. Try again tomorrow.`;
    if (e.status === 0) return e.detail;
  }
  return COPY.ourSide;
}

function relayProblem(e: unknown): string {
  if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
    return "This sign-in has ended. Send a new code.";
  }
  if (e instanceof ApiError && e.status === 0) return e.detail;
  return "Couldn't send that. Check your connection and try again.";
}

function failureHint(message: string, app: AppInfo): string {
  switch (message) {
    case COPY.expired:
      return `Codes last 3 minutes. Try again and ${app.name} sends a new one.`;
    case COPY.badCode:
      return `Check the code from ${app.name}. Trying again sends a new one.`;
    case COPY.ourSide:
      return "Nothing changed on your account. It usually works on a second try.";
    default:
      return "";
  }
}

export interface ConnectFlowProps {
  app: AppInfo;
  /** Consent for this app (current version) is stored; skip straight to the phone step. */
  consented: boolean;
  onConsented?: (app: AppInfo) => void;
  /** Resume a connect job that is already running (e.g. after the sheet was closed). */
  attachJobId?: string;
  onJobStarted?: (app: AppInfo, jobId: string) => void;
  /** Called once the sheet has closed. */
  onClose: () => void;
  onConnectNext: (app: AppInfo) => void;
  /** The app to offer after this one syncs. */
  nextApp?: AppInfo;
}

/**
 * Connecting one app, in one bottom sheet: consent (once per app) → phone → the job's live
 * progress (OTP or question → signing in → first sync → what was found). Failures say what went
 * wrong and always offer a retry. The phone number is held only in this component's memory (for
 * "Send a new code" / "Try again") and dropped when it unmounts; the OTP and answer are cleared
 * from state as soon as they are sent. Nothing is written to browser storage or logged.
 */
export function ConnectFlow({
  app,
  consented,
  onConsented,
  attachJobId,
  onJobStarted,
  onClose,
  onConnectNext,
  nextApp,
}: ConnectFlowProps) {
  useKeyboardInset();
  const firstStep: Step = attachJobId ? "job" : consented ? "phone" : "consent";

  const [appId, setAppId] = useState(app.id);
  const [step, setStep] = useState<Step>(firstStep);
  const [jobId, setJobId] = useState<string | null>(attachJobId ?? null);
  const [job, setJob] = useState<Job | null>(null);
  const [otpStartedAt, setOtpStartedAt] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const phoneRef = useRef<string | null>(null);
  const otpSeenFor = useRef<string | null>(null);
  const mounted = useRef(true);

  // "Connect <next app>" reuses this sheet: start over for the new app.
  if (appId !== app.id) {
    setAppId(app.id);
    setStep(firstStep);
    setJobId(attachJobId ?? null);
    setJob(null);
    setOtpStartedAt(null);
    setStarting(false);
    setStartError(null);
  }

  useEffect(() => {
    phoneRef.current = null; // never carry one app's number into another app's flow
    otpSeenFor.current = null;
  }, [app.id]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      phoneRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!jobId) return;
    return watchJob(jobId, (next) => {
      setJob(next);
      if (next.progress.stage === "otp_needed" && !isTerminal(next) && otpSeenFor.current !== next.id) {
        otpSeenFor.current = next.id;
        setOtpStartedAt(Date.now());
      }
    });
  }, [jobId]);

  /** POST connect for the remembered number; resolves to a problem to show, or null. */
  const startConnect = useCallback(
    async (digits: string): Promise<string | null> => {
      phoneRef.current = digits;
      setStarting(true);
      setStartError(null);
      for (let attempt = 0; ; attempt++) {
        try {
          const res = await apiFetch<{ job_id: string }>(`/connections/${app.id}/connect`, {
            method: "POST",
            body: JSON.stringify({ phone: `+91${digits}` }),
          });
          if (!mounted.current) return null;
          otpSeenFor.current = null;
          setOtpStartedAt(null);
          setJob(null);
          setJobId(res.job_id);
          setStep("job");
          setStarting(false);
          onJobStarted?.(app, res.job_id);
          return null;
        } catch (e) {
          if (e instanceof ApiError && e.status === 409 && attempt < BUSY_RETRIES) {
            await sleep(BUSY_WAIT_MS);
            if (!mounted.current) return null;
            continue;
          }
          if (!mounted.current) return null;
          setStarting(false);
          if (e instanceof ApiError && e.status === 412) {
            setStep("consent"); // the stored consent is gone or outdated: ask again
            return null;
          }
          return connectProblem(e, app);
        }
      }
    },
    [app, onJobStarted],
  );

  function retry() {
    const phone = phoneRef.current;
    if (!phone) {
      setJobId(null);
      setJob(null);
      setStep("phone");
      return;
    }
    void startConnect(phone).then((problem) => {
      if (problem && mounted.current) setStartError(problem);
    });
  }

  function changeNumber() {
    phoneRef.current = null;
    setJobId(null);
    setJob(null);
    setStartError(null);
    setStep("phone");
  }

  async function relay(path: "otp" | "answer", value: string): Promise<string | null> {
    if (!jobId) return COPY.ourSide;
    try {
      await apiFetch(`/jobs/${jobId}/${path}`, {
        method: "POST",
        body: JSON.stringify(path === "otp" ? { otp: value } : { answer: value }),
      });
      return null;
    } catch (e) {
      return relayProblem(e);
    }
  }

  // ---- what to show ------------------------------------------------------------------------

  let title: string;
  let description: ReactNode = undefined;
  let body: ReactNode;
  let footer: ReactNode = undefined;

  if (step === "consent") {
    title = `Before you connect ${app.name}`;
    body = <ConsentBody app={app} />;
    footer = (
      <ConsentAction
        app={app}
        onAgreed={() => {
          onConsented?.(app);
          setStep("phone");
        }}
      />
    );
  } else if (step === "phone") {
    title = `Connect ${app.name}`;
    description = `Use the number on your ${app.name} account.`;
    body = <PhoneStep app={app} onSubmit={startConnect} />;
  } else {
    const view: View = startError ? { kind: "failed", message: startError } : viewForJob(job, app);
    switch (view.kind) {
      case "otp":
        title = `Enter ${app.name}'s OTP`;
        description = `Sent to ${phoneRef.current ? maskPhone(phoneRef.current) : "your phone"}. Auto-fills from Messages on iPhone.`;
        body = (
          <OtpStep
            key={jobId ?? ""}
            startedAt={otpStartedAt ?? Date.now()}
            onSubmit={(otp) => relay("otp", otp)}
            onResend={retry}
            resending={starting}
          />
        );
        break;
      case "question":
        title = `${app.name} has a question`;
        description = "Answer it to finish signing in.";
        body = <QuestionStep key={jobId ?? ""} question={view.question} onSubmit={(a) => relay("answer", a)} />;
        break;
      case "working":
        title = `Connecting ${app.name}`;
        body = (
          <div className="flex flex-col gap-2.5 py-2" role="status">
            <IndeterminateBar label={view.text} />
            <p className="text-body font-medium">{view.text}</p>
            <p className="text-label text-ink-muted">This takes a few seconds.</p>
          </div>
        );
        break;
      case "syncing":
      case "synced":
        title = `${app.name} connected`;
        body = (
          <SyncProgress
            app={app}
            state={view.kind}
            ordersFound={job?.progress.orders_found}
            amountFound={job?.progress.amount_found}
            nextApp={nextApp}
            onNext={onConnectNext}
            onDone={onClose}
          />
        );
        break;
      case "failed": {
        title = `Connect ${app.name}`;
        const hint = failureHint(view.message, app);
        body = (
          <div className="flex flex-col gap-5">
            <div className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className="grid size-10 shrink-0 place-items-center rounded-full bg-danger-soft text-danger"
              >
                <AlertIcon size={22} />
              </span>
              <div className="min-w-0 pt-2">
                <p role="alert" className="text-headline">
                  {view.message}
                </p>
                {hint ? <p className="mt-1 text-body text-ink-muted">{hint}</p> : null}
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <Button size="lg" fullWidth loading={starting} onClick={retry}>
                Try again
              </Button>
              <Button variant="ghost" fullWidth disabled={starting} onClick={changeNumber}>
                Use a different number
              </Button>
            </div>
          </div>
        );
        break;
      }
    }
  }

  return (
    <Sheet
      open
      onClose={onClose}
      title={title}
      description={description}
      footer={footer}
      className="mb-[var(--kb-inset,0px)]"
    >
      {body}
    </Sheet>
  );
}
