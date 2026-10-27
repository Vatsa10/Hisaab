import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ---- mocks -------------------------------------------------------------------------------

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

type JobCb = (job: import("@/lib/jobs").Job) => void;
type ConnCb = (rows: import("@/lib/jobs").Connection[]) => void;
const live = vi.hoisted(() => ({
  jobs: new Map<string, (job: unknown) => void>(),
  jobUnsubs: [] as string[],
  connections: [] as Array<(rows: unknown) => void>,
  connectionUnsubs: 0,
}));
vi.mock("@/lib/jobs", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/jobs")>();
  return {
    ...real,
    watchJob: vi.fn((id: string, cb: JobCb) => {
      live.jobs.set(id, cb as (job: unknown) => void);
      return () => {
        live.jobUnsubs.push(id);
        live.jobs.delete(id);
      };
    }),
    watchConnections: vi.fn((cb: ConnCb) => {
      live.connections.push(cb as (rows: unknown) => void);
      return () => {
        live.connectionUnsubs += 1;
      };
    }),
    firstOrderMonth: vi.fn(async () => "Oct 2025"),
  };
});

import { ApiError } from "@/lib/api";
import { watchConnections, watchJob, type Connection, type Job } from "@/lib/jobs";
import { AccountsScreen } from "@/components/connect/AccountsScreen";
import { AppRow } from "@/components/connect/AppRow";
import { ConnectFlow } from "@/components/connect/ConnectFlow";
import { CONSENT_POINTS, CONSENT_VERSION, failureMessage } from "@/components/connect/copy";
import { APPS } from "@/lib/apps";

const zepto = APPS[0]!;
const swiggy = APPS[2]!;

function job(over: Partial<Job> & { progress?: Job["progress"] }): Job {
  return {
    id: "j1",
    site: "swiggy",
    type: "connect",
    status: "running",
    progress: {},
    error: null,
    ...over,
  };
}

/** Push a job update to whoever is watching it. */
function push(update: Job) {
  const cb = live.jobs.get(update.id);
  if (!cb) throw new Error(`nobody watches ${update.id}`);
  act(() => cb(update));
}

/** Render the connect flow for Swiggy (consent already given) and get to a running job. */
async function startFlow(opts: { phone?: string } = {}) {
  api.apiFetch.mockResolvedValueOnce({ job_id: "j1" });
  const onClose = vi.fn();
  const utils = render(
    <ConnectFlow app={swiggy} consented onClose={onClose} onConnectNext={vi.fn()} nextApp={APPS[3]} />,
  );
  const input = screen.getByLabelText("Phone number");
  fireEvent.change(input, { target: { value: opts.phone ?? "9876543210" } });
  fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
  await waitFor(() => expect(live.jobs.has("j1")).toBe(true));
  return { ...utils, onClose };
}

beforeEach(() => {
  api.apiFetch.mockReset();
  live.jobs.clear();
  live.jobUnsubs.length = 0;
  live.connections.length = 0;
  live.connectionUnsubs = 0;
  vi.mocked(watchJob).mockClear();
  vi.mocked(watchConnections).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

// ---- the connect flow ---------------------------------------------------------------------

describe("ConnectFlow", () => {
  it("sends the phone with the +91 prefix and follows the job", async () => {
    await startFlow();
    expect(api.apiFetch).toHaveBeenCalledWith("/connections/swiggy/connect", {
      method: "POST",
      body: JSON.stringify({ phone: "+919876543210" }),
    });
    expect(watchJob).toHaveBeenCalledWith("j1", expect.any(Function));
    expect(screen.getByText("Opening Swiggy…")).toBeInTheDocument();
  });

  it("explains the phone step: +91 prefix and who sends the code", () => {
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    expect(screen.getByText("+91")).toBeInTheDocument();
    expect(
      screen.getByText("Swiggy sends the code. We keep only an encrypted session, never your OTP."),
    ).toBeInTheDocument();
  });

  it("rejects a number that is not 10 digits without calling the API", async () => {
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "98765" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("10-digit");
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("OTP input attributes: one large numeric one-time-code field", async () => {
    await startFlow();
    push(job({ progress: { stage: "otp_needed" } }));
    const otp = screen.getByLabelText(/code/i, { selector: "input" });
    expect(otp).toHaveAttribute("inputmode", "numeric");
    expect(otp).toHaveAttribute("autocomplete", "one-time-code");
    expect(otp).toHaveAttribute("pattern", "\\d{6}");
    expect(otp).toHaveAttribute("maxlength", "6");
    expect(document.querySelectorAll('input[autocomplete="one-time-code"]')).toHaveLength(1);
  });

  it("auto-submits a pasted 6-digit code and clears it", async () => {
    await startFlow();
    push(job({ progress: { stage: "otp_needed" } }));
    api.apiFetch.mockResolvedValueOnce(undefined);
    const otp = screen.getByLabelText(/code/i, { selector: "input" });
    fireEvent.change(otp, { target: { value: "123 456" } });
    await waitFor(() =>
      expect(api.apiFetch).toHaveBeenCalledWith("/jobs/j1/otp", {
        method: "POST",
        body: JSON.stringify({ otp: "123456" }),
      }),
    );
    await waitFor(() => expect(otp).toHaveValue(""));
  });

  it("does not submit fewer than 6 digits", async () => {
    await startFlow();
    push(job({ progress: { stage: "otp_needed" } }));
    fireEvent.change(screen.getByLabelText(/code/i, { selector: "input" }), { target: { value: "12a34" } });
    expect(screen.getByLabelText(/code/i, { selector: "input" })).toHaveValue("1234");
    expect(api.apiFetch).toHaveBeenCalledTimes(1); // only the connect call
  });

  it("renders question prompt", async () => {
    await startFlow({ phone: "9876540000" });
    push(job({ progress: { stage: "question_needed", question: "Which city do you order from?" } }));
    expect(screen.getByText("Which city do you order from?")).toBeInTheDocument();
    const box = screen.getByRole("textbox", { name: "Which city do you order from?" });
    expect(box.tagName).toBe("TEXTAREA");
    expect(box).toHaveAttribute("maxlength", "500");
    expect(screen.getByText("0/500")).toBeInTheDocument();
    expect(document.querySelector('input[autocomplete="one-time-code"]')).toBeNull();

    api.apiFetch.mockResolvedValueOnce(undefined);
    await userEvent.type(box, "Vadodara");
    expect(screen.getByText("8/500")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(api.apiFetch).toHaveBeenLastCalledWith("/jobs/j1/answer", {
      method: "POST",
      body: JSON.stringify({ answer: "Vadodara" }),
    });
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("test OTP timeout shows retry", async () => {
    await startFlow();
    push(job({ progress: { stage: "otp_needed" } }));
    push(job({ status: "failed", error: "otp_timeout", progress: { stage: "otp_needed" } }));
    expect(screen.getByText("Code expired — try again")).toBeInTheDocument();
    expect(screen.queryByLabelText(/code/i, { selector: "input" })).toBeNull();

    api.apiFetch.mockResolvedValueOnce({ job_id: "j2" });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(live.jobs.has("j2")).toBe(true));
    // Same site and phone, from memory only.
    expect(api.apiFetch).toHaveBeenLastCalledWith("/connections/swiggy/connect", {
      method: "POST",
      body: JSON.stringify({ phone: "+919876543210" }),
    });
    expect(live.jobUnsubs).toContain("j1");
  });

  it.each([
    ["question_timeout", "Code expired — try again"],
    ["login_failed", "That code didn't work"],
    ["proxy_unavailable", "Something went wrong on our side — try again"],
    ["worker_lost", "Something went wrong on our side — try again"],
    ["worker_shutdown", "Something went wrong on our side — try again"],
  ])("maps the %s failure to copy with a retry", async (code, copy) => {
    await startFlow();
    push(job({ status: "failed", error: code }));
    expect(screen.getByText(copy)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("resend appears when timer ends", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    api.apiFetch.mockResolvedValueOnce({ job_id: "j1" });
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "98765 43210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    push(job({ progress: { stage: "otp_needed" } }));
    expect(screen.getByText("Resend in 3:00")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send a new code" })).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(179_000);
    });
    expect(screen.getByText("Resend in 0:01")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    const resend = screen.getByRole("button", { name: "Send a new code" });

    api.apiFetch.mockResolvedValueOnce({ job_id: "j2" });
    fireEvent.click(resend);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(api.apiFetch).toHaveBeenLastCalledWith("/connections/swiggy/connect", {
      method: "POST",
      body: JSON.stringify({ phone: "+919876543210" }),
    });
    expect(live.jobs.has("j2")).toBe(true);
  });

  it("a resend that races the old job's timeout waits for it (409) and tries again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    api.apiFetch.mockResolvedValueOnce({ job_id: "j1" });
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    push(job({ progress: { stage: "otp_needed" } }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    api.apiFetch
      .mockRejectedValueOnce(new ApiError(409, "a connect job is already active"))
      .mockResolvedValueOnce({ job_id: "j2" });
    fireEvent.click(screen.getByRole("button", { name: "Send a new code" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(live.jobs.has("j2")).toBe(true);
  });

  it("while a resend waits out a 409, the old job's otp_timeout does not flash 'Code expired'", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    api.apiFetch.mockResolvedValueOnce({ job_id: "j1" });
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    push(job({ progress: { stage: "otp_needed" } }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    api.apiFetch
      .mockRejectedValueOnce(new ApiError(409, "a connect job is already active"))
      .mockResolvedValueOnce({ job_id: "j2" });
    fireEvent.click(screen.getByRole("button", { name: "Send a new code" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    push(job({ status: "failed", error: "otp_timeout", progress: { stage: "otp_needed" } }));
    expect(screen.queryByText("Code expired — try again")).toBeNull();
    expect(screen.getByText("Asking Swiggy for a new code…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });
    expect(live.jobs.has("j2")).toBe(true);
    expect(screen.queryByText("Code expired — try again")).toBeNull();
  });

  it("a first Send OTP that gets 409 says the app is busy at once (no waiting loop)", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(409, "a connect job is already active"));
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Swiggy is still busy");
    expect(api.apiFetch).toHaveBeenCalledTimes(1);
  });

  it("412 on connect goes back to the consent step", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(412, "consent required"));
    render(<ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    expect(await screen.findByRole("button", { name: "I understand" })).toBeInTheDocument();
    expect(screen.getByText("You'll only see this once for each app.")).toBeInTheDocument();
  });

  it("a resumed job counts the resend timer from when the job started, not a fresh 180 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.setSystemTime(new Date("2026-10-27T10:01:00Z"));
    render(<ConnectFlow app={swiggy} consented attachJobId="j9" onClose={vi.fn()} onConnectNext={vi.fn()} />);
    push(
      job({ id: "j9", started_at: "2026-10-27T10:00:00Z", progress: { stage: "otp_needed" } }),
    );
    expect(screen.getByText("Resend in 2:00")).toBeInTheDocument();
  });

  it("syncing shows an indeterminate bar and Finding your orders…", async () => {
    await startFlow();
    push(job({ progress: { stage: "syncing" } }));
    expect(screen.getByText("Finding your orders…")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(screen.queryByText("Found so far")).toBeNull();
  });

  it("syncing shows the live count found so far in a polite live region", async () => {
    await startFlow();
    push(job({ progress: { stage: "syncing", orders_found: 12, amount_found: 3400 } }));
    const label = screen.getByText("Found so far");
    expect(label.closest("[aria-live='polite']")).not.toBeNull();
    expect(screen.getByText("12 orders")).toBeInTheDocument();
  });

  it("first sync shows count and total", async () => {
    const onConnectNext = vi.fn();
    api.apiFetch.mockResolvedValueOnce({ job_id: "j1" });
    render(
      <ConnectFlow app={swiggy} consented onClose={vi.fn()} onConnectNext={onConnectNext} nextApp={APPS[3]} />,
    );
    fireEvent.change(screen.getByLabelText("Phone number"), { target: { value: "9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send OTP" }));
    await waitFor(() => expect(live.jobs.has("j1")).toBe(true));
    push(job({ status: "done", progress: { stage: "synced", orders_found: 43, amount_found: 13240 } }));
    expect(screen.getByText("Found")).toBeInTheDocument();
    expect(screen.getByText("43 orders")).toHaveClass("text-title");
    expect(await screen.findByText("₹13,240 · Oct 2025 – today")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect Zomato" }));
    expect(onConnectNext).toHaveBeenCalledWith(APPS[3]);
  });

  it("first sync renders without amount_found", async () => {
    await startFlow();
    push(job({ status: "done", progress: { stage: "synced", orders_found: 1 } }));
    expect(screen.getByText("1 order")).toBeInTheDocument();
    expect(await screen.findByText("Oct 2025 – today")).toBeInTheDocument();
  });

  it("moves focus to the sheet title when a step change drops it", async () => {
    await startFlow();
    (document.activeElement as HTMLElement | null)?.blur();
    push(job({ status: "failed", error: "worker_lost" }));
    await waitFor(() => expect(document.activeElement?.tagName).toBe("H2"));
    expect(document.activeElement).toHaveTextContent("Connect Swiggy");
  });

  it("keeps the phone and OTP out of browser storage and stops watching on unmount", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { unmount } = await startFlow();
    push(job({ progress: { stage: "otp_needed" } }));
    api.apiFetch.mockResolvedValueOnce(undefined);
    fireEvent.change(screen.getByLabelText(/code/i, { selector: "input" }), { target: { value: "123456" } });
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(2));
    expect(setItem).not.toHaveBeenCalled();
    unmount();
    expect(live.jobUnsubs).toContain("j1");
    setItem.mockRestore();
  });
});

// ---- accounts -----------------------------------------------------------------------------

describe("AccountsScreen", () => {
  const profile = { address: "Surmandir Society, Gorwa, Vadodara", lat: 22.3291, lon: 73.1645 };

  it("consent required before phone step", async () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Connect Zepto" }));
    const dialog = await screen.findByRole("dialog");
    for (const point of CONSENT_POINTS("Zepto")) expect(within(dialog).getByText(point)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Phone number")).toBeNull();

    api.apiFetch.mockResolvedValueOnce(undefined);
    await userEvent.click(within(dialog).getByRole("button", { name: "I understand" }));
    expect(api.apiFetch).toHaveBeenCalledWith("/consents", {
      method: "POST",
      body: JSON.stringify({ site: "zepto", text_version: CONSENT_VERSION }),
    });
    expect(CONSENT_VERSION).toBe("2026-10-27");
    expect(await screen.findByLabelText("Phone number")).toBeInTheDocument();
  });

  it.each([
    [new ApiError(401, "invalid token"), "Please sign in again."],
    [new ApiError(0, "Can't reach the server. Check your connection and try again."), "Can't reach the server"],
    [new ApiError(500, "boom"), "Couldn't save that. Try again."],
  ])("consent errors: %o", async (err, copy) => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Connect Zepto" }));
    api.apiFetch.mockRejectedValueOnce(err);
    await userEvent.click(await screen.findByRole("button", { name: "I understand" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(copy);
    expect(screen.queryByLabelText("Phone number")).toBeNull();
  });

  it("skips the consent sheet once consent is stored", async () => {
    render(
      <AccountsScreen
        initialConnections={[]}
        initialConsents={[{ site: "zepto", text_version: CONSENT_VERSION }]}
        initialProfile={null}
        initialJobs={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Connect Zepto" }));
    expect(await screen.findByLabelText("Phone number")).toBeInTheDocument();
  });

  it("an old consent version is not enough", async () => {
    render(
      <AccountsScreen
        initialConnections={[]}
        initialConsents={[{ site: "zepto", text_version: "2025-01-01" }]}
        initialProfile={null}
        initialJobs={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Connect Zepto" }));
    expect(await screen.findByRole("button", { name: "I understand" })).toBeInTheDocument();
  });

  it("shows the promise line, the Instamart note and live connection updates", () => {
    const { unmount } = render(
      <AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />,
    );
    expect(screen.getByText("Instamart orders come in with your Swiggy account.")).toBeInTheDocument();
    expect(screen.getByText("Read-only. We never order or pay.")).toBeInTheDocument();
    expect(watchConnections).toHaveBeenCalledOnce();
    const rows: Connection[] = [{ site: "blinkit", status: "connected", last_synced_at: new Date().toISOString() }];
    act(() => live.connections[0]!(rows));
    expect(screen.getByText("✓ Synced just now")).toBeInTheDocument();
    unmount();
    expect(live.connectionUnsubs).toBe(1);
  });

  it("refresh now posts the refresh and shows Syncing…", async () => {
    api.apiFetch.mockResolvedValueOnce({ job_id: "r1" });
    render(
      <AccountsScreen
        initialConnections={[{ site: "zepto", status: "connected", last_synced_at: new Date(Date.now() - 2 * 3600e3).toISOString() }]}
        initialConsents={[]}
        initialProfile={null}
        initialJobs={[]}
      />,
    );
    expect(screen.getByText("✓ Synced 2h ago")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh Zepto now" }));
    expect(api.apiFetch).toHaveBeenCalledWith("/connections/zepto/refresh", { method: "POST" });
    expect(await screen.findByText("Syncing…")).toBeInTheDocument();
    push(job({ id: "r1", site: "zepto", type: "sync", status: "done", progress: { stage: "synced" } }));
    await waitFor(() => expect(screen.queryByText("Syncing…")).toBeNull());
  });

  it("edits the delivery address with PUT /profile and shows the resolved place", async () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={profile} initialJobs={[]} />);
    expect(screen.getByText(profile.address)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    const field = await screen.findByLabelText("Address");
    await userEvent.clear(field);
    await userEvent.type(field, "Alkapuri, Vadodara");
    api.apiFetch.mockResolvedValueOnce({ address: "Alkapuri, Vadodara", lat: 22.3119, lon: 73.1723 });
    await userEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(api.apiFetch).toHaveBeenCalledWith("/profile", {
      method: "PUT",
      body: JSON.stringify({ address: "Alkapuri, Vadodara" }),
    });
    expect(await screen.findByText("Alkapuri, Vadodara")).toBeInTheDocument();
    expect(screen.getByText("✓ Found on the map")).toBeInTheDocument();
    expect(screen.queryByText(/22\.3119/)).toBeNull(); // no raw coordinates
  });

  it("names the resolved place when the API returns one", async () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    await userEvent.type(await screen.findByLabelText("Address"), "Surmandir Society, Gorwa, Vadodara");
    api.apiFetch.mockResolvedValueOnce({
      address: "Surmandir Society, Gorwa, Vadodara",
      lat: 22.33,
      lon: 73.16,
      display: "Gorwa, Vadodara, Vadodara Taluka, Gujarat, 390016, India",
    });
    await userEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(await screen.findByText("Pinned near Gorwa, Vadodara")).toBeInTheDocument();
  });

  it("explains a geocoder outage (503)", async () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    await userEvent.type(await screen.findByLabelText("Address"), "Alkapuri, Vadodara");
    api.apiFetch.mockRejectedValueOnce(new ApiError(503, "geocoding is unavailable, try again"));
    await userEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Address lookup is down right now");
  });

  it.each([
    [409, "Zepto is already syncing."],
    [404, "Connect Zepto first."],
  ])("refresh %i shows its own copy", async (status, copy) => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(status, "x"));
    render(
      <AccountsScreen
        initialConnections={[{ site: "zepto", status: "connected", last_synced_at: new Date().toISOString() }]}
        initialConsents={[]}
        initialProfile={null}
        initialJobs={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Refresh Zepto now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(copy);
  });

  it("follows each job on its own channel and does not rebuild the others", async () => {
    api.apiFetch.mockResolvedValueOnce({ job_id: "r2" });
    render(
      <AccountsScreen
        initialConnections={[
          { site: "zepto", status: "connected", last_synced_at: new Date().toISOString() },
          { site: "blinkit", status: "connected", last_synced_at: new Date().toISOString() },
        ]}
        initialConsents={[]}
        initialProfile={null}
        initialJobs={[job({ id: "r1", site: "zepto", type: "sync", status: "running" })]}
      />,
    );
    expect(vi.mocked(watchJob).mock.calls.map((c) => c[0])).toEqual(["r1"]);
    await userEvent.click(screen.getByRole("button", { name: "Refresh Blinkit now" }));
    await waitFor(() => expect(vi.mocked(watchJob).mock.calls.map((c) => c[0])).toEqual(["r1", "r2"]));
    expect(live.jobUnsubs).toEqual([]); // r1 kept its channel
    push(job({ id: "r1", site: "zepto", type: "sync", status: "done" }));
    await waitFor(() => expect(live.jobUnsubs).toEqual(["r1"]));
    expect(vi.mocked(watchJob)).toHaveBeenCalledTimes(2);
  });

  it("explains an address the geocoder cannot find", async () => {
    render(<AccountsScreen initialConnections={[]} initialConsents={[]} initialProfile={null} initialJobs={[]} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    await userEvent.type(await screen.findByLabelText("Address"), "zzzzzz");
    api.apiFetch.mockRejectedValueOnce(new ApiError(422, "address not found"));
    await userEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn't find");
  });
});

describe("AppRow", () => {
  const noop = () => {};
  it.each<[Connection | undefined, boolean, string]>([
    [undefined, false, "Connect ›"],
    [{ site: "zepto", status: "connected", last_synced_at: new Date(Date.now() - 2 * 3600e3).toISOString() }, false, "✓ Synced 2h ago"],
    [{ site: "zepto", status: "syncing", last_synced_at: null }, false, "Syncing…"],
    [{ site: "zepto", status: "connected", last_synced_at: null }, true, "Syncing…"],
    [{ site: "zepto", status: "needs_login", last_synced_at: null }, false, "Needs login"],
    [{ site: "zepto", status: "error", last_synced_at: null }, false, "Connect ›"],
  ])("shows %o (busy %s) as %s", (connection, busy, label) => {
    render(
      <ul>
        <AppRow app={zepto} connection={connection} busy={busy} onConnect={noop} onRefresh={noop} />
      </ul>,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("needs login leads to reconnect", async () => {
    const onConnect = vi.fn();
    render(
      <ul>
        <AppRow
          app={zepto}
          connection={{ site: "zepto", status: "needs_login", last_synced_at: null }}
          onConnect={onConnect}
          onRefresh={noop}
        />
      </ul>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Reconnect Zepto/ }));
    expect(onConnect).toHaveBeenCalledWith(zepto);
  });

  it("Connect › is in the accent colour", () => {
    render(
      <ul>
        <AppRow app={zepto} onConnect={noop} onRefresh={noop} />
      </ul>,
    );
    expect(screen.getByText("Connect ›")).toHaveClass("text-accent");
  });
});

describe("failureMessage", () => {
  it("falls back to the our-side copy for unknown codes", () => {
    expect(failureMessage("no_recipe: orders")).toBe("Something went wrong on our side — try again");
    expect(failureMessage(null)).toBe("Something went wrong on our side — try again");
  });
});

// ---- lib/jobs (the real module, with a fake Supabase client) ------------------------------

type Handler = (payload: { new: Record<string, unknown> }) => void;

interface FakeChannel {
  name: string;
  on: ReturnType<typeof vi.fn>;
  subscribe: ReturnType<typeof vi.fn>;
  handler?: Handler;
  status?: (s: string) => void;
}

function fakeClient(rows: { jobs?: Record<string, unknown> | null; connections?: Record<string, unknown>[] }) {
  const channels: FakeChannel[] = [];
  const selects: string[] = [];
  const client = {
    channel: vi.fn((name: string) => {
      const ch: FakeChannel = { name, on: vi.fn(), subscribe: vi.fn() };
      ch.on.mockImplementation((_type: string, _filter: unknown, handler: Handler) => {
        ch.handler = handler;
        return ch;
      });
      ch.subscribe.mockImplementation((cb: (s: string) => void) => {
        ch.status = cb;
        return ch;
      });
      channels.push(ch);
      return ch;
    }),
    removeChannel: vi.fn(async () => "ok"),
    from: vi.fn((table: string) => ({
      select: (cols: string) => {
        selects.push(`${table}:${cols}`);
        if (table === "jobs") {
          return { eq: () => ({ maybeSingle: async () => ({ data: rows.jobs ?? null, error: null }) }) };
        }
        return Promise.resolve({ data: rows.connections ?? [], error: null });
      },
    })),
  };
  return { client, channels, selects };
}

describe("lib/jobs watchJob / watchConnections", () => {
  it("watchJob subscribes to the job's row, re-fetches on (re)connect, and unsubscribes", async () => {
    const real = await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
    const row = {
      id: "j1",
      site: "zepto",
      type: "connect",
      status: "running",
      progress: { stage: "otp_needed" },
      error: null,
      payload: { phone: "9876543210" },
    };
    const { client, channels, selects } = fakeClient({ jobs: row });
    const onChange = vi.fn();
    const stop = real.watchJob("j1", onChange, client as never);

    expect(channels).toHaveLength(1);
    expect(channels[0]!.on).toHaveBeenCalledWith(
      "postgres_changes",
      { event: "*", schema: "public", table: "jobs", filter: "id=eq.j1" },
      expect.any(Function),
    );
    // Subscribed (and every later reconnect): the current row is fetched so nothing is missed.
    channels[0]!.status!("SUBSCRIBED");
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(selects[0]).toMatch(/^jobs:/);
    expect(selects[0]).not.toMatch(/payload/);
    expect(onChange.mock.calls[0]![0]).not.toHaveProperty("payload");
    expect(onChange.mock.calls[0]![0]).toMatchObject({ id: "j1", progress: { stage: "otp_needed" } });

    channels[0]!.handler!({ new: { ...row, progress: { stage: "otp_received" } } });
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange.mock.calls[1]![0]).not.toHaveProperty("payload");

    channels[0]!.status!("SUBSCRIBED");
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(3));

    stop();
    expect(client.removeChannel).toHaveBeenCalledWith(channels[0]);
    channels[0]!.handler!({ new: { ...row, status: "done" } });
    expect(onChange).toHaveBeenCalledTimes(3);
  });

  it("watchJob drops a re-read that a realtime event overtook (no stage roll-back)", async () => {
    const real = await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
    const { client, channels } = fakeClient({});
    // Re-reads answer only when the test says so.
    const pending: Array<(row: Record<string, unknown>) => void> = [];
    client.from.mockImplementation(() => ({
      select: () => ({
        eq: () => ({
          maybeSingle: () =>
            new Promise((resolve) => pending.push((row) => resolve({ data: row, error: null }))),
        }),
      }),
    }));
    const onChange = vi.fn();
    real.watchJob("j1", onChange, client as never);
    const base = { id: "j1", site: "zepto", type: "connect", error: null };

    channels[0]!.status!("SUBSCRIBED"); // re-read #1 starts (snapshot: still queued)
    channels[0]!.handler!({ new: { ...base, status: "running", progress: { stage: "otp_needed" } } });
    pending[0]!({ ...base, status: "queued", progress: {} }); // #1 answers late, with the old state
    await new Promise((r) => setTimeout(r, 0));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0]![0].progress.stage).toBe("otp_needed");

    // Two re-reads in flight: only the newer may deliver.
    channels[0]!.status!("SUBSCRIBED"); // #2
    channels[0]!.status!("SUBSCRIBED"); // #3
    pending[2]!({ ...base, status: "running", progress: { stage: "otp_received" } });
    pending[1]!({ ...base, status: "running", progress: { stage: "otp_needed" } });
    await new Promise((r) => setTimeout(r, 0));
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange.mock.calls[1]![0].progress.stage).toBe("otp_received");
  });

  it("watchJob never goes back from a finished job to a running one", async () => {
    const real = await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
    const { client, channels } = fakeClient({ jobs: null });
    const onChange = vi.fn();
    real.watchJob("j1", onChange, client as never);
    const base = { id: "j1", site: "zepto", type: "connect", progress: {}, error: null };
    channels[0]!.handler!({ new: { ...base, status: "failed", error: "otp_timeout" } });
    channels[0]!.handler!({ new: { ...base, status: "running" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("watchConnections re-reads the user's connections on every change and on (re)connect", async () => {
    const real = await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
    const rows = [{ site: "zepto", status: "connected", last_synced_at: "2026-10-27T10:00:00Z" }];
    const { client, channels } = fakeClient({ connections: rows });
    const onChange = vi.fn();
    const stop = real.watchConnections(onChange, client as never);
    expect(channels[0]!.on).toHaveBeenCalledWith(
      "postgres_changes",
      { event: "*", schema: "public", table: "connections" },
      expect.any(Function),
    );
    channels[0]!.status!("SUBSCRIBED");
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(rows));
    channels[0]!.handler!({ new: { site: "zepto", status: "syncing" } });
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(2));
    stop();
    expect(client.removeChannel).toHaveBeenCalledWith(channels[0]);
  });
});
