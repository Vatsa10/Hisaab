import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { BrandMark } from "@/components/ui/BrandMark";
import { Button } from "@/components/ui/Button";
import { brandGlyphPathData } from "@/lib/brand-glyph.mjs";
import { Card } from "@/components/ui/Card";
import { Money } from "@/components/ui/Money";
import { Sheet } from "@/components/ui/Sheet";

describe("Money", () => {
  it("renders an Indian-grouped rupee amount with tabular digits", () => {
    render(<Money amount={123456} />);
    const el = screen.getByText("₹1,23,456");
    expect(el.className).toContain("tabular-nums");
  });
});

describe("Button", () => {
  it("is a 44px-minimum button that fires onClick", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Send OTP</Button>);
    const btn = screen.getByRole("button", { name: "Send OTP" });
    expect(btn).toHaveAttribute("type", "button");
    expect(btn.className).toContain("min-h-11");
    await userEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("is disabled and busy while loading", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Verify
      </Button>,
    );
    const btn = screen.getByRole("button", { name: /Verify/ });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
    await userEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("keeps full colour while busy; only a truly disabled button dims", () => {
    render(<Button loading>Verify</Button>);
    const cls = screen.getByRole("button", { name: /Verify/ }).className;
    const dim = cls.indexOf("disabled:opacity-50");
    const busy = cls.indexOf("aria-busy:opacity-100");
    expect(dim).toBeGreaterThan(-1);
    expect(busy).toBeGreaterThan(dim);
    expect(cls).toContain("aria-busy:cursor-progress");
  });
});

describe("BrandMark", () => {
  it("draws the same rupee glyph paths the icon script rasterises", () => {
    const { container } = render(<BrandMark />);
    const d = container.querySelector("svg path")?.getAttribute("d");
    expect(d).toBe(brandGlyphPathData(100));
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("is the only glyph source: the icon script imports it", () => {
    const script = readFileSync(resolve(process.cwd(), "scripts/make-icons.mjs"), "utf-8");
    expect(script).toMatch(/from "\.\.\/lib\/brand-glyph\.mjs"/);
  });
});

describe("Card", () => {
  it("renders its children in the requested element", () => {
    render(
      <Card as="section" aria-label="This month">
        hello
      </Card>,
    );
    expect(screen.getByRole("region", { name: "This month" })).toHaveTextContent("hello");
  });
});

describe("Sheet", () => {
  it("renders nothing while closed", () => {
    render(
      <Sheet open={false} onClose={() => {}} title="Before you connect">
        body
      </Sheet>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is a labelled modal dialog that takes focus", () => {
    render(
      <Sheet open onClose={() => {}} title="Before you connect">
        <p>Consent text</p>
      </Sheet>,
    );
    const dialog = screen.getByRole("dialog", { name: "Before you connect" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  describe("closing", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    function renderOpen(onClose = vi.fn()) {
      render(
        <Sheet open onClose={onClose} title="Before you connect">
          body
        </Sheet>,
      );
      return onClose;
    }

    const handle = () => document.querySelector<HTMLElement>("[data-sheet-handle]")!;

    it("plays the 200ms exit animation, then calls onClose (close button)", () => {
      const onClose = renderOpen();
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(screen.getByRole("dialog")).toHaveAttribute("data-state", "closing");
      act(() => vi.advanceTimersByTime(199));
      expect(onClose).not.toHaveBeenCalled();
      act(() => vi.advanceTimersByTime(1));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("still closes on Escape, after the exit animation", () => {
      const onClose = renderOpen();
      fireEvent.keyDown(document, { key: "Escape" });
      expect(onClose).not.toHaveBeenCalled();
      act(() => vi.advanceTimersByTime(200));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("calls onClose once even when closed twice during the exit", () => {
      const onClose = renderOpen();
      fireEvent.keyDown(document, { key: "Escape" });
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      act(() => vi.advanceTimersByTime(400));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("closes at once when reduced motion is preferred", () => {
      vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), media: q }));
      const onClose = renderOpen();
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("dismisses when the handle is dragged down past 80px", () => {
      const onClose = renderOpen();
      fireEvent.pointerDown(handle(), { clientY: 100, pointerId: 1 });
      act(() => vi.advanceTimersByTime(400));
      fireEvent.pointerMove(handle(), { clientY: 200, pointerId: 1 });
      fireEvent.pointerUp(handle(), { clientY: 200, pointerId: 1 });
      act(() => vi.advanceTimersByTime(200));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("springs back from a short, slow drag", () => {
      const onClose = renderOpen();
      fireEvent.pointerDown(handle(), { clientY: 100, pointerId: 1 });
      act(() => vi.advanceTimersByTime(400));
      fireEvent.pointerMove(handle(), { clientY: 150, pointerId: 1 });
      fireEvent.pointerUp(handle(), { clientY: 150, pointerId: 1 });
      act(() => vi.advanceTimersByTime(400));
      expect(onClose).not.toHaveBeenCalled();
      expect(screen.getByRole("dialog")).toHaveAttribute("data-state", "open");
    });

    it("dismisses on a fast downward flick under the distance threshold", () => {
      const onClose = renderOpen();
      fireEvent.pointerDown(handle(), { clientY: 100, pointerId: 1 });
      act(() => vi.advanceTimersByTime(50));
      fireEvent.pointerMove(handle(), { clientY: 140, pointerId: 1 });
      fireEvent.pointerUp(handle(), { clientY: 140, pointerId: 1 });
      act(() => vi.advanceTimersByTime(200));
      expect(onClose).toHaveBeenCalledOnce();
    });
  });
});
