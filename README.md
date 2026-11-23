# FestCast

Live production control for the school festival. FestCast orchestrates; **OBS** renders, records and encodes;
phones are the cameras (through this PC, or VDO.Ninja); **MediaMTX** delivers the programme to TV browsers and
YouTube / Twitch / any RTMP server. Camera phones get tally lights (red = on air, green = preview).

Runs on **Windows, Linux and macOS**.

## Download

Get the zip for your system from [Releases](../../releases/latest): `FestCast-<version>-windows.zip`,
`-linux-x64`, `-linux-arm64`, `-macos-arm64` (Apple silicon) or `-macos-x64` (Intel). Unzip and run `start.bat`
(Windows), `start.command` (macOS) or `./start.sh` (Linux). Nothing to install except OBS Studio: each zip carries its
own Python, the built dashboard and MediaMTX. See the `README.txt` inside.

```
Phones ──VDO.Ninja (WebRTC)──▶ OBS browser sources ──▶ OBS scenes ──▶ Recording (MKV/NVENC, your drive)
                                   ▲  probe page reports real stats         └──WHIP──▶ MediaMTX ──WHEP──▶ TVs
FestCast (FastAPI + dashboard) ── OBS WebSocket v5 ──┘   HDMI: OBS fullscreen projector
```

## Run from source (Windows)

1. **OBS**: Tools → WebSocket Server Settings → enable, port 4455, authentication on, copy the password.
   Settings → Video: 1920×1080, 30 fps. Settings → Output: Recording encoder NVENC H.264.
2. Copy `.env.example` to `.env` and set `FESTCAST_OBS_PASSWORD`.
3. *(TV output only)* `powershell -ExecutionPolicy Bypass -File scripts\get-mediamtx.ps1`
4. Double-click **`start.bat`**. It picks a free port, prints the addresses and opens the dashboard.

To try it without OBS, run `start-simulation.bat` (everything is labelled SIMULATION).

Fedora: `./start.sh` (needs python3.12+, nodejs, obs-studio; MediaMTX: download `linux_amd64` from the same release
into `tools/mediamtx/`).

## Manual

The full operator manual is built into FestCast: **Manual** in the dashboard sidebar, or `/manual` (works
offline, printable, includes a one-page camera operator sheet). Source: `backend/public/manual.html`.

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
.venv\Scripts\python -m pytest -q          # backend tests against the mock OBS
cd frontend && npm run typecheck && npm run build
```

## Releases

Automatic (`.github/workflows/release.yml`): **every push to `main` that changes the app** is tested, built on
Windows, Linux (x64, arm64) and macOS (arm64, Intel), smoke-tested and published as a GitHub release with all zips
and `SHA256SUMS.txt`.

* Version: `pyproject.toml`'s version the first time, then the next patch each push (1.0.0, 1.0.1, 1.0.2 ...).
  Set `version = "1.1.0"` (or `2.0.0`) in `pyproject.toml` to start a new series.
* Not released: pushes that only change markdown/docs, `LICENSE`, `tests/` or editor files, and any commit whose
  message contains `[skip ci]`.
* A specific version: `git tag v1.2.3` and `git push origin v1.2.3` (a tag with `-`, like `v1.3.0-beta.1`, is a
  pre-release).
* Build without releasing: **Actions → Release → Run workflow** (zips kept as workflow artifacts).

Each zip is made by `packaging/build.py` (python-build-standalone CPython via uv, dependencies, built dashboard,
MediaMTX pinned and checksum-verified, LICENSE, THIRD-PARTY-NOTICES.txt, README.txt) and checked by
`packaging/smoke.py`. Local build: `python packaging/build.py --python-dir <uv python install dir>`.

## Licence

FestCast is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE).
