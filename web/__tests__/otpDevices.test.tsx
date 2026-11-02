import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), apiFetch: api.apiFetch }));

import { OTP_COPY, OtpDevicesCard } from "@/components/account/OtpDevicesCard";

const device = { id: "d1", name: "Pixel 8", created_at: null, last_seen_at: null };

describe("OtpDevicesCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/otp-devices") return { devices: [device] };
      if (path === "/otp-devices/pairing") return { code: "ABCD2345", expires_in: 600 };
      if (path === "/otp-devices/d1" && init?.method === "DELETE") return undefined;
      throw new Error(path);
    });
  });

  it("shows the honest copy and lists paired phones", async () => {
    render(<OtpDevicesCard />);
    expect(screen.getByText(OTP_COPY)).toBeInTheDocument();
    expect(OTP_COPY).toContain("Android only");
    expect(OTP_COPY).toContain("iPhone can't do this");
    expect(await screen.findByText("Pixel 8")).toBeInTheDocument();
  });

  it("pairing shows the code and a countdown", async () => {
    render(<OtpDevicesCard />);
    await userEvent.click(screen.getByRole("button", { name: "Pair an Android phone" }));
    expect(await screen.findByLabelText("Pairing code")).toHaveTextContent("ABCD2345");
    expect(screen.getByText(/Expires in (10:00|9:5\d)/)).toBeInTheDocument();
    expect(api.apiFetch).toHaveBeenCalledWith("/otp-devices/pairing", { method: "POST" });
  });

  it("revoke removes the phone", async () => {
    render(<OtpDevicesCard />);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke Pixel 8" }));
    await waitFor(() => expect(screen.queryByText("Pixel 8")).not.toBeInTheDocument());
    expect(api.apiFetch).toHaveBeenCalledWith("/otp-devices/d1", { method: "DELETE" });
  });

  it("a pairing failure shows an alert", async () => {
    api.apiFetch.mockImplementation(async (path: string) => {
      if (path === "/otp-devices") return { devices: [] };
      throw new Error("x");
    });
    render(<OtpDevicesCard />);
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Pair an Android phone" }));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't make a pairing code");
  });
});
