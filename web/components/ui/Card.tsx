import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export interface CardProps extends HTMLAttributes<HTMLElement> {
  /** Element to render; use "section" with aria-label for a named region, "li" inside a list. */
  as?: "div" | "section" | "article" | "li";
  /** Inner padding. "none" for cards whose rows bring their own padding. */
  padding?: "md" | "none";
}

/** White, 14px-radius surface on the grey canvas (the mockups' basic building block). */
export function Card({ as: Tag = "div", padding = "md", className, ...rest }: CardProps) {
  return (
    <Tag
      className={cn("rounded-card bg-surface shadow-card", padding === "md" && "p-4", className)}
      {...rest}
    />
  );
}
