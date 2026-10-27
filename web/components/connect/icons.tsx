// Small inline icons for the connect flow, drawn like the tab bar's (24px grid, 1.75 stroke,
// round caps) so the product has one icon voice. All decorative: labels carry the meaning.

import type { SVGProps } from "react";

function Icon({ size = 20, children, ...rest }: SVGProps<SVGSVGElement> & { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

type P = SVGProps<SVGSVGElement> & { size?: number };

export const LockIcon = (p: P) => (
  <Icon {...p}>
    <rect x="5" y="10.5" width="14" height="10" rx="2.5" />
    <path d="M8.5 10.5V7.75a3.5 3.5 0 0 1 7 0v2.75" />
  </Icon>
);

export const RefreshIcon = (p: P) => (
  <Icon {...p}>
    <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" />
    <path d="M19.5 4.5v4h-4" />
  </Icon>
);

export const CheckIcon = (p: P) => (
  <Icon {...p}>
    <path d="M5.5 12.5l4 4 9-9" />
  </Icon>
);

export const PinIcon = (p: P) => (
  <Icon {...p}>
    <path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z" />
    <circle cx="12" cy="10" r="2.25" />
  </Icon>
);

export const AlertIcon = (p: P) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.75v5" />
    <path d="M12 16.25h.01" />
  </Icon>
);

export const ShieldIcon = (p: P) => (
  <Icon {...p}>
    <path d="M12 3.5l7 2.75v5.25c0 4.4-3 7.9-7 9-4-1.1-7-4.6-7-9V6.25z" />
    <path d="M9 12l2.25 2.25L15.5 10" />
  </Icon>
);

/** A 16px spinner for "Syncing…" (stops spinning under reduced motion; the label still says it). */
export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={className} width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
