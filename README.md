# FestCast

Live production control for the school festival. FestCast orchestrates; **OBS** renders, records and encodes;
**VDO.Ninja** carries the phone cameras; **MediaMTX** delivers the programme to TV browsers.

```
Phones ──VDO.Ninja (WebRTC)──▶ OBS browser sources ──▶ OBS scenes ──▶ Recording (MKV/NVENC, your drive)
                                   ▲  probe page reports real stats         └──WHIP──▶ MediaMTX ──WHEP──▶ TVs
FestCast (FastAPI + dashboard) ── OBS WebSocket v5 ──┘   HDMI: OBS fullscreen projector
```

## Quick start (Windows)

1. **OBS**: Tools → WebSocket Server Settings → enable, port 4455, authentication on, copy the password.
   Settings → Video: 1920×1080, 30 fps. Settings → Output: Recording encoder NVENC H.264.
2. Copy `.env.example` to `.env` and set `FESTCAST_OBS_PASSWORD`.
3. *(TV output only)* `powershell -ExecutionPolicy Bypass -File scripts\get-mediamtx.ps1`
4. Double-click **`start.bat`**, then open http://localhost:8420.

To try it without OBS, run `start-simulation.bat` (everything is labelled SIMULATION).

Fedora: `./start.sh` (needs python3.12+, nodejs, obs-studio; MediaMTX: download `linux_amd64` from the same release
into `tools/mediamtx/`).

## Event-day flow

1. **Cameras → Create production** (3 slots by default). **Print QR sheet** or **Copy link** per camera.
2. Each operator opens the link on the festival Wi-Fi → **Open camera** → allow → rear camera → **Start**.
   The card turns **Live** with the *received* resolution/FPS/bitrate.
3. **Production**: pick a template in the layout editor, adjust, **Preview → TAKE**.
4. **Recordings**: pick a drive, **Apply to OBS & verify**, **Start** (or enable auto-start with the production).
5. **Outputs**: **+ Wireless TV** → open its link on the TV → **Start TV output**.
6. **Overview → Start production**. Keep the big **STANDBY** button in mind.

See [docs/SETUP.md](docs/SETUP.md) for details and the hardware rehearsal checklist, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works.

## Tests

```
.venv\Scripts\python -m pytest -q          # 86 backend tests against the mock OBS
cd frontend && npm run typecheck && npm run build
```
