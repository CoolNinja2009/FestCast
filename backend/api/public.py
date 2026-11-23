"""Routes reachable without operator login:
* /join/<token>      camera operator page (phones)
* /cam/<token>       local camera page (phones, on the https listener): sends the camera to MediaMTX via FestCast
* /tv/<token>        TV receiver page
* /api/public/...    token-gated JSON for those pages
* /obs/...           pages that run INSIDE OBS browser sources (loopback + local key only)
* /api/auth/...      login for remote operators
* /api/ws            live dashboard WebSocket (operator only)"""
from __future__ import annotations

import asyncio
import hmac
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from ..security.auth import LOOPBACK, SESSION_COOKIE, SESSION_TTL

router = APIRouter()
PAGES = Path(__file__).resolve().parent.parent / "public"
RECEIVER = Path(__file__).resolve().parents[2] / "receiver"
NOSTORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


def page(path: Path) -> HTMLResponse:
    return HTMLResponse(path.read_text("utf-8"), headers=NOSTORE)


def obs_only(request: Request, k: str | None) -> None:
    c = request.app.state.ctx
    if not (request.client and request.client.host in LOOPBACK) or not k or not hmac.compare_digest(k, c.local_key):
        raise HTTPException(403, "OBS pages are only available on the production PC")


# -- operator manual (works offline at the venue) -------------------------------------------------------------
@router.get("/manual")
async def manual():
    return page(PAGES / "manual.html")


# -- camera operators --------------------------------------------------------------------------------------------
@router.get("/join/{token}")
async def join_page(token: str):
    return page(PAGES / "join.html")


@router.get("/api/public/join/{token}")
async def join_info(request: Request, token: str):
    c = request.app.state.ctx
    r = c.cameras.resolve_invite(token)
    if not r:
        raise HTTPException(404, "This camera link is invalid, expired or was revoked. Ask the producer for a new one.")
    cam, push = r
    prod = c.cameras.production()
    return JSONResponse({"camera": cam.name, "slot": cam.slot, "production": prod.name if prod else "",
                         "quality": cam.resolution_target, "fps": prod.fps if prod else 30, "publish_url": push,
                         "transport": cam.transport, "https_port": c.https_port or None,
                         # VDO.Ninja cameras open inside FestCast's https page, which adds auto-turn (vcam.html)
                         "wrap_url": (f"{c.https_base()}/vcam/{token}"
                                      if cam.transport != "local" and c.https_port else None)},
                        headers=NOSTORE)


def sdp_response(status: int, text: str, loc: str | None) -> Response:
    headers = dict(NOSTORE)
    if loc:
        headers["X-Session"] = loc
    return Response(text, status_code=status, media_type="application/sdp" if status == 201 else "text/plain",
                    headers=headers)


@router.get("/cam/{token}")
async def cam_page(request: Request, token: str):
    c = request.app.state.ctx
    if request.url.scheme != "https" and c.https_port:
        # phones only open the camera on https pages
        return RedirectResponse(f"{c.https_base()}/cam/{token}", status_code=307)
    return page(PAGES / "cam.html")


@router.get("/api/public/cam/{token}")
async def cam_info(request: Request, token: str):
    c = request.app.state.ctx
    cam = c.cameras.local_cam_for_token(token)
    if not cam:
        raise HTTPException(404, "This camera link is invalid, expired or was revoked. Ask the producer for a new one.")
    d = c.cameras.cam_dict(cam)
    from ..transports.vdoninja import RESOLUTIONS
    w, hgt = RESOLUTIONS.get(cam.resolution_target, (1920, 1080))
    return JSONResponse({"camera": cam.name, "slot": cam.slot, "production": (c.cameras.production() or cam).name,
                         "width": w, "height": hgt, "fps": d["fps"], "bitrate_kbps": cam.bitrate_kbps,
                         "enabled": cam.enabled, "reload": c.cameras.reload_nonce.get(cam.id, 0)}, headers=NOSTORE)


@router.get("/vcam/{token}")
async def vcam_page(request: Request, token: str):
    c = request.app.state.ctx
    if request.url.scheme != "https" and c.https_port:
        return RedirectResponse(f"{c.https_base()}/vcam/{token}", status_code=307)
    return page(PAGES / "vcam.html")


@router.post("/api/public/cam/{token}/orientation")
async def cam_orientation(request: Request, token: str, body: dict):
    """The local camera page reads the phone's motion sensor and says how the phone is held: `held` is the turn
    (0, 90 or 270) a picture taken that way needs if it arrives tall (iPhone with Portrait Orientation Lock on).
    FestCast applies it only while the frames it receives really are tall, so a phone that already sends landscape
    frames is never turned."""
    c = request.app.state.ctx
    r = c.cameras.resolve_invite(token)               # local and VDO.Ninja cameras alike
    if not r:
        raise HTTPException(404, "This camera link is invalid, expired or was revoked.")
    cam = r[0]
    held = body.get("held") if isinstance(body, dict) else None
    if held not in (0, 90, 180, 270):
        raise HTTPException(400, "held must be 0, 90, 180 or 270")
    rot = await c.cameras.phone_held(cam.id, held)
    return {"ok": True, "rotation": rot}


@router.get("/api/public/cam/{token}/tally")
async def cam_tally(request: Request, token: str, v: int = -1):
    """Tally light for the camera page (local and VDO.Ninja alike). Long-poll: with `v` = the version the page has,
    the answer waits (up to 20 s) until the tally changes, so the light follows a cut within a moment."""
    c = request.app.state.ctx
    r = c.cameras.resolve_invite(token)
    if not r:
        raise HTTPException(404, "This camera link is invalid, expired or was revoked.")
    await c.tally.wait(v)
    return JSONResponse({"tally": c.tally.state(r[0].id), "v": c.tally.version}, headers=NOSTORE)


@router.post("/api/public/cam/{token}/whip")
async def cam_whip(request: Request, token: str):
    body = (await request.body())[:200_000].decode("utf-8", errors="replace")
    return sdp_response(*await request.app.state.ctx.cameras.phone_whip(token, body))


@router.delete("/api/public/cam/{token}/whip")
async def cam_whip_delete(request: Request, token: str, session: str):
    await request.app.state.ctx.cameras.phone_whip_delete(token, session)
    return {"ok": True}


# -- TV receivers ------------------------------------------------------------------------------------------------------
@router.get("/tv/{token}")
async def tv_page(token: str):
    return page(RECEIVER / "tv.html")


@router.get("/api/public/tv/{token}")
async def tv_info(request: Request, token: str):
    r = request.app.state.ctx.outputs.by_token(token)
    if not r:
        raise HTTPException(403, "receiver revoked or unknown")
    return {"name": r.name}


@router.post("/api/public/tv/{token}/whep")
async def tv_whep(request: Request, token: str):
    body = (await request.body())[:200_000].decode("utf-8", errors="replace")
    status, text, loc = await request.app.state.ctx.outputs.whep(token, body)
    headers = dict(NOSTORE)
    if loc:
        headers["X-Session"] = loc
    return Response(text, status_code=status, media_type="application/sdp" if status == 201 else "text/plain",
                    headers=headers)


@router.delete("/api/public/tv/{token}/whep")
async def tv_whep_delete(request: Request, token: str, session: str):
    await request.app.state.ctx.outputs.whep_delete(token, session)
    return {"ok": True}


@router.post("/api/public/tv/{token}/stats")
async def tv_stats(request: Request, token: str):
    payload = await request.json()
    if not isinstance(payload, dict) or not request.app.state.ctx.outputs.report(token, payload):
        raise HTTPException(403, "receiver revoked or unknown")
    return {"ok": True}


# -- OBS-side pages -------------------------------------------------------------------------------------------------------
@router.get("/obs/cam/{cid}")
async def obs_cam(request: Request, cid: str, k: str | None = None):
    obs_only(request, k)
    return page(PAGES / "obs_cam.html")


@router.get("/obs/cam/{cid}/config")
async def obs_cam_config(request: Request, cid: str, k: str | None = None):
    obs_only(request, k)
    c = request.app.state.ctx
    try:
        cam = c.cameras.camera(cid)
    except KeyError:
        raise HTTPException(404)
    return JSONResponse({"enabled": cam.enabled, "transport": cam.transport,
                         "view_url": c.cameras.adapter.view_url(c.cameras.cam_dict(cam)) if cam.transport != "local"
                         else None,
                         "rotation": cam.rotation or 0, "frame": cam.frame or "fit",
                         "reload": c.cameras.reload_nonce.get(cid, 0),
                         "page": int((PAGES / "obs_cam.html").stat().st_mtime)}, headers=NOSTORE)


@router.post("/obs/cam/{cid}/whep")
async def obs_cam_whep(request: Request, cid: str, k: str | None = None):
    obs_only(request, k)
    body = (await request.body())[:200_000].decode("utf-8", errors="replace")
    return sdp_response(*await request.app.state.ctx.cameras.obs_whep(cid, body))


@router.delete("/obs/cam/{cid}/whep")
async def obs_cam_whep_delete(request: Request, cid: str, session: str, k: str | None = None):
    obs_only(request, k)
    await request.app.state.ctx.cameras.obs_whep_delete(session)
    return {"ok": True}


@router.post("/obs/cam/{cid}/stats")
async def obs_cam_stats(request: Request, cid: str, k: str | None = None):
    obs_only(request, k)
    payload = await request.json()
    return request.app.state.ctx.cameras.report(cid, payload if isinstance(payload, dict) else {})


@router.get("/obs/standby")
async def obs_standby(request: Request, k: str | None = None):
    obs_only(request, k)
    return page(PAGES / "standby.html")


@router.get("/obs/standby/state")
async def obs_standby_state(request: Request, k: str | None = None, at: str | None = None,
                            seq: str | None = None):
    obs_only(request, k)
    request.app.state.ctx.presentations.reported(at, seq)
    return JSONResponse(request.app.state.ctx.presentations.player_state(), headers=NOSTORE)


@router.get("/obs/slides/{pid}/{file}")
async def obs_slide(request: Request, pid: str, file: str):
    if not (request.client and request.client.host in LOOPBACK):
        raise HTTPException(403)
    p = request.app.state.ctx.presentations.slide_path(pid, file)
    if not p:
        raise HTTPException(404)
    return FileResponse(p)


# -- auth -------------------------------------------------------------------------------------------------------------------
class LoginIn(BaseModel):
    password: str


@router.post("/api/auth/login")
async def login(request: Request, body: LoginIn):
    tok = request.app.state.auth.login(request, body.password)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL, httponly=True, samesite="strict")
    return resp


@router.post("/api/auth/logout")
async def logout(request: Request):
    request.app.state.auth.logout(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/api/auth/me")
async def me(request: Request):
    a = request.app.state.auth
    return {"admin": a.is_admin(request), "local": a.is_local(request), "remote_enabled": bool(a.password)}


# -- live websocket -----------------------------------------------------------------------------------------------------------
@router.websocket("/api/ws")
async def ws(websocket: WebSocket):
    app = websocket.app
    host = websocket.client.host if websocket.client else ""
    tok = websocket.cookies.get(SESSION_COOKIE)
    import time
    if host not in LOOPBACK and not (tok and app.state.auth.sessions.get(tok, 0) > time.time()):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    bus = app.state.ctx.bus
    q = bus.subscribe()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), 15)
            except asyncio.TimeoutError:
                msg = {"topic": "ping", "data": None}
            await websocket.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
