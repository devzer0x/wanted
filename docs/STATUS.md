# WANTED — STATUS

Single source of truth. Nothing appears in "Works / verified" without evidence (command output,
run log, or URL) noted next to it. Last updated: 2026-08-25.

## Current phase

**Phase 0 — Setup & research: in progress** (this file updates to "done" only with evidence).

## Works / verified

- (nothing yet — no product code exists)

## In progress

- Phase 0: repo skeleton committed; research fan-out running; contracts to be frozen from its
  output; human checklist to be delivered.

## Broken / known gaps

- No server exists yet. This checkout is on the human's macOS laptop; every game-adjacent
  verification is impossible until the Windows GPU server is provisioned (human checklist item 1).
- No Supabase project, no Vercel project, no API keys yet (human checklist items 3–4).

## Blockers on the human (see checklist in chat, delivered end of Phase 0)

1. Order the GPU server (or hand over cloud credentials).
2. Buy GTA V on their Steam account.
3. Claude API key; Supabase org access; Vercel access.
4. Stream channel choice + stream key (entered into OBS by the human only).
5. Two one-time logins on the server (Steam, Rockstar Launcher → offline mode, BattlEye off).

## Cost

- Measured $/hour: n/a (no harness yet). Target: < ~$1.50/streamed hour at normal cadence.
- Monthly projection: to be added before Phase 7 (server flat cost + brain $/h × schedule).

## Environment facts

- Repo root: `~/Downloads/GTAAA` on macOS (dev machine, docs/infra/web work only).
- Server: not yet ordered. Recommended: Hetzner GEX44 + Windows Server + HDMI dummy add-on.
- Paste-corruption note: the master brief arrived with minor copy damage; two spots were
  reconstructed from context and are flagged in docs/CONTRACTS.md (mood enum) and docs/PLAN.md
  (none material). If the original differs, correct CONTRACTS.md before Phase 2.
