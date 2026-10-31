import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, apiFetch: api.apiFetch };
});

const live = vi.hoisted(() => ({ subs: [] as Array<(rows: unknown) => void>, unsubs: 0 }));
vi.mock("@/lib/addresses", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/addresses")>();
  return {
    ...real,
    watchAddresses: vi.fn((cb: (rows: unknown) => void) => {
      live.subs.push(cb);
      return () => {
        live.unsubs += 1;
      };
    }),
  };
});

import { ApiError } from "@/lib/api";
import { AddressPicker } from "@/components/account/AddressPicker";
import { AddressRow } from "@/components/connect/AddressRow";
import { groupOptions, type AppAddress } from "@/lib/addresses";

function addr(over: Partial<AppAddress>): AppAddress {
  return {
    site: "zepto",
    address_id: "z-home",
    label: "Home",
    line: "Flat 12, Lakeview Residency",
    area: "HSR Layout",
    city: "Bengaluru",
    lat: 12.9116,
    lon: 77.6474,
    is_default: false,
    ...over,
  };
}

const ROWS: AppAddress[] = [
  addr({}),
  addr({ site: "swiggy", address_id: "s-home", lat: 12.912, lon: 77.6478, is_default: true }), // ~60 m away
  addr({ address_id: "z-work", label: "Work", line: "Tower B, Prestige Tech Park", area: "Koramangala", lat: 12.9352, lon: 77.6245 }),
  addr({ site: "zomato", address_id: "m-work", label: "Office", line: "Tower B,  Prestige Tech Park!", area: "Koramangala", lat: null, lon: null }),
];

/** Push a fresh list to whoever watches the saved addresses. */
function pushRows(rows: AppAddress[]) {
  act(() => {
    for (const cb of live.subs) cb(rows);
  });
}

beforeEach(() => {
  api.apiFetch.mockReset();
  live.subs.length = 0;
  live.unsubs = 0;
});

describe("groupOptions", () => {
  it("merges the same place across apps (150 m, or the same line) and puts the default first", () => {
    const opts = groupOptions(ROWS);
    expect(opts).toHaveLength(2);
    expect(opts[0]!.sites).toEqual(["zepto", "swiggy"]);
    expect(opts[0]!.is_default).toBe(true);
    expect(opts[1]!.sites).toEqual(["zepto", "zomato"]);
    expect(opts[1]!.members).toEqual([
      { site: "zepto", address_id: "z-work" },
      { site: "zomato", address_id: "m-work" },
    ]);
  });

  it("compares with the group's lead, so places 140 m apart do not chain", () => {
    // A, B, C on three apps, each 140 m north of the last: A-B and B-C are "the same place", A-C is not.
    const step = 140 / 111_195; // degrees of latitude
    const opts = groupOptions([
      addr({ site: "zepto", address_id: "a", line: "A", lat: 12.9 }),
      addr({ site: "blinkit", address_id: "b", line: "B", lat: 12.9 + step }),
      addr({ site: "swiggy", address_id: "c", line: "C", lat: 12.9 + 2 * step }),
    ]);
    expect(opts.map((o) => o.members.map((m) => m.address_id))).toEqual([["a", "b"], ["c"]]);
  });

  it("never puts two addresses from one app in one place", () => {
    const opts = groupOptions([
      addr({ address_id: "flat-1", line: "Flat 1", lat: 12.9 }),
      addr({ address_id: "flat-2", line: "Flat 2", lat: 12.9 + 50 / 111_195 }), // 50 m away, same app
    ]);
    expect(opts).toHaveLength(2);
  });

  it("a tap sends the lead it shows, or the first member with a pin", () => {
    const [home, work] = groupOptions(ROWS);
    expect(home!.send).toEqual({ site: "swiggy", address_id: "s-home" }); // the default leads
    const sent = ROWS.find((r) => r.address_id === home!.send.address_id)!;
    expect(sent.line).toBe(home!.line); // the profile text is the line the user tapped
    expect(work!.send).toEqual({ site: "zepto", address_id: "z-work" });
    const [unpinned] = groupOptions([
      addr({ site: "zepto", address_id: "z", lat: null, lon: null, is_default: true }),
      addr({ site: "swiggy", address_id: "s" }),
    ]);
    expect(unpinned!.send).toEqual({ site: "swiggy", address_id: "s" });
  });

  it("keeps far-apart places separate", () => {
    const opts = groupOptions([addr({ line: "A" }), addr({ site: "blinkit", address_id: "b", line: "B", lat: 12.92 })]);
    expect(opts).toHaveLength(2);
  });
});

describe("AddressPicker", () => {
  it("picker shows options with app dots", () => {
    render(
      <AddressPicker
        options={groupOptions(ROWS)}
        chosen={{ site: "zomato", address_id: "m-work" }}
        onChosen={vi.fn()}
        onTypeInstead={vi.fn()}
      />,
    );
    const group = screen.getByRole("radiogroup", { name: "Your saved addresses" });
    const options = within(group).getAllByRole("radio");
    expect(options).toHaveLength(2);
    expect(options[0]).toHaveAccessibleName(/Home/);
    expect(within(options[0]!).getByText("Flat 12, Lakeview Residency")).toBeInTheDocument();
    expect(within(options[0]!).getByText("HSR Layout, Bengaluru")).toBeInTheDocument();
    expect(within(options[0]!).getByText("Zepto, Swiggy")).toBeInTheDocument(); // the dots also say it in words
    expect(options[0]!.querySelectorAll("[data-app-dot]")).toHaveLength(2);
    expect(within(options[1]!).getByText("Zepto, Zomato")).toBeInTheDocument();
    // the current choice is checked, wherever it came from
    expect(options[0]).toHaveAttribute("aria-checked", "false");
    expect(options[1]).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Enter an area instead" })).toBeInTheDocument();
  });

  it("choice calls PUT /profile", async () => {
    const onChosen = vi.fn();
    const res = {
      address: "Flat 12, Lakeview Residency, HSR Layout, Bengaluru",
      lat: 12.9116,
      lon: 77.6474,
      display: "HSR Layout, Bengaluru",
      chosen: { site: "swiggy", address_id: "s-home" },
    };
    api.apiFetch.mockResolvedValueOnce(res);
    render(<AddressPicker options={groupOptions(ROWS)} chosen={null} onChosen={onChosen} onTypeInstead={vi.fn()} />);
    await userEvent.click(screen.getAllByRole("radio")[0]!);
    expect(api.apiFetch).toHaveBeenCalledWith("/profile", {
      method: "PUT",
      body: JSON.stringify({ choice: { site: "swiggy", address_id: "s-home" } }),
    });
    await waitFor(() => expect(onChosen).toHaveBeenCalledWith(res));
  });

  it("says so when a choice cannot be saved, and keeps the list", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(404, "not one of your saved addresses"));
    render(<AddressPicker options={groupOptions(ROWS)} chosen={null} onChosen={vi.fn()} onTypeInstead={vi.fn()} />);
    await userEvent.click(screen.getAllByRole("radio")[1]!);
    expect(await screen.findByRole("alert")).toHaveTextContent("That address changed in the app");
    expect(screen.getAllByRole("radio")).toHaveLength(2);
  });
});

describe("AddressPicker refresh and lines", () => {
  it("has Refresh addresses on top that POSTs /addresses/refresh", async () => {
    api.apiFetch.mockResolvedValueOnce({ sites: ["zepto"] });
    render(<AddressPicker options={groupOptions(ROWS)} chosen={null} onChosen={vi.fn()} onTypeInstead={vi.fn()} />);
    const btn = screen.getByRole("button", { name: "Refresh addresses" });
    expect(btn.compareDocumentPosition(screen.getByRole("radiogroup")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(btn);
    expect(api.apiFetch).toHaveBeenCalledWith("/addresses/refresh", { method: "POST" });
    expect(await screen.findByRole("status")).toBeInTheDocument();
  });

  it("explains the rate limit", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(429, "slow down"));
    render(<AddressPicker options={groupOptions(ROWS)} chosen={null} onChosen={vi.fn()} onTypeInstead={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Refresh addresses" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Just refreshed");
  });

  it("shows an app's own line when it differs", () => {
    const rows = [addr({}), addr({ site: "swiggy", address_id: "s1", line: "12, Lakeview Apts", lat: 12.912, lon: 77.6478 })];
    render(<AddressPicker options={groupOptions(rows)} chosen={null} onChosen={vi.fn()} onTypeInstead={vi.fn()} />);
    expect(screen.getByText("Swiggy: 12, Lakeview Apts")).toBeInTheDocument();
  });
});

describe("AddressPicker keyboard", () => {
  it("is one tab stop, and arrow keys move between the radios without saving", async () => {
    render(
      <AddressPicker
        options={groupOptions(ROWS)}
        chosen={{ site: "zepto", address_id: "z-work" }}
        onChosen={vi.fn()}
        onTypeInstead={vi.fn()}
      />,
    );
    const [home, work] = screen.getAllByRole("radio");
    expect(screen.getByRole("radiogroup").tagName).toBe("DIV");
    expect(work).toHaveAttribute("tabindex", "0"); // the checked one holds the tab stop
    expect(home).toHaveAttribute("tabindex", "-1");
    work!.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(home).toHaveFocus(); // wraps
    await userEvent.keyboard("{ArrowUp}");
    expect(work).toHaveFocus();
    expect(api.apiFetch).not.toHaveBeenCalled();
  });
});

describe("AddressRow with saved addresses", () => {
  const profile = { address: "Flat 12, Lakeview Residency, HSR Layout, Bengaluru", lat: 12.9116, lon: 77.6474 };

  it("new addresses appear live", async () => {
    render(<AddressRow profile={profile} onSaved={vi.fn()} initialAddresses={[]} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Connect an app and we'll show its saved addresses here")).toBeInTheDocument();
    expect(within(dialog).queryByRole("radio")).toBeNull();

    pushRows(ROWS.slice(0, 2)); // a connect just finished and its addresses arrived
    expect(await within(dialog).findAllByRole("radio")).toHaveLength(1);
    pushRows(ROWS);
    await waitFor(() => expect(within(dialog).getAllByRole("radio")).toHaveLength(2));
  });

  it("opens on the saved addresses and saves a choice", async () => {
    const onSaved = vi.fn();
    api.apiFetch.mockResolvedValueOnce({
      address: "Tower B, Prestige Tech Park, Koramangala, Bengaluru",
      lat: 12.9352,
      lon: 77.6245,
      display: "Koramangala, Bengaluru",
      chosen: { site: "zepto", address_id: "z-work" },
    });
    render(<AddressRow profile={profile} onSaved={onSaved} initialAddresses={ROWS} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    await userEvent.click(await screen.findByRole("radio", { name: /Work/ }));
    await waitFor(() =>
      expect(onSaved).toHaveBeenCalledWith({
        address: "Tower B, Prestige Tech Park, Koramangala, Bengaluru",
        lat: 12.9352,
        lon: 77.6245,
        place: "Koramangala, Bengaluru",
        chosen: { site: "zepto", address_id: "z-work" },
      }),
    );
  });

  it("type instead fallback", async () => {
    const onSaved = vi.fn();
    render(<AddressRow profile={profile} onSaved={onSaved} initialAddresses={ROWS} />);
    await userEvent.click(screen.getByRole("button", { name: /Delivery address/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Enter an area instead" }));
    const field = await screen.findByLabelText("Address");
    await userEvent.clear(field);
    await userEvent.type(field, "Indiranagar, Bengaluru");
    api.apiFetch.mockResolvedValueOnce({ address: "Indiranagar, Bengaluru", lat: 12.97, lon: 77.64 });
    await userEvent.click(screen.getByRole("button", { name: "Save address" }));
    expect(api.apiFetch).toHaveBeenCalledWith("/profile", {
      method: "PUT",
      body: JSON.stringify({ address: "Indiranagar, Bengaluru" }),
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ chosen: null })));
  });

  it("stops watching when it unmounts", () => {
    const { unmount } = render(<AddressRow profile={null} onSaved={vi.fn()} initialAddresses={[]} />);
    expect(live.subs).toHaveLength(1);
    unmount();
    expect(live.unsubs).toBe(1);
  });
});

describe("watchAddresses (the real module, with a fake Supabase client)", () => {
  it("re-reads the user's rows on join and on every change, and unsubscribes", async () => {
    const real = await vi.importActual<typeof import("@/lib/addresses")>("@/lib/addresses");
    let handler: (() => void) | undefined;
    let status: ((s: string) => void) | undefined;
    const channel = {
      on: vi.fn((_t: string, _f: unknown, h: () => void) => {
        handler = h;
        return channel;
      }),
      subscribe: vi.fn((cb: (s: string) => void) => {
        status = cb;
        return channel;
      }),
    };
    let data: unknown[] = [];
    const selects: string[] = [];
    const client = {
      channel: vi.fn(() => channel),
      auth: { getSession: async () => ({ data: { session: null } }) },
      removeChannel: vi.fn(async () => "ok"),
      from: vi.fn((table: string) => ({
        select: (cols: string) => {
          selects.push(`${table}:${cols}`);
          return Promise.resolve({ data, error: null });
        },
      })),
    };
    const seen: AppAddress[][] = [];
    const stop = real.watchAddresses((rows) => seen.push(rows), client as never, "u1");
    await waitFor(() => expect(client.channel).toHaveBeenCalled());
    // Own inserts and updates only, filtered to the user. Never DELETE (or "*"): Supabase delivers
    // DELETE events to every subscriber, unfiltered.
    expect(channel.on.mock.calls.map((c) => [c[0], c[1]])).toEqual([
      ["postgres_changes", { event: "INSERT", schema: "public", table: "app_addresses", filter: "user_id=eq.u1" }],
      ["postgres_changes", { event: "UPDATE", schema: "public", table: "app_addresses", filter: "user_id=eq.u1" }],
    ]);
    status!("SUBSCRIBED");
    await waitFor(() => expect(seen).toEqual([[]]));
    data = [{ ...ROWS[0], extra: "dropped" }];
    handler!();
    await waitFor(() => expect(seen[1]).toEqual([ROWS[0]]));
    // a delete (no event reaches us) shows on the next wake
    data = [];
    window.dispatchEvent(new Event("online"));
    await waitFor(() => expect(seen[2]).toEqual([]));
    expect(selects.every((s) => s === `app_addresses:${real.ADDRESS_COLUMNS}`)).toBe(true);
    stop();
    expect(client.removeChannel).toHaveBeenCalledWith(channel);
  });
});
