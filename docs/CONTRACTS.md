# WANTED — CONTRACTS

**Version: 1.10 — FROZEN 2026-09-02.** Executors treat this file as read-only; changes go through
Fable (the orchestrator) and bump the version. Research backing every external-API claim:
docs/RESEARCH.md (decisions D1–D10) + raw sourced briefs in docs/research/.
Changelog:
- v1.10 (root-caused from the 2026-09-02 live follow-mission failure — "drives and then stops").
  Four changes, three additive and one a semantic DEFECT FIX:
  1. **`objective_blip.handle` / `route_blips[].handle` now carry the ENTITY handle when
     `kind == "entity"`** (the ped/vehicle the blip is attached to, resolved via the game's own
     blip→entity lookup). Until now the bridge emitted the BLIP's handle — a different handle
     space — so `follow_entity` on "the game's own route to Lamar's car" resolved no entity and
     failed `target_lost` on arrival; the follower's re-post latch then held, nothing was driving,
     and the car coasted to a stop. That was never usable and no consumer can have depended on it,
     which is why this is a fix rather than a break. When the blip is an entity blip but the
     entity cannot be resolved, the bridge emits `kind: "coord"` (the position is still honest).
  2. **`nearby.peds[]` gains `in_vehicle_handle: int|null`** (null = on foot). Research
     (docs/research: SHVDN `World.GetNearbyPeds` is a raw CPed-pool scan with only radius+model
     filters — seated peds are NOT excluded) killed the first theory that occupants vanish from
     the scan; what actually happens is the crewmate's car pulls beyond the scan radius within
     seconds, and the long-range source (the route blip on their car) was unusable because of the
     handle defect fixed in item 1. `in_vehicle_handle` exists so the harness can do the
     ped→vehicle hand-off the instant the crewmate mounts up (follow the CAR, whose blip and
     handle outlive the ped's presence in `nearby`), instead of only discovering the loss later.
     Same 8-cap, same distance sort, same filters as v1.5/v1.6.
  3. **`mission.script: string|null`** — the active story-mission script name (e.g.
     `"armenian1"`), read from the game's own running script threads and filtered against the
     pinned allowlist in docs/research/brief-mission-scripts.json, emitted only while
     `mission.active`; null when unknown. Script→TITLE pairings are deliberately NOT shipped
     (research found no verifiable table): the harness treats the name as a stable identity key
     and learns script↔title pairs from the real game (OCR'd title card observed together with
     `mission.script`), persisted in harness state.
  4. **Task liveness:** when the game itself clears a task the bridge issued (mission scripted
     beats and cutscenes do this), `last_task` reports `failed` with detail `"cleared_by_game"`
     instead of `running` forever. Consumers already handle `failed` + re-plan; `running` must
     mean the engine is actually still executing the task.
  Harness types widen first (optional, defaulted), then the bridge emits — the v1.5/v1.6
  sequencing. No new task type; the §1 verb set is unchanged.
- v1.9 (behavioural, root-caused from a live stream failure): `follow_entity` gains `style` and
  `speed_mps`. Until now `StartFollowEntity` hard-coded `DrivingStyles.Normal` and a 15 m/s cruise
  cap (`TaskEngine.cs`), so a tail obeyed red lights and topped out at 54 km/h while the NPC being
  tailed did neither. Following a mission car was therefore **not achievable** — the target simply
  drove away, the mission failed, and no amount of prompting could fix it, because the losing
  behaviour was in the bridge, not the decision. Observed live as "Franklin lost Lamar" on a follow
  mission whose only objective is to stay with Lamar. New defaults **when the caller omits them**:
  style `ignore_lights`, `speed_mps` 30.0 (108 km/h). Both are chosen to match, not exceed, what a
  mission NPC does; `avoid_traffic` remains available for a genuine chase, and a caller may still
  pass `normal` for a leisurely tail. The on-foot branch is unchanged (it already runs). Rationale
  for touching the default rather than only the parameter: every existing caller is the harness's
  own follow logic, which wants to keep up in all cases, and a default that loses the target is a
  bug rather than a preference. No new task type, no new event, no `/state` change.
  **Asymmetry, on purpose:** the bridge ACCEPTS `style` on `follow_entity`, but the brain's decision
  schema does NOT expose it (`ACTION_PARAM_KEYS["follow_entity"] = (handle, in_vehicle, speed_mps)`).
  `style` is one of the three non-nullable wire keys from v1.4, so listing it would put
  `style: "normal"` on *every* follow the model ever emits and re-create the bug. Left off, the
  bridge default applies. `speed_mps` is nullable, so it is absent unless a caller means it, and
  that is the one lever the brain and `MissionFollower` get for a widening gap.
- v1.8 (additive; **retroactive — the bridge has emitted this since 2026-09-02, the contract
  entry was missed**): `mission.route_blips[]` = `[{ "pos": {x,y,z}, "kind": "coord"|"entity",
  "handle": int, "color": string }]` — the blips the game has actually plotted a GPS route to,
  nearest first, at most 5 (`MaxRouteBlips`). `objective_blip` is the bridge's single best pick out
  of the same set; the list exists so that the harness can reason when there is more than one (a
  follow target plus a drop-off), and so a wrong pick is recoverable instead of invisible. `color`
  is the SHVDN `BlipColor` member name, and an index with no name in the pinned enum serializes as
  its integer rendered as a string — consumers must tolerate that. `kind: "entity"` means the route
  points at a moving thing (`handle` is valid and can be passed to `follow_entity`); `kind: "coord"`
  means a fixed place. The key is always present as an array, never null.
- v1.7 (additive): `player.protagonist` = `michael|franklin|trevor|unknown` (from the player ped
  model), and `mission.starts[]` = `[{ "pos": {x,y,z}, "protagonist": "michael|franklin|trevor|unknown" }]`
  — the mission-start markers currently on the map (the M/F/T letter blips, identified by the
  game's own per-protagonist blip colours; nearest first, at most 8). Walking or driving into one
  starts that mission. Purpose: the agent can now DECIDE to go and do a job instead of only reacting
  once a mission is already running. Harness types widened (optional, defaulted) before the
  bridge emits them.
- v1.6 (additive): `nearby.vehicles[]` and `nearby.peds[]` gain `pos: {x,y,z}` (same shape as
  `player.pos`). Until now they carried only `distance` + `handle`, so the agent knew a crewmate or an
  enemy was "20 m away" but not in which direction — the minimap's red/blue dots were invisible to
  him as data. With `pos` he can `walk_to`/`drive_to` a crewmate, reason about where enemies are,
  and use `nearby` positions as a legitimate coordinate source. Harness types were widened
  (optional field) before the bridge began emitting it.
- v1.5 (additive): `nearby.peds[].relationship` gains `friendly`. Until now every non-hostile ped
  was `neutral`, so a mission crewmate standing beside the agent was indistinguishable from a bystander
  and "stay with the crew" / `follow_entity` on a crewmate could not be expressed. The harness
  type was widened before the bridge began emitting the value.
- v1.4 (root-caused by a 25-agent adversarial pass over 174 real decisions + server logs):
  §2 `action.params` is now a **closed, typed object** — every key §1's task table and §2's primitive
  list already name (`x y z speed_mps style arrive_radius_m run prefer search_radius_m radius_m
  duration_s handle in_vehicle seconds station ms direction`). On the wire this is a
  strict subset of what v1.3 permitted and adds no task type, event type or decision field. Why it
  had to change: the harness sends the decision schema through the Anthropic SDK's strict
  `transform_schema`, which rewrites a free-form object into `properties: {} +
  additionalProperties: false` — a grammar whose only legal value is `{}`. Constrained decoding
  therefore forced `params: {}` on **every one of 174 real decisions**, so every `drive_to` /
  `walk_to` / `follow_entity` was dead on arrival and the parameterless `enter_nearest_vehicle` /
  `exit_vehicle` pair made up 63% of all actions — the observed get-in-car / get-out / get-in-car
  loop. A typed model is the only fix a decoding grammar respects; prose in the catalog cannot.
  Wire-shape constraints, MEASURED against the real API 2026-09-02 (forced, not chosen): every
  param key is `required` on the wire and the model writes an explicit value or `null` per key,
  because the constrained-decoding grammar compiles in time exponential in the number of OPTIONAL
  properties (n>=15 optional => the server drops the connection at 60 s; all-required => ~3 s cold /
  ~1.3 s warm). The endpoint also rejects >16 union-typed params, so `style`/`run`/`direction` are
  non-nullable with their contract defaults (`normal`/`false`/`left`), holding the union count at 14.
  `wire_params()` strips keys an action does not take (an `ACTION_PARAM_KEYS` allowlist = the §1
  table + §2 primitives), so `decisions.action` / `POST /task` keep exactly the documented shape and
  no default leaks onto an action with no such param. Cost: +~85 output tokens/decision (~+$0.0004);
  the params schema rides inside the cached prefix. `follow_entity.in_vehicle`, when the model omits
  it, is absent from the POST body and the bridge defaults it to `false`.
  Companion (non-contract) findings fixed in the same pass: the harness left `world.timescale` at
  0.15 after a failed restore (the observed "slow motion"); a synchronous screen-grab retry loop
  blocked the tick for up to 172 s; a local schema failure was charged to the API-outage backoff.
- v1.3 (from the first live play session on the real game): `mission_start` now carries a
  **screenshot**. Rationale: /state exposes no mission name and no objective text — verified, the
  field does not exist anywhere in the pipeline — so the ONLY honest source of "what does this
  mission actually want" is the screen the game draws it on. Previously the director could see the
  screen on `death`, `busted`, `mission_fail` and `wanted_change` — i.e. only after things had gone
  wrong, never at the moment the objective is displayed. Observed live: the agent stood in a mission
  with no idea what it wanted. Cost is bounded: missions start rarely, so this adds roughly one
  vision call per mission rather than per tick.
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
              "objective_blip": null, "starts": [], "route_blips": []},
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
  "handle": <entity handle when kind=="entity", else the blip's own handle — see v1.10>}`.
  Identification rule (since v1.8): the bridge picks its best candidate from the ROUTED blip set
  (preferring yellow), falling back to the legacy sprite Standard(1) + colour Yellow(66) + route
  convention only when nothing is routed; details in bridge/README without changing this field's
  shape.
- `mission.script` (v1.10): the active story-mission script name (e.g. `"armenian1"`) from the
  pinned table in docs/RESEARCH.md, or null. Only ever non-null while `mission.active`.
- `mission.route_blips[]` (v1.8, nearest first, at most 5; handle semantics fixed in v1.10):
  `{"pos": {x,y,z},
  "kind": "coord|entity", "handle": <entity handle>, "color": "<BlipColor member name>"}` — every
  blip the game has plotted a GPS route to, i.e. the yellow line on the minimap made readable as
  data. `objective_blip` is the bridge's single best pick out of this same set. Use the list when
  there is more than one route (a follow target plus a drop-off) or when the pick looks wrong.
  `kind: "entity"` means the route points at something that moves and `handle` may be passed
  straight to `follow_entity`. `color` is a member name such as `"Yellow"`, but an enum index with
  no name in the pinned SHVDN build serializes as its integer as a string — tolerate that.
- `nearby.vehicles[]` (top 8 by distance): `{"handle": 5678, "model": "...", "display_name": "...",
  "class": "...", "distance": 12.3, "driver": "player|npc|empty"}`.
- `nearby.peds[]` (top 8): `{"handle": 9012, "model": "...", "distance": 5.2,
  "relationship": "neutral|hostile|friendly", "pos": {x,y,z}, "in_vehicle_handle": null}`.
  `friendly` (v1.5) = the engine's
  own Companion/Like/Respect relationship towards the player, i.e. mission crewmates. `pos` (v1.6)
  = world position, same shape as `player.pos`; vehicles carry it too. `in_vehicle_handle`
  (v1.10) = the vehicle the ped is currently seated in, or null on foot.
- `last_task.status` lifecycle: `idle` (no task ever / cleared) → `running` → `done` | `failed`.
  **v1.2:** when no task has ever been posted (fresh bridge load / script reload), `id` and `type`
  are `null`; every consumer must treat them as nullable. `status` and `detail` are always present.
  `detail` carries failure reason (`"preempted"`, `"timeout"`, `"target_lost"`,
  `"cleared_by_game"` (v1.10 — the game's own scripts wiped the task; re-plan, don't wait), ...).
- Entity `handle`s are the game's entity handles; valid only while the entity exists. The
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
| `follow_entity` | `{handle, in_vehicle: bool, style: "ignore_lights", speed_mps: 30.0}` | runs until preempted or entity gone (`failed`, `"target_lost"`). `style`/`speed_mps` apply to the in-vehicle tail only; on foot he always runs. Defaults keep pace with a mission NPC — see v1.9 |
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
  "action": { "type": "<one of the bridge tasks or a manual-control primitive>",
              "params": { "<v1.4: only the keys §1/§2 name for THIS type; see the v1.4 note on the closed 17-key wire object>": "..." } },
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
| `mission_start` | `{name}` | yes |
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
