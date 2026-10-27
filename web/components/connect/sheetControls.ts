// Small helpers for driving the shared Sheet (components/ui/Sheet.tsx) from inside its content,
// without editing it: Sheet has no imperative API, so these act on the DOM it renders.

function dialogOf(el: Element | null | undefined): HTMLElement | null {
  return (el?.closest('[role="dialog"]') as HTMLElement | null) ?? null;
}

/**
 * Close the sheet `el` sits in the same way its X button does (exit animation, then onClose).
 * Returns false when there is no such button, so the caller can close it directly.
 */
export function closeSheetFrom(el: Element | null | undefined): boolean {
  const button = dialogOf(el)?.querySelector<HTMLButtonElement>('button[aria-label="Close"]');
  if (!button) return false;
  button.click();
  return true;
}

/**
 * After a step change, move focus to the sheet's title if focus fell out of the sheet (e.g. the
 * button that was pressed unmounted and focus dropped to <body>). Focus that is still inside the
 * sheet (an autofocused field, the panel itself) is left alone.
 */
export function focusSheetTitleIfLost(el: Element | null | undefined): void {
  const dialog = dialogOf(el);
  if (!dialog) return;
  const active = document.activeElement;
  if (active && active !== document.body && dialog.contains(active)) return;
  const title = dialog.querySelector<HTMLElement>("h2");
  if (!title) return;
  if (!title.hasAttribute("tabindex")) title.setAttribute("tabindex", "-1");
  // A landing spot for focus, not a control: no focus ring around the title text.
  title.style.outline = "none";
  title.focus({ preventScroll: true });
}
