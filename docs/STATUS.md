# WANTED — STATUS

Single source of truth. Nothing appears in "Works / verified" without evidence (command output,
run log, or URL) noted next to it. Last updated: 2026-08-25.

## Current phase

**Phase 0 — Setup & research: DONE (2026-08-25).** Next: Phase 0a (blocked on human checklist),
with Supabase schema + web scaffold as the sanctioned wait-work.

Phase 0 evidence:
- Repo skeleton commit `0331ff6`; CLAUDE.md (non-negotiables verbatim), 4 agent files, docs
  skeleton, .gitignore, directory ownership READMEs.
- Research: 8 parallel researcher briefs, 193 sourced facts total, raw JSON preserved in
  `docs/research/brief-*.json`; synthesis + decisions D1–D10 in docs/RESEARCH.md. Every
  API-shaped claim carries a source URL; unconfirmed items are listed explicitly (RESEARCH.md
  "Open items").
- docs/CONTRACTS.md **v1.0 frozen 2026-08-25**: bridge endpoints + JSON, 11 task types with
  driving-style values, decision schema, event enum + payloads, table shapes, overlay SSE,
  governor levels, ownership.
- Human checklist delivered in chat 2026-08-25 (also mirrored below).

## Works / verified

- (no product code yet — Phase 1+ will populate this section with run evidence)

## Broken / known gaps

- No server exists. **Hetzner GEX44 lead time is currently "several weeks"** (RESEARCH.md §7) —
  the master brief assumed 1–3 days; ordering is the schedule-critical item.
- No Supabase project, no Vercel project, no API keys yet.
- ViGEmBus/vgamepad on Windows Server 2025 unverified (known Code-28 failures on Server SKUs);
  plan of record is SendInput keyboard/mouse (CONTRACTS §2).
- This checkout runs on the human's macOS laptop — fine for docs/infra/web; all game-adjacent
  verification waits for the server.

## Blockers on the human (checklist delivered 2026-08-25)

1. Order Hetzner GEX44 (+ Windows Server 2025 add-on + "HDMI emulator" add-on) — or provide cloud
   credentials for an interim GPU VM. This is the long pole.
2. Buy "Grand Theft Auto V Enhanced" on Steam (app 3240220; includes the Legacy edition we run).
3. Claude API key (org must support > $500/month spend — Build tier or raised cap); Supabase
   project or plugin auth; Vercel access.
4. Stream channel (Twitch recommended for embeds) — stream key goes into OBS by hand, never the repo.
5. Later, on the server: one-time Steam + Rockstar logins; then offline args + BattlEye off.

## Cost

- Measured $/hour: n/a (no harness yet). Target: < ~$1.50/streamed hour at L0.
- **Design-model projection (RESEARCH.md §3, to be replaced by measurement in Phase 2):**
  tactical `claude-haiku-4-5-20251001` ~240 calls/h ≈ $0.37/h; director `claude-sonnet-5`
  ~40 calls/h ≈ $0.33/h → ≈ **$0.70/h ≈ $500/month at 24/7** — under target, but at the Start-tier
  monthly spend cap, hence checklist item 3.
- Server: ≈ €184/mo + ~€28 Windows + €1.10 HDMI emulator + €79 setup (confirm live prices in the
  order form). VB-CABLE professional license to budget (self-priced donationware).
- Supabase Realtime bills per recipient — negligible at launch audience, ≈ $1,900/mo at 300
  concurrent viewers × 1 msg/s; mitigation path contracted (CONTRACTS §5, coalesced digests).
- Full monthly projection table due before Phase 7 (per master brief).

## Environment facts

- Repo root: `~/Downloads/GTAAA` on macOS (dev machine, docs/infra/web work only).
- Game target: GTA V **Legacy** (Steam app 271590) via Enhanced purchase (3240220); SHV
  1.0.3889.0/1.0.1158.13; SHVDN official nightly (pin recorded here at Phase 1 start);
  .NET Framework 4.8; `-nobattleye`.
- Paste-corruption note: the master brief arrived with minor copy damage; reconstructed spots are
  flagged in CONTRACTS.md §2 (mood enum — `scared` restored). If the original differs, fix
  CONTRACTS.md before Phase 2.
