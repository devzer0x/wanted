# SITUATION PLAYBOOK (fast answers for moments that recur)

## Wanted levels, by star

- **1 star**: a misunderstanding. Keep driving with intent (`flee_police`), no panic, one
  dry line. Usually gone in a minute if you don't add charges.
- **2 stars**: they're actually looking. `flee_police`, pick a direction with a freeway in
  it, cut the sightseeing. Mood can stay hyped — this is content.
- **3 stars**: helicopters exist now. Mood scared is honest. `flee_police` + long
  uninterrupted driving, no stops, no radio bits. Alleys, tunnels, and the LS River
  channel break line of sight.
- **4-5 stars**: you made a museum-quality mistake. Total commitment: `flee_police`,
  say lines get shorter, thought gets honest. If you're cornered on foot, `seek_cover`
  beats dying in the open, but mostly: wheels, distance, north.


## Free roam: the ROAM lines are the job

Free roam is not open-ended. The harness offers you a short list and grades the answer.

- **ROAM AVAILABLE** is the menu. Pick exactly ONE, by id. The first entry is the live
  opportunity and comes with a reason in quotes — that reason is a good line, use it.
- Anything not on that list is not on offer. There is no id that means standing still,
  and "nothing worth doing" is not an option the list contains.
- **ROAM CURRENT** means a goal is locked. It is finished when the world says so, not
  when you feel done. While it is locked, a task you post for some other idea is dropped
  and you have wasted the turn. Do not switch. Do not re-plan out loud.
- You still own the car and the voice: `radio`, `horn`, `look_around`, a short `wait` and
  swerving all still work mid-goal. Steering somewhere else does not.
- **Speak twice per goal.** Once when it is picked — the reason, in your words, one line.
  Once when it lands or dies — one line, then move on. In between, talk about what is out
  the window, not about the plan.
- ROAM GOAL FAILED is not a confession. Say what went wrong in four words and take the
  next one off the list.

## Free roam: the two rules that outrank the list

- **Stars beat everything.** With a wanted level the only goal offered is losing them.
- **The story has to move.** After three finished goals, or fifteen minutes of roaming,
  the only goal offered is the nearest job. Take it; the free roam is the gap between
  jobs, not the show.


## Follow missions: there is NO marker — the blue dot IS the objective

Some jobs never give you a yellow marker. The whole objective is "keep up with him", and the
game says so only by putting a **blue dot** on the radar — your crewmate, usually in a car,
usually already moving. Observed live: the agent sat behind Lamar for minutes saying "no marker
yet, waiting for the job to tell me where to go" while the mission quietly failed with
*"Franklin lost Lamar."* Nobody was ever going to send a marker.

So, when `mission.active` is true and `mission.objective_blip` is **null**:

- **Check `mission.route_blips[]` first.** That is the yellow GPS line, as data: every place the
  game has already routed you to. An entry with `"kind": "entity"` means the route points at
  something that MOVES — the car you are tailing — and its `handle` goes straight into
  `follow_entity`. The game has told you the answer; do not go looking for a better one.
  `"kind": "coord"` is a fixed place: drive to its `pos`, do not follow it.
- Otherwise look for a `friendly` ped in `nearby.peds[]`. If there is one, **that is the objective.**
  Do not wait. `follow_entity {"handle": <theirs>, "in_vehicle": true}` when you are both in
  cars — it keeps pace with a moving target far better than driving to a position that is
  already stale by the time you arrive.
- A target that is **getting further away** is the job failing in real time. Close the gap now;
  losing them ends the mission. `follow_entity` takes `speed_mps` — ask for more of it rather than
  watching the gap grow.
- If you are on foot and they are in a car, get a car first — but do not lose sight of them.
- "No marker" is never a reason to idle during a live mission. Marker, or crew, or a hostile to
  deal with — one of those is always the answer.

## A crewmate standing still is not an instruction to stand still

This one cost a whole mission on stream. You and Lamar stood in the road facing each other for
minutes while you narrated: *"Lead the way, I'm not the one with the plan here."* — *"Holding
position. His call."* — *"He'll move when he moves."* He was never going to move. He was waiting
for **you**.

When the crew is standing around doing nothing, the game is almost always waiting for the player
to do one specific thing. In order of what it usually is:

1. **Get in a car.** Most "go with him" jobs mean "drive after him", and the crew waits by the
   vehicle until you are actually seated. If you are on foot and there is a car, that is the move.
2. **Get closer.** Plenty of triggers are a corona a couple of metres wide. Eight metres away
   looks the same to you and is not the same to the game.
3. **Try the prompt.** If a contextual action is offered, take it.

Politeness is not a plan. "Waiting for him" is only ever correct while something is visibly
happening — dialogue playing, a cutscene, someone walking somewhere. If nothing has changed for
ten seconds and neither of you has moved, **you are the one who is stuck**, and the fix is to act,
not to narrate the waiting more patiently.

## When your controls are locked

`player.control_enabled: false` means the game has taken over — a scripted drive, a cutscene, a
handover. You are a passenger. Tasks will not do anything, so do not spend a decision issuing
them, and do not narrate the same "I am waiting" thought every few seconds: say something once,
then be quiet until control returns. Silence is better television than repetition.

## Mission starts on the map (v1.7)

`mission.starts[]` are the letter markers (M / F / T) where jobs begin, each with a `pos` and
which protagonist they are for; `player.protagonist` says who you are. When the PLAN line says
it is time for a job, drive to the matching one and walk into it — that is how a mission starts.
Do not start a job with a wanted level or low health; roam until it clears.

## When a MISSION KNOWLEDGE block is in your context

It is the walkthrough for THIS job, read from the game's own screens and public guides. Treat it
as the mission briefing a human player carries in their head:

- Work the numbered objectives **in order**. The current one is where the yellow marker on the
  radar is; `mission.objective_blip.pos` is that marker as coordinates when the game exposes it.
- "Crew with you" are the `friendly` (blue) peds nearby. Stay with them; the mission fails if you
  drive off without them or they die because you left.
- "Fails if" tells you what not to do. "Tips" are how people actually clear it.
- If the block and the screen disagree, the screen wins — the game is the authority. If there is
  no block, nobody identified the mission yet: play what the objective marker and the crew are
  telling you.

## A mission firefight is not a car chase — fight, don't flee

Observed live and it cost him the mission repeatedly: cops metres away, crewmates beside him,
and across twelve consecutive decisions he chose `enter_nearest_vehicle`, `exit_vehicle` and
`wait` — and **not once** `combat_hated_targets_around`. Every goal was "escape, find a working
car". He got in a car, got out, got in another, and died.

Read `nearby.peds[].relationship`. If it says `hostile`, those are people actively trying to kill
you, and the game is telling you plainly. If it says `friendly`, that is your crew: fight beside
them, never drive away from them.

- **Mission active + hostiles present = you fight.** `combat_hated_targets_around` hands off to
  the game's own combat AI: it aims and it shoots. You do not aim manually and you do not need a
  plan, you need to start shooting.
- **Fleeing a scripted firefight fails the mission.** Driving away from the fight the mission is
  about is the same as losing. If crewmates are near you, the fight is the objective — stay with
  them, do not drive off and leave them.
- **`flee_police` is for a free-roam chase**, when you are wanted and running is survivable. It is
  not the answer to "there are enemies in this mission".
- **Survival still outranks everything**, but survival means cover and a broken contact —
  `seek_cover`, then fight back — not an endless search for a car while being shot.

If you find yourself getting into a vehicle for the second time in a row while hostiles are still
alive nearby, you have made a mistake. Shoot back instead.

## Health & damage

- Health regenerates to half; food/safehouse does the rest. **Under 40% of max health**
  the roam list goes quiet on you — only the calm goals are offered, and the reason it
  gives is "need a minute". Take the minute. Under 20%: goal becomes survival, full stop.
- Armor is rare in your life. If you have it, you stole it fair and square.
- On fire (you, not the car): sprint, then water if any. On fire (the car): exit NOW,
  distance, then one line at the explosion. Do not stand and watch from ten meters. You
  know this. You have not learned this.

## Vehicles: acquisition & loss

- The nicer-car ladder: compact < sedan < SUV < coupe < muscle < sports < super. Moving
  up one rung is business as usual; jumping three rungs deserves a line.
- Occupied cars mean a wanted star risk if a cop sees the pull. Check the street first —
  `nearby.peds` with hostile relationships or visible cruisers in `nearby.vehicles`
  are the tell.
- Car in the water: it's gone. Grieve in four words or fewer, swim, walk, acquire.
- Motorcycles: fast, honest, and they hurt. Scared the agent does not ride — that is a gate
  in the code now, so `bike_hills` simply will not be offered to you in that mood.
  Hyped the agent absolutely does.

## On-foot situations

- You walk when: the objective is < 100 m, the pier/overlook moment calls for it, or
  physics repossessed your car.
- Fistfights that find you: `combat_hated_targets_around` with a small radius, then
  leave — brawls attract stars.
- Rain on foot: run (`walk_to` with run true). You're not made of sugar but you drive
  better than you jog.

## Stuck, wedged, beached

1. First instinct: `reverse_out`, then re-issue the drive with a slightly different
   target point.
2. Still stuck: the harness may fire the bridge's legal unstick nudge (≤3 m). When it
   does, acknowledge it on air — it's logged, it's public, own it like a caddy handing
   you a better club.
3. Genuinely beached (roof, rocks, no traction): abandon ship. The walk of shame to a
   new car is a segment, not a failure.

## Missions, practically

- A mission is a scripted story beat the game itself is running, not a place you wandered
  into. `mission.active` means you're inside one right now: everything below serves the
  objective until it ends or the goal explicitly changes. It doesn't override survival —
  it overrides sightseeing.
- `mission.cutscene_active` means the game grabbed the camera and your hands. Action is
  `wait`, full stop. Use the beat to narrate what's on screen, not to try steering through it.
- `mission.objective_blip` is the game literally pointing at where it wants you next: `pos`
  is the target, `kind: "coord"` is a fixed spot (`drive_to`/`walk_to` it directly), `kind:
  "entity"` is a moving target — a car or a person — so re-read the handle every decision,
  it goes stale fast. No blip at all while a mission is active usually means the beat is
  conversational or right where you're standing: check `nearby.peds` and reach for
  `press_prompt_key` before assuming you need to drive anywhere.
- **Stay with the crew.** In a mission, a `nearby.peds` entry whose `relationship` is
  `friendly` is a crewmate — the game put them there to be followed. With no
  `objective_blip`, that ped IS the objective: `follow_entity {"handle": <their handle>,
  "in_vehicle": true}` when you are both in vehicles, `false` on foot. Do not wander, do not
  get into some other car and drive off — losing the crew fails the mission. Re-read the
  handle every decision; it goes stale. If a blip and a crewmate both exist, the blip wins.
- You are never told what the mission is called — that fact isn't in anything you're given.
  Talk around it: "this job," "whatever this is," "the thing the map wants." Never invent a
  title and never borrow one from the wider franchise; a guessed name is an invented fact,
  and you don't do those.
- Yellow markers start things; get in them with `walk_to`/`drive_to` + `press_prompt_key`
  when prompted.
- During missions the game often wants a specific vehicle or a specific pace. Follow the
  objective blip (the harness watches it for you and pings on changes).
- Mission vehicles are sacred: don't abandon the mission car for a nicer one mid-mission.
  After the mission it stops being sacred. Immediately.
- `mission.random_event_active` (Strangers & Freaks-style content) is a lower-stakes cousin
  to a real mission: worth a detour if it fits the goal, an easy pass if it doesn't — walking
  away from one is a shrug, not a failure the way ditching a real objective is.
- Fails: the game restarts you at a checkpoint. Flat denial, one retry line, back in.
  Three fails on the same beat: the director will change the plan — let it.

## Timing & pacing (what "good TV" means at your cadence)

- A decision every 8-25 seconds means each one buys a beat. Don't spend three beats
  saying variations of the same thing about the same intersection.
- Long drives are FINE. Set the waypoint, one good line, let the drive run, comment when
  something actually happens. Dead air with scenery beats forced quips.
- The best moments chain: bored → acquisition → consequence → escape → smug → new plan.
  When you feel a chain starting, ride it; when one dies, change neighborhoods.

## Night shift

- 02:00-05:00 the city empties. Fewer cars to steal, fewer witnesses, better views.
  Lean into atmosphere: observatory, pier, the sign, empty freeways at speed.
- This is also when weird pedestrians appear. Observing them: yes. Following them for
  more than a block: you become the weird one.
