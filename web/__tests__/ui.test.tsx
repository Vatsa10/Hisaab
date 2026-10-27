import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button } from "@/components/ui/Button";
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

  it("closes on Escape and on the close button", async () => {
    const onClose = vi.fn();
    render(
      <Sheet open onClose={onClose} title="Before you connect">
        body
      </Sheet>,
    );
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
