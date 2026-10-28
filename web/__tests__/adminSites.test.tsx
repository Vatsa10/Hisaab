import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));
vi.mock("@/lib/jobs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/jobs")>()),
  watchJob: vi.fn(() => () => {}),
}));

import { SitesPanel } from "@/components/admin/SitesPanel";
import type { SiteHealth } from "@/lib/admin";

function site(over: Partial<SiteHealth> = {}): SiteHealth {
  return {
    site: "zepto",
    kind: "orders",
    version: 4,
    learned_at: null,
    healthy: true,
    paused: false,
    health_note: null,
    syncs_ok_7d: 52,
    syncs_failed_7d: 1,
    last_ok_at: null,
    admin_connected: true,
    cooldown_until: null,
    backoff_level: 0,
    failures_window: 0,
    jobs_window: 0,
    ...over,
  };
}

function show(s: SiteHealth) {
  api.apiFetch.mockResolvedValue([s]);
  render(<SitesPanel onForbidden={() => {}} />);
}

describe("SitesPanel site health", () => {
  beforeEach(() => api.apiFetch.mockReset());

  it("cooling badge shows until time and level", async () => {
    const until = new Date(Date.now() + 40 * 60_000);
    show(site({ cooldown_until: until.toISOString(), backoff_level: 2 }));
    const hh = String(until.getHours()).padStart(2, "0");
    const mm = String(until.getMinutes()).padStart(2, "0");
    expect(await screen.findByText(`Cooling down until ${hh}:${mm} · level 2`)).toBeInTheDocument();
  });

  it("no cooling badge once the cool-down has passed", async () => {
    show(site({ cooldown_until: new Date(Date.now() - 60_000).toISOString(), backoff_level: 1 }));
    await screen.findByText("Healthy");
    expect(screen.queryByText(/Cooling down/)).toBeNull();
  });

  it("failure line shows pct", async () => {
    show(site({ failures_window: 3, jobs_window: 12 }));
    expect(await screen.findByText("Failures (24 h): 3 of 12 (25%)")).toBeInTheDocument();
  });

  it("auto-pause note visible", async () => {
    show(site({ paused: true, healthy: false }));
    expect(await screen.findByText(/^Paused automatically/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume Zepto" })).toBeInTheDocument();
  });
});
