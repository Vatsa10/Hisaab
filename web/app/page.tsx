import { redirect } from "next/navigation";
import { HOME_PATH } from "@/lib/routes";

export default function Root() {
  redirect(HOME_PATH);
}
