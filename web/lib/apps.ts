// The delivery apps a user can connect, in the order the Accounts screen lists them.
// Instamart is not here: its orders arrive through the Swiggy connection.

export type SiteId = "zepto" | "blinkit" | "swiggy" | "zomato";

export interface AppInfo {
  id: SiteId;
  name: string;
  /** Tailwind background class for the app's colour dot / logo tile. */
  dotClass: string;
}

export const APPS: readonly AppInfo[] = [
  { id: "zepto", name: "Zepto", dotClass: "bg-app-zepto" },
  { id: "blinkit", name: "Blinkit", dotClass: "bg-app-blinkit" },
  { id: "swiggy", name: "Swiggy", dotClass: "bg-app-swiggy" },
  { id: "zomato", name: "Zomato", dotClass: "bg-app-zomato" },
];
