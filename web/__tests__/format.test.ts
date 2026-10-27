import { describe, expect, it } from "vitest";
import { formatINR, monthLabel, pctChange } from "@/lib/format";

describe("formatINR", () => {
  it("groups digits the Indian way with no decimals from ₹100 up", () => {
    expect(formatINR(123456)).toBe("₹1,23,456");
    expect(formatINR(100)).toBe("₹100");
    expect(formatINR(1102)).toBe("₹1,102");
    expect(formatINR(10000000)).toBe("₹1,00,00,000");
  });

  it("rounds amounts of ₹100 and more to whole rupees", () => {
    expect(formatINR(1234567.6)).toBe("₹12,34,568");
    expect(formatINR(5113.4)).toBe("₹5,113");
  });

  it("shows two decimals below ₹100", () => {
    expect(formatINR(49.5)).toBe("₹49.50");
    expect(formatINR(7)).toBe("₹7.00");
    expect(formatINR(0.05)).toBe("₹0.05");
  });

  it("does not show ₹100.00 for amounts that round up to 100", () => {
    expect(formatINR(99.996)).toBe("₹100");
  });

  it("shows a zero total as ₹0, as in the approved mockup", () => {
    expect(formatINR(0)).toBe("₹0");
  });

  it("puts the minus sign before the rupee sign for refunds", () => {
    expect(formatINR(-1500)).toBe("-₹1,500");
    expect(formatINR(-49.5)).toBe("-₹49.50");
  });

  it("never prints NaN or Infinity", () => {
    expect(formatINR(Number.NaN)).toBe("—");
    expect(formatINR(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("monthLabel", () => {
  it("names the month and year", () => {
    expect(monthLabel("2026-07")).toBe("July 2026");
    expect(monthLabel("2026-01")).toBe("January 2026");
    expect(monthLabel("2025-12")).toBe("December 2025");
  });

  it("accepts a full ISO date and uses its month", () => {
    expect(monthLabel("2026-10-01")).toBe("October 2026");
  });

  it("returns the input unchanged when it is not a month", () => {
    expect(monthLabel("2026-13")).toBe("2026-13");
    expect(monthLabel("soon")).toBe("soon");
  });
});

describe("pctChange", () => {
  it("is null when the previous value is zero or missing", () => {
    expect(pctChange(0, 0)).toBeNull();
    expect(pctChange(500, 0)).toBeNull();
    expect(pctChange(500, null)).toBeNull();
    expect(pctChange(500, undefined)).toBeNull();
    expect(pctChange(500, Number.NaN)).toBeNull();
  });

  it("is null when the current value is missing", () => {
    expect(pctChange(null, 500)).toBeNull();
  });

  it("returns the percentage change from the previous value", () => {
    expect(pctChange(150, 100)).toBe(50);
    expect(pctChange(1102, 2504)).toBeCloseTo(-55.99, 2);
    expect(pctChange(0, 400)).toBe(-100);
  });
});
