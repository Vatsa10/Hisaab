import type { Metadata } from "next";
import Link from "next/link";
import { LoginForm } from "@/components/auth/LoginForm";
import { BrandMark } from "@/components/ui/BrandMark";
import { authErrorCode, authErrorMessage } from "@/lib/authErrors";
import { hasSupabaseEnv } from "@/lib/env";
import { safeNextPath } from "@/lib/routes";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string; email?: string; deleted?: string }>;
}) {
  const { next, error, email, deleted } = await searchParams;
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
        <LoginForm next={safeNextPath(next)} initialError={error && !authErrorCode(error) ? authErrorMessage(error, email) : undefined}
          initialNotice={
            error && authErrorCode(error)
              ? authErrorMessage(error, email)
              : deleted === "1"
                ? "Your account and all its data are deleted."
                : undefined
          }
        />
      ) : (
        <p role="alert" className="mt-8 rounded-card bg-up-soft p-4 text-label text-up">
          Sign-in isn&rsquo;t configured on this server. Set NEXT_PUBLIC_SUPABASE_URL and
          NEXT_PUBLIC_SUPABASE_ANON_KEY (see web/README.md) and rebuild.
        </p>
      )}

      <p className="mt-auto pt-8 text-center">
        <Link
          href="/privacy"
          className="inline-flex min-h-11 items-center px-3 text-label font-medium text-ink-muted underline-offset-4 hover:underline"
        >
          Privacy
        </Link>
      </p>
    </main>
  );
}
