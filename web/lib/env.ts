// Public configuration. Each value is read when it is first needed, not at import time, so
// tests and `next build` work without a running Supabase. Next inlines NEXT_PUBLIC_* into the
// browser bundle at build time, so each one must be read with its literal name.
// The PWA only ever uses these three; the service-role key never belongs here.

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`${name} is not set. Create web/.env.local as described in web/README.md.`);
  }
  return value;
}

export function supabaseUrl(): string {
  return required("NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL);
}

export function supabaseAnonKey(): string {
  return required("NEXT_PUBLIC_SUPABASE_ANON_KEY", process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY);
}

export function apiUrl(): string {
  return required("NEXT_PUBLIC_API_URL", process.env.NEXT_PUBLIC_API_URL);
}

/** True when both Supabase values are present (lets middleware degrade instead of crashing). */
export function hasSupabaseEnv(): boolean {
  return Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY);
}
