# The radar, translated into your data

You cannot look at the minimap between screenshots — but everything on it reaches you as data,
every tick. Read the radar THROUGH these fields, and re-read them on every decision: markers
move, targets move, and the last tick's picture is already old.

| On the radar | In your state | What to do with it |
|---|---|---|
| **Yellow dot / yellow marker** — "Locations are marked with a ● on the Radar" — the mission's current objective or destination | `mission.objective_blip.pos` (`kind` coord or entity) | This is where the job wants you next. `drive_to` / `walk_to` it. The game's own driving AI follows the road, which is exactly the **yellow GPS route line** on the map. |
| **Yellow GPS line** to the objective | (implied by `objective_blip`) | Do not plan a route yourself — issue the destination and the route is the engine's. |
| **Purple marker / purple line** — a waypoint someone set | `set_waypoint {x,y}` sets one; there is no read-back | Only for your own navigation aids. Missions use yellow. |
| **Red dots** — enemies, targets, people shooting at you | `nearby.peds[]` with `relationship: hostile`, each with `pos` and `distance` | Count them, read where they are, fight or take cover. A red dot that closes fast is the priority. |
| **Blue dots** — friendlies: crew, allies, things to protect or reach | `nearby.peds[]` with `relationship: friendly` (+ `pos`) | Your crew. Stay with them, `follow_entity {handle}` if they are moving, never drive off without them mid-mission. |
| **Vehicles on the radar** | `nearby.vehicles[]` with `pos`, `distance`, `driver` | The empty one closest to you is the quick ride; a `driver: npc` one is a carjack. |
| **Police** — the radar edge flashes red/blue while a cop can SEE you; each officer has a view **cone** on the radar (there is no fixed search circle). Once nobody sees you the stars **flash**, and the slower they flash the closer you are to losing them | `player.wanted` (1–5) | Wanted > 0: break line of sight and stay out of every cone until the flashing stops; `flee_police`. Being seen again resets the timer. |
| **You** — the white arrow; **N** — north; a small arrow **inside** a blip means it is above/below you; indoors the radar becomes a floor plan | `player.pos`, `player.heading` | Distances in your state are from here. A blip above/below you needs stairs or a ramp, not a straight line. |
| **Protagonist-coloured icons** — mission start points: Michael = **blue**, Franklin = **green**, Trevor = **orange**; a **"?"** in one of those colours = a Strangers & Freaks side job | `mission.starts[]` (pos + protagonist), `player.protagonist` | Where jobs begin; drive to a matching one and walk in when the PLAN says it is time. Once a mission is running only the yellow marker matters. |
| **H** hospital, police station, house (safehouse), spray can (Los Santos Customs), gun (Ammu-Nation) | not in your state | Landmarks; irrelevant mid-mission. |

Two habits that keep you unstuck:

1. **Re-read the yellow dot every decision.** If it moved, the objective moved — go to the NEW one.
   If it vanished while `mission.active` is still true, a cutscene or a new phase has started:
   `wait`, then read again. Never keep driving to a stale marker.
2. **Red beats yellow while red is close.** A hostile inside ~40 m is the current problem; the
   objective can wait ten seconds. Blue never gets left behind.
