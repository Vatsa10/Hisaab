"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { CheckIcon } from "@/components/connect/icons";
import { cn } from "@/lib/cn";
import { GROCERY_SITES, SITE_DOTS, SITE_NAMES, type AccessMap, type GrocerySite } from "@/lib/cart";

export interface AppAccessRowProps {
  /** null while loading. */
  access: AccessMap | null;
  /** The status read failed: show the apps without a status (the server still checks). */
  failed?: boolean;
}

/** The connection a grocery app is priced through (Instamart comes with Swiggy). */
function connectLabel(site: GrocerySite): string {
  return site === "instamart" ? "Connect Swiggy to compare Instamart" : `Connect ${SITE_NAMES[site]} to compare`;
}

function Name({ site }: { site: GrocerySite }) {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", SITE_DOTS[site])} />
      <span className="truncate font-medium text-ink">{SITE_NAMES[site]}</span>
    </span>
  );
}

const CELL = "flex min-h-14 flex-col justify-center gap-0.5 rounded-control px-3 py-1.5";

/**
 * The three grocery apps with what each needs before it can be priced: "Connected", or a link
 * to Accounts that says "Connect" or "Log in again". Only connected apps are priced.
 */
export function AppAccessRow({ access, failed = false }: AppAccessRowProps) {
  return (
    <ul aria-label="Apps we compare" className="grid grid-cols-3 gap-1 p-1">
      {GROCERY_SITES.map((site) => {
        const status = access?.[site];
        let body: ReactNode;
        if (status === "not_connected" || status === "needs_login" || (access && !status)) {
          const relogin = status === "needs_login";
          return (
            <li key={site} className="min-w-0">
              <Link
                href="/accounts"
                aria-label={relogin ? `${SITE_NAMES[site]} logged you out. Log in again to compare.` : connectLabel(site)}
                className={cn(CELL, "transition-colors duration-150 hover:bg-canvas active:bg-line-soft")}
              >
                <Name site={site} />
                <span className={cn("flex items-center gap-0.5 text-label font-semibold", relogin ? "text-up" : "text-accent")}>
                  <span>{relogin ? "Log in again" : "Connect"}</span>
                  <span aria-hidden="true">›</span>
                </span>
              </Link>
            </li>
          );
        }
        if (status === "ok") {
          body = (
            <span className="flex items-center gap-1 text-label font-semibold text-accent">
              <CheckIcon size={14} />
              <span>Connected</span>
            </span>
          );
        } else if (status === "unavailable") {
          body = <span className="text-label text-ink-muted">Unavailable now</span>;
        } else if (failed) {
          body = <span className="text-label text-ink-muted">Status unknown</span>;
        } else {
          body = (
            <span aria-hidden="true" className="my-[3px] block h-3 w-16 animate-pulse rounded-full bg-line-soft" />
          );
        }
        return (
          <li key={site} className={cn(CELL, "min-w-0")} aria-busy={!access && !failed ? true : undefined}>
            <Name site={site} />
            {body}
          </li>
        );
      })}
    </ul>
  );
}
