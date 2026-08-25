# WANTED — RESEARCH (Phase 0)

Synthesized 2026-08-25 from 8 parallel researcher briefs. **Raw briefs with every claim + source
URL live in `docs/research/brief-*.json`** — executors quote exact signatures/hashes from there,
never from memory. Items below marked *unconfirmed* must be verified on the real target before
code relies on them. Decisions are numbered D1–D10 and referenced by PLAN.md and CONTRACTS.md.

---

## 1. Game edition & scripting stack (`brief-shvdn-editions.json`, `brief-steam-rockstar.json`)

**D1 — Buy "Grand Theft Auto V Enhanced" on Steam (app 3240220), install and run the Legacy
edition (app 271590).** Legacy is delisted from standalone sale but every Enhanced purchase grants
it (Rockstar Newswire 2025-03-04; PCGamingWiki). Rationale: official ScriptHookVDotNet is
Legacy-only (Enhanced support closed "not planned", issue #1558); Legacy has a decade of ecosystem.
Fallback if ever forced onto Enhanced: the `ScriptHookVDotNetEnhanced` fork (v1.1.0.6, 2026-07-15).

- Script Hook V (dev-c.com): one download supports both editions since 2025-03-23; current release
  2026-07-15, patches 1.0.3889.0 / 1.0.1158.13. Ships ASI loader (dinput8.dll) + Native Trainer.
  SHV itself closes the game if GTA Online is entered.
- SHVDN: stable v3.6.0 (2022!) is **broken on game ≥ 1.0.3258.0** — use the official nightly
  (v3.7.0-nightly.189, 2026-08-05, or later). Scripts target **.NET Framework 4.8** (not .NET
  Core), compiled `.dll` goes in `<game root>/scripts/`, releases ship XML API docs.
- BattlEye exists in **both** editions (Legacy since 2024-09-17). Official Story-Mode disable:
  launcher setting or `-nobattleye` in `commandline.txt` (title update 1.69). Without it the ASI
  loader triggers 0xc000009a startup errors. Online is blocked with it off — by design for us.
- **D10 — install/launch chain:** SteamCMD is useless here (Steamworks DRM + Rockstar launcher
  still required; second Steam Guard headache). Flow: silent Steam install
  (`winget install --exact --id Valve.Steam --silent …` or `SteamSetup.exe /S`), human logs in
  once, `steam://install/271590`, first launch installs Rockstar Launcher + Social Club, human
  signs into Rockstar (one-time link), then offline args: Launcher → Settings → GTA V → Launch
  Arguments → `-scofflineonly` (best-effort; periodic revalidation reported — keep the server
  online). Scripted launch: `steam.exe -applaunch 271590` (Steam itself started with `-silent`);
  the spawned process exits — **the watchdog polls for `GTA5.exe`**, never waits on the PID.
  Resolution: pre-seed `Documents\Rockstar Games\GTA V\settings.xml` (`ScreenWidth 1280`,
  `ScreenHeight 720`, `Windowed`) *after* the first auto-detect run; `commandline.txt` supports
  `-windowed -borderless -width -height`. No official intro-skip flag; SkipIntro ASI is acceptable
  on our offline Legacy install.
- *Unconfirmed:* `-scofflineonly` on Enhanced; exact first-run sequence; `Windowed` value mapping
  (community: 0=fs, 1=windowed, 2=borderless) — verify on the server.

## 2. Natives for the bridge (`brief-natives.json` — all hashes verified against alloc8or
nativedb, updated 2026-07-16; SHVDN wrappers verified in source)

**D2 — the bridge pins the SHVDN nightly API and uses these verified natives.** Full signature
table in the brief; highlights:

- Driving: `TASK_VEHICLE_DRIVE_TO_COORD_LONGRANGE` (SHVDN `Ped.Task.DriveTo`), `TASK_VEHICLE_DRIVE_WANDER`
  (`CruiseWithVehicle`), mid-task `SET_DRIVE_TASK_DRIVING_STYLE` / `SET_DRIVE_TASK_CRUISE_SPEED`.
- **D3 — driving-style flag values (documented bits, named combos partly convention):**
  `normal = 786603`, `rushed = 1074528293`, `ignore_lights = 786475`, `avoid_traffic = 786468`.
  Individual bits are documented (FiveM eVehicleDrivingFlags = SHVDN VehicleDrivingFlags); the
  *names* for combos are gtaforums convention — tune empirically in Phase 3, values stay in the
  bridge, names in the contract.
- On foot: `TASK_FOLLOW_NAV_MESH_TO_COORD` / `TASK_GO_STRAIGHT_TO_COORD`; enter vehicle:
  `TASK_ENTER_VEHICLE` (+ `GET_CLOSEST_VEHICLE`); flee: `TASK_SMART_FLEE_PED`; combat:
  `TASK_COMBAT_HATED_TARGETS_AROUND_PED` (**attacks only the single closest hated target despite
  the name; needs relationship setup or exits immediately**); cover: `TASK_SEEK_COVER_FROM_POS`
  (no SHVDN wrapper — raw `Function.Call`); follow: `TASK_FOLLOW_TO_OFFSET_OF_ENTITY`.
- State: `SET_TIME_SCALE` (0.0–1.0 only — cannot speed up), `GET_MISSION_FLAG` **plus**
  `GET_RANDOM_EVENT_FLAG` (Strangers & Freaks use the latter — check both), `IS_CUTSCENE_ACTIVE`
  vs `IS_CUTSCENE_PLAYING` (semantic difference is community knowledge — verify), `SET_PLAYER_CONTROL`
  (+ documented SPC flags), wanted read `GET_PLAYER_WANTED_LEVEL` (pure read), street/zone
  `GET_STREET_NAME_AT_COORD`→`GET_STREET_NAME_FROM_HASH_KEY`, `GET_NAME_OF_ZONE`(+localize),
  clock `GET_CLOCK_*`, weather `GET_PREV_WEATHER_TYPE_HASH_NAME`, radio
  `SET_RADIO_TO_STATION_NAME`, horn `START_VEHICLE_HORN`, `IS_ENTITY_UPSIDEDOWN`,
  `IS_ENTITY_IN_WATER`, `GET_ENTITY_SUBMERGED_LEVEL`.
- **Online guard: `NETWORK_IS_SESSION_STARTED`** (nativedb comment literally says "block your mod
  if true"); belt-and-braces with `NETWORK_IS_GAME_IN_PROGRESS`.
- Death/arrest: `IS_PLAYER_DEAD`; `IS_PLAYER_BEING_ARRESTED(player, atArresting)` — `true` arg
  fires early (hands-up), `false` only once busted; `IS_PLAYER_PLAYING` = single "alive & free"
  check.
- Blips: iterator natives are per-sprite; SHVDN `World.GetAllBlips` uses a **memory scan**
  (pattern-dependent — the fragile part on any edition change). Objective-blip rule
  "sprite Standard(1) + colour Yellow(66) + route on" is **empirical convention** — validate
  per mission in Phase 4 and document in bridge/README.
- Gotcha: SHVDN API churn stable→nightly (DriveTo arg order changed, `Player.WantedLevel` →
  `Player.Wanted.WantedLevel`, `World.CurrentTimeOfDay` → `GTA.Chrono.GameClock`) — pin one
  nightly version, compile against it, record it in STATUS.md.

## 3. Claude API — brain tiers (`brief-claude-api.json`, cross-checked with the bundled claude-api
skill; all pricing from platform.claude.com fetched 2026-08-25)

**D4 — tactical = `claude-haiku-4-5-20251001` ($1/$5 per MTok), director = `claude-sonnet-5`
($2/$10 per MTok — the intro price is now permanent; the skill's cached table showing $3/$15 after
2026-08-31 is outdated; live docs win).**

- Caching: writes 1.25× (5-min TTL) / 2× (1-h), reads 0.1×, **reads refresh the TTL free** — the
  8–25 s tactical cadence keeps a 5-min cache warm indefinitely. Max 4 breakpoints; hierarchy
  tools→system→messages; **changing `tool_choice` invalidates cached message blocks** — keep it
  constant. **Haiku 4.5 minimum cacheable prefix = 4096 tokens** (Sonnet 5 = 1024): the tactical
  static prefix must exceed 4K tokens or caching silently does nothing — verify
  `cache_read_input_tokens > 0` in telemetry.
- Structured output: `output_config: {format: {type: "json_schema", schema}}` (GA, no beta) or
  Python `client.messages.parse(..., output_format=PydanticModel)` → `.parsed_output`. Strict
  tools: `strict: true` + `additionalProperties: false`. **Forced `tool_choice` errors with
  manual extended thinking — keep thinking off on the tactical tier.**
- **D5 — vision:** cost = `ceil(w/28)×ceil(h/28)` tokens. Our spec'd 768-px-long-edge JPEG ≈
  **448 tokens ≈ $0.0009 on Sonnet 5** — never send raw 1080p (2,691 tokens on Sonnet 5,
  high-res tier).
- Tokenizer asymmetry: Sonnet 5 tokenizes ~30% more tokens than Haiku for the same text — budget
  accounting must use per-model token counts from `usage`, never estimates.
- Rate limits: renamed Start/Build/Scale/Custom. Per-minute limits are irrelevant at our ~20 RPM.
  **The binding constraint is the monthly spend cap: Start = $500/month** — a 24/7 brain at
  ~$0.70/h ≈ $500/month hits it. Human checklist: confirm org tier (need Build+). New orgs may
  start in a lower "Evaluation tier".
- Cost model at spec cadence (to be replaced by measured numbers in Phase 2): tactical ~240
  calls/h ≈ $0.37/h + director ~40 calls/h (occasional 448-token image) ≈ $0.33/h → **≈ $0.70/h,
  well under the $1.50 target**; L1/L2 governor levels cut further.

## 4. OBS & clips (`brief-obs.json`)

**D7 — obsws-python 1.8.0 (sync; `ReqClient` + `EventClient`).** obs-websocket v5 bundled since
OBS 28 (OBS 32.2.2 → 5.7.4; rpcVersion 1 throughout — any v5 client works).
- Request names confirmed: `StartReplayBuffer`, `GetReplayBufferStatus`, `SaveReplayBuffer`,
  `GetLastReplayBufferReplay`. **`SaveReplayBuffer` returns before the file exists** — sync on the
  `ReplayBufferSaved` event (carries `savedReplayPath`), then open-with-retry before upload
  (Windows may briefly hold the handle).
- Replay buffer defaults are 20 s / 512 MB — set 30 s explicitly and raise memory. Unavailable
  with "Custom Output (FFmpeg)" recording type.
- Auth: SHA256 challenge/salt handshake (the client lib does it); password lives in OBS config as
  plaintext — bind the websocket to localhost, keep the harness's copy in `.env`.
- Browser source: CEF, transparent default CSS, does *not* reload on scene change by default;
  `window.obsstudio` events available.

## 5. Supabase (`brief-supabase.json`)

**D6 — v1 uses `postgres_changes` (publication on `decisions`, `events`, `stats`; RLS
public-SELECT for `anon`), with a documented migration path to Broadcast-from-Database + Replay
when the audience grows.**
- RLS is enforced per Realtime subscriber (anon needs `GRANT SELECT` + `for select to anon using
  (true)` policy). DELETE events leak to everyone — we never rely on RLS for deletes.
- **Cost trap (architecture-relevant):** Realtime messages bill **per recipient** ($2.50/M after
  2M free / 5M Pro). 1 insert/s × 300 subscribers ≈ 780M msgs/month ≈ $1,900. Fine at early
  audience size; the site must be built so decisions/events can later arrive as **coalesced
  digests** (one broadcast per ~5 s) without UI changes — feed rendering keys off rows, not
  socket messages. Free plan also caps at 200 concurrent connections / 100 msgs/s (Pro 500/500).
- Anon private-channel join for Broadcast is **unconfirmed** in docs — must be tested before any
  broadcast migration (fallback: public broadcast channel, losing replay).
- Writer (harness): supabase-py — API errors raise postgrest `APIError`, offline raises raw
  `httpx` exceptions; catch both broadly, buffer to the on-disk queue, flush as batched
  `.insert([...])`; stats via `.upsert(..., on_conflict=…, returning='minimal')`. Plain PostgREST
  over httpx is the sanctioned alternative if the client lib misbehaves long-running.
- Storage: public buckets bypass RLS for reads only; uploads always to **new timestamped paths**
  (CDN staleness on overwrite); explicit `content-type` (defaults to text/html!). Standard upload
  fine ≤6 MB, TUS above.
- Next.js: `@supabase/ssr` — `createServerClient` for initial rows, `createBrowserClient` +
  `subscribe()` in a client component; statuses `SUBSCRIBED/CHANNEL_ERROR/TIMED_OUT/CLOSED`;
  **no gap-fill on postgres_changes** — on re-`SUBSCRIBED`, refetch rows newer than last-seen id.

## 6. Vercel / Next.js (`brief-vercel-next.json`)

**D8 — Twitch is the default embed** (`player.twitch.tv` iframe/JS with `parent` computed at
runtime from `window.location.hostname` — works on previews without config); YouTube alternative
embeds a server-resolved live videoId (the `embed/live_stream?channel=` URL is undocumented, and
live embedding may be gated on channel settings/monetization — Twitch first).
- Stream provider/channel comes from a **Supabase config row, not `NEXT_PUBLIC_*`** —
  NEXT_PUBLIC values are baked per-deployment at build time; a runtime row lets us switch
  provider without redeploy.
- Browser→Supabase WebSockets never touch Vercel (no constraint). Serverless holding sockets is
  irrelevant to us.
- Live page: static shell + `no-store` initial data + Realtime client push. Never ISR-per-second.
  Vercel strips `s-maxage`/`stale-while-revalidate` from client responses (CDN-only).
- OG images: `ImageResponse` from `next/og` (flexbox-only Satori, ttf/otf/woff, 500 KB budget).
- Playwright vs preview protection: header `x-vercel-protection-bypass:
  $VERCEL_AUTOMATION_BYPASS_SECRET` + `x-vercel-set-bypass-cookie: samesitenone` (iframes).

## 7. Windows GPU server (`brief-winserver.json`)

**D9 — Hetzner GEX44 (RTX 4000 SFF Ada 20 GB, i5-13500, 64 GB) + Windows Server 2025 Standard
add-on (~€28/mo est.) + "HDMI emulator" add-on (€1.10/mo), ≈ €184/mo + €79 setup.**
⚠ **Lead time is currently quoted as "several weeks"** for GEX44-1 — not the 1–3 days assumed in
the master brief. Order immediately; optionally bridge with a cloud Windows GPU VM for Phases
0a–2 development (AWS G4dn/G5 — provisioned via `infra/server/` if credentials are provided).
- Console session is mandatory: Desktop Duplication dies on RDP attach/detach
  (`DXGI_ERROR_SESSION_DISCONNECTED`). Disconnect procedure: elevated
  `tscon %SESSIONNAME% /dest:console` (goes into RUNBOOK + a helper script). Harness capture
  needs re-init logic on `ACCESS_LOST`.
- Auto-logon via **Sysinternals Autologon** (LSA secret, not plaintext registry). Lock screen off
  (`NoLockScreen=1`), idle lock off (`InactivityTimeoutSecs=0`), screensaver policy off. Windows
  Update: **notify-only (AUOptions 2/7)** — `NoAutoRebootWithLoggedOnUsers` is unreliable for
  disconnected sessions; maintenance is a scheduled manual window. High-perf power plan
  `8c5e7fda-…`, `monitor-timeout-ac 0`.
- Capture: **dxcam 0.3.0 (revived 2026-03, Python 3.10–3.14, dxgi + winrt backends)** — works
  headless *with the HDMI emulator on the RTX card* in the console session. Fallbacks:
  winrt backend, `windows-capture` (active 2026-08).
- Audio: **VB-CABLE v45** — enable Windows Audio + Endpoint Builder services first, reboot after
  install; OBS captures the CABLE device explicitly (RDP audio redirection can hijack "Default").
  24/7 monetized stream counts as professional use → budget the (self-priced) license.
- Input: **ViGEmBus is archived (2023) and has unresolved Code-28 failures on Windows Server
  SKUs.** vgamepad still auto-installs it (and pops a GUI installer — pre-install silently).
  → Manual-control primitives are implemented **keyboard/mouse-first via SendInput** (GTA V is
  fully keyboard-playable); virtual gamepad is an enhancement, tested day one on the server, cut
  without ceremony if Code 28 appears.
- SSH: OpenSSH Server ships in Server 2025 (`Start-Service sshd`, default shell via
  `HKLM:\SOFTWARE\OpenSSH`). **Skip Parsec** (free tier prohibits commercial use; VDD needs
  Teams/Warp; HDMI emulator already provides the display) — RDP for setup + OBS preview via the
  stream itself.

## Open items carried into later phases

1. Blip-objective identification rule — empirical validation per mission (Phase 4).
2. `IS_CUTSCENE_ACTIVE` vs `_PLAYING` semantics — observe in Phase 1 smoke test.
3. vgamepad/ViGEmBus on Server 2025 — day-one test in Phase 0a; SendInput is the plan of record.
4. Anon private-channel Realtime join — test before any Broadcast migration (post-Phase 5).
5. `-scofflineonly` durability — watchdog treats online revalidation as a known failure mode.
6. Live GEX44 price/lead time — human confirms in the Robot order form.
7. Anthropic org tier / monthly spend cap — human confirms Build tier or raises cap.
