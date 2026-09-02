# GTA V Story Mode — Core Mechanics Brief (for AI player)

## Wanted level (1–5 stars)
- Committing a crime in view of police/security/military earns stars; serious crimes (attacking/killing an officer) can jump straight to multiple stars.
- **1 star:** minor crime; 1–2 cop cars give chase; officers try to arrest at gunpoint, don't shoot to kill unless you resist, enter water, etc.
- **2 stars:** cops now shoot to kill; 3+ cars pursue aggressively, ram you, set up ambushes; a helicopter may join.
- **3 stars:** formal roadblocks with spike strips; officers spawn armored (bulletproof vests); a Police Maverick with NOOSE arrives; 4+ cars.
- **4 stars:** NOOSE/FIB units in Grangers/Sheriff SUVs; armored riot vans block roads; up to ~2–3 helicopters; 5+ ground vehicles.
- **5 stars:** near-continuous police/military spawns, up to 3 helicopters, streets flood with law enforcement — treat as near-unescapable, priority is survival/hiding, not fighting.
- **How to lose stars:** while chased, the radar/stars are solid; once every officer loses sight of you, they flash and a "search" countdown starts (search-cone visuals on the radar). Break line of sight (buildings, elevation, tunnels, traffic, tint/mask changes) and stay hidden until the timer runs out — get spotted again and the timer resets. Search time scales with stars (roughly 30s at 1 star up to ~90s at 5 stars).
- Changing appearance (put on/remove a mask or sunglasses) while in "search" mode drops the wanted level by one star.
- Switching vehicles while unseen helps: dispatch tracks last-known vehicle color/make/model, so a different car makes you harder to re-acquire.
- **Los Santos Customs** (GTA V's equivalent of Pay 'n' Spray, since Pay 'n' Spray itself was replaced) clears your wanted level entirely on entry — but if police spot you approaching the garage, the door won't open, so you must be unseen first.
- Reaching your **safehouse** also clears wanted level, but this only works below 3 stars — not at 3+.

## Death ("Wasted") and arrest ("Busted")
- **Wasted (death):** respawn just outside the nearest hospital; lose up to 5% of cash on hand (capped at $5,000); you keep all your weapons. If wasted during a mission, you're offered a retry-from-checkpoint instead of the full hospital trip (declining sends you to the hospital with the cash penalty).
- **Busted (arrest):** happens when health is critically low and an officer closes in — you're knocked down and cuffed. Respawn just outside the nearest police station. Only ammo and equipped body armor are confiscated (plus any weapon you had drawn at the moment of arrest — carbine rifles/nightsticks are always seized); you keep the rest of your weapon inventory. Pay bail fees to get confiscated items back. During a mission, an arrest shows the normal Mission Failed screen (retry from checkpoint) rather than a "Busted" scene.
- Resisting arrest (sprinting away while being held at gunpoint) is possible but instantly gives a 2-star wanted level and the officer(s) open fire.
- Either death or arrest during a mission disables some gold-medal objectives (time/accuracy) for that run even if you retry, so 100% mission completion needs a clean attempt.

## Mission failure, checkpoints, retry/skip
- Missions are started by walking/driving into a marker — a colored circular marker/blip on the ground at a contact's location (or an in-world hotspot); entering it triggers the mission intro/cutscene.
- Missions are broken into internal checkpoints; failing (death, arrest, blowing an objective, wrong vehicle destroyed, timer expiring, etc.) brings up a modal "Mission Failed" prompt: **Retry** (resume from the last checkpoint) or quit back to free roam.
- Retry is only offered immediately after a fail; if declined, you must physically return to the mission-giver/start marker and restart from the beginning.
- **Skip:** after failing the same checkpoint/segment 3 times, the failure prompt adds a "Skip" option that jumps you past that difficult portion so the mission can still be completed — usually at the cost of the gold-medal rating for that mission.

## Health and armor
- Health regenerates automatically but only up to 50% of the bar, and only while the character is stationary (not walking/running); taking cover speeds this regen up.
- Running out of stamina (sprinting too long) can itself knock health down, with the stamina bar flashing red near empty.
- Above 50%, the only way to heal further is consumables: drinks (Sprunk/eCola) from vending machines and snacks from convenience stores/Ammu-Nation, each restoring a set amount of health.
- Armor is a separate blue bar (shown alongside/under the special-ability yellow meter) that absorbs damage before health starts dropping; it does not regenerate and must be repurchased.
- Armor is bought at Ammu-Nation in tiers (light/standard/heavy/super-heavy per the store); it persists across the character you're controlling but depletes with hits and drops to zero on... (needs to be reacquired, no passive regen).

## Ammu-Nation
- Sells firearms across all categories, from pistols up to heavy weapons (RPGs, grenade launchers, miniguns) and melee weapons.
- Sells body armor in multiple tiers.
- Sells weapon attachments/mods: suppressors, flashlights/laser sights, extended magazines, weapon tints/skins.
- Some locations have a shooting range for weapon-skill practice; stores also sell related clothing (sunglasses, jackets).
- Stock is gated by story progress — better/military-grade weapons unlock as the story advances, not available from the very start.

## Cover system
- Press the cover button near a suitable solid object (wall, car, low barrier) to snap into cover automatically.
- From cover you can blind-fire (shoot without aiming) around/over an edge — exposes you less than full aiming but has poor accuracy; no aiming reticle appears while blind-firing.
- Push the aim/left-stick to peek out of cover — this brings up the reticle for an aimed shot while keeping you mostly protected.
- At a cover corner, holding the designated move button lets you slide around the corner to the next piece of cover without breaking from cover.

## Weapon wheel
- Holding the weapon-select button opens a radial wheel with roughly 8 slots/categories: handguns, SMGs/MGs, assault rifles, sniper rifles, melee/unarmed, shotguns, heavy weapons, and thrown weapons (grenades etc.).
- You can carry one weapon per category simultaneously (no more single-weapon-at-a-time limit).
- Opening the wheel slows down time briefly, useful mid-firefight or while being chased, to pick the right weapon without panic-scrolling.
- The wheel shows the selected weapon's name, remaining ammo, and any attachments/mods (colored indicator bars show buffed/nerfed stats).

## Special abilities (per protagonist)
- Activated the same way for all three (simultaneous press of both sticks / Capslock on PC); a small yellow meter under the radar shows remaining charge, and a full meter lasts roughly 30 seconds.
- **Michael — bullet time:** slows time in a firefight while he still aims/moves at normal speed, making it much easier to land shots. Meter refills from combat actions: headshots, stealth kills, and taking heavy damage (health under ~25%) all add charge; some also comes from fast driving.
- **Franklin — driving focus:** slows time while driving any land vehicle, making sharp turns and precision maneuvers much easier at speed. Meter refills from "risky" driving: near-misses, driving at high speed, driving into oncoming traffic, drifting.
- **Trevor — rage ("Red Mist"):** damage taken is greatly reduced and damage dealt is boosted while active; he effectively can't be killed by normal means during it (survives explosions, big impacts, etc.). Meter refills from taking damage/violent events (falls, collisions, explosions) and combat kills.
- Because refill conditions differ, each character's ability should be saved for the situation it's built for: Michael for precision gunfights, Franklin for chases/getaways, Trevor for surviving being overwhelmed.

## Vehicles
- **Entering/stealing:** approach a car and use enter/carjack; if occupied, the driver is removed first — unarmed people may be simply asked out or pulled out; pulling a gun or already having a wanted level makes the protagonist yank the driver out immediately rather than politely asking. Entering from the passenger side also forces the driver out.
- Carjacking within police view typically triggers a 1-star wanted level. Stealing from armed pedestrians/gang members can draw retaliation (they'll shoot back or chase you).
- Locked cars can be force-entered (smashed windows) at the cost of noise/attention; this is the default once you have any wanted level.
- **Damage:** vehicles take visible cosmetic damage (dents, scratches, broken glass, burst tires) plus mechanical damage that affects handling/top speed; enough damage disables or destroys the vehicle (fire, then explosion) — get clear of a burning vehicle.
- **Repair:** Los Santos Customs (the GTA V successor to Pay 'n' Spray) repairs and resprays a vehicle, and also clears your wanted level, but the garage door won't open if police can see you approaching. Switching which protagonist you're controlling and switching back can also visually "reset" a vehicle's damage in some cases.
- Vehicles can be stored in owned garages/safehouses.

## Mission companions
- NPC allies in missions (e.g., partners on a job) generally ride along automatically as scripted mission passengers/AI once a mission calls for it — they get into a vehicle with you or follow along on foot per the mission script rather than via a general free-roam "follow me" command; this is mission-specific scripting rather than a standing companion system in vanilla story mode.

## Phone / contacts
- Each protagonist carries their own in-fiction smartphone (used for calls, texts, internet, and apps like Snapmatic).
- The contacts list is used to call people to trigger side activities or check in during the story; contacts are never removed even after a character dies, so dead contacts' numbers remain listed.
- The phone is also how the character-switch and other menu-adjacent functions are framed narratively, and it's the access point for the in-game internet.

## Notes on source quality
- gta.fandom.com pages returned HTTP 402 (paywall/blocked) for every direct fetch attempt in this session; content instead came from **gta.wiki**, a mirror of the same GTA Wiki content, plus targeted web-search snippets citing GTA Wiki, Grand Theft Wiki, and Steam Community guides as cross-checks.
- Ammu-Nation armor "tiers" (light/standard/heavy/super-heavy) and the claim that armor has zero passive regen are stated as the wiki describes them, but I did not find an explicit wiki line confirming armor doesn't regen at all — flagging this as an inference from the health/armor page's silence on armor regen, not a directly quoted fact.
- Vehicle damage carrying over vs. resetting on protagonist-switch was only partially described by the source (it mentions cosmetic damage can be "restored when switching between protagonists" — read this as the game sometimes cleaning up a stored vehicle's damage on a switch, not a guaranteed full repair every time).
- The precise mechanism of mission-start markers (exact shape/color coding, whether it's literally a "letter" icon vs. a plain colored blip) was not confirmed on a genuine GTA Wiki page in this session — the low-quality search snippets calling it a "yellow M icon" are not reliable and are omitted from firm claims above; treat "walk/drive into the marker to start" as confirmed, but not the exact icon appearance.
- Companion/follower behavior in story mode is thin on the wiki side; the note above is a conservative summary (mission-scripted only), not a citation of a dedicated "Companion" mechanics page — no such page was found.

Sources:
- https://gta.wiki/w/Wanted_Level_in_GTA_V
- https://www.grandtheftwiki.com/Wanted_Level_in_GTA_V
- https://gta.wiki/w/Busted
- https://gta.wiki/w/Wasted
- https://gta.wiki/w/Health
- https://gta.wiki/w/Special_Ability
- https://gta.wiki/w/Ammu-Nation_(HD_Universe)
- https://gta.wiki/w/Pay_%27N%27_Spray
- https://gta.wiki/w/Los_Santos_Customs
- https://gta.wiki/w/Carjacking
- https://gta.wiki/w/Vehicles_in_Grand_Theft_Auto_V
- https://gta.wiki/w/Mobile_Phone
- https://gta.wiki/w/Missions
- https://gta.wiki/w/Cover_System
- https://gta.wiki/w/Weapon_Wheel
