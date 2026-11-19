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

## Running for 9+ hours: what FestCast handles and what you must do

**Handled by FestCast automatically**
- Windows sleep / screen-off is blocked while FestCast runs.
- `start.bat` restarts FestCast within 3 s if it ever crashes; OBS keeps recording through it and FestCast
  re-attaches (recording state, cameras, layouts, TV output) on restart.
- Background loops (metrics, standby rules, previews, OBS connection) are supervised and restarted if one dies.
- TV output: if MediaMTX dies, or OBS's stream to it drops or stops delivering pictures for 15 s, it is restarted
  (backoff 5 s → 60 s). It also resumes after FestCast or OBS restarts.
- Cameras: a camera that had a picture but shows none for 45 s gets a fresh viewer automatically (only cameras
  that are already down are touched).
- Recording: OBS records (MKV survives crashes and power loss up to the last seconds). FestCast warns if, at the
  bitrate OBS is really writing, the drive fills before the planned show length (Recordings → Show length).

**Your checklist on the day (things FestCast cannot control)**
1. **Windows Update**: Settings → Windows Update → *Pause updates* (1 week). An update restart ends the show.
2. **Notifications**: turn on *Do not disturb*; close Discord, Steam, game launchers and other heavy apps.
3. **Power**: plug the PC in; Settings → Power → *Best performance*.
4. **Recording drive**: pick one with room for 9 h. Check the *recording bitrate* on the Recordings page after
   1 minute: GB needed ≈ Mb/s × 4 (for 9 h). E.g. 40 Mb/s → ~160 GB. Use your big drive, or set OBS to
   1920×1080 @ 30 fps and "Small" quality to roughly halve it.
5. **OBS**: Settings → Advanced → *Automatically reconnect* on. Don't open OBS's settings dialog while live.
6. **Network**: PC on **Ethernet** to the router if at all possible; phones close to the router; a dedicated
   router for the production if the school Wi-Fi is busy.
7. **Phones**: on chargers/power banks, auto-lock off, Low Power Mode off, out of direct sun/stage lights.
8. Run `scripts\soak_test.py --minutes 540` the night before on the real setup (see below).

## Soak test

With FestCast running from `start.bat`: `.venv\Scripts\python scripts\soak_test.py --minutes 540` runs a full 9-hour
rehearsal: recording, TV output, programme switching, dashboard churn, with ground truth read straight from OBS
and MediaMTX. Add `--kill-mtx-at 60 --kill-festcast-at 120` to inject crashes and prove recovery. It prints memory
trends extrapolated to 9 hours and writes a CSV to `data\soak\`, then cleans up after itself.

## OBS settings that matter

* **Video**: base and output 1920×1080, 30 fps.
* **Output → Recording**: FestCast sets folder, filename, format (MKV) and encoder through the profile and shows
  *requested vs. what OBS reports* on the Recordings page (**Apply to OBS & verify**). After every recording,
  ffprobe reports the real codec/resolution in History.
* **Output → Streaming encoder**: H.264 (NVENC) for the TV output; browsers can't play HEVC/AV1 over WebRTC.
* Do not rename or delete the `FC_*` sources/scenes by hand; FestCast recreates missing ones on reconnect and
  never touches anything else.

## Port

Ports are automatic, so FestCast runs on any PC and any network without editing config. It prefers **8420** (and
the port it used last time, so printed QR codes keep working), and moves to the next free port if another program
owns it. "Owns" includes programs listening only on 127.0.0.1, which OBS's browser sources would otherwise reach
instead of FestCast. The addresses are printed in the FestCast window and shown at the bottom left of the dashboard.
MediaMTX ports (preferred 8889 / 8189 / 9997) are chosen the same way and shown on the Outputs page.
Force a port with `FESTCAST_PORT=…` in `.env`.

**New network / venue:** the phone/TV address follows the PC's current IP. Print the QR sheet and copy links
*at the venue*, after the PC is on the festival network.

## Real-OBS smoke test

With FestCast running against OBS: `.venv\Scripts\python scripts\obs_smoke_test.py`. It checks the connection,
camera sources, probe pages, layout transforms and records an 8-second clip that ffprobe verifies, then removes its
test layout. It never changes your programme scene.

## Network

* Phones, TVs and the PC must be on the same network. The address phones use is shown at the bottom left of the
  dashboard. If it picks the wrong adapter (VPN/VirtualBox), set `FESTCAST_PUBLIC_HOST=192.168.x.x` in `.env`.
* Windows Firewall: allow Python (dashboard/links) and MediaMTX (TV output) when Windows asks
  (TV output). Windows asks the first time; choose *Private networks*.
* **Local cameras** (the default) never touch the internet: the phone sends straight to MediaMTX on this PC
  (UDP 8189), so picture quality is only limited by the Wi-Fi. The camera page is
  `https://<PC IP>:8443/cam/...` with FestCast's own certificate (`data/tls/`): the first time, each phone shows a
  security warning; tap **Advanced → Proceed** (Chrome) or **Show Details → visit this website** (Safari).
  Per camera you can switch back to VDO.Ninja with the **Local / VDO.Ninja** selector on the camera card.
* For a 9-hour show use a dedicated 5 GHz router (client isolation off), the PC on a cable, phones near the router.
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
| Picture sideways / portrait with black bars | iPhone Portrait Orientation Lock is on. In the layout editor (Production page), click the camera tile: **⟳ Landscape/Portrait** turns the tile portrait so an upright picture fills it; **Turn picture** fixes a picture lying on its side, or have the operator unlock rotation and re-open the link. For a deliberately upright shot use **Fill** to crop to 16:9. |
| Live but below target | The phone picked the front camera or is in portrait. Local camera page: **Switch camera**. VDO.Ninja: use its settings to pick the rear camera. |
| Phone link says invalid | It was revoked/expired (48 h). Copy a fresh link from the camera card. |
| TV: "Waiting for the programme" | Press **Start TV output**; check the MediaMTX log on Outputs. |
| Echo / hum / strange sound from the PC speakers | Don't play the TV page (or OBS monitoring) on the production PC's own speakers while OBS captures **Desktop Audio**: the sound loops back into the programme. Test TV pages on the TV, or mute Desktop Audio in FestCast's Audio panel. |
| TV: picture but no sound | Tap the screen once (browsers block autoplay audio). |
| TV never connects | Firewall UDP 8189 / TCP 8889; check OBS stream encoder is H.264. |
| Local camera: phone page says "Waiting for FestCast" or never goes LIVE | MediaMTX must be running (Outputs page); firewall must allow MediaMTX (UDP 8189) and Python (TCP 8443); phone and PC on the same network. |
| Local camera: low resolution / frame rate | The phone page shows what it sends and why it is limited ("limited by bandwidth" = Wi-Fi, "cpu" = phone too hot/slow: lower to 720p). |
| PPTX import fails | Install LibreOffice, or save the deck as PDF and import that. |

Logs: `data\logs\festcast.jsonl`. Settings → **Download support bundle** creates a redacted zip.
