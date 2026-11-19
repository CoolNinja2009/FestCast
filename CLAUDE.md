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

Verified: 86 backend tests; full dashboard click-through in simulation; OBS probe page against live vdo.ninja
(IFRAME API stats shape confirmed); phone page → VDO.Ninja publish hand-off.
Not yet verified on hardware: real phone feed, real OBS 33 connection, NVENC recording, MediaMTX TV playback
(see docs/SETUP.md checklist).
