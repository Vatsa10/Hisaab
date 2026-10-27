import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no PointerEvent, so fireEvent.pointer* would drop clientY/pointerId. Real
// browsers have it; this shim only makes the test DOM carry the same fields.
if (typeof window !== "undefined" && !("PointerEvent" in window)) {
  class PointerEventShim extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
      this.pointerType = init.pointerType ?? "touch";
    }
  }
  (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventShim;
}

afterEach(() => {
  cleanup();
});
