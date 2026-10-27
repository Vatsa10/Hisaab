import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import { formatINR } from "@/lib/format";

export interface MoneyProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  amount: number;
}

/** A rupee amount with Indian grouping and tabular digits, so columns of amounts line up. */
export function Money({ amount, className, ...rest }: MoneyProps) {
  return (
    <span className={cn("tabular-nums", className)} {...rest}>
      {formatINR(amount)}
    </span>
  );
}
