import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));

import { UseMyLocation, distanceLabel } from "@/components/account/UseMyLocation";

function gps(result: "ok" | "denied") {
  const getCurrentPosition = vi.fn((ok: PositionCallback, bad: PositionErrorCallback) => {
    if (result === "ok") ok({ coords: { latitude: 22.3, longitude: 73.17 } } as GeolocationPosition);
    else bad({ code: 1, message: "denied" } as GeolocationPositionError);
  });
  Object.defineProperty(navigator, "geolocation", { value: { getCurrentPosition }, configurable: true });
  return getCurrentPosition;
}

const MATCH = { site: "swiggy", address_id: "a1", label: "Home", line: "1 Test Society", area: "Gorwa", city: "Testville", distance_m: 34 };

describe("UseMyLocation", () => {
  beforeEach(() => api.apiFetch.mockReset());
  afterEach(() => Reflect.deleteProperty(navigator, "geolocation"));

  it("formats distances", () => {
    expect(distanceLabel(34)).toBe("30 m");
    expect(distanceLabel(3)).toBe("10 m");
    expect(distanceLabel(1240)).toBe("1.2 km");
  });

  it("matches the pin to the nearest saved address and lets the user use it", async () => {
    gps("ok");
    api.apiFetch
      .mockResolvedValueOnce({ matches: [MATCH] })
      .mockResolvedValueOnce({ address: "1 Test Society, Gorwa, Testville", lat: 22.3, lon: 73.17, chosen: { site: "swiggy", address_id: "a1" } });
    const onChosen = vi.fn();
    render(<UseMyLocation onChosen={onChosen} onPinSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Use my current location/ }));
    expect(await screen.findByText("Home")).toBeInTheDocument();
    expect(screen.getByText(/Swiggy · 30 m away/)).toBeInTheDocument();
    expect(api.apiFetch).toHaveBeenCalledWith("/profile/locate", { method: "POST", body: JSON.stringify({ lat: 22.3, lon: 73.17 }) });
    fireEvent.click(screen.getByRole("button", { name: /Home/ }));
    await waitFor(() => expect(onChosen).toHaveBeenCalled());
    expect(api.apiFetch).toHaveBeenLastCalledWith("/profile", { method: "PUT", body: JSON.stringify({ choice: { site: "swiggy", address_id: "a1" } }) });
  });

  it("with nothing close, offers the exact spot", async () => {
    gps("ok");
    api.apiFetch.mockResolvedValueOnce({ matches: [] }).mockResolvedValueOnce({ address: "Current location", lat: 22.3, lon: 73.17 });
    const onPinSaved = vi.fn();
    render(<UseMyLocation onChosen={vi.fn()} onPinSaved={onPinSaved} />);
    fireEvent.click(screen.getByRole("button", { name: /Use my current location/ }));
    expect(await screen.findByText("None of your saved addresses are close")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use this exact spot" }));
    await waitFor(() => expect(onPinSaved).toHaveBeenCalledWith({ address: "Current location", lat: 22.3, lon: 73.17 }));
    expect(api.apiFetch).toHaveBeenLastCalledWith("/profile", { method: "PUT", body: JSON.stringify({ pin: { lat: 22.3, lon: 73.17 } }) });
  });

  it("explains a denied permission and never calls the API", async () => {
    gps("denied");
    render(<UseMyLocation onChosen={vi.fn()} onPinSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Use my current location/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Location is off");
    expect(api.apiFetch).not.toHaveBeenCalled();
  });
});
