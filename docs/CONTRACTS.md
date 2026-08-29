# WANTED — CONTRACTS

**Version: 1.2 — FROZEN 2026-08-29.** Executors treat this file as read-only; changes go through
Fable (the orchestrator) and bump the version. Research backing every external-API claim:
docs/RESEARCH.md (decisions D1–D10) + raw sourced briefs in docs/research/.
Changelog:
- v1.1 adds the `site_config` table (§5) so stream provider/channel are runtime-switchable
  (D8 — `NEXT_PUBLIC_*` values are baked per-deployment and cannot switch at runtime).
- v1.2 (from the delivery-day readiness pass, all three found by cross-package verification):
  `last_task.id`/`type` are **nullable** before the first task (this exact mismatch would have
  crashed the harness on its first poll); `/health.edition` may be `unknown` before edition
  detection completes; and the bridge **error-code set is enumerated** above and closed per
  version, with consumers required to tolerate unknown codes.

Platform baseline (D1): GTA V **Legacy** edition (Steam app 271590, granted by an Enhanced
purchase), Script Hook V (current: 1.0.3889.0/1.0.1158.13 build), **official SHVDN nightly
≥ v3.7.0-nightly.189** pinned at Phase 1 start, scripts target .NET Framework 4.8, launched with
`-nobattleye`. The pinned SHVDN version is recorded in STATUS.md and bridge/README.

Conventions used everywhere:
- Timestamps: ISO 8601 UTC (`ts`).
- Coordinates: world-space meters, floats `{x, y, z}`. Heading: degrees 0–360. Speed: m/s.
- JSON over HTTP, UTF-8. Errors: HTTP status + `{"error": "<snake_code>", "detail": "human text"}`.
- **v1.2 — the error-code set** (closed per contract version; adding one bumps the version):
  `online_session_active` (503, every endpoint, online-session latch) · `not_ready` (503, /state
  before the first tick has published a snapshot) · `game_thread_stalled` (503, a queued command
  was not applied because the game thread is not ticking) · `queue_full` (503) ·
  `unknown_task_type` (400) · `invalid_params` (400) · `invalid_json` (400) ·
  `not_in_vehicle` (409, vehicle-only task while on foot) ·
  `unstick_conditions_not_met` (409). Consumers must handle unknown codes gracefully
  (log + treat as a transient failure) rather than crashing.

---

## 1. Bridge HTTP API v1 — `http://127.0.0.1:7777`

Served by the SHVDN script. HTTP thread only reads a cached snapshot and enqueues commands; the
game thread applies commands and refreshes the snapshot each tick. **Natives are never called from
the HTTP thread.**

**Safety rule (every endpoint):** if a network/online session is detected at startup or on any
tick, the script disables itself and every endpoint returns
`503 {"error": "online_session_active"}`. There is no teleport endpoint, no god-mode, no money, no
weapon-giving endpoint. `unstick` (≤3 m) is the only positional nudge and self-enforces its
preconditions.

### GET /state → 200

```json
{
  "ts": "2026-08-25T21:14:03.221Z",
  "tick": 123456,
  "player": {
    "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
    "heading": 0.0,
    "health": 200, "max_health": 200, "armor": 0,
    "wanted": 0, "cash": 0,
    "dead": false, "arrested": false,
    "in_vehicle": false,
    "control_enabled": true
  },
  "vehicle": null,
  "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
  "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
  "mission": {"active": false, "random_event_active": false, "cutscene_active": false,
              "objective_blip": null},
  "nearby": {"vehicles": [], "peds": []},
  "last_task": {"id": "t-000123", "type": "drive_to", "status": "running", "detail": ""},
  "bridge": {"version": "1.0.0", "edition": "legacy"}
}
```

Field notes:
- `vehicle` (when in one): `{"handle": 1234, "model": "adder", "display_name": "Adder",
  "class": "Super", "speed": 41.2, "health": 980.0, "upside_down": false, "in_water": false,
  "stopped_for_s": 0.0}`.
- `mission.active` ⇔ `GET_MISSION_FLAG`; `mission.random_event_active` ⇔ `GET_RANDOM_EVENT_FLAG`
  (Strangers & Freaks style content sets the latter, not the former — RESEARCH.md §2).
- `mission.objective_blip` (when identifiable): `{"pos": {x,y,z}, "kind": "coord|entity",
  "handle": <blip handle>}`. Starting identification rule (empirical convention, validated per
  mission in Phase 4): sprite Standard(1) + colour Yellow(66) + route enabled; refinements are
  documented in bridge/README without changing this field's shape.
- `nearby.vehicles[]` (top 8 by distance): `{"handle": 5678, "model": "...", "display_name": "...",
  "class": "...", "distance": 12.3, "driver": "player|npc|empty"}`.
- `nearby.peds[]` (top 8): `{"handle": 9012, "model": "...", "distance": 5.2,
  "relationship": "neutral|hostile"}`.
- `last_task.status` lifecycle: `idle` (no task ever / cleared) → `running` → `done` | `failed`.
  **v1.2:** when no task has ever been posted (fresh bridge load / script reload), `id` and `type`
  are `null`; every consumer must treat them as nullable. `status` and `detail` are always present.
  `detail` carries failure reason (`"preempted"`, `"timeout"`, `"target_lost"`, ...).
- Entity `handle`s are the game's entity/blip handles; valid only while the entity exists. The
  harness must treat them as ephemeral and re-read them from `/state` before use.

### POST /task → 202 `{"task_id": "t-000124"}`

One task at a time. Posting a new task preempts the running one (old task → `failed`,
`detail: "preempted"`). Body: `{"type": "<type>", "params": {...}}`. Unknown type → 400.
Tasks map to the game's own ped-task natives (the engine's pathfinding/driving — same system NPCs
use; verified signatures in docs/research/brief-natives.json). `style` maps to driving-style flag
bitfields (D3): `normal=786603`, `rushed=1074528293`, `ignore_lights=786475`,
`avoid_traffic=786468`. The *names* are the contract; the bridge may tune the underlying bit
values empirically (Phase 3) without a contract change.

| type | params | done when / notes |
|---|---|---|
| `drive_to` | `{x,y,z, speed_mps, style, arrive_radius_m: 8.0}` | player's vehicle within radius; `failed` if not in a vehicle |
| `walk_to` | `{x,y,z, run: false}` | within 2 m |
| `enter_nearest_vehicle` | `{prefer: "nicer"\|"any", search_radius_m: 30}` | `in_vehicle: true`; "nicer" = higher vehicle class rank than current/last, bridge-side heuristic |
| `exit_vehicle` | `{}` | on foot |
| `wander_drive` | `{style}` | never completes; runs until preempted |
| `flee_police` | `{}` | runs while wanted > 0; `done` when wanted = 0 |
| `combat_hated_targets_around` | `{radius_m}` | engine combat task; `done` when no hated targets remain in radius |
| `seek_cover` | `{duration_s: 10}` | cover reached or timeout |
| `follow_entity` | `{handle, in_vehicle: bool}` | runs until preempted or entity gone (`failed`, `"target_lost"`) |
| `set_waypoint` | `{x, y}` | immediate (`done` same tick); map waypoint only, no movement |
| `stop` | `{}` | clears current task → `idle` |

- `style` enum everywhere: `normal | rushed | ignore_lights | avoid_traffic`.

### POST /timescale → 200 `{"value": 0.15}`
Body `{"value": 0.1–1.0}` (clamped). Used while the brain thinks on a decision that matters;
harness must restore to 1.0.

### POST /control → 200 `{"enabled": true}`
Body `{"enabled": bool}`. Toggles player control (engine tasks on the player ped can be interrupted
by input; observed behavior documented in bridge/README during Phase 1).

### POST /radio → 200 — body `{"station": "<station name>" | "off"}`
### POST /horn → 200 — body `{"ms": 1–3000}`
### POST /unstick → 200 `{"moved": true, "distance_m": 2.4}`
Preconditions enforced bridge-side: speed ≈ 0 for > 20 s **and** a drive task `running`; otherwise
`409 {"error": "unstick_conditions_not_met"}`. Nudge ≤ 3 m. Always logged; harness announces it in
commentary.

### GET /health → 200
`{"version": "1.0.0", "edition": "legacy|enhanced|unknown", "tick_hz": 60.0, "queue_depth": 0,
"game_fps": 59.8, "online_blocked": false}` — the watchdog's liveness probe. Connection refused ⇒
game/bridge down; `503` ⇒ online session detected.

---

## 2. Decision schema (structured output via tool use)

One model call produces the decision **and** the commentary — never a separate commentary call.
This object is the whole public feed.

```json
{
  "thought": "≤40 words. The agent's private-ish reasoning, shown on site as 'what he was thinking'.",
  "say": "≤20 words. The agent's out-loud line in his voice. This is the commentary.",
  "mood": "chill | bored | hyped | scared | smug",
  "action": { "type": "<one of the bridge tasks or a manual-control primitive>", "params": {} },
  "goal": "current goal in ≤12 words, unchanged unless the director changed it",
  "confidence": 0.0
}
```

- **Paste-corruption note:** the master brief's mood enum arrived damaged
  (`"chill | bored | hyped |  smug"`); `scared` is restored from the brief's own driving-style spec
  ("scared avoids traffic"). If the original enum differs, fix here before Phase 2.
- `action.type` catalog = the 11 bridge task types (§1) **plus** harness-side primitives executed
  via SendInput keyboard/mouse (primary; a virtual gamepad is an optional enhancement — ViGEmBus
  is archived with known Windows Server failures, RESEARCH.md D9), v1 set:
  `look_around` (camera stick sweep), `brake_tap`, `swerve`, `reverse_out`,
  `press_prompt_key` (context prompt, e.g. E), `wait {seconds}`, `radio {station}`, `horn {ms}`.
  Adding a primitive bumps the contract version.
- `confidence` ∈ [0,1]. Enforced via strict JSON-schema tool use; a response failing validation is
  retried once, then the reflex layer keeps control and the failure is logged.

## 3. Brain tiers and pricing (D4; source: https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-08-25; re-verified at harness startup with a 1-token call per model)

| Layer | Model ID | Input $/MTok | Output $/MTok | Notes |
|---|---|---|---|---|
| tactical | `claude-haiku-4-5-20251001` (pinned snapshot) | 1.00 | 5.00 | text-only; thinking OFF (forced tool_choice + manual thinking errors); **min cacheable prefix 4096 tokens** — static prefix must exceed this or caching silently fails |
| director | `claude-sonnet-5` (dateless ID is itself pinned) | 2.00 | 10.00 | the $2/$10 intro price is now permanent (live docs beat older cached tables); text + occasional image; min cacheable prefix 1024 tokens |

- Prompt caching: reads 0.1× input price; writes 1.25× (5-min TTL — correct here, the 8–25 s
  cadence keeps it warm and each read refreshes TTL free). Never the 2× 1-h TTL.
- Decisions come back via structured output (`output_config.format: json_schema` /
  `messages.parse`) or strict tool use; **`tool_choice` stays constant across calls** (changing it
  invalidates cached message blocks).
- Screenshots (D5): 768-px-long-edge JPEG ≈ 448 visual tokens (`ceil(w/28)×ceil(h/28)`); raw
  1080p is never sent.
- `harness/config/pricing.yaml` is the runtime source of truth and must cite the official pricing
  URL + retrieval date. Budget accounting uses per-model `usage` token counts (Sonnet 5 tokenizes
  ~30% more tokens than Haiku for the same text — never estimate cross-model).

## 4. Event types (`events.type` + payload shape)

| type | payload | screenshot |
|---|---|---|
| `death` | `{cause: "?", street, deaths_total}` | yes |
| `busted` | `{wanted_at_arrest, street, busted_total}` | yes |
| `mission_start` | `{name}` | no |
| `mission_end` | `{name, outcome: "passed", duration_s, deaths, attempts}` | yes |
| `mission_fail` | `{name, reason_text, attempt}` | yes |
| `wanted_change` | `{from, to}` | only if to ≥ 3 |
| `stunt` | `{kind: "jump\|big_air", airtime_s}` | yes |
| `clip` | `{clip_id, event_type}` | no |
| `break` | `{phase: "start"\|"end", planned_s}` | no |
| `governor_level` | `{from, to, reason: "hourly_cap\|reset\|manual"}` | no |
| `bridge_down` / `bridge_up` | `{consecutive_failures}` / `{downtime_s}` | no |
| `unstick` | `{distance_m, stuck_for_s}` | no |
| `activity_start` / `activity_end` | `{activity, params}` / `{activity, outcome, duration_s}` | no |
| `session_start` / `session_end` | `{harness_version, game_edition}` / `{reason}` | no |

The enum is closed per contract version; adding a type bumps the version.

## 5. Supabase data model

Write path: harness only, service-role/secret key, batched inserts with an on-disk offline queue.
Read path: browser, anon/publishable key, RLS public SELECT only (`for select to anon,
authenticated using (true)`; no write policies ⇒ writes denied). Realtime (D6): `postgres_changes`
publication on `decisions`, `events`, `stats` for v1; the web feed renders from **rows** (not
socket messages) so a later migration to coalesced Broadcast-from-Database digests (Realtime bills
per recipient — see RESEARCH.md §5 cost trap) needs no UI contract change. Storage uploads always
go to new timestamped paths with explicit content-type. Exact SQL lands in
`infra/supabase/schema.sql` (Phase 0a-wait work).

```
sessions   id uuid pk, started_at timestamptz, ended_at timestamptz?, game_edition text,
           harness_version text
decisions  id bigint pk, session_id fk, ts, layer 'tactical'|'director', thought text, say text,
           mood text, goal text, action jsonb, confidence real, model text, input_tokens int,
           output_tokens int, cached_tokens int, cost_usd numeric(12,6)
events     id bigint pk, session_id fk, ts, type text (§4 enum), payload jsonb,
           screenshot_url text?
missions   id bigint pk, session_id fk, name text, started_at, ended_at?, outcome
           'passed'|'failed'|'skipped'?, attempts int, deaths int, tokens bigint, summary text
clips      id bigint pk, session_id fk, ts, event_id fk events, storage_path text,
           duration_s real, caption text
stats      session_id uuid pk (one row per session, upserted): deaths int, busted int,
           missions_passed int, hours_alive real, cost_today_usd numeric, cost_per_hour_usd
           numeric, governor_level smallint, heartbeat_at timestamptz, current_goal text,
           hud jsonb
site_config key text pk, value jsonb, updated_at timestamptz — public read; known keys (v1.1):
           "stream" → {"provider": "twitch"|"youtube", "channel": str, "video_id": str|null}.
           Written only by operators (service role); the web app reads it at request time with an
           env-var fallback when the row is absent.
```

- Indexes: `(session_id, ts)` on `decisions` and `events`.
- `stats.heartbeat_at` drives the site's "the agent is offline" state (stale > 60 s ⇒ offline); the
  site never guesses from feed silence.
- `stats.hud` shape (frozen): `{"health": int, "armor": int, "wanted": int, "cash": int,
  "vehicle": str|null, "street": str, "zone": str, "clock": "HH:MM", "weather": str}`.
- Storage buckets: `clips`, `shots` — public read, service-role write. `shots` retention 7 days.

## 6. Overlay (harness-local, for OBS browser source) — `http://127.0.0.1:7788`

- `GET /overlay` — transparent-background page OBS embeds.
- `GET /overlay/stream` — SSE; event types:
  `banner {"kind": "wasted"|"busted"}`, `counters {"deaths": n, "busted": n,
  "missions_passed": n}`, `say {"text": "...", "mood": "..."}`,
  `governor {"level": 0-3, "note": "..."}`.
No external assets; must render with the game offline (blank counters, no banner).

## 7. Budget governor levels

| Level | Behavior (all announced via `governor_level` event + feed line) |
|---|---|
| L0 | normal cadence |
| L1 | slower tactical timers, no flavor shots |
| L2 | director-only; reflex layer drives (native wander/drive with mood-based style) |
| L3 | "asleep in the car": parks somewhere scenic, commentary paused with an honest on-screen note, resumes when the hourly window resets |

Target: < ~$1.50 per streamed hour at L0; measured number reported in STATUS.md every live check.

## 8. Directory ownership (per CLAUDE.md §8)

`bridge/` bridge executors · `harness/` harness executors · `web/` web executors ·
`infra/` infra executor · `scripts/` + `docs/` Fable only. Cross-directory needs are reported back,
never edited directly.
