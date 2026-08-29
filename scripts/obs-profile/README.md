# WANTED — OBS configuration for the delivered server

Target: Hetzner auction dedicated server, **Intel Core i5-12500 (6C/12T), Intel UHD Graphics 770
integrated GPU, no discrete GPU**, 64 GB RAM, Windows Server 2025, **no monitor and no HDMI
emulator** — the console session's only display target comes from an indirect display driver
installed by `server-setup.ps1`.

## What is in this folder, and what is not

| File | Status |
|---|---|
| `basic.ini` | **Installed automatically** by `server-setup.ps1` step 15, selected automatically by `run.ps1` (`--profile WASTED`). Every section/key/value format was checked against the OBS Studio 32.2.2 source that reads and writes this file. |
| scene collection JSON | **Deliberately not authored.** Built by hand from the checklist below — but it must be named `WASTED`, because `run.ps1` passes `--collection WASTED`. |

The scene collection (`%APPDATA%\obs-studio\basic\scenes\*.json`) is a serialization of each
source plugin's own settings object — `game_capture`, `wasapi_output_capture`, `browser_source`
and so on each define their own keys. There is no OBS install on the machine this was authored
on, so those keys could not be verified, and a scene collection with a wrong key silently loads
as an empty or broken scene. Writing one from memory would be exactly the kind of unverified
guess this project does not ship. The checklist below is short; do it once in the UI.

### Where `basic.ini` was verified from

Source files are from OBS Studio **32.2.2** (the current release; `winget install
OBSProject.OBSStudio` gets it):

- `frontend/OBSApp.cpp` — the config root (`GetAppConfigPath` ⇒ `%APPDATA%` on Windows) and
  `OBSProfileSubDirectory = "obs-studio/basic/profiles"` /
  `OBSScenesSubDirectory = "obs-studio/basic/scenes"`.
- `frontend/widgets/OBSBasic_Profiles.cpp` — profile config file name (`basic.ini`), one
  directory per profile, and the fact that a profile's **name** is
  `config_get_string(config, "General", "Name")`, *not* the directory name.
- `frontend/widgets/OBSBasic.cpp` (`InitBasicConfigDefaults`) — the literal section and key
  names and default value formats for `[Output]`, `[SimpleOutput]`, `[Video]`, `[Audio]`.
- `frontend/widgets/OBSBasic.hpp` — the `SIMPLE_ENCODER_*` string values (`x264`, `qsv`, …).
- `frontend/obs-main.cpp` — the command-line parser: `--profile <string>`,
  `--collection <string>`, `--scene <string>`, `--startreplaybuffer` are real options, and
  unrecognised arguments are ignored rather than rejected.
- `frontend/widgets/OBSBasic_SceneCollections.cpp` — a collection is one `.json` under
  `basic/scenes`, matched on the `"name"` field inside the file.

Anything OBS does not recognise in this file falls back to the built-in default rather than
breaking, so the worst case of a stale key is that you get OBS's default for that one setting.

## Installing the profile

**`server-setup.ps1` does this for you** (step 15): it copies `basic.ini` to
`%APPDATA%\obs-studio\basic\profiles\WASTED\basic.ini`, backing up any existing copy and
skipping when the file is already identical. `%APPDATA%` is per-user, so run it as the account
that will run the stream — setup warns when that is not `-AutologonUser`.

By hand, if you ever need to:

```powershell
$dir = Join-Path $env:APPDATA 'obs-studio\basic\profiles\WASTED'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
Copy-Item .\obs-profile\basic.ini (Join-Path $dir 'basic.ini') -Force
```

## How the profile actually gets used

`run.ps1` launches OBS with:

```
--profile WASTED --collection WASTED --startreplaybuffer --disable-shutdown-check
```

Both names are resolved **by name**, and OBS **silently** keeps the last-used profile / scene
collection when a name does not exist — no error, no dialog. That failure mode is a stream at the
wrong resolution and bitrate with no replay buffer, so:

- `run.ps1` checks both before launching and warns with the exact remedy if either is missing.
- `bootstrap.ps1`'s `obs` phase will not complete until `obs64.exe`, a profile whose
  `[General] Name` is `WASTED`, **and** a scene collection whose `"name"` is `WASTED` all exist.

If you rename either, change it in all three places (`server-setup.ps1 -ObsProfileName`,
`run.ps1 -ObsProfile`/`-ObsSceneCollection`, `bootstrap.ps1 -ObsProfileName`).

## The encoder settings, and why

| Setting | Value | Why |
|---|---|---|
| Output mode | Simple | Nothing here needs the advanced pane; fewer knobs to drift. |
| Stream encoder | `x264` | Quick Sync needs a healthy Intel graphics driver on a Server SKU — the single least certain thing on this machine — and Intel's encode offload is reported to underperform on Server, with work falling back to the CPU anyway. Plan for x264, treat QSV as an upside. |
| Preset | `veryfast` | At 720p30 this costs roughly one to two of the six cores, leaving headroom for GTA V and the harness. Do not go slower while the game shares this CPU. |
| Video bitrate | 3500 kbps | Inside every platform's 720p envelope and trivially inside a 1 Gbit NIC. |
| Audio bitrate | 160 kbps | OBS default; no reason to deviate. |
| Canvas / output | 1280×720 | Matches the game (`settings.xml` 1280×720, `Windowed=2`) and the virtual display driver's first listed mode, so there is no rescale pass. |
| FPS | 30 | 30 fps halves encoder cost versus 60 and is right for commentary-paced content. |
| Replay buffer | on, 30 s, 1024 MB cap | Covers a death plus its run-up. `run.ps1` starts OBS with `--startreplaybuffer`; the harness triggers saves over obs-websocket and reads the file path out of the `ReplayBufferSaved` event, so the filename is discovered rather than configured. |

**Upgrade path to Quick Sync** (only after the Intel driver is verified and the stream has been
stable on x264): set `StreamEncoder=qsv` and `RecEncoder=qsv` in `basic.ini`, restart OBS, and
re-measure both CPU usage and dropped frames before committing. Roll back if either gets worse.

## Scene collection checklist (do once, in the UI)

Create a scene collection named **exactly `WASTED`** (that is the name `run.ps1` passes as
`--collection`; anything else and OBS quietly keeps whatever scenes it had) with one scene,
`Live`, containing these sources — **in this order, top to bottom**:

1. **Overlay** — Browser source.
   - URL `http://127.0.0.1:7788/overlay` (CONTRACTS.md §6), width `1280`, height `720`.
   - Leave the background transparent; the overlay page is authored that way.
   - Tick "Shutdown source when not visible" off — the SSE stream should stay connected.

2. **Game** — **Game Capture**.
   - Mode: *Capture specific window*, Window: `[GTA5.exe]: Grand Theft Auto V`.
   - Untick **Capture Cursor**.
   - Game Capture hooks the game's Direct3D 11 swapchain directly. It does **not** depend on
     Desktop Duplication or on the desktop compositor, which is what makes it the right choice
     when the only display is synthetic. This is the primary capture path.

3. **Game (fallback)** — **Window Capture**, *disabled* (eye closed).
   - Capture Method: **Windows Graphics Capture**, Window: `[GTA5.exe]`.
   - WGC survives RDP session changes far better than Desktop Duplication does. Enable this and
     disable Game Capture only if the hook fails.

4. **Do NOT add a Display Capture source.** It uses DXGI Desktop Duplication, needs a real
   output in the *active* console session, and throws `DXGI_ERROR_SESSION_DISCONNECTED` /
   `ACCESS_LOST` whenever anyone attaches or detaches RDP. On a box whose display is virtual it
   is the most fragile option available.

5. **Game audio** — **Audio Output Capture**.
   - Device: **`CABLE Output (VB-Audio Virtual Cable)`**, selected explicitly.
   - Never leave it on *Default*: RDP audio redirection hijacks Default and the stream goes
     silent the moment someone connects.
   - VB-CABLE only appears after `AudioEndpointBuilder`, `Audiosrv` and `MMCSS` are running and
     the machine has been rebooted — `server-setup.ps1` does the services, the reboot is yours.

## WebSocket and stream key

- **Tools → WebSocket Server Settings**: enable, bind `127.0.0.1:4455`, set a password. That
  password goes in the harness `.env` (gitignored) and nowhere else.
- **Settings → Stream**: type the stream key by hand. OBS stores it in the profile's
  `service.json`. No script in this repo reads, writes or logs it, and it is never committed.

## Known risks specific to this machine

| Risk | Symptom | Fallback |
|---|---|---|
| OBS is not supported on Server SKUs and is designed as a local app | Misbehaves when driven through an RDP session | Do setup over RDP, then leave with `detach-rdp.ps1`; run the stream from the console session only |
| Audio devices missing | No `CABLE Output` in the device list | Enable the three audio services, install VB-CABLE, reboot, *then* configure OBS — in that order |
| Game Capture hook fails | Black game source | Enable the Window Capture (WGC) fallback source |
| CPU saturated by x264 + game | Dropped/skipped frames in OBS stats | Drop to 2500 kbps, then to 720p24, then try `qsv` once the Intel driver is verified |
| Virtual display drops its monitor | Everything black; `watchdog.ps1` logs "NO adapter reports a desktop mode of at least 1280x720" | Relaunching will not help — repair the display device (see `scripts/README.md`, "Display stack") |
