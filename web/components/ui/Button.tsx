import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "md" | "lg";

export interface ButtonStyle {
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  className?: string;
}

const BASE =
  "inline-flex select-none items-center justify-center gap-2 rounded-control font-semibold " +
  "transition-[background-color,box-shadow,transform,opacity] duration-150 ease-out-quart " +
  "active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 " +
  // Busy (loading) buttons are disabled too, but keep full colour; only truly disabled ones dim.
  "aria-busy:pointer-events-auto aria-busy:cursor-progress aria-busy:opacity-100";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-white hover:bg-accent-pressed active:bg-accent-pressed",
  secondary: "bg-surface text-accent ring-1 ring-inset ring-accent hover:bg-accent-soft",
  ghost: "text-accent hover:bg-accent/5 active:bg-accent/10",
  danger: "bg-surface text-danger ring-1 ring-inset ring-line hover:bg-danger-soft",
};

// Every size keeps at least a 44px hit area.
const SIZES: Record<ButtonSize, string> = {
  md: "min-h-11 px-4 text-body",
  lg: "min-h-12 px-5 text-headline",
};

/** Button classes, for links (next/link) that should look like buttons. */
export function buttonClassName({
  variant = "primary",
  size = "md",
  fullWidth = false,
  className,
}: ButtonStyle = {}): string {
  return cn(BASE, VARIANTS[variant], SIZES[size], fullWidth && "w-full", className);
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, ButtonStyle {
  /** Disables the button and shows a spinner while an action runs. */
  loading?: boolean;
  /** Optional icon shown before the label (hidden while loading). */
  icon?: ReactNode;
}

export function Button({
  variant,
  size,
  fullWidth,
  className,
  loading = false,
  icon,
  disabled,
  type = "button",
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClassName({ variant, size, fullWidth, className })}
      {...rest}
    >
      {loading ? <Spinner /> : icon}
      {children}
    </button>
  );
}

function Spinner() {
  return (
    <svg className="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.3" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
