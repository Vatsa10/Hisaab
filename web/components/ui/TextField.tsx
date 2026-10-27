import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label: string;
  /** Persistent help under the field. */
  hint?: ReactNode;
  /** Error under the field; also marks the input invalid. */
  error?: ReactNode;
  /** Element inside the field, after the input (e.g. a show-password button). */
  trailing?: ReactNode;
}

/**
 * Label above, 48px input, hint and error below (announced). 16px text so iOS does not zoom
 * on focus. Pass `inputMode` / `autoComplete` for the right keyboard and autofill.
 */
export function TextField({ label, hint, error, trailing, id, className, ...input }: TextFieldProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ") || undefined;

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={inputId} className="text-label font-medium text-ink">
        {label}
      </label>
      <div
        className={cn(
          "flex min-h-12 items-center rounded-field border bg-surface transition-[border-color,box-shadow] duration-150",
          "focus-within:ring-2",
          // An invalid field stays red while focused, so the error is not hidden by the focus ring.
          error
            ? "border-danger focus-within:ring-danger/20"
            : "border-line-field focus-within:border-accent focus-within:ring-accent/20",
        )}
      >
        <input
          id={inputId}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          className="h-12 min-w-0 flex-1 rounded-field bg-transparent px-3.5 text-field text-ink placeholder:text-ink-muted/80 focus:outline-none focus-visible:outline-none"
          {...input}
        />
        {trailing}
      </div>
      {error ? (
        <p id={errorId} role="alert" className="text-label text-danger">
          {error}
        </p>
      ) : null}
      {hint ? (
        <p id={hintId} className="text-label text-ink-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
