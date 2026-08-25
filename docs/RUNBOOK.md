# WANTED — RUNBOOK

Filled in as phases complete. Sections marked (0a), (6), (8) etc. gain content when that phase's
work is verified — nothing speculative goes here.

## 1. Moving work onto the server (written now; exercised in Phase 0a)

1. RDP into the server once with the admin credentials from the provider.
2. Install Git + Claude Code (server-setup.ps1 does both).
3. `git clone <repo>` into `C:\wasted` (or transfer this folder), `claude --model fable` in it.
4. From then on, prefer SSH (`ssh <user>@<server-ip>`) + Claude Code in the terminal over RDP.
   RDP only for the two one-time logins (Steam, Rockstar) and for anything needing the desktop.
5. **Session rule:** the game must run in the *console* session. A game launched from inside an RDP
   window lands in the RDP session and dies on disconnect. Exact reattach/tscon procedure: (0a).

## 2. Start / stop / restart (Phase 0a/2)

- To be written from the real `scripts/run.ps1` + `watchdog.ps1` behavior once they exist.

## 3. OBS scene setup (Phase 6)

- To be documented with screenshots taken from the real setup. Stream key lives only in OBS,
  entered by the human; the harness never touches it.

## 4. Failure playbook — 3am page (Phase 7/8)

- Game crash → watchdog relaunch chain; three failures in 30 min → `bridge_down` event, site shows
  the agent offline honestly, human is paged. Details from the real implementation.

## 5. Key rotation (Phase 8)

- Claude API key, Supabase service role, Vercel token, stream key: locations + rotation steps.
