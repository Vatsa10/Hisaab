import { apiFetch } from "@/lib/api";

export interface Me {
  user_id: string;
  email: string;
  is_admin: boolean;
}

export interface Limits {
  refresh_left: Record<string, number>;
  refresh_limit: number;
  cart_checks_left: number;
  cart_checks_limit: number;
  budget_paused: boolean;
  resets_at: string;
}

export const getMe = () => apiFetch<Me>("/me");
export const getLimits = () => apiFetch<Limits>("/me/limits");
