# FestCast architecture

A modular monolith: one Python process (FastAPI + asyncio) and a static TypeScript dashboard.

## Responsibilities

| Concern | Owner | Notes |
|---|---|---|
| Rendering, encoding, recording, streaming | **OBS** | FestCast never encodes video. |
| Camera transport | **Local (MediaMTX)**, default; or **VDO.Ninja** per camera | `transports/local.py`, `transports/vdoninja.py` (`CameraInputAdapter`) |
| Programme distribution | **MediaMTX** | OBS → WHIP → MediaMTX → WHEP → TV browser |
| Local cameras | **MediaMTX** | phone `/cam/<token>` (https) → WHIP via FestCast → MediaMTX path `cam_<stream_id>` → WHEP → `/obs/cam/<id>` in OBS |
| Orchestration, state, UI | **FestCast** | `backend/` + `frontend/` |

## Backend layout

```
backend/
  main.py                 app factory, auth middleware, error mapping, CLI
  config.py               Settings (FESTCAST_* env / .env)
  core/events.py          EventBus → /api/ws, notifications, JSON log ring + file
  core/context.py         dependency container; wires OBS events to services; previews loop; diagnostics
  obs/client.py           native asyncio OBS WebSocket v5 client (Hello/Identify/Request/Event)
  obs/mock.py             in-memory OBS implementing the same request names (simulation + tests)
  obs/controller.py       reconnect loop, cached state, ownership registry, typed helpers
  services/cameras.py     production/room, camera slots, invitations, OBS sources, probe stats → status
  services/layouts.py     normalised layouts ↔ OBS scenes via SetSceneItemTransform
  services/production.py  preview/programme/transitions + standby rules engine
  services/recording.py   OBS profile config, confirmed state, history, metadata.json, ffprobe, remux
  services/outputs.py     receivers, MediaMTX process, WHEP proxy, HDMI projectors
  services/presentations.py  PDF/PPTX/image import → PNG slides (threaded)
  services/audio.py       mixer, meters, clipping warnings; hall inputs (mic in / line in)
  services/metrics.py     central aggregation loop + rolling history
  api/admin.py            operator REST API
  api/public.py           /join, /tv, /obs pages, token-gated JSON, login, /api/ws
  public/*.html           phone page, OBS probe page, OBS standby player
receiver/tv.html          TV WHEP player
```

## Key decisions

* **Own asyncio OBS client** instead of obsws-python: obsws-python is synchronous (thread per call); the protocol is
  small and a native client keeps the event loop free and gives us events on the same loop.
* **No VDO.Ninja room for phones.** Room guests also pull each other's video (wasted phone uplink). Each camera is
  `?push=<random id>&password=<production secret>`; OBS pulls `?view=<id>&password=…`. The FestCast production is the
  logical room. Parameters (`push`, `view`, `password`, `label`, `webcam`, `facing=rear`, `width`, `height`, `fps`,
  `videobitrate`, `cleanoutput`) were checked in vdo.ninja `main.js`.
* **OBS-side probe.** Each camera's OBS browser source loads `http://127.0.0.1:<port>/obs/cam/<id>`, which embeds the
  VDO.Ninja viewer and asks it for stats through the documented IFRAME API (`postMessage({getStats:true})` →
  `{stats:{inbound:{…}}}`, verified live). So camera health is real and measured even with no dashboard open, and the
  stream id can rotate (revocation) without touching the OBS source.
* **Rotation / fit in the probe page.** Phones with orientation lock send sideways 1080×1920 frames; rotating the
  16:9 browser source in OBS would rotate baked-in black bars too. The probe page sizes the VDO.Ninja iframe to the
  video's real aspect (from getStats) and rotates/fits/fills it with CSS, per camera (`rotation`, `frame`).
* **Previews via OBS screenshots** (`GetSourceScreenshot`, ~0.7 fps) instead of extra WebRTC viewers, so each phone
  uploads exactly one stream.
* **Hall audio inputs** (mic in / line in, Audio page). OBS captures them itself (`wasapi_input_capture`,
  `use_device_timing: false`): no extra hop, and a drifting sound-card clock cannot raise OBS's audio buffering.
  Processing is zero-latency only: noise gate → gain → limiter (`FC Gate`/`FC Gain`/`FC Limiter`, no look-ahead);
  noise suppression is not offered (10-20 ms+). Inputs `FC_AUDIO_<id>` live in the owned scene `FC Audio`, nested into
  every FestCast scene (layouts, camera scenes, standby), so the hall sound stays on air through every cut and
  transition (the same source in both scenes: no dip). Config in KV `audio_inputs`. Track 1 = programme; an input can
  also get its own isolated track 2-6: owned inputs leave that track, non-owned ones are only reported, and in OBS
  Advanced output mode `AdvOut.RecTracks` gains those bits at record start (OR-ed, never removing operator tracks).
* **Unknown ≠ zero.** Metrics are `None` when not measured. VDO.Ninja does not always report packet loss; the UI shows
  "—" rather than inventing 0%.
* **Ownership registry** (`obs_owned` table). FestCast only removes/renames inputs and scenes it created. Names:
  `FC_CAM_<id>`, `FC_GRAPHIC_<id>`, `FC_STANDBY_main`; scenes `FC Cam · <name>`, `FC Layout · <name>`, `FC Standby`.
* **Layouts are real OBS scenes.** Elements are stored normalised (0..1); sync uses top-left alignment and bounds
  (`OBS_BOUNDS_SCALE_INNER` = fit, `OBS_BOUNDS_STRETCH`), crop in source pixels. Edits to a live layout are held
  until "Push to air" (default).
* **OBS output reload.** OBS 33 caches recording format/encoder/quality in its outputs: changing the profile alone
  still wrote the old container (seen on the real PC: "mkv" requested, MP4 written). When those settings change and
  nothing is recording/streaming, FestCast switches to a temporary profile and back (waiting for each async switch),
  which rebuilds the outputs. ffprobe after every recording warns if the container still differs.
* **Any OBS canvas.** Layouts are normalised and full-screen camera/standby items are fitted to the canvas, so a
  2560×1440 canvas works; 1920×1080 @ 30 is still the recommended production setting.
* **Recording truth comes from OBS events.** `StartRecord` is only reported after `RecordStateChanged: STARTED`;
  stops not requested by FestCast raise an error notification; restarts reconcile open rows.
* **TV output** uses OBS's streaming output (WHIP), so OBS cannot stream elsewhere at the same time. TVs post their
  WHEP offer to FestCast, which checks the receiver token and forwards it to MediaMTX on localhost; MediaMTX only
  accepts loopback. Media then flows MediaMTX → TV over UDP 8189.
* **Tally lights** (`services/tally.py`): a camera is "program"/"preview" when its OBS input `FC_CAM_<id>` is a visible
  item of the programme / preview scene, directly or through nested scenes and groups (read with `GetSceneItemList`,
  never inferred from FestCast's own records). Recomputed on OBS scene / scene-item events and every 1 s; OBS down =
  "unknown". Phones (cam.html, vcam.html) long-poll `/api/public/cam/<token>/tally?v=<version>` (answers at once on
  change, else after 20 s), so no WebSocket is needed on the https listener.
* **Live streaming to platforms** (`services/live.py`) can't use OBS's stream output (the TVs have it), so it is fed
  from MediaMTX: one ffmpeg per destination reads the programme over RTSP (loopback, TCP only) and pushes it to
  RTMP/RTMPS (FLV) or SRT (MPEG-TS). Video is `-c:v copy` (FestCast still never encodes video); audio Opus → AAC.
  Going live starts the TV output if needed; the TV output can't be stopped under a live stream. Each destination is
  independent and restarted with backoff (2 → 30 s) when ffmpeg exits or sends nothing for 20 s. "Sending" means
  ffmpeg's `-progress` byte count is growing; the platform's side is not claimed. Destinations + keys in KV
  `live_destinations`; keys masked in views, redacted from ffmpeg output and diagnostics. `live_on` survives restarts;
  ffmpegs left by a killed FestCast (`data/live_pids.json`) are stopped before resuming, so two never share a key.
* **Security.** Loopback is the operator; remote operators need `FESTCAST_ADMIN_PASSWORD`; phones/TVs only get
  random revocable tokens; `/obs/*` pages need loopback + a local key; OBS password never leaves the server;
  support bundles redact stream ids, tokens and passwords.
* **SQLite** with Alembic migrations (applied on start, never dropping data). Queries are sub-millisecond and run
  inline; file I/O, rendering, ffmpeg and nvidia-smi run in threads/subprocesses.

## Known limitations

* VDO.Ninja's public signalling server needs internet. Self-hosting VDO.Ninja is possible later (change
  `FESTCAST_VDO_BASE`).
* A resolution change applies when the operator re-opens the link; FestCast shows what is actually received.
* PPTX → images loses animations (by design; preserves originals).
* MediaMTX runs as a child of FestCast: restarting FestCast briefly interrupts TVs (they reconnect). Recording is
  unaffected.
* Segmenting recordings needs OBS Advanced output mode.
