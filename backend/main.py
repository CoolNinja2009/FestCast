"""FestCast entry point: `python -m backend.main` (or start.bat)."""
from __future__ import annotations

import asyncio
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
OPEN_PREFIXES = ("/join/", "/cam/", "/tv/", "/api/public/", "/obs/", "/api/auth/", "/assets/", "/favicon")


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
    import argparse
    import os

    import uvicorn

    ap = argparse.ArgumentParser(description="FestCast")
    ap.add_argument("--simulation", action="store_true", help="use the in-memory OBS simulator")
    ap.add_argument("--port", type=int)
    ap.add_argument("--data-dir")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser once started")
    a = ap.parse_args()
    if a.simulation:
        os.environ["FESTCAST_SIMULATION"] = "true"
    if a.port:
        os.environ["FESTCAST_PORT"] = str(a.port)
    if a.data_dir:
        os.environ["FESTCAST_DATA_DIR"] = a.data_dir
    get_settings.cache_clear()
    s = get_settings()
    from .core import ports as P
    from .services.metrics import lan_ip

    wanted = s.port or None
    last = P.remembered(s.data_dir).get("port")
    s.port = P.pick([wanted, last, 8420], range(8421, 8500))
    if wanted and s.port != wanted:
        print(f"FestCast: port {wanted} is busy (another program); using {s.port} instead.", flush=True)
    P.remember(s.data_dir, port=s.port)
    host = s.public_host or lan_ip()
    from .core.tls import ensure_cert
    tls = ensure_cert(s.data_dir, [host], [f"{s.mdns_name}.local"] if s.mdns_name else [])
    if tls:
        hwanted = s.https_port or None
        s.https_port = P.pick([hwanted, P.remembered(s.data_dir).get("https_port"), 8443],
                              range(8444, 8544), avoid={s.port})
        P.remember(s.data_dir, https_port=s.https_port)
    else:
        s.https_port = 0
    local_url = f"http://localhost:{s.port}"
    alt = f"\n                       http://{s.mdns_name}.local:{s.port}  (name, via mDNS)" if s.mdns_name else ""
    cam = f"\n  Local camera page:   https://{host}:{s.https_port}  (phones confirm the certificate once)" if tls else ""
    print(f"\n  FestCast dashboard:  {local_url}\n  Phones / TVs reach:  http://{host}:{s.port}{alt}{cam}\n",
          flush=True)
    if a.open:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(local_url)).start()
    app = create_app(s)
    servers = [uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.port, log_level="warning",
                                             ws_ping_interval=20))]
    if tls:
        # second listener, same app, for the phone camera page (getUserMedia needs https). Lifespan runs once.
        servers.append(uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.https_port, log_level="warning",
                                                     ssl_certfile=tls[0], ssl_keyfile=tls[1], lifespan="off",
                                                     ws_ping_interval=20)))

    async def serve() -> None:
        tasks = [asyncio.create_task(x.serve()) for x in servers]
        try:
            await tasks[0]     # the dashboard listener decides; if the https one ever fails, the rest keeps going
        finally:
            for x in servers:
                x.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()

_ = Path
