# FestCast: project notes

Live production system for a school festival. Read `docs/ARCHITECTURE.md` first (decisions and why).

## Commands

- Run: `start.bat` (real OBS), `start-simulation.bat` (mock OBS), or `.venv\Scripts\python -m backend.main --simulation --port 8010 --data-dir data-sim`
- Backend tests: `.venv\Scripts\python -m pytest -q` (all against `obs/mock.py`; fast)
- Frontend: `cd frontend && npm run typecheck && npm run build` (the backend serves `frontend/dist`)
- New DB column: edit `backend/database/models.py`, then `.venv\Scripts\python -m alembic revision --autogenerate -m "..."`

## Conventions

- OBS is the source of truth for programme/recording state; never report a state OBS hasn't confirmed.
- Only touch OBS objects in the `obs_owned` registry (`OBSController.register/remove_owned_*`).
- Metrics: `None` = unknown. Never substitute 0 or demo values.
- Every OBS request name/field must exist in obs-websocket v5 `protocol.md`; mirror new ones in `obs/mock.py`.
- No automatic programme switching except the operator-enabled standby rules.
- Frontend: vanilla TS, `h()` helper, `live(el, topics, render)` regions fed by `/api/ws` topics
  (`cameras`, `obs`, `production`, `layouts`, `recording`, `outputs`, `presentations`, `metrics`, `previews`, `meters`).
  Regions that must outlive a page register outside `disposers`.
- Secrets live in `.env` (gitignored). Never log tokens/stream ids; diagnostics redact them.

## Status (2026-11-19)

Verified: 96 backend tests; dashboard click-through in simulation and on real OBS 33.0.1 (WS 5.7.6);
`scripts/obs_smoke_test.py` all-pass on the real PC (sources, probe pages inside OBS, layout transforms,
NVENC H.264 1920x1080 MKV verified by ffprobe); probe page against live vdo.ninja; phone page hand-off.
TV output verified end to end on the real PC: OBS 33 WHIP -> MediaMTX 1.21.4 -> WHEP TV page (1920x1080, 0 loss);
stop restores OBS's stream service exactly (OBS rejects empty streamServiceSettings: restore uses {"key": ""}).
Not yet verified: real phone feed (see docs/SETUP.md checklist).
Ports are automatic (`backend/core/ports.py`, remembered in `data/ports.json`); a port someone owns on 127.0.0.1 counts as
busy (this PC has a service on 127.0.0.1:8000). OBS canvas here is 2560x1440@60 -> 1080p output.
Cameras: unlimited slots (+ Add camera); templates up to 6 plus an auto-grid for any count.
