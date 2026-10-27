"use client";

import { useEffect, useState } from "react";

/** Below this, a viewport difference is browser chrome settling, not a keyboard. */
const MIN_KEYBOARD_PX = 80;

/**
 * How many pixels of the layout viewport the on-screen keyboard covers, from `visualViewport`.
 * iOS Safari does not shrink the layout viewport for the keyboard, so a `position: fixed`
 * bottom bar would sit behind it; lifting the bar by this inset keeps it just above the keys.
 * Returns 0 without a keyboard, while pinch-zoomed, or where visualViewport is missing.
 */
export function useKeyboardInset(): number {
  const [inset, setInset] = useState(0);

  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => {
      if (vv.scale > 1.01) {
        setInset(0);
        return;
      }
      const covered = Math.round(window.innerHeight - vv.height - vv.offsetTop);
      setInset(covered >= MIN_KEYBOARD_PX ? covered : 0);
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);

  return inset;
}
