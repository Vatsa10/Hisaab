"use client";

import Link from "next/link";
import type { MouseEvent } from "react";

/** Goes back in history when there is a page to go back to; the plain link (/login) is the fallback. */
export function BackLink() {
  function onClick(e: MouseEvent<HTMLAnchorElement>) {
    if (window.history.length > 1) {
      e.preventDefault();
      window.history.back();
    }
  }
  return (
    <Link
      href="/login"
      onClick={onClick}
      className="-ml-3 inline-flex min-h-11 items-center gap-1 rounded-control px-3 text-body font-medium text-accent active:bg-accent/10"
    >
      <span aria-hidden="true">‹</span> Back
    </Link>
  );
}
