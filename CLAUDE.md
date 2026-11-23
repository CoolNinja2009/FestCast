# FestCast: project notes

Live production system for a school festival. Read `docs/ARCHITECTURE.md` first (decisions and why).

## Commands

- Run: `start.bat` (real OBS), `start-simulation.bat` (mock OBS), or `.venv\Scripts\python -m backend.main --simulation --port 8010 --data-dir data-sim`
- Backend tests: `.venv\Scripts\python -m pytest -q` (all against `obs/mock.py`; fast)
- Frontend: `cd frontend && npm run typecheck && npm run build` (the backend serves `frontend/dist`)
- Release: tag `v*` and push -> `.github/workflows/release.yml` (tests, 5 platform zips via `packaging/build.py`,
  smoke test, auto-published GitHub release). Bundled Python always runs with `-E -s` (no user site / PYTHON* leaks).
  No Claude co-author lines in commits (GPL-3.0 repo on GitHub).
- New DB column: edit `backend/database/models.py`, then `.venv\Scripts\python -m alembic revision --autogenerate -m "..."`

## Conventions

- OBS is the source of truth for programme/recording state; never report a state OBS hasn't confirmed.
- Only touch OBS objects in the `obs_owned` registry (`OBSController.register/remove_owned_*`).
- Metrics: `None` = unknown. Never substitute 0 or demo values.
- Every OBS request name/field must exist in obs-websocket v5 `protocol.md`; mirror new ones in `obs/mock.py`.
- No automatic programme switching except the operator-enabled standby rules.
- Frontend: vanilla TS, `h()` helper, `live(el, topics, render)` regions fed by `/api/ws` topics
  (`cameras`, `obs`, `production`, `layouts`, `recording`, `outputs`, `live`, `presentations`, `metrics`, `previews`, `meters`).
  Regions that must outlive a page register outside `disposers`.
- Secrets live in `.env` (gitignored). Never log tokens/stream ids; diagnostics redact them.

## Status (2026-11-19)

Verified: 97 backend tests; dashboard click-through in simulation and on real OBS 33.0.1 (WS 5.7.6);
`scripts/obs_smoke_test.py` all-pass on the real PC (sources, probe pages inside OBS, layout transforms,
NVENC H.264 1920x1080 MKV verified by ffprobe); probe page against live vdo.ninja; phone page hand-off.
TV output verified end to end on the real PC: OBS 33 WHIP -> MediaMTX 1.21.4 -> WHEP TV page (1920x1080, 0 loss);
stop restores OBS's stream service exactly (OBS rejects empty streamServiceSettings: restore uses {"key": ""}).
Not yet verified: real phone feed (see docs/SETUP.md checklist).
Ports are automatic (`backend/core/ports.py`, remembered in `data/ports.json`); a port someone owns on 127.0.0.1 counts as
busy (this PC has a service on 127.0.0.1:8000). OBS canvas here is 2560x1440@60 -> 1080p output.
Cameras: unlimited slots (+ Add camera); templates up to 6 plus an auto-grid for any count.
Local camera transport (default, `FESTCAST_CAMERA_TRANSPORT`): phone https page `/cam/<token>` (second uvicorn
listener, self-signed cert in data/tls, port 8443+) -> WHIP via FestCast -> MediaMTX `cam_<stream_id>` -> WHEP in the
OBS page. Verified in simulation with real MediaMTX: 1920x1080 30 fps H.264 ~6-7 Mbps; revoke kicks the publisher.
Not yet verified on real phones (self-signed cert prompt on Android/iOS). iOS refuses a cert without EKU serverAuth or
valid > 825 days with no proceed option (page just never loads): `core/tls.py` makes 396-day certs with EKU and
replaces older ones on start. iOS 27 also won't open https on the `.local` name (stuck loading) but does on the
IP, so the camera page link is always `https://<LAN IP>:<https_port>` (`Ctx.https_base`); verified on a real iPhone.
Camera OBS sources have the picture's own shape (`CameraService.source_size`: tall for an upright phone, canvas until
known); layout tiles just fit them, and the editor draws the same (contain). Reshaped + layouts re-synced when a
camera's aspect/rotation/frame changes. Auto-turn: phone pages report how they're held (`held`, motion sensor) to
`/api/public/cam/<token>/orientation`; `CameraService.apply_held` turns only while received frames are tall. Local:
cam.html; VDO.Ninja: join opens https `/vcam/<token>` (vcam.html, VDO.Ninja push page in an iframe + sensor).
Layout "Auto arrange cameras" (`layout_auto:<id>` in KV, `layouts.arrange`): camera tiles placed from picture shapes.
Not yet verified on a real phone: sensor sign (iOS inverted, learnt when upright), camera in iframe on iOS.
Auto-turn has an operator switch per camera (`cam_autoturn:<id>` KV, default on; Cameras page + layout editor show
what the phone last reported). Latency: OBS raises audio buffering (max 960 ms, never lowered until OBS restarts)
when an audio source lags, and the stream video waits for it; `metrics.ObsLogWatch` reads OBS's log and warns.
2026-11-20 real PC: 960 ms caused by Desktop Audio. TV page sets jitterBufferTarget 100 ms.

Audio page (2026-11-22): hall inputs mic in / line in as OBS WASAPI capture (`AudioService` in services/audio.py,
`/api/audio-inputs`), nested `FC Audio` scene in every FC scene, optional isolated recording tracks 2-6.
Verified in simulation only; not yet on the real PC (real device ids, Advanced-mode RecTracks, buffer stays low).

Tally lights (2026-11-23): camera phones (local cam.html + VDO.Ninja vcam.html) show a red border when on programme, green
in preview (`services/tally.py`, from OBS scene items incl. nested scenes; long-poll `/api/public/cam/<token>/tally`).
Verified in simulation (green 53 ms after a cut); not yet on real phones.
Live streaming (2026-11-23): Outputs page "Live stream" -> YouTube/Twitch/Facebook/Kick/any RTMP(S)/SRT, several at
once, alongside the TVs (MediaMTX RTSP on loopback -> ffmpeg per destination, video copied, audio -> AAC; topic `live`).
Verified with real MediaMTX 1.21.4 + ffmpeg 8.1 (WHIP in -> RTMP out: H.264 copied + AAC 48 kHz) and in simulation;
not yet against a real platform or with real OBS as the source.

Long-run (2026-11-19, real OBS): `scripts/soak_test.py` 30 min with MediaMTX killed at 8 min and FestCast hard-killed
at 16 min -> 0 OBS recording gaps; 8 min run with FestCast killed -> TV outage 0 s (restarted FestCast re-attaches to
the running MediaMTX). Self-healing: start.bat supervisor, supervised loops, TV output watchdog (+ resume via
`tv_output_on`), camera viewer remount after 45 s without video, Windows keep-awake, show-length disk warning.
A full 9 h soak (`--minutes 540`) has not been run yet.
OBS autostart (`obs/launcher.py`, 2026-11-22): FestCast starts OBS if not running with `--profile FestCast
--collection FestCast` (profile copied once from the user's current one, without service.json); a detached,
double-spawned watcher restores user.ini [Basic] when that OBS exits (`data/obs_session.json`, also restored on the next
start). Warns on connect if OBS is on another collection. Unit-tested only; not yet run end to end on the real PC.
