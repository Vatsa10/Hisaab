import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { ServiceWorkerRegistrar } from "@/components/ServiceWorkerRegistrar";
import { STARTUP_SCREENS, startupImageFile, startupImageMedia } from "@/lib/startup-images.mjs";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Hisaab", template: "%s · Hisaab" },
  description: "Your Blinkit, Zepto, Swiggy and Zomato spending in one place.",
  applicationName: "Hisaab",
  appleWebApp: {
    capable: true,
    title: "Hisaab",
    statusBarStyle: "default",
    // Launch images in the brand background, so iOS doesn't flash white while starting.
    startupImage: STARTUP_SCREENS.map((s) => ({
      url: `/splash/${startupImageFile(s)}`,
      media: startupImageMedia(s),
    })),
  },
  formatDetection: { telephone: false, email: false, address: false },
  icons: {
    icon: [{ url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" }],
    apple: [{ url: "/icons/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  // Next 15 emits only `mobile-web-app-capable`; older iOS still reads the Apple name.
  other: { "apple-mobile-web-app-capable": "yes" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  // Matches the page background so Safari's bars blend in; the manifest carries the brand green.
  themeColor: "#f6f7f9",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en-IN">
      <body>
        {children}
        <ServiceWorkerRegistrar />
      </body>
    </html>
  );
}
