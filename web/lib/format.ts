// Number and date formatting shared by every screen. Pure functions, no Intl/locale dependence,
// so the output is identical in Node (tests, server render) and on the phone.

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;

/** Indian digit grouping of a non-negative integer string: 1234567 -> "12,34,567". */
function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${rest},${last3}`;
}

/**
 * Rupees with Indian grouping: whole rupees from ₹100 up ("₹1,23,456"); below ₹100 two decimals
 * only when there are paise ("₹49.50", but "₹90", never "₹90.00"). Zero is "₹0" (as in the
 * approved mockup); refunds are "-₹1,500"; NaN/Infinity is "—".
 */
export function formatINR(amount: number): string {
  if (!Number.isFinite(amount)) return "—";
  const cents = Math.round(Math.abs(amount) * 100);
  if (cents === 0) return "₹0";
  const sign = amount < 0 ? "-" : "";
  if (cents >= 10000) {
    const rupees = Math.round(cents / 100);
    return `${sign}₹${groupIndian(String(rupees))}`;
  }
  const rupees = Math.floor(cents / 100);
  if (cents % 100 === 0) return `${sign}₹${rupees}`;
  const paise = String(cents % 100).padStart(2, "0");
  return `${sign}₹${rupees}.${paise}`;
}

/** "2026-07" (or "2026-07-01") -> "July 2026". Anything else comes back unchanged. */
export function monthLabel(month: string): string {
  const m = /^(\d{4})-(\d{2})(?:-\d{2})?$/.exec(month);
  if (!m) return month;
  const name = MONTHS[Number(m[2]) - 1];
  return name ? `${name} ${m[1]}` : month;
}

/** Percentage change from prev to cur; null when either is missing or prev is zero. */
export function pctChange(
  cur: number | null | undefined,
  prev: number | null | undefined,
): number | null {
  if (cur == null || prev == null || !Number.isFinite(cur) || !Number.isFinite(prev) || prev === 0) {
    return null;
  }
  return ((cur - prev) / Math.abs(prev)) * 100;
}
