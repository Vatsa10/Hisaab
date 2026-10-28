import type { Metadata } from "next";
import { AdminScreen } from "@/components/admin/AdminScreen";

export const metadata: Metadata = { title: "Admin" };

/**
 * Admin view. All data comes from the API's /admin/* routes, which check that the caller is an
 * admin (403 "admin only" otherwise); the page itself holds no privileged key or data.
 */
export default function AdminPage() {
  return <AdminScreen />;
}
