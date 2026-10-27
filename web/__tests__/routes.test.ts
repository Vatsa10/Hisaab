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

  it("uses /spend as home", () => {
    expect(HOME_PATH).toBe("/spend");
  });
});
