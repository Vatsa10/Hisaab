import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));

import { SetupPanel } from "@/components/admin/SetupPanel";
import type { Journey } from "@/lib/admin";

function journey(over: Partial<Journey> = {}): Journey {
  return {
    site: "swiggy",
    name: "Swiggy",
    steps: [],
    done: 1,
    total: 5,
    next: { id: "orders", label: "Learn the order history", status: "todo", detail: null, op: "learn", kind: "orders" },
    run: null,
    ...over,
  };
}

describe("SetupPanel", () => {
  beforeEach(() => api.apiFetch.mockReset());

  it("shows progress, what is next, and one Set up button", async () => {
    api.apiFetch.mockResolvedValueOnce([journey()]);
    render(<SetupPanel onForbidden={() => {}} />);
    expect(await screen.findByText(/1 of 5 done/)).toHaveTextContent("Next: Learn the order history");
    expect(screen.getByRole("progressbar", { name: "Swiggy setup" })).toHaveAttribute("aria-valuenow", "1");
    expect(screen.getByRole("button", { name: "Set up Swiggy" })).toBeInTheDocument();
  });

  it("pressing Set up starts the run and reloads", async () => {
    api.apiFetch
      .mockResolvedValueOnce([journey()])
      .mockResolvedValueOnce(journey({ run: { status: "running", step: "orders", message: "Learn the order history..." } }))
      .mockResolvedValue([journey({ run: { status: "running", step: "orders", message: "Learn the order history..." } })]);
    render(<SetupPanel onForbidden={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Set up Swiggy" }));
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalledWith("/admin/setup/swiggy", { method: "POST" }));
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.getByText("Setting up")).toBeInTheDocument();
  });

  it("a paused run says why and offers Continue", async () => {
    api.apiFetch.mockResolvedValueOnce([
      journey({ run: { status: "paused", step: "orders", message: "Learn the order history: needs your OK in Approvals." } }),
    ]);
    render(<SetupPanel onForbidden={() => {}} />);
    expect(await screen.findByText(/needs your OK in Approvals/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue Swiggy" })).toBeInTheDocument();
  });

  it("a finished app shows Ready and no button, and the all-apps button goes away", async () => {
    api.apiFetch.mockResolvedValueOnce([journey({ done: 5, next: null })]);
    render(<SetupPanel onForbidden={() => {}} />);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Set up/ })).not.toBeInTheDocument();
  });

  it("a failed start shows the problem", async () => {
    api.apiFetch.mockResolvedValueOnce([journey()]).mockRejectedValueOnce(new Error("boom"));
    render(<SetupPanel onForbidden={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Set up all apps" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
