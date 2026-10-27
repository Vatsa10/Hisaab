# Expense Agent PWA (`web/`)

The iPhone-first Progressive Web App: Next.js 15 (App Router) + TypeScript, Tailwind CSS 3.4,
Supabase Auth via `@supabase/ssr`, Vitest + Testing Library. A standalone pnpm project; the
Python service lives at the repo root.

## Setup

```bash
pnpm --dir web install
```

Create `web/.env.local` (gitignored; `scripts/dev_supabase.sh` writes it for local Supabase):

```bash
NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon key from `npx supabase status`>
NEXT_PUBLIC_API_URL=http://localhost:8000
```

These three public values are the only configuration the PWA uses. The service-role key must
never go in any `NEXT_PUBLIC_*` variable or anywhere in `web/`.

## Scripts

| Command | What it does |
| --- | --- |
| `pnpm --dir web dev` | Dev server on :3000 (no service worker in dev) |
| `pnpm --dir web test` | Unit and component tests (no Supabase needed) |
| `pnpm --dir web lint` | ESLint (`next/core-web-vitals` + `next/typescript`), zero warnings |
| `pnpm --dir web typecheck` | `tsc --noEmit` |
| `pnpm --dir web build` | Production build |
| `pnpm --dir web start` | Serve the production build on :3000 |
| `pnpm --dir web icons` | Regenerate `public/icons/*.png` and the iOS launch images `public/splash/*.png` from `lib/brand-glyph.mjs` (dependency-free) |

### Building without Supabase

`pnpm build` succeeds with no env at all: every value is read when first used, not at import.
Next inlines `NEXT_PUBLIC_*` into the bundle **at build time**, though, so a build made without
them cannot sign anyone in (the login page says so, and app routes redirect there). For a
build you will actually run, set the three values first. For a build that only needs to render
(CI, screenshots), placeholders are fine:

```bash
NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 NEXT_PUBLIC_SUPABASE_ANON_KEY=placeholder \
NEXT_PUBLIC_API_URL=http://localhost:8000 pnpm --dir web build
```

## How it fits together

- **Auth**: email/password sign-up and sign-in, plus "Continue with Google"
  (`signInWithOAuth({ provider: "google" })`, works once Google is enabled in Supabase).
  OAuth and email-confirmation links land on `/auth/callback`, which exchanges the code for a
  session cookie and redirects to a same-site `next` path only.
- **`middleware.ts`** refreshes the Supabase session on every request. Every route except
  `/login` and `/auth/*` needs a user; signed-out visitors go to `/login?next=<path>`.
- **Data**: screens read the user's own rows with `getSupabaseBrowserClient()` /
  `getSupabaseServerClient()` (anon key + RLS). Actions call the FastAPI service through
  `apiFetch(path, init)` (`lib/api.ts`), which adds `Authorization: Bearer <access token>` and
  throws `ApiError(status, detail)`; status `0` means the server was unreachable.
- **PWA**: `app/manifest.ts` (name "Expense Agent", short_name "Spend", standalone, theme
  `#0a7c55`, background `#f6f7f9`), iOS meta tags, apple-touch-icon and
  `apple-touch-startup-image` launch images (iPhone 12 mini to 15 Pro Max, portrait, listed in
  `lib/startup-images.mjs`) in `app/layout.tsx`, and `public/sw.js`, registered in production
  only. The worker handles same-origin navigations (network first, last good copy offline, at
  most 30 pages, oldest dropped) and `/_next/static` + `/icons` (cache first). It never
  intercepts or caches the API, Supabase, non-GET requests or `/auth/*`. Cached pages are
  dropped on every sign-in (password: `lib/pwa.ts#clearPageCache`; Google / email link: the
  worker on `/auth/callback`) and on sign-out, so one account's pages never show to another.
- **Redirects**: `?next=` is only honoured through `lib/routes.ts#safeNextPath`, which resolves
  it against a sentinel origin, requires that origin, rejects whitespace/control characters
  (raw or percent-encoded), backslashes and encoded `//`, and returns the normalised
  path + query + hash.
- **Privacy**: phone numbers and OTPs are never written to localStorage, sessionStorage,
  IndexedDB or the Cache API, and nothing logs cookies, tokens, OTPs or phone numbers.

## Design system

Tokens live in `app/globals.css` (CSS variables as RGB channels) and `tailwind.config.ts`;
build screens from these, not raw hex values.

| Token | Value | Use |
| --- | --- | --- |
| `bg-canvas` | `#f6f7f9` | page background |
| `bg-surface` | `#ffffff` | cards, sheets, tab bar |
| `text-ink` / `text-ink-muted` | `#14161a` / `#5f6670` | text (muted is 5.4:1 on canvas) |
| `accent` (+ `-pressed`, `-soft`, `-strong`, `-bar`) | `#0a7c55` | primary actions, active tab, "down" changes |
| `up` / `up-soft` | `#c2410c` / `#fff7ed` | spending went up |
| `danger` / `danger-soft` | `#b42318` / `#fef3f2` | errors, sign out |
| `app-zepto` `app-blinkit` `app-swiggy` `app-instamart` `app-zomato` | brand colours | app dots and tiles |
| `rounded-card` / `rounded-control` / `rounded-field` / `rounded-tile` / `rounded-logo` / `rounded-t-sheet` | 14 / 12 / 10 / 8 / 16 / 20px | `tile` is concentric inside `control` with a 4px gap |
| `shadow-card`, `shadow-sheet` | tinted shadows | |
| `text-caption` `text-label` `text-body` `text-field` `text-headline` `text-title` `text-figure` | 11 / 13 / 15 / 16 / 17 (600) / 24 / 30px | `headline`: card and sheet titles, large buttons |
| `px-safe`, `pt-safe`, `pb-safe` | `env(safe-area-inset-*)` | notch and home indicator |

Hover styles apply only on devices that really hover (`future.hoverOnlyWhenSupported`), so
taps on iPhone never leave a stuck hover state.

Font: the system stack `-apple-system, "SF Pro Text", "Segoe UI", sans-serif` (SF Pro on iPhone).
Light theme only, as in the approved mockups.

Components (`components/ui/`), all typed and server-safe unless noted:

- `Card` (`as`, `padding`): white 14px-radius surface.
- `Button` (`variant` primary | secondary | ghost | danger, `size` md | lg, `fullWidth`,
  `loading`, `icon`) and `buttonClassName()` for button-styled `next/link`s. Always 44px+ tall.
  A loading button is disabled + `aria-busy` but keeps full colour; only truly disabled ones dim.
- `Money` (`amount`): `formatINR` with tabular digits.
- `TabBar` (client): Spend · Orders · Cart · Ask · Accounts, fixed bottom, safe-area padded,
  `aria-current="page"` on the active tab.
- `Sheet` (client; `open`, `onClose`, `title`, `description`, `footer`): bottom-sheet modal
  dialog with focus trap. Escape, backdrop, close button, or dragging the handle down (past
  80px or a quick flick) plays a 200ms slide-out and then calls `onClose` (immediately with
  reduced motion), so `onClose` should just set `open` to false.
- `BrandMark` (`size`): the green tile with the rupee glyph, drawn from `lib/brand-glyph.mjs`,
  the same geometry the icon script rasterises.
- `TextField` (`label`, `hint`, `error`, `trailing`): label above, 48px input at 16px, error
  announced below. Pass `inputMode="numeric"` + `autoComplete="one-time-code"` for OTPs.
- `PageHeader` (`title`, `eyebrow`, `subtitle`, `action`) and `EmptyState` (`title`, `icon`,
  `action`).

Helpers: `lib/format.ts` (`formatINR`, `monthLabel`, `pctChange`), `lib/apps.ts` (connectable
apps with names and colour classes), `lib/routes.ts`, `lib/cn.ts`.
