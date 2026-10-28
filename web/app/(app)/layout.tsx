import type { ReactNode } from "react";
import { ReloginBanner } from "@/components/account/ReloginBanner";
import { TabBar } from "@/components/ui/TabBar";

/**
 * Signed-in shell: one centred phone-width column on the grey canvas, padded for the notch and
 * for the fixed tab bar (its height plus the home-indicator inset) so nothing hides behind it.
 * The middleware has already sent signed-out users to /login.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <main
        id="main"
        className="px-safe mx-auto w-full max-w-[480px] pb-[calc(theme(spacing.tabbar)+env(safe-area-inset-bottom)+1.5rem)] pt-[calc(env(safe-area-inset-top)+1rem)]"
      >
        <ReloginBanner />
        {children}
      </main>
      <TabBar />
    </>
  );
}
