"""FestCast entry point: `python -m backend.main` (or start.bat)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, public
from .config import ROOT, Settings, get_settings
from .core.context import Ctx
from .obs.client import ObsError
from .security.auth import Auth

log = logging.getLogger("festcast")
DIST = ROOT / "frontend" / "dist"
OPEN_PREFIXES = ("/join/", "/tv/", "/api/public/", "/obs/", "/api/auth/", "/assets/", "/favicon")


def create_app(settings: Settings | None = None, ctx: Ctx | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx.start()
        yield
        await app.state.ctx.stop()

    app = FastAPI(title="FestCast", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx or Ctx(settings)
    app.state.auth = Auth(settings.admin_password)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(OPEN_PREFIXES) and path != "/api/ws":
            if not app.state.auth.is_admin(request):
                return JSONResponse({"detail": "login required"}, status_code=401)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    @app.exception_handler(ObsError)
    async def obs_error(_: Request, e: ObsError):
        return JSONResponse({"detail": f"OBS: {e.comment or e.request}"}, status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(KeyError)
    async def key_error(_: Request, e: KeyError):
        return JSONResponse({"detail": f"not found: {e}"}, status_code=404)

    app.include_router(public.router)
    app.include_router(admin.router)

    if (DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        idx = DIST / "index.html"
        if idx.exists():
            return FileResponse(idx, headers={"Cache-Control": "no-store"})
        return HTMLResponse("<h1>FestCast</h1><p>Frontend not built. Run <code>npm run build</code> in "
                            "<code>frontend/</code> (start.bat does this).</p>")

    return app


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning", ws_ping_interval=20)


if __name__ == "__main__":
    main()

_ = Path
