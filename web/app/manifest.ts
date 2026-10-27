import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Expense Agent",
    short_name: "Spend",
    description: "Your Blinkit, Zepto, Swiggy and Zomato spending in one place.",
    id: "/",
    start_url: "/spend",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    theme_color: "#0a7c55",
    background_color: "#f6f7f9",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      // Full-bleed artwork with the glyph inside the safe zone, so it also works masked.
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
