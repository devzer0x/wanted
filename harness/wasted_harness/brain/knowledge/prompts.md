# GTA V (PC) — On-Screen Prompts & Keyboard Bindings Brief

## Mission / free-roam on-screen prompts
- "Go to the yellow marker" / yellow blip on radar — navigate to the marked spot to trigger the next mission beat.
- "Go to the blue marker" — secondary destination, often where a companion NPC is waiting; distinct from the primary yellow marker.
- "Get in the vehicle" / "Get in your car" — board the indicated car/boat/aircraft before the mission continues.
- "Choose one of the cars" — start-of-mission prompt letting the player pick between offered vehicles.
- "Follow [Name]" (e.g. "Follow Lamar", "Follow Michael") — stay within range of an NPC's blue blip on foot or by vehicle; falling too far behind fails the mission.
- "Park next to [Name]" / "Park in the marker" — precision-parking objective tied to a marker outline.
- "Wait for [Name]" (e.g. "Wait for Lamar to hop in") — hold position until a companion boards or finishes an action.
- "Lose the cops" / "Lose the wanted level" — break line of sight and stay outside the search radius until wanted stars clear.
- "Take cover" — contextual prompt near cover-eligible objects during firefights, tied to the cover key.
- "Press E to [interact]" — generic tap-to-interact prompt (open doors, pick up items/weapons, talk to NPCs, use objects).
- "Hold F to enter vehicle" — vehicle-boarding prompt tied to the enter/exit key (default F).
- Cutscene skip prompt — an on-screen button hint appears during cutscenes already viewed once, letting the player skip ahead; first-time story cutscenes generally cannot be skipped.
- Mission Failed screen — offers a retry from the last in-mission checkpoint and a full mission restart; repeated failures can also surface an offer to skip ahead to the next checkpoint.

## Default PC keyboard bindings
- Enter/Exit vehicle — **F**
- Take cover — **Q**
- Sprint/Run — **Left Shift** (hold)
- Jump — **Spacebar**
- Weapon wheel (select weapon) — **Tab**
- Aim — **Right Mouse Button**
- Fire — **Left Mouse Button**
- Phone — **Up Arrow** (middle mouse button also reported as an alternate)
- Pause menu — **P**
- Horn — **E**
- Skip cutscene — **Esc** (Enter/Space also reported as working, on cutscenes already seen once)
- Mission Failed → retry from checkpoint — **Enter**
- Mission Failed → restart mission from the start — **Tab**

## Notes / source reliability
- gta.fandom.com (GTA Wiki), the requested primary source, returned HTTP 402 on every direct-fetch attempt this session for both the controls page (`Controls_for_GTA_V`) and the wanted-level page — could not be verified directly; only reached indirectly through search-engine result snippets, so its content is not cited as a fetched source below.
- The keyboard bindings (F / Q / Shift / Space / Tab / RMB / LMB / Up Arrow / P / E) are corroborated consistently across four independently fetched secondary pages (GTA-DB, BisectHosting, GTABoom, ComputerCity) — high confidence.
- The Mission Failed Enter=retry / Tab=restart-mission mapping, and the "repeated failures → skip offered" behavior, come only from search-engine-synthesized summaries of a GTAForums thread (the thread itself returned HTTP 403 on direct fetch) — not confirmed against a primary page directly. Verify in-game on the target machine before hard-coding these into the agent's input logic.
- Exact verbatim wording of the Mission Failed screen buttons and of any "Skip Mission?" prompt text was not found on any page actually fetched; the phrasing above is a paraphrase of what sources describe, not a verbatim quote, per instructions to write in original words.
- On-screen prompt phrasing ("Follow Lamar", "Lose the Cops", "Get in your car", "Wait for X to hop in", "Take cover") is drawn from two fetched walkthrough pages (GTABase's Franklin and Lamar mission page, and the Fextralife GTA5 Wiki walkthrough index) and paraphrased into the imperative forms above rather than quoted verbatim.

Sources:
- https://www.gtabase.com/grand-theft-auto-v/missions/franklin-and-lamar/
- https://gta5.wiki.fextralife.com/Walkthrough
- https://www.gta-db.com/gta-5-controls/
- https://www.bisecthosting.com/blog/gta-v-controls-guide-pc-playstation-xbox-keyboard-mouse-gamepad
- https://www.gtaboom.com/gta-5-pc-keyboard-controls-guide/
- https://computercity.com/software/gaming/keyboard-controls-for-gta-5
