import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Hisaab",
    short_name: "Hisaab",
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
      // Full-bleed artwork with the mark at 80% size, inside every launcher mask's safe zone.
      { src: "/icons/icon-512-maskable.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
      { src: "/icons/badge-96.png", sizes: "96x96", type: "image/png", purpose: "monochrome" },
    ],
  };
}
