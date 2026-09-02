# WANTED — web

Owner: web executors. Next.js on Vercel. No code before CONTRACTS.md v1 + Phase 5 brief
(scaffold may start during 0a wait).

The public site for WANTED: an AI character called **the agent** plays a famous open-world story
mode 24/7 on stream. This app renders the live feed, HUD, counters, missions board, and clip
gallery from Supabase rows — and renders honest offline/empty states when there is nothing to
show. It never fabricates data.

## Stack

- Next.js 15.5 (App Router, TypeScript, Tailwind v4), npm
- `@supabase/supabase-js` + `@supabase/ssr` — server components fetch initial rows with the
  publishable/anon key (no-store); a client component subscribes to Realtime
  `postgres_changes` (INSERT on `decisions`/`events`, UPDATE on `stats`)
- All rendering derives from **rows**. Realtime messages only trigger a refetch keyed on the
  last-seen id (also done on every re-`SUBSCRIBED`, since postgres_changes has no gap-fill), so
  a later migration to coalesced broadcast digests needs no UI change (CONTRACTS §5, D6).

## Pages

| Route | Contents |
|---|---|
| `/` | Stream embed (Twitch/YouTube from the `site_config` "stream" row, env fallback), commentary feed (newest-top, expandable thoughts, pause-on-scroll), HUD, counters, current goal, brain bill, offline banner |
| `/missions` | Mission board + overall story progress from `missions` rows |
| `/clips` | Clip gallery from `clips` rows (public bucket URLs, share links, newest first); `/clips/[id]` has a `next/og` OG image |
| `/agent` | Who the agent is: three-layer brain, no-cheats policy, cost transparency, AI disclosure |
| `/api/health` | `{ ok, supabase_configured }` — honest, no-store |

The offline banner is driven **only** by `stats.heartbeat_at` staleness (> 60 s or absent) —
never inferred from feed silence.

## Environment

Copy `.env.example` → `.env.local`. Accepted names (first wins):
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, then `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
`STREAM_PROVIDER` / `STREAM_CHANNEL` are the server-only fallback when the `site_config`
"stream" row is absent. No secret/service-role key is ever used in this app.

`NEXT_PUBLIC_SITE_URL` is the canonical origin behind `metadataBase` and every absolute share-card
URL. When it is unset the app falls back to Vercel's `VERCEL_PROJECT_PRODUCTION_URL` system env var
and only then to `http://localhost:3000` — without that fallback an unset value silently publishes
`og:image` URLs pointing at localhost from production.

## Share metadata

Next.js **replaces** `openGraph` and `alternates` per route segment rather than merging them: a
route that declares neither inherits its ancestor's values verbatim, and a route that declares one
loses everything the ancestor put in it (including a file-convention `og:image`). Declaring
`openGraph.url` / `alternates.canonical` in the root layout therefore makes every other route
advertise the homepage.

So the root layout declares only what is true everywhere (origin, title template, description,
site-wide card) and **no** `url` / `canonical`, and every route builds its own block with
`routeMetadata()` from `src/lib/metadata.ts`. `/clips/[id]` passes `hasOwnOgImage: true` because it
ships its own `opengraph-image`; every other route gets the site card named explicitly. The 404
boundary resolves against the root object alone and so claims no canonical and no og:url, which is
correct for a page that does not exist.

Both Playwright suites assert per-route `<title>` / `og:title` / `og:url` / `canonical` and require
those values to be **distinct across routes** — inheritance from a common ancestor cannot satisfy
that, which is the point.

## Commands

```bash
npm run dev               # local dev server
npm run build             # production build
npm run typecheck         # tsc --noEmit
npm run lint              # eslint
npm run test:e2e          # Playwright against a local production build (see below)
npm run test:e2e:offline  # Playwright with Supabase made unreachable
```

## E2E

Two suites, one config (`playwright.config.ts`), each run in both a desktop and a 390x844 mobile
project. The local server is a **production build** (`next build && next start`), not `next dev`:
dev overrides `metadataBase` with `http://localhost:<port>` and compiles routes lazily, so
share-card URLs and first-hit timing would not be the ones that ship.

| Command | What it runs |
|---|---|
| `npm run test:e2e` | `e2e/site.spec.ts` against a local build wired to the real Supabase in `.env.local` |
| `PLAYWRIGHT_BASE_URL=https://… npm run test:e2e` | the same suite against a deployment; **no local server is started** |
| `npm run test:e2e:offline` | `e2e/offline.spec.ts` against a local build whose Supabase URL is unroutable |

`PLAYWRIGHT_OFFLINE=1` and `PLAYWRIGHT_BASE_URL` are mutually exclusive and the config refuses the
combination: a deployed site's backend cannot be made unreachable from a test runner, so the pair
would assert nothing.

**Neither suite builds into `.next`.** `NEXT_PUBLIC_*` values are inlined at build time, and both
suites bake `NEXT_PUBLIC_SITE_URL=http://127.0.0.1:<port>` (the offline suite additionally bakes an
unroutable Supabase URL). Sharing `.next` would leave a later `npm run start` serving canonical,
`og:url` and `og:image` URLs pointing at the test port. The live suite builds into `.next-e2e`, the
offline suite into `.next-offline` (both gitignored, both honoured by `next build` *and*
`next start` via `NEXT_DIST_DIR` → `next.config.ts`); `.next` belongs to `npm run build` alone.

Both alternate output directories are pre-declared in `tsconfig.json`'s `include`, because
`next build` rewrites that tracked file whenever `<distDir>/types/**/*.ts` is missing from it — so
a test run leaves the working tree clean.

The live suite runs at **3 workers**, not one per core. Every page it loads is `force-dynamic` and
fans out into several Supabase queries under a 4 s deadline (`src/lib/data.ts`); a dozen concurrent
request-time renders against one project made those queries abort, the site honestly reported "data
link down", and data assertions failed for a reason that was never about the site. The offline
suite has no backend to contend for and keeps the default.

`e2e/site.spec.ts` never assumes a row count — every data assertion is either structural or
conditional on rows being present. Tests tagged **`@current-build`** assert markup or metadata
introduced in this working tree; against an older deployment they fail by design, which is the
signal that a redeploy is due. To health-check a deployment on its own terms:

```bash
PLAYWRIGHT_BASE_URL=https://<production-domain> npx playwright test --grep-invert @current-build
```

Realtime delivery still cannot be exercised here (no local Realtime service); it is measured
against the real project.

## Brand rules (CLAUDE.md §7)

Original WANTED wordmark (heavy grotesque + red misprint offset) — no Pricedown or
death-screen-typography imitation, no Rockstar marks or names in UI copy. The footer on every
page carries the non-affiliation disclaimer and the "the agent is an AI; commentary is AI-generated"
disclosure.
