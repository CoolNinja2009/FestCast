# Setup, rehearsal checklist and troubleshooting

## Requirements

| | |
|---|---|
| OBS Studio | 30+ (tested target: 33). WebSocket server enabled (built in). |
| Python | 3.12+ (`start.bat` creates `.venv`) |
| Node.js | only to build the dashboard once (`start.bat` does it) |
| FFmpeg | optional: verifies recordings (ffprobe) and remuxes MKV→MP4 |
| LibreOffice | optional: PPTX import (otherwise export PDF from PowerPoint) |
| MediaMTX | for wireless TV output: `scripts\get-mediamtx.ps1` (checksum-verified official release) |
| Internet | VDO.Ninja needs it for signalling (camera media itself flows on the LAN when possible) |

## OBS settings that matter

* **Video**: base and output 1920×1080, 30 fps.
* **Output → Recording**: FestCast sets folder, filename, format (MKV) and encoder through the profile and shows
  *requested vs. what OBS reports* on the Recordings page (**Apply to OBS & verify**). After every recording,
  ffprobe reports the real codec/resolution in History.
* **Output → Streaming encoder**: H.264 (NVENC) for the TV output; browsers can't play HEVC/AV1 over WebRTC.
* Do not rename or delete the `FC_*` sources/scenes by hand; FestCast recreates missing ones on reconnect and
  never touches anything else.

## Port

FestCast uses port **8420** (`FESTCAST_PORT`). It refuses to start if another program already owns that port on
127.0.0.1, because OBS's browser sources would otherwise reach that program instead of FestCast.

## Real-OBS smoke test

With FestCast running against OBS: `.venv\Scripts\python scripts\obs_smoke_test.py`. It checks the connection,
camera sources, probe pages, layout transforms and records an 8-second clip that ffprobe verifies, then removes its
test layout. It never changes your programme scene.

## Network

* Phones, TVs and the PC must be on the same network. The address phones use is shown at the bottom left of the
  dashboard. If it picks the wrong adapter (VPN/VirtualBox), set `FESTCAST_PUBLIC_HOST=192.168.x.x` in `.env`.
* Windows Firewall: allow Python on **TCP 8420** (dashboard/links) and MediaMTX on **TCP 8889 + UDP 8189**
  (TV output). Windows asks the first time; choose *Private networks*.
* School Wi-Fi often isolates clients ("AP/client isolation"). If phones can't open the link, ask IT or use a
  dedicated router for the production.

## Rehearsal checklist (do this with the real hardware before the event)

These steps need hardware or a network that the automated tests can't provide:

1. [ ] OBS WebSocket connects: the top bar shows `OBS 33.x`, not `OBS DOWN`.
2. [ ] Create production → three `FC Cam · Camera N` scenes appear in OBS.
3. [ ] **Real phone** (iPhone Safari and Android Chrome): scan QR → Open camera → allow → Start.
       The card turns **Live** within ~10 s, showing the real resolution (expect 1920×1080 in landscape;
       portrait gives 1080×1920 and is letterboxed).
4. [ ] The phone feed is visible in OBS and in the dashboard thumbnail.
5. [ ] Lock the phone screen → the card goes **Disconnected**, a warning appears, the programme does **not**
       change. Unlock → Live again.
6. [ ] Layout template → Take live → OBS programme matches the editor.
7. [ ] Recording to your chosen drive: start, wait 2 min, stop. History shows `h264 1920×1080` from ffprobe.
       Play the file.
8. [ ] TV: `get-mediamtx.ps1`, add a Wireless TV, open the link on the TV, **Start TV output**. Picture within
       a few seconds; tap once for sound. Unplug/replug the TV's Wi-Fi → it reconnects by itself.
9. [ ] Close the FestCast window while recording → OBS keeps recording. Start FestCast again → it shows the
       recording as in progress.
10. [ ] Kill OBS → FestCast shows OBS DOWN; restart OBS → everything reappears.
11. [ ] Run for 1 hour with 3 phones: watch CPU/GPU/NVENC and camera bitrates on Overview.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OBS DOWN: authentication failed` | Password in `.env` must match OBS → WebSocket Server Settings. |
| `OBS not reachable` | OBS isn't running, or its WebSocket server is off. |
| Camera stays **Waiting for phone** | Phone didn't press Start, or is on another network. Re-open the link. |
| Camera **Not monitored** | OBS hasn't loaded its browser source; check the source exists in OBS. |
| Live but below target | The phone picked the front camera or is in portrait. On the phone use VDO.Ninja's settings to pick the rear camera. |
| Phone link says invalid | It was revoked/expired (48 h). Copy a fresh link from the camera card. |
| TV: "Waiting for the programme" | Press **Start TV output**; check the MediaMTX log on Outputs. |
| TV: picture but no sound | Tap the screen once (browsers block autoplay audio). |
| TV never connects | Firewall UDP 8189 / TCP 8889; check OBS stream encoder is H.264. |
| PPTX import fails | Install LibreOffice, or save the deck as PDF and import that. |

Logs: `data\logs\festcast.jsonl`. Settings → **Download support bundle** creates a redacted zip.
