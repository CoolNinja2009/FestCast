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

Verified: 88 backend tests; dashboard click-through in simulation and on real OBS 33.0.1 (WS 5.7.6);
`scripts/obs_smoke_test.py` all-pass on the real PC (sources, probe pages inside OBS, layout transforms,
NVENC H.264 1920x1080 MKV verified by ffprobe); probe page against live vdo.ninja; phone page hand-off.
Not yet verified: real phone feed, MediaMTX TV playback (see docs/SETUP.md checklist).
This PC: another service owns 127.0.0.1:8000 (hence port 8420); OBS canvas is 2560x1440@60 -> 1080p output.
