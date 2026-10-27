import { describe, expect, it } from "vitest";
import { HOME_PATH, isPublicPath, safeNextPath } from "@/lib/routes";

describe("isPublicPath", () => {
  it("lets the login page and auth callback through", () => {
    expect(isPublicPath("/login")).toBe(true);
    expect(isPublicPath("/auth/callback")).toBe(true);
  });

  it("protects every app route and anything unknown", () => {
    for (const p of ["/", "/spend", "/orders", "/orders/swiggy/1", "/cart", "/ask", "/accounts", "/loginx"]) {
      expect(isPublicPath(p)).toBe(false);
    }
  });
});

describe("safeNextPath", () => {
  it("keeps a same-site path", () => {
    expect(safeNextPath("/orders")).toBe("/orders");
    expect(safeNextPath("/orders/swiggy/1?x=1")).toBe("/orders/swiggy/1?x=1");
  });

  it("falls back to home for anything that could leave the site", () => {
    for (const bad of [null, undefined, "", "orders", "//evil.example", "/\\evil.example", "https://evil.example", "/login"]) {
      expect(safeNextPath(bad)).toBe(HOME_PATH);
    }
  });

  it("rejects paths that URL parsers turn into another host (tab, CR, LF stripped)", () => {
    for (const bad of ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/%09/evil.example"]) {
      expect(safeNextPath(bad), JSON.stringify(bad)).toBe(HOME_PATH);
    }
    // what a browser would do with the first one, for the record
    expect(new URL("/\t/evil.example", "https://app.example").host).toBe("evil.example");
  });

  it("rejects any whitespace or control character and encoded slashes or backslashes", () => {
    for (const bad of ["/orders x", "/orders\u0000", "/orders\u007f", "/%2F/evil.example", "/%5Cevil.example", "/%0a/evil.example"]) {
      expect(safeNextPath(bad), JSON.stringify(bad)).toBe(HOME_PATH);
    }
  });

  it("returns the normalised same-origin path, query and hash", () => {
    expect(safeNextPath("/orders?month=2026-07#top")).toBe("/orders?month=2026-07#top");
    expect(safeNextPath("/orders/../ask")).toBe("/ask");
  });

  it("uses /spend as home", () => {
    expect(HOME_PATH).toBe("/spend");
  });
});
