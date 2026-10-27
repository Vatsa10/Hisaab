"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface Tab {
  href: string;
  label: string;
  icon: ReactNode;
}

const ICON_PROPS = {
  width: 24,
  height: 24,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.75,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

/** The five top-level screens, in the order the mockups fix. */
export const TABS: readonly Tab[] = [
  {
    href: "/spend",
    label: "Spend",
    icon: (
      <svg {...ICON_PROPS}>
        <path d="M5 20v-7M12 20V5M19 20v-10" />
      </svg>
    ),
  },
  {
    href: "/orders",
    label: "Orders",
    icon: (
      <svg {...ICON_PROPS}>
        <path d="M6 3.5h12v17l-3-1.75-3 1.75-3-1.75-3 1.75z" />
        <path d="M9.5 8.5h5M9.5 12.5h5" />
      </svg>
    ),
  },
  {
    href: "/cart",
    label: "Cart",
    icon: (
      <svg {...ICON_PROPS}>
        <path d="M3 4h2.2l2.3 11.1a1 1 0 0 0 1 .8h8.9a1 1 0 0 0 1-.78L20 8H6.1" />
        <circle cx="9.5" cy="19.5" r="1.25" />
        <circle cx="17" cy="19.5" r="1.25" />
      </svg>
    ),
  },
  {
    href: "/ask",
    label: "Ask",
    icon: (
      <svg {...ICON_PROPS}>
        <path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4.2A7.5 7.5 0 1 1 20 11.5z" />
      </svg>
    ),
  },
  {
    href: "/accounts",
    label: "Accounts",
    icon: (
      <svg {...ICON_PROPS}>
        <circle cx="12" cy="8.5" r="3.5" />
        <path d="M5 20a7 7 0 0 1 14 0" />
      </svg>
    ),
  },
];

export function isTabActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Fixed bottom tab bar (iOS style). Each tab fills its share of the width and is at least
 * 56px tall; the bar pads itself by the home-indicator safe area.
 */
export function TabBar() {
  const pathname = usePathname() ?? "";
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface pb-safe"
    >
      <ul className="mx-auto flex max-w-[480px]">
        {TABS.map((tab) => {
          const active = isTabActive(pathname, tab.href);
          return (
            <li key={tab.href} className="flex-1">
              <Link
                href={tab.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex min-h-tabbar flex-col items-center justify-center gap-0.5 rounded-control pt-1",
                  "text-caption transition-colors duration-150 active:opacity-60",
                  active ? "font-semibold text-accent" : "font-medium text-ink-muted hover:text-ink",
                )}
              >
                {tab.icon}
                <span>{tab.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
