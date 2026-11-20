"""Production files (.festcast): save the whole workspace (production, camera slots, layouts, presentations, standby
message) to one zip, and open one to replace the current workspace.

Not saved: stream ids, invitation tokens, room/VDO passwords. Opening a file always makes fresh ones, so a file
passed around can never be used to join or hijack a camera. Phones need their new camera links after an open."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..database.models import Presentation, Production, new_id
from .layouts import layout_scene

if TYPE_CHECKING:
    from ..core.context import Ctx

FORMAT = 1
MAX_TOTAL = 2 * 1024 ** 3          # uncompressed bytes we accept from one file
SLIDE_RE = re.compile(r"slide_\d{3}\.(png|jpg|webp)")
CAM_FIELDS = ("enabled", "resolution_target", "bitrate_kbps", "rotation", "frame", "transport")


class WorkspaceError(ValueError):
    pass


class WorkspaceService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx

    # -- save -------------------------------------------------------------------------------------------------
    def export(self) -> tuple[str, bytes]:
        c = self.ctx
        prod = c.cameras.production()
        cams = c.cameras.cameras()
        index = {cam.id: i for i, cam in enumerate(cams)}
        layouts = []
        for l in c.layouts.all():
            els = []
            for e in l.elements:
                e = dict(e)
                if e.get("type") == "camera":
                    i = index.get(e.pop("camera_id", None))
                    e["camera_index"] = i      # None: the camera was removed; the element stays empty
                els.append(e)
            layouts.append({"name": l.name, "elements": els})
        active = c.db.get("active_presentation")
        manifest = {
            "festcast": FORMAT, "saved_at": datetime.now(timezone.utc).isoformat(),
            "production": {"name": prod.name, "fps": prod.fps, "canvas_w": prod.canvas_w,
                           "canvas_h": prod.canvas_h} if prod else None,
            "cameras": [{"name": cam.name, **{k: getattr(cam, k) for k in CAM_FIELDS}} for cam in cams],
            "layouts": layouts, "presentations": [], "standby_message": c.db.get("standby_message"),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, p in enumerate(c.presentations.all()):
                slides = [sl for sl in p.slides if (c.presentations.root / p.id / "slides" / sl["file"]).exists()]
                for sl in slides:
                    # slides are already compressed images: store them as they are
                    z.write(c.presentations.root / p.id / "slides" / sl["file"], f"presentations/{i}/{sl['file']}",
                            compress_type=zipfile.ZIP_STORED)
                manifest["presentations"].append({
                    "folder": i, "name": p.name, "original_file": p.original_file, "slides": slides,
                    "default_duration": p.default_duration, "transition": p.transition, "background": p.background,
                    "branding": p.branding, "active": p.id == active})
            z.writestr("festcast.json", json.dumps(manifest, indent=1))
        name = re.sub(r'[\\/:*?"<>|]+', "_", prod.name if prod else "FestCast").strip() or "FestCast"
        return f"{name}.festcast", buf.getvalue()

    # -- open -------------------------------------------------------------------------------------------------
    def _read(self, data: bytes) -> tuple[dict, zipfile.ZipFile]:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            if sum(i.file_size for i in z.infolist()) > MAX_TOTAL:
                raise WorkspaceError("This file is too large to open.")
            m = json.loads(z.read("festcast.json"))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            raise WorkspaceError("Not a FestCast production file (.festcast).") from None
        if not isinstance(m, dict) or not isinstance(m.get("festcast"), int):
            raise WorkspaceError("Not a FestCast production file (.festcast).")
        if m["festcast"] > FORMAT:
            raise WorkspaceError("This file was saved by a newer FestCast. Update FestCast to open it.")
        for k in ("cameras", "layouts", "presentations"):
            if not isinstance(m.get(k, []), list):
                raise WorkspaceError(f"Damaged production file ({k}).")
        names = set(z.namelist())
        for p in m.get("presentations", []):
            for sl in p.get("slides", []):
                f = sl.get("file", "") if isinstance(sl, dict) else ""
                if not SLIDE_RE.fullmatch(f) or f"presentations/{p.get('folder')}/{f}" not in names:
                    raise WorkspaceError(f"Damaged production file (slide {f!r} of '{p.get('name')}').")
        return m, z

    def blockers(self) -> str | None:
        """Why the workspace can't be replaced right now (None: it can)."""
        if self.ctx.production.started():
            return "The production is running. End it before opening or starting another one."
        if self.ctx.recording.status.get("active"):
            return "OBS is recording. Stop the recording first."
        pgm = self.ctx.obs.state.get("program_scene") if self.ctx.obs.connected else None
        for l in self.ctx.layouts.all():
            if pgm == layout_scene(l.name):
                return f"Layout '{l.name}' is on programme. Put another scene on programme first."
        return None

    async def open(self, data: bytes) -> dict:
        m, z = self._read(data)               # validate everything before touching the current workspace
        why = self.blockers()
        if why:
            raise WorkspaceError(why)
        c = self.ctx
        # 1. clear the current workspace
        await c.cameras.close_production()
        for l in c.layouts.all():
            await c.layouts.delete(l.id)
        for p in c.presentations.all():
            c.presentations.delete(p.id)
        # 2. production + cameras (fresh stream ids and invitations)
        ids: list[str | None] = []
        pm = m.get("production")
        if isinstance(pm, dict):
            prod = await c.cameras.create_production(str(pm.get("name") or "FestCast Production")[:120], 0)
            with c.db.session() as s:
                p = s.get(Production, prod.id)
                for k, lo, hi in (("fps", 1, 240), ("canvas_w", 320, 7680), ("canvas_h", 240, 4320)):
                    if isinstance(pm.get(k), int):
                        setattr(p, k, max(lo, min(pm[k], hi)))
            for cm in m.get("cameras", []):
                cm = cm if isinstance(cm, dict) else {}
                cam = await c.cameras.add_camera(str(cm.get("name") or "") or None)
                fields = {k: cm[k] for k in CAM_FIELDS if k in cm}
                if fields:
                    try:
                        await c.cameras.update_camera(cam.id, **fields)
                    except (ValueError, TypeError):
                        pass                  # an odd value in the file: keep the defaults for that camera
                ids.append(cam.id)
        # 3. layouts, camera elements pointed at the new cameras
        for lm in m.get("layouts", []):
            if not isinstance(lm, dict):
                continue
            els = []
            for e in lm.get("elements") or []:
                if not isinstance(e, dict):
                    continue
                e = dict(e)
                if e.get("type") == "camera":
                    i = e.pop("camera_index", None)
                    e["camera_id"] = ids[i] if isinstance(i, int) and 0 <= i < len(ids) else None
                els.append(e)
            await c.layouts.create(str(lm.get("name") or "Layout"), els)
        # 4. presentations
        active = None
        for pm in m.get("presentations", []):
            pid = new_id()
            d = c.presentations.root / pid / "slides"
            d.mkdir(parents=True)
            slides = []
            for sl in pm.get("slides", []):
                (d / sl["file"]).write_bytes(z.read(f"presentations/{pm['folder']}/{sl['file']}"))
                dur = sl.get("duration")
                slides.append({"file": sl["file"], "enabled": bool(sl.get("enabled", True)),
                               "duration": max(1.0, min(float(dur), 3600)) if isinstance(dur, (int, float)) else None})
            with c.db.session() as s:
                s.add(Presentation(id=pid, name=str(pm.get("name") or "Presentation")[:120],
                                   original_file=str(pm.get("original_file") or "")[:400], slides=slides))
            try:
                c.presentations.update(pid, {k: pm[k] for k in ("default_duration", "transition", "background",
                                                                 "branding") if k in pm})
            except (ValueError, TypeError):
                pass
            if pm.get("active"):
                active = pid
        c.presentations.activate(active)
        if isinstance(m.get("standby_message"), str):
            c.db.set("standby_message", m["standby_message"][:200])
        c.presentations.publish()
        c.cameras.publish()
        name = (m.get("production") or {}).get("name") or "workspace"
        c.bus.notify("info", f"Opened production file '{name}'. Camera links are new: send them to the operators.",
                     "cameras")
        return c.cameras.snapshot()

    async def new(self, name: str, cameras: int, keep: bool) -> dict:
        """Close the current production and start a new one. keep=False also removes layouts and presentations."""
        why = self.blockers()
        if why:
            raise WorkspaceError(why)
        c = self.ctx
        await c.cameras.close_production()
        if not keep:
            for l in c.layouts.all():
                await c.layouts.delete(l.id)
            for p in c.presentations.all():
                c.presentations.delete(p.id)
        await c.cameras.create_production(name, cameras)
        return c.cameras.snapshot()
