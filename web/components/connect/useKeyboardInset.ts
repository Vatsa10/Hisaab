"use client";

import { useEffect } from "react";

/**
 * While mounted, keeps `--kb-inset` on <html> equal to the height the on-screen keyboard covers
 * (from visualViewport), so a bottom sheet can sit above the iOS keyboard instead of behind it.
 * The shared Sheet is not edited: the connect sheet adds `mb-[var(--kb-inset,0px)]` itself.
 */
export function useKeyboardInset(active = true): void {
  useEffect(() => {
    const vv = typeof window !== "undefined" ? window.visualViewport : null;
    const root = document.documentElement;
    if (!vv || !active) return;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        // Ignore toolbar wobble; a keyboard is far taller than 80px.
        root.style.setProperty("--kb-inset", covered > 80 ? `${Math.round(covered)}px` : "0px");
      });
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      cancelAnimationFrame(frame);
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      root.style.removeProperty("--kb-inset");
    };
  }, [active]);
}
