import type { NextRequest } from "next/server";
import { updateSession } from "@/lib/supabase/middleware";

export async function middleware(request: NextRequest) {
  return updateSession(request);
}

export const config = {
  // Everything except static files, the PWA manifest/worker and images. /privacy is public: it is
  // allowed through by isPublicPath (lib/routes.ts), not excluded here, so the session still refreshes.
  matcher: [
    "/((?!_next/static|_next/image|icons/|sw\\.js|manifest\\.webmanifest|favicon\\.ico|robots\\.txt|.*\\.(?:png|jpg|jpeg|svg|webp|ico)$).*)",
  ],
};
