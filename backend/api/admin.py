"""Operator API (/api/*). Every route here requires an operator (see security/auth.py)."""
from __future__ import annotations

import base64
import io
import json
import re
import time

import qrcode
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..services.cameras import cam_scene_name
from ..services.layouts import layout_scene

router = APIRouter(prefix="/api")


def ctx(request: Request):
    return request.app.state.ctx


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    b = io.BytesIO()
    img.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


# -- state ------------------------------------------------------------------------------------------------
@router.get("/state")
async def state(request: Request):
    c = ctx(request)
    return {"obs": c.obs.public_state(), "cameras": c.cameras.snapshot(), "production": c.production.view(),
            "layouts": c.layouts.snapshot(), "recording": c.recording.view(), "outputs": c.outputs.view(),
            "presentations": c.presentations.snapshot(), "metrics": c.metrics.latest,
            "notifications": list(c.bus.notifications)[:50], "previews": c.previews,
            "server": {"lan_ip": c.lan_ip, "port": c.settings.port, "public_base": c.public_base,
                       "simulation": c.settings.simulation, "remote_access": bool(c.settings.admin_password),
                       "local": request.app.state.auth.is_local(request)}}


@router.get("/metrics/history")
async def metrics_history(request: Request, seconds: int = 600):
    c = ctx(request)
    since = time.time() - seconds
    return [h for h in c.metrics.history if h["ts"] >= since]


@router.get("/health")
async def health(request: Request):
    c = ctx(request)
    m = c.metrics.latest or {}
    return {"backend": "ok", "obs": "ok" if c.obs.connected else "down", "simulation": c.obs.simulated,
            "database": "ok" if c.db.get("local_key") else "error",
            "recording_destination": "ok" if c.recording.status.get("dest_ok") else "check",
            "mediamtx": c.outputs.mtx_state, "metrics_age_s": round(time.time() - m["ts"], 1) if m else None,
            "cameras_live": c.cameras.usable_count()}


# -- production / cameras ------------------------------------------------------------------------------------
class ProdIn(BaseModel):
    name: str = "School Festival"
    cameras: int = 3


@router.post("/production")
async def create_production(request: Request, body: ProdIn):
    c = ctx(request)
    await c.cameras.create_production(body.name, body.cameras)
    return c.cameras.snapshot()


@router.delete("/production")
async def close_production(request: Request):
    await ctx(request).cameras.close_production()
    return ctx(request).cameras.snapshot()


@router.post("/production/start")
async def start_production(request: Request):
    return await ctx(request).production.start_production()


@router.post("/production/end")
async def end_production(request: Request):
    return await ctx(request).production.end_production()


class CamIn(BaseModel):
    name: str | None = None


@router.post("/cameras")
async def add_camera(request: Request, body: CamIn):
    c = ctx(request)
    cam = await c.cameras.add_camera(body.name)
    return c.cameras.view(cam)


class CamPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    resolution_target: str | None = None
    bitrate_kbps: int | None = None


@router.patch("/cameras/{cid}")
async def patch_camera(request: Request, cid: str, body: CamPatch):
    c = ctx(request)
    cam = await c.cameras.update_camera(cid, **body.model_dump(exclude_none=True))
    return c.cameras.view(cam)


@router.delete("/cameras/{cid}")
async def delete_camera(request: Request, cid: str):
    await ctx(request).cameras.remove_camera(cid)
    return {"ok": True}


@router.post("/cameras/{cid}/invite")
async def regen_invite(request: Request, cid: str):
    c = ctx(request)
    c.cameras.new_invitation(cid)
    c.cameras.publish()
    return c.cameras.view(c.cameras.camera(cid))


@router.post("/cameras/{cid}/revoke")
async def revoke_invite(request: Request, cid: str):
    c = ctx(request)
    await c.cameras.revoke(cid)
    return c.cameras.view(c.cameras.camera(cid))


@router.get("/cameras/{cid}/qr")
async def camera_qr(request: Request, cid: str):
    c = ctx(request)
    inv = c.cameras.invitation(cid)
    if not inv:
        raise HTTPException(404, "no active invitation")
    link = c.cameras.invite_link(inv.token)
    return {"link": link, "qr": qr_data_uri(link)}


@router.post("/cameras/{cid}/reconnect")
async def reconnect(request: Request, cid: str):
    await ctx(request).cameras.reconnect(cid)
    return {"ok": True}


class AudioSel(BaseModel):
    active: bool
    exclusive: bool = True


@router.post("/cameras/{cid}/audio")
async def camera_audio(request: Request, cid: str, body: AudioSel):
    await ctx(request).cameras.set_audio(cid, body.active, body.exclusive)
    return {"ok": True}


@router.get("/cameras/{cid}/raw")
async def camera_raw(request: Request, cid: str):
    return ctx(request).cameras.live.get(cid, {}).get("raw")


@router.post("/cameras/{cid}/golive")
async def camera_golive(request: Request, cid: str, transition: str | None = None):
    c = ctx(request)
    await c.production.go_live(cam_scene_name(c.cameras.camera(cid).name), transition)
    return c.production.view()


# -- programme control -----------------------------------------------------------------------------------------
@router.get("/scenes")
async def scenes(request: Request):
    c = ctx(request)
    names = await c.obs.scenes()
    return [{"name": n, "owned": bool(c.obs.owned(n))} for n in names]


class SceneIn(BaseModel):
    scene: str
    transition: str | None = None
    duration_ms: int | None = None


@router.post("/program/preview")
async def set_preview(request: Request, body: SceneIn):
    await ctx(request).production.preview(body.scene)
    return ctx(request).production.view()


class TakeIn(BaseModel):
    transition: str | None = None
    duration_ms: int | None = None


@router.post("/program/take")
async def take(request: Request, body: TakeIn):
    await ctx(request).production.take(body.transition, body.duration_ms)
    return ctx(request).production.view()


@router.post("/program/live")
async def go_live(request: Request, body: SceneIn):
    await ctx(request).production.go_live(body.scene, body.transition, body.duration_ms)
    return ctx(request).production.view()


@router.post("/program/studio")
async def studio(request: Request, enabled: bool):
    await ctx(request).obs.call("SetStudioModeEnabled", {"studioModeEnabled": enabled})
    return ctx(request).production.view()


@router.post("/standby/on")
async def standby_on(request: Request):
    await ctx(request).production.activate_standby("manual")
    return ctx(request).production.view()


class ReturnIn(BaseModel):
    scene: str | None = None


@router.post("/standby/off")
async def standby_off(request: Request, body: ReturnIn):
    await ctx(request).production.return_to_production(body.scene)
    return ctx(request).production.view()


@router.put("/standby/rules")
async def standby_rules(request: Request, body: dict):
    sch = body.get("schedule")
    if sch is not None:
        if not isinstance(sch, list) or any(not re.fullmatch(r"\d\d:\d\d", str(s.get("start", ""))) or
                                            not re.fullmatch(r"\d\d:\d\d", str(s.get("end", ""))) for s in sch):
            raise HTTPException(400, "schedule entries need start/end as HH:MM")
    return ctx(request).production.set_rules(body)


@router.post("/standby/override")
async def standby_override(request: Request, mode: str):
    ctx(request).production.set_override(mode)
    return ctx(request).production.view()


# -- layouts ------------------------------------------------------------------------------------------------------
class LayoutIn(BaseModel):
    name: str | None = None
    elements: list[dict] | None = None


@router.get("/layouts")
async def layouts(request: Request):
    return ctx(request).layouts.snapshot()


@router.post("/layouts")
async def create_layout(request: Request, body: LayoutIn):
    c = ctx(request)
    return c.layouts.view(await c.layouts.create(body.name or "Layout", body.elements or []))


@router.put("/layouts/{lid}")
async def update_layout(request: Request, lid: str, body: LayoutIn):
    c = ctx(request)
    return c.layouts.view(await c.layouts.update(lid, body.name, body.elements))


@router.post("/layouts/{lid}/duplicate")
async def dup_layout(request: Request, lid: str):
    c = ctx(request)
    return c.layouts.view(await c.layouts.duplicate(lid))


@router.delete("/layouts/{lid}")
async def del_layout(request: Request, lid: str):
    await ctx(request).layouts.delete(lid)
    return {"ok": True}


@router.post("/layouts/{lid}/sync")
async def sync_layout(request: Request, lid: str):
    return await ctx(request).layouts.sync(lid)


@router.get("/layouts/{lid}/obs")
async def layout_obs(request: Request, lid: str):
    return await ctx(request).layouts.obs_state(lid)


@router.get("/layouts/{lid}/export")
async def export_layout(request: Request, lid: str):
    d = ctx(request).layouts.export(lid)
    return Response(json.dumps(d, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="layout-{re.sub(r"[^A-Za-z0-9_-]", "_", d["name"])}.json"'})


@router.post("/layouts/import")
async def import_layout(request: Request, file: UploadFile = File(...)):
    data = json.loads((await file.read(2_000_000)).decode("utf-8"))
    c = ctx(request)
    return c.layouts.view(await c.layouts.import_(data))


@router.post("/layouts/{lid}/preview")
async def layout_preview(request: Request, lid: str):
    c = ctx(request)
    await c.layouts.sync(lid)
    await c.production.preview(layout_scene(c.layouts.get(lid).name))
    return c.production.view()


@router.post("/layouts/{lid}/live")
async def layout_live(request: Request, lid: str, body: TakeIn):
    c = ctx(request)
    await c.layouts.sync(lid)
    await c.production.go_live(layout_scene(c.layouts.get(lid).name), body.transition, body.duration_ms)
    return c.production.view()


# -- media (images for layouts) --------------------------------------------------------------------------------------
@router.post("/media")
async def upload_media(request: Request, file: UploadFile = File(...)):
    c = ctx(request)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "image")[:80]
    if not re.search(r"\.(png|jpe?g|webp|gif|svg)$", name, re.I):
        raise HTTPException(400, "images only (png, jpg, webp, gif, svg)")
    data = await file.read(30_000_000)
    stem = f"{int(time.time())}_{name}"
    (c.media_dir / stem).write_bytes(data)
    return {"src": stem, "url": f"/api/media/{stem}"}


@router.get("/media")
async def list_media(request: Request):
    c = ctx(request)
    return [{"src": p.name, "url": f"/api/media/{p.name}"} for p in sorted(c.media_dir.iterdir()) if p.is_file()]


@router.get("/media/{name}")
async def get_media(request: Request, name: str):
    c = ctx(request)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise HTTPException(400)
    p = c.media_dir / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# -- presentations ----------------------------------------------------------------------------------------------------
@router.get("/presentations")
async def presentations(request: Request):
    return ctx(request).presentations.snapshot()


@router.post("/presentations")
async def import_presentation(request: Request, name: str = Form(""), files: list[UploadFile] = File(...)):
    c = ctx(request)
    data = [(f.filename or "file", await f.read()) for f in files]
    p = await c.presentations.import_files(name, data)
    return c.presentations.view(p)


@router.patch("/presentations/{pid}")
async def update_presentation(request: Request, pid: str, body: dict):
    c = ctx(request)
    return c.presentations.view(c.presentations.update(pid, body))


@router.delete("/presentations/{pid}")
async def delete_presentation(request: Request, pid: str):
    ctx(request).presentations.delete(pid)
    return {"ok": True}


@router.post("/presentations/{pid}/activate")
async def activate_presentation(request: Request, pid: str):
    ctx(request).presentations.activate(pid if pid != "none" else None)
    return ctx(request).presentations.snapshot()


@router.put("/standby/message")
async def standby_message(request: Request, body: dict):
    c = ctx(request)
    c.db.set("standby_message", str(body.get("message", ""))[:200])
    c.presentations.nonce += 1
    c.presentations.publish()
    return {"ok": True}


@router.get("/presentations/{pid}/slides/{file}")
async def slide(request: Request, pid: str, file: str):
    p = ctx(request).presentations.slide_path(pid, file)
    if not p:
        raise HTTPException(404)
    return FileResponse(p)


# -- recording -----------------------------------------------------------------------------------------------------------
@router.get("/recording")
async def recording(request: Request):
    return ctx(request).recording.view()


@router.put("/recording/config")
async def recording_config(request: Request, body: dict):
    return ctx(request).recording.set_config(body)


@router.get("/recording/destination")
async def recording_destination(request: Request):
    c = ctx(request)
    import asyncio
    return {"check": await asyncio.to_thread(c.recording.check_destination), "drives": c.recording.drives()}


@router.post("/recording/apply")
async def recording_apply(request: Request):
    return await ctx(request).recording.apply_to_obs()


@router.post("/recording/start")
async def recording_start(request: Request):
    return await ctx(request).recording.start()


@router.post("/recording/stop")
async def recording_stop(request: Request):
    return await ctx(request).recording.stop()


@router.post("/recording/pause")
async def recording_pause(request: Request):
    await ctx(request).recording.pause()
    return {"ok": True}


@router.post("/recording/resume")
async def recording_resume(request: Request):
    await ctx(request).recording.resume()
    return {"ok": True}


@router.get("/recording/history")
async def recording_history(request: Request):
    return ctx(request).recording.history()


@router.get("/recording/files")
async def recording_files(request: Request):
    import asyncio
    return await asyncio.to_thread(ctx(request).recording.files)


@router.post("/recording/{rid}/remux")
async def remux(request: Request, rid: str):
    import asyncio
    c = ctx(request)
    asyncio.create_task(c.recording.remux(rid))
    return {"ok": True, "state": "started"}


# -- outputs -------------------------------------------------------------------------------------------------------------
class RecvIn(BaseModel):
    name: str = "TV"
    kind: str = "webrtc"
    monitor_index: int | None = None


@router.get("/outputs")
async def outputs(request: Request):
    return ctx(request).outputs.view()


@router.post("/outputs/receivers")
async def add_receiver(request: Request, body: RecvIn):
    c = ctx(request)
    c.outputs.add(body.name, body.kind, body.monitor_index)
    return c.outputs.view()


@router.patch("/outputs/receivers/{rid}")
async def rename_receiver(request: Request, rid: str, body: dict):
    c = ctx(request)
    c.outputs.rename(rid, str(body.get("name", "")))
    return c.outputs.view()


@router.delete("/outputs/receivers/{rid}")
async def revoke_receiver(request: Request, rid: str):
    c = ctx(request)
    await c.outputs.revoke(rid)
    return c.outputs.view()


@router.get("/outputs/receivers/{rid}/qr")
async def receiver_qr(request: Request, rid: str):
    c = ctx(request)
    r = next((r for r in c.outputs.receivers() if r.id == rid), None)
    if not r:
        raise HTTPException(404)
    link = c.outputs.link(r)
    return {"link": link, "qr": qr_data_uri(link)}


@router.post("/outputs/programme/start")
async def programme_start(request: Request):
    await ctx(request).outputs.start_programme()
    return ctx(request).outputs.view()


@router.post("/outputs/programme/stop")
async def programme_stop(request: Request):
    await ctx(request).outputs.stop_programme()
    return ctx(request).outputs.view()


@router.post("/outputs/mediamtx/{action}")
async def mediamtx(request: Request, action: str):
    o = ctx(request).outputs
    if action == "start":
        await o.start_mtx()
    elif action == "stop":
        await o.stop_mtx()
    elif action == "restart":
        await o.restart_mtx()
    else:
        raise HTTPException(400)
    return o.view()


@router.get("/outputs/monitors")
async def monitors(request: Request):
    return await ctx(request).outputs.monitors()


@router.post("/outputs/projector")
async def projector(request: Request, monitor_index: int):
    await ctx(request).outputs.open_projector(monitor_index)
    return {"ok": True}


# -- audio ---------------------------------------------------------------------------------------------------------------------
@router.get("/audio")
async def audio(request: Request):
    return await ctx(request).audio.mixer()


class AudioIn(BaseModel):
    muted: bool | None = None
    volume_db: float | None = None
    sync_offset_ms: int | None = None
    monitor: str | None = None


@router.patch("/audio/{name}")
async def audio_set(request: Request, name: str, body: AudioIn):
    await ctx(request).audio.set(name, **body.model_dump(exclude_none=True))
    return {"ok": True}


# -- network / diagnostics / config ------------------------------------------------------------------------------------------
@router.get("/network")
async def network(request: Request):
    import psutil
    c = ctx(request)
    ifs = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        v4 = [a.address for a in addrs if a.family.name == "AF_INET"]
        if not v4:
            continue
        st = stats.get(name)
        ifs.append({"name": name, "ipv4": v4, "up": st.isup if st else None, "speed_mbps": st.speed if st else None,
                    "primary": c.lan_ip in v4})
    return {"lan_ip": c.lan_ip, "interfaces": ifs, "history": list(c.cameras.history)[:200]}


@router.get("/logs")
async def logs(request: Request, limit: int = 300):
    return list(ctx(request).bus.logs)[-limit:]


@router.get("/diagnostics")
async def diagnostics(request: Request):
    data = ctx(request).diagnostics_zip()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="festcast-diagnostics-{int(time.time())}.zip"'})


@router.get("/config/export")
async def config_export(request: Request):
    return Response(json.dumps(ctx(request).export_config(), indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="festcast-config.json"'})


@router.post("/config/import")
async def config_import(request: Request, file: UploadFile = File(...)):
    data = json.loads((await file.read(5_000_000)).decode("utf-8"))
    return await ctx(request).import_config(data)


@router.post("/notifications/clear")
async def clear_notifications(request: Request):
    ctx(request).bus.notifications.clear()
    return {"ok": True}
