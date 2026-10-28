import type { Metadata } from "next";
import { LoginForm } from "@/components/auth/LoginForm";
import { BrandMark } from "@/components/ui/BrandMark";
import { authErrorMessage } from "@/lib/authErrors";
import { hasSupabaseEnv } from "@/lib/env";
import { safeNextPath } from "@/lib/routes";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string; email?: string }>;
}) {
  const { next, error, email } = await searchParams;
  const configured = hasSupabaseEnv();

  return (
    <main className="px-safe mx-auto flex min-h-[100dvh] w-full max-w-[420px] flex-col pb-[calc(env(safe-area-inset-bottom)+1.5rem)] pt-[calc(env(safe-area-inset-top)+3rem)]">
      <BrandMark size={56} />
      <p className="mt-5 text-label font-semibold text-accent">Hisaab</p>
      <h1 className="text-title">Your food and grocery spending, sorted</h1>
      <p className="mt-1 text-body text-ink-muted">
        Your Blinkit, Zepto, Swiggy and Zomato spending in one place. Read-only: we never order or pay.
      </p>

      {configured ? (
        <LoginForm next={safeNextPath(next)} initialError={error ? authErrorMessage(error, email) : undefined} />
      ) : (
        <p role="alert" className="mt-8 rounded-card bg-up-soft p-4 text-label text-up">
          Sign-in isn&rsquo;t configured on this server. Set NEXT_PUBLIC_SUPABASE_URL and
          NEXT_PUBLIC_SUPABASE_ANON_KEY (see web/README.md) and rebuild.
        </p>
      )}
    </main>
  );
}
