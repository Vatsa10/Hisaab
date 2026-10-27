import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The real ApiError, a mocked apiFetch: /ask is never called for real in these tests.
const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));

// monthly_spend through the Supabase client: from().select().eq() resolves to db.result.
const db = vi.hoisted(() => ({
  result: { data: [] as unknown[] | null, error: null as unknown },
  from: vi.fn(),
  eq: vi.fn(),
}));
vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({
    from: (table: string) => {
      db.from(table);
      return {
        select: () => ({
          eq: (col: string, value: string) => {
            db.eq(col, value);
            return Promise.resolve(db.result);
          },
        }),
      };
    },
  }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/ask" }));

import { ApiError } from "@/lib/api";
import { AskChat, SLOW_AFTER_MS, SUGGESTIONS } from "@/components/ask/AskChat";
import { currentMonth } from "@/components/ask/useMonthSpend";
import { TabBar } from "@/components/ui/TabBar";
import { monthLabel } from "@/lib/format";

const TABLE_ANSWER = [
  "Here is the split:",
  "",
  "| App | Orders | Spend | vs Sep |",
  "|---|---|---|---|",
  "| Swiggy | 9 | ₹2,363 | +12% |",
  "| Zomato | 7 | ₹1,930 | -4% |",
].join("\n");

const RANK_ANSWER = [
  "| # | Restaurant | App | Spend |",
  "|---|---|---|---|",
  "| 1 | Meghana Foods | Swiggy | ₹5,113 |",
  "| 2 | Truffles | Zomato | ₹3,872 |",
].join("\n");

/** A promise the test resolves or rejects by hand. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const composer = () => screen.getByRole("textbox", { name: /ask a question/i });
const askCall = (question: string) => [
  "/ask",
  expect.objectContaining({ method: "POST", body: JSON.stringify({ question }) }),
];

async function answerWith(answer: string, chip = "Top restaurants") {
  api.apiFetch.mockResolvedValue({ answer });
  const view = render(<AskChat />);
  await userEvent.click(screen.getByRole("button", { name: chip }));
  return view;
}

describe("AskChat", () => {
  beforeEach(() => {
    api.apiFetch.mockReset();
    db.result = { data: [], error: null };
    db.from.mockReset();
    db.eq.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("offers the four suggested questions", () => {
    render(<AskChat />);
    expect(SUGGESTIONS).toEqual([
      "Where did my money go this month?",
      "Food vs groceries",
      "Biggest month this year",
      "Top restaurants",
    ]);
    for (const q of SUGGESTIONS) expect(screen.getByRole("button", { name: q })).toBeInTheDocument();
  });

  it("sends a chip's question to POST /ask and shows the question and the answer", async () => {
    await answerWith("You spent **₹4,293** on food.", "Food vs groceries");

    expect(api.apiFetch).toHaveBeenCalledWith(...askCall("Food vs groceries"));
    const log = screen.getByRole("log", { name: "Conversation" });
    expect(within(log).getByText("Food vs groceries")).toBeInTheDocument();
    expect(await within(log).findByText("₹4,293")).toBeInTheDocument();
    expect(within(log).getByText("₹4,293").tagName).toBe("STRONG");
    // The suggestions make way for the conversation.
    expect(screen.queryByRole("button", { name: "Top restaurants" })).toBeNull();
  });

  it("shows a Thinking state while the analyst works", async () => {
    const reply = deferred<{ answer: string }>();
    api.apiFetch.mockReturnValue(reply.promise);
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));

    const log = screen.getByRole("log", { name: "Conversation" });
    expect(within(log).getByText("Thinking…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    await act(async () => reply.resolve({ answer: "Meghana Foods, 6 orders." }));
    expect(screen.queryByText("Thinking…")).toBeNull();
    expect(screen.getByText("Meghana Foods, 6 orders.")).toBeInTheDocument();
  });

  it("says it is still working after 8 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.apiFetch.mockReturnValue(new Promise(() => {}));
    render(<AskChat />);
    fireEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(SLOW_AFTER_MS - 100));
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByText("Still working on it…")).toBeInTheDocument();
  });

  it("announces through the one polite log only: no status or alert roles inside it", async () => {
    api.apiFetch.mockRejectedValueOnce(new ApiError(502, "x")).mockReturnValueOnce(new Promise(() => {}));
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    await screen.findByText("The analyst is busy — try again");
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("log", { name: "Conversation" })).toHaveAttribute("aria-live", "polite");
  });

  it("renders a markdown table inside a horizontally scrollable card", async () => {
    await answerWith(TABLE_ANSWER, "Biggest month this year");

    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "App",
      "Orders",
      "Spend",
      "vs Sep",
    ]);
    expect(within(table).getByRole("cell", { name: "₹2,363" })).toBeInTheDocument();
    // Number columns line up on the right; the label column stays left.
    expect(within(table).getByRole("cell", { name: "₹2,363" })).toHaveStyle({ textAlign: "right" });
    expect(within(table).getByRole("cell", { name: "Swiggy" })).not.toHaveStyle({ textAlign: "right" });

    const scroller = screen.getByRole("region", { name: "Table, scrolls sideways" });
    expect(scroller).toContainElement(table);
    expect(scroller.className).toContain("overflow-x-auto");
    expect(scroller).toHaveAttribute("tabindex", "0");
  });

  it("pins the first column so a scrolled row keeps its label", async () => {
    await answerWith(TABLE_ANSWER);
    const table = await screen.findByRole("table");
    for (const cls of ["first:sticky", "first:left-0", "first:z-[1]", "first:bg-surface"]) {
      expect(within(table).getByRole("cell", { name: "Swiggy" }).className).toContain(cls);
    }
    expect(within(table).getByRole("columnheader", { name: "App" }).className).toContain("first:bg-canvas");
    expect(within(table).getByRole("columnheader", { name: "App" })).toHaveAttribute("data-pin-cap");
  });

  it("also pins the name column when the first column is only a rank", async () => {
    await answerWith(RANK_ANSWER);
    const table = await screen.findByRole("table");
    const name = within(table).getByRole("cell", { name: "Meghana Foods" });
    expect(name.className).toContain("sticky");
    expect(name.className).toContain("left-11");
    expect(name).toHaveAttribute("data-pin-second");
    expect(within(table).getByRole("cell", { name: "1" }).className).toContain("w-11");
    expect(within(table).getByRole("cell", { name: "1" })).not.toHaveAttribute("data-pin-cap");
    expect(within(table).getByRole("columnheader", { name: "Restaurant" }).className).toContain("bg-canvas");
  });

  it("caps the pinned label column at two lines, keeping the full name in title", async () => {
    const long = "Meghana Foods Koramangala 5th Block";
    await answerWith(
      ["| Restaurant | Orders | Spend |", "|---|---|---|", `| ${long} | 17 | ₹5,113 |`].join("\n"),
    );
    const table = await screen.findByRole("table");
    const cell = within(table).getByRole("cell", { name: long });
    expect(cell).toHaveAttribute("title", long);
    const clamp = cell.firstElementChild as HTMLElement;
    expect(clamp.textContent).toBe(long);
    for (const cls of ["line-clamp-2", "max-w-[6.5rem]", "whitespace-normal"]) expect(clamp.className).toContain(cls);
    // Other columns are not capped.
    expect(within(table).getByRole("cell", { name: "₹5,113" })).not.toHaveAttribute("title");
    expect(within(table).getByRole("cell", { name: "₹5,113" }).firstElementChild).toBeNull();
  });

  it("caps the pinned name column after a rank too", async () => {
    await answerWith(RANK_ANSWER);
    const table = await screen.findByRole("table");
    const name = within(table).getByRole("cell", { name: "Meghana Foods" });
    expect(name).toHaveAttribute("title", "Meghana Foods");
    expect((name.firstElementChild as HTMLElement).className).toContain("line-clamp-2");
  });

  describe("second pin vs. card width", () => {
    /** Cells: rank 44px, name 120px (164px pinned). The card is `card` px wide. */
    function layout(card: number) {
      vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
        if (this instanceof HTMLTableCellElement) return this.cellIndex === 0 ? 44 : 120;
        return 0;
      });
      vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) {
        return this.getAttribute("role") === "region" ? card : 0;
      });
    }

    it("drops the second pin when both would cover more than 45% of the card", async () => {
      layout(313); // 164 > 0.45 × 313 = 140.85
      await answerWith(RANK_ANSWER);
      await screen.findByRole("table");
      const scroller = screen.getByRole("region", { name: "Table, scrolls sideways" });
      expect(scroller).toHaveAttribute("data-unpin-second");
      expect(scroller.className).toContain("group/table");
      const name = screen.getByRole("cell", { name: "Meghana Foods" });
      expect(name.className).toContain("group-data-[unpin-second]/table:static");
    });

    it("keeps the second pin when there is room", async () => {
      layout(600); // 164 < 0.45 × 600 = 270
      await answerWith(RANK_ANSWER);
      await screen.findByRole("table");
      expect(screen.getByRole("region", { name: "Table, scrolls sideways" })).not.toHaveAttribute("data-unpin-second");
    });
  });

  it("writes negative numbers with a true minus sign", async () => {
    await answerWith(TABLE_ANSWER);
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("cell", { name: "−4%" })).toBeInTheDocument();
    expect(within(table).queryByRole("cell", { name: "-4%" })).toBeNull();
    expect(within(table).getByRole("cell", { name: "+12%" })).toBeInTheDocument();
  });

  it("styles answer headings by level, with room above them", async () => {
    await answerWith("Intro.\n\n# Big picture\n\nText.\n\n### Detail\n\nMore.");
    const big = await screen.findByText("Big picture");
    const detail = screen.getByText("Detail");
    expect(big.className).toContain("text-headline");
    expect(detail.className).toContain("font-semibold");
    expect(detail.className).not.toContain("text-headline");
    expect(big.className).toContain("[&:not(:first-child)]:mt-4");
  });

  it("never renders raw HTML from an answer", async () => {
    const { container } = await answerWith(
      'Safe <img src="x" onerror="alert(1)"> text <script>alert(2)</script> and <b>bold</b>\n\n<iframe src="https://evil.example"></iframe>',
    );
    await screen.findByText(/Safe/);
    expect(container.querySelector("img, script, b, iframe")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
  });

  it("shows a markdown image's alt text instead of loading it", async () => {
    const { container } = await answerWith("Look: ![x](https://evil.example/p.png)");
    await screen.findByText(/Look:/);
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("evil.example");
    expect(screen.getByText("x")).toBeInTheDocument();
  });

  it("opens only http(s) links, in a new tab with noopener noreferrer", async () => {
    await answerWith("[Swiggy](https://www.swiggy.com/) and [bad](javascript:alert(1)) and [local](/spend)");

    const link = await screen.findByRole("link", { name: "Swiggy" });
    expect(link).toHaveAttribute("href", "https://www.swiggy.com/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
    expect(screen.queryByRole("link", { name: "local" })).toBeNull();
    expect(screen.getByText("bad")).toBeInTheDocument();
  });

  it("explains a busy analyst (502) and retries the same question", async () => {
    api.apiFetch
      .mockRejectedValueOnce(new ApiError(502, "llm failed"))
      .mockResolvedValueOnce({ answer: "Groceries ₹3,120, food ₹4,293." });
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Food vs groceries" }));

    const message = await screen.findByText("The analyst is busy — try again");
    const error = message.closest<HTMLElement>("[data-ask-error]")!;
    expect(error).not.toHaveTextContent("llm failed");

    await userEvent.click(within(error).getByRole("button", { name: "Try again" }));
    expect(api.apiFetch).toHaveBeenCalledTimes(2);
    expect(api.apiFetch.mock.calls[1]![1].body).toBe(api.apiFetch.mock.calls[0]![1].body);
    expect(await screen.findByText("Groceries ₹3,120, food ₹4,293.")).toBeInTheDocument();
    expect(screen.queryByText("The analyst is busy — try again")).toBeNull();
    // The question is asked once in the conversation, not repeated by the retry.
    expect(screen.getAllByText("Food vs groceries")).toHaveLength(1);
  });

  it("says Ask isn't set up yet on 503", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(503, "ask not configured"));
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    const message = await screen.findByText("Ask isn't set up yet");
    const error = message.closest<HTMLElement>("[data-ask-error]")!;
    expect(within(error).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("aborts the request in flight when the screen is left", async () => {
    api.apiFetch.mockReturnValue(new Promise(() => {}));
    const { unmount } = render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    const signal = api.apiFetch.mock.calls[0]![1].signal as AbortSignal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("sends on Enter and adds a new line on Shift+Enter", async () => {
    api.apiFetch.mockResolvedValue({ answer: "ok" });
    render(<AskChat />);
    const box = composer();
    await userEvent.type(box, "Spend in July{Shift>}{Enter}{/Shift}by app");
    expect(box).toHaveValue("Spend in July\nby app");
    expect(api.apiFetch).not.toHaveBeenCalled();

    await userEvent.type(box, "{Enter}");
    expect(api.apiFetch).toHaveBeenCalledWith(...askCall("Spend in July\nby app"));
    expect(box).toHaveValue("");
  });

  it("does not send an empty or whitespace-only question", async () => {
    render(<AskChat />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.type(composer(), "   {Enter}");
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("caps questions at 1000 characters, counting only from 800", () => {
    render(<AskChat />);
    const box = composer();
    expect(box).toHaveAttribute("maxlength", "1000");
    fireEvent.change(box, { target: { value: "a".repeat(799) } });
    expect(screen.queryByText(/\/1000$/)).toBeNull();
    fireEvent.change(box, { target: { value: "a".repeat(800) } });
    expect(screen.getByText("800/1000").className).not.toContain("text-danger");
    fireEvent.change(box, { target: { value: "a".repeat(1000) } });
    expect(screen.getByText("1000/1000").className).toContain("text-danger");
  });

  it("hides the real tab bar while typing and brings it back on blur", () => {
    render(
      <>
        <AskChat />
        <TabBar />
      </>,
    );
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toBeVisible();
    fireEvent.focus(composer());
    expect(nav).not.toBeVisible();
    fireEvent.blur(composer());
    expect(nav).toBeVisible();
  });

  it("lifts the composer above the on-screen keyboard (visualViewport)", () => {
    const listeners: Record<string, () => void> = {};
    const vv = {
      height: 844,
      offsetTop: 0,
      scale: 1,
      addEventListener: (type: string, fn: () => void) => {
        listeners[type] = fn;
      },
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal("visualViewport", vv);
    vi.stubGlobal("innerHeight", 844);
    render(<AskChat />);
    fireEvent.focus(composer());

    vv.height = 508; // the keyboard takes 336px
    act(() => listeners.resize!());
    const bar = document.querySelector<HTMLElement>("[data-ask-composer]")!;
    expect(bar.style.transform).toBe("translateY(-336px)");
  });

  it("shows this month's spend and orders under the title, from monthly_spend", async () => {
    db.result = {
      data: [
        { orders: 3, amount: 702.5 },
        { orders: 2, amount: "399.50" },
      ],
      error: null,
    };
    render(<AskChat />);
    const month = currentMonth();
    const name = monthLabel(month).split(" ")[0];
    expect(await screen.findByText(`₹1,102 spent in ${name} · 5 orders`)).toBeInTheDocument();
    expect(db.from).toHaveBeenCalledWith("monthly_spend");
    expect(db.eq).toHaveBeenCalledWith("month", month);
  });

  it("leaves the context line out when the month has no orders or the query fails", async () => {
    db.result = { data: null, error: { message: "nope" } };
    render(<AskChat />);
    await act(async () => {});
    expect(screen.queryByText(/spent in/)).toBeNull();
  });

  it("keeps the conversation out of browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    await answerWith("ok");
    await screen.findByText("ok");
    expect(setItem).not.toHaveBeenCalled();
  });
});
