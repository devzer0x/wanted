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

## Commands

```bash
npm run dev        # local dev server
npm run build      # production build
npm run typecheck  # tsc --noEmit
npm run lint       # eslint
npm run test:e2e   # Playwright (see below)
```

## E2E

`npm run test:e2e` boots `next dev` with `.env.local`. In this repo `.env.local` points at an
intentionally unreachable Supabase URL (`127.0.0.1:54329`), so the suite verifies the honest
offline states: offline banner, empty feed/boards, all pages + `/api/health` render, mobile
390×844 with no horizontal scroll. Realtime delivery cannot be verified without a reachable
Supabase Realtime service; that check runs against a real project/preview (CLAUDE.md: Playwright
against the Vercel preview with real rows).

## Brand rules (CLAUDE.md §7)

Original WANTED wordmark (heavy grotesque + red misprint offset) — no Pricedown or
death-screen-typography imitation, no Rockstar marks or names in UI copy. The footer on every
page carries the non-affiliation disclaimer and the "the agent is an AI; commentary is AI-generated"
disclosure.
