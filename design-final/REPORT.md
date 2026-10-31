# Hisaab phone design pass (390x844, busiest screens also 375x812)

Capture: `bash scripts/e2e_local.sh tests/zz-design-shots.spec.ts` (fake sites, local Supabase). 123 screenshots plus `audit.json`
(per screenshot: tap targets under 44px, contrast under AA, horizontal overflow). Compared against the mockups in
`.superpowers/brainstorm/24332-1793102961/content/` (tokens match: canvas #f6f7f9, accent #0a7c55, 14px cards, pill badges,
outlined best card, green low price).

Not captured this run (soft misses, flaky or not reachable on the fake stack): Ask answer/error (the answer turn timed out
on the final run; it was captured on the previous run, the error state's 502 route stub never showed the error row), the
Fill re-price sheet (captured on the previous run), the "logged you out" card from a worker restart, and a real
"split order" suggestion (fake prices never make another app cheaper, so only the "No item is cheaper on another app" line shows).

| Screen | Issue | Severity | Fixed? |
|---|---|---|---|
| All | No horizontal scroll at 390 or 375 on any captured screen | none | n/a |
| All | Contrast: no text under AA found by the audit (muted ink #5f6670 on white 5.9:1) | none | n/a |
| Cart item table | "Pick" button 36x44, under 44px wide | Low | Yes (min-w-11) |
| Cart usual sheet, fill question | Radio/checkbox inputs 16-20px; they sit inside full-width rows so the row is the tap target | Low | No (by design) |
| Ask, Sites, Fees, Same-restaurant sheet | Em dashes in copy ("busy — try again", "Unverified — check", "No, different — use") | Low | Yes (tests updated) |
| Push notifications | Service worker used `/icons/192.png` (404) as the icon | Medium | Yes (icon-192.png + badge-96.png, SW cache v3) |
| Full-page shots | The fixed tab bar shows mid-image in tall screenshots: a capture artefact, not an overlap bug; content has tab-bar padding | Info | n/a |
| Every app screen | There was no brand in the signed-in header | Low | Yes (28px mark + "Hisaab" row in the app layout) |
| Typography | System font stack | Medium | Yes (Bricolage Grotesque 600/700 for titles and rupee totals, Geist 400/500/600 for text; tabular figures; swap; OFL licences in public/fonts) |
| Admin Sites | "Speed 0.0s typical" on fake sites looks odd; real sites will show seconds | Low | No (fake data) |
| Admin Sites | Long list (about 4000px at 390): groups are clear but there is no jump nav | Low | No (bigger change) |
| Accounts | Two "Disconnect"/"Delete" actions live in one Your data card; destructive buttons are separated and confirmed by sheets | Info | n/a |
| Brand | The display font has no rupee sign in its Latin file, so the sign falls back to Geist inside totals; looks consistent in shots | Info | No |
| Scheduler | `server.scheduler --once` and the worker's usage write failed with UndefinedColumn in an earlier run while other agents had uncommitted server changes; not reproduced on the final runs | Medium | No (outside this task; recheck migration 0011 vs server/usage.py) |

## Icon and fonts
- Mark: tally-H (two uprights and one slash), white on accent. Geometry in `web/lib/brand-glyph.mjs`; `pnpm icons` writes
  `app/icon.svg`, `app/favicon.ico` (16/32/48), `public/icons/icon-192.png`, `icon-512.png`, `icon-512-maskable.png` (mark at 80%),
  `apple-touch-icon.png` (180), `badge-96.png` (white on transparent) and `badge.svg`, plus the iOS launch images.
- Wired into the manifest (any, maskable, monochrome), layout metadata and the service worker. Shown on login and in the app header.
