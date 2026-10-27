import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The real ApiError, a mocked apiFetch: /ask is never called for real in these tests.
const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: api.apiFetch,
}));

import { ApiError } from "@/lib/api";
import { AskChat, SUGGESTIONS } from "@/components/ask/AskChat";

const TABLE_ANSWER = [
  "Here is the split:",
  "",
  "| App | Orders | Spend |",
  "|---|---|---|",
  "| Swiggy | 9 | ₹2,363 |",
  "| Zomato | 7 | ₹1,930 |",
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

describe("AskChat", () => {
  beforeEach(() => {
    api.apiFetch.mockReset();
  });

  afterEach(() => {
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
    api.apiFetch.mockResolvedValue({ answer: "You spent **₹4,293** on food." });
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Food vs groceries" }));

    expect(api.apiFetch).toHaveBeenCalledWith("/ask", {
      method: "POST",
      body: JSON.stringify({ question: "Food vs groceries" }),
    });
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

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Thinking…");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    await act(async () => reply.resolve({ answer: "Meghana Foods, 6 orders." }));
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("Meghana Foods, 6 orders.")).toBeInTheDocument();
  });

  it("renders a markdown table inside a horizontally scrollable card", async () => {
    api.apiFetch.mockResolvedValue({ answer: TABLE_ANSWER });
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Biggest month this year" }));

    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "App",
      "Orders",
      "Spend",
    ]);
    expect(within(table).getByRole("cell", { name: "₹2,363" })).toBeInTheDocument();
    // Number columns line up on the right; the label column stays left.
    expect(within(table).getByRole("cell", { name: "₹2,363" })).toHaveStyle({ textAlign: "right" });
    expect(within(table).getByRole("cell", { name: "Swiggy" })).not.toHaveStyle({ textAlign: "right" });

    const scroller = table.parentElement!;
    expect(scroller.className).toContain("overflow-x-auto");
    expect(scroller).toHaveAttribute("tabindex", "0");
  });

  it("never renders raw HTML from an answer", async () => {
    api.apiFetch.mockResolvedValue({
      answer:
        'Safe <img src="x" onerror="alert(1)"> text <script>alert(2)</script> and <b>bold</b>\n\n<iframe src="https://evil.example"></iframe>',
    });
    const { container } = render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));

    await screen.findByText(/Safe/);
    expect(container.querySelector("img, script, b, iframe")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
  });

  it("opens only http(s) links, in a new tab with noopener noreferrer", async () => {
    api.apiFetch.mockResolvedValue({
      answer: "[Swiggy](https://www.swiggy.com/) and [bad](javascript:alert(1)) and [local](/spend)",
    });
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));

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

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The analyst is busy — try again");
    expect(alert).not.toHaveTextContent("llm failed");

    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(api.apiFetch).toHaveBeenCalledTimes(2);
    expect(api.apiFetch.mock.calls[1]).toEqual(api.apiFetch.mock.calls[0]);
    expect(await screen.findByText("Groceries ₹3,120, food ₹4,293.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    // The question is asked once in the conversation, not repeated by the retry.
    expect(screen.getAllByText("Food vs groceries")).toHaveLength(1);
  });

  it("says Ask isn't set up yet on 503", async () => {
    api.apiFetch.mockRejectedValue(new ApiError(503, "ask not configured"));
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Ask isn't set up yet");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("sends on Enter and adds a new line on Shift+Enter", async () => {
    api.apiFetch.mockResolvedValue({ answer: "ok" });
    render(<AskChat />);
    const box = composer();
    await userEvent.type(box, "Spend in July{Shift>}{Enter}{/Shift}by app");
    expect(box).toHaveValue("Spend in July\nby app");
    expect(api.apiFetch).not.toHaveBeenCalled();

    await userEvent.type(box, "{Enter}");
    expect(api.apiFetch).toHaveBeenCalledWith("/ask", {
      method: "POST",
      body: JSON.stringify({ question: "Spend in July\nby app" }),
    });
    expect(box).toHaveValue("");
  });

  it("does not send an empty or whitespace-only question", async () => {
    render(<AskChat />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.type(composer(), "   {Enter}");
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("caps questions at 1000 characters and counts them", async () => {
    render(<AskChat />);
    const box = composer();
    expect(box).toHaveAttribute("maxlength", "1000");
    fireEvent.change(box, { target: { value: "a".repeat(12) } });
    expect(screen.getByText("12/1000")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "a".repeat(1000) } });
    expect(screen.getByText("1000/1000").className).toContain("text-danger");
  });

  it("hides the tab bar while typing and brings it back on blur", () => {
    render(<AskChat />);
    const hidden = () => Array.from(document.querySelectorAll("style")).some((s) => s.textContent?.includes('nav[aria-label="Main"]'));
    expect(hidden()).toBe(false);
    fireEvent.focus(composer());
    expect(hidden()).toBe(true);
    fireEvent.blur(composer());
    expect(hidden()).toBe(false);
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

  it("keeps the conversation out of browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    api.apiFetch.mockResolvedValue({ answer: "ok" });
    render(<AskChat />);
    await userEvent.click(screen.getByRole("button", { name: "Top restaurants" }));
    await screen.findByText("ok");
    expect(setItem).not.toHaveBeenCalled();
  });
});
