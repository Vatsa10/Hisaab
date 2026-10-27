import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const config = [
  { ignores: [".next/**", "node_modules/**", "coverage/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    files: ["public/sw.js"],
    languageOptions: {
      sourceType: "script",
      globals: { self: "readonly", caches: "readonly", fetch: "readonly", Response: "readonly", URL: "readonly" },
    },
  },
];

export default config;
