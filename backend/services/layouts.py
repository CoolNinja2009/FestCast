"""Layouts: normalised element lists persisted in SQLite and materialised as real OBS scenes
("FC Layout · <name>") using documented scene-item transform requests."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ..database.models import Layout, new_id
from ..obs.client import ObsError
from .audio import BUS_SCENE
from .cameras import cam_input_name

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.layouts")

EL_TYPES = {"camera", "text", "image", "color"}


def layout_scene(name: str) -> str:
    return f"FC Layout · {name}"


def graphic_name(eid: str) -> str:
    return f"FC_GRAPHIC_{eid}"


def hex_to_abgr(h: str, alpha: float = 1.0) -> int:
    h = (h or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        r = g = b = 255
    a = max(0, min(255, int(alpha * 255)))
    return (a << 24) | (b << 16) | (g << 8) | r


def clean_element(e: dict) -> dict:
    t = e.get("type")
    if t not in EL_TYPES:
        raise ValueError(f"unknown element type {t!r}")

    def f(k: str, d: float, lo: float = -1.0, hi: float = 2.0) -> float:
        try:
            return max(lo, min(hi, float(e.get(k, d))))
        except (TypeError, ValueError):
            return d
    crop = e.get("crop") or {}
    out = {
        "id": re.sub(r"[^a-zA-Z0-9]", "", str(e.get("id") or new_id()))[:32] or new_id(),
        "type": t, "name": str(e.get("name") or t)[:60],
        "x": f("x", 0), "y": f("y", 0), "w": f("w", 0.25, 0.005, 3), "h": f("h", 0.25, 0.005, 3),
        "z": int(e.get("z") or 0), "locked": bool(e.get("locked")), "hidden": bool(e.get("hidden")),
        "fit": e.get("fit") if e.get("fit") in ("fit", "stretch") else "fit",
        "crop": {k: max(0.0, min(0.45, float(crop.get(k, 0) or 0))) for k in ("l", "t", "r", "b")},
        "group": (str(e["group"])[:32] if e.get("group") else None),
        "opacity": f("opacity", 1, 0, 1),
    }
    if t == "camera":
        out["camera_id"] = str(e.get("camera_id") or "")[:32]
        if turned_crop(out["crop"]):            # old Landscape/Portrait crop: not needed any more
            out["crop"] = {"l": 0.0, "t": 0.0, "r": 0.0, "b": 0.0}
    if t == "text":
        out.update(text=str(e.get("text") or "Text")[:500], font=str(e.get("font") or "Arial")[:60],
                   size=int(f("size", 64, 8, 400)), color=str(e.get("color") or "#ffffff")[:9],
                   bold=bool(e.get("bold")))
    if t == "image":
        out["src"] = re.sub(r"[^a-zA-Z0-9._-]", "", str(e.get("src") or ""))[:120]
    if t == "color":
        out["color"] = str(e.get("color") or "#000000")[:9]
    return out


def arrange(aspects: list[float], W: float, H: float) -> list[tuple[float, float, float, float]]:
    """Auto arrange: place pictures of these aspects (in order) on a W x H canvas as large as possible, in rows
    (each row as tall as its pictures allow side by side), centred. Tries every row count and keeps the one that
    covers the most screen. Returns normalised (x, y, w, h) per picture."""
    n = len(aspects)
    if not n:
        return []
    best: tuple[float, list] | None = None
    for rows in range(1, n + 1):
        base, extra = divmod(n, rows)
        sizes = [base + (1 if i < extra else 0) for i in range(rows)]
        if 0 in sizes:
            continue
        groups, i = [], 0
        for k in sizes:
            groups.append(aspects[i:i + k]); i += k
        heights = [W / sum(g) for g in groups]               # each row fills the width
        k = min(1.0, H / sum(heights))                        # then shrink everything to fit the height
        area = sum(a * (h * k) ** 2 for g, h in zip(groups, heights) for a in g)
        if best is None or area > best[0] * 1.001:
            boxes, y = [], (H - sum(heights) * k) / 2
            for g, h in zip(groups, heights):
                h *= k
                x = (W - sum(g) * h) / 2
                for a in g:
                    boxes.append((x / W, y / H, a * h / W, h / H)); x += a * h
                y += h
            best = (area, boxes)
    return best[1]


def turned_crop(c: dict) -> bool:
    """The crop older versions of the editor's Landscape/Portrait button put on a portrait tile (the 9:16 middle of
    a 16:9 source). Camera sources now have the picture's own shape, so this crop is ignored."""
    return abs(c["l"] - 0.35) < 0.003 and abs(c["r"] - 0.35) < 0.003 and abs(c["t"] - c["b"]) < 0.003 and c["t"] < 0.05


class LayoutService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.sync_state: dict[str, dict] = {}
        self._resync: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def camera_changed_shape(self, cid: str) -> None:
        """A camera turned (portrait <-> landscape) or its rotation/frame changed: reshape its OBS source and re-apply
        the layouts that show it, once things settle (a phone being turned reports a few sizes in a row)."""
        if cid in self._resync:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._resync.add(cid)

        async def later() -> None:
            await asyncio.sleep(1.5)
            self._resync.discard(cid)
            try:
                await self.ctx.cameras.apply_held(cid)          # phone held sideways + tall frames: turn it
                await self.ctx.cameras.sync_camera_obs(cid)     # the source takes the new shape
            except KeyError:
                return
            for l in self.all():
                if any(e.get("type") == "camera" and e.get("camera_id") == cid for e in l.elements):
                    await self.sync(l.id)
        loop.create_task(later())

    def all(self) -> list[Layout]:
        with self.ctx.db.session() as s:
            return list(s.query(Layout).order_by(Layout.created_at))

    def get(self, lid: str) -> Layout:
        with self.ctx.db.session() as s:
            l = s.get(Layout, lid)
            if not l:
                raise KeyError(lid)
            return l

    def auto_arrange(self, lid: str) -> bool:
        return bool(self.ctx.db.get(f"layout_auto:{lid}"))

    def _arranged(self, l: Layout, W: int, H: int) -> list[dict]:
        """With auto arrange on, the camera tiles' boxes come from the pictures' real shapes (unknown = canvas
        shape); the result is saved so the editor shows exactly what OBS gets."""
        els = l.elements
        if not self.auto_arrange(l.id):
            return els
        cams = sorted((e for e in els if e["type"] == "camera" and not e["hidden"]),
                      key=lambda e: (round(e["y"], 2), e["x"]))
        aspects = []
        for e in cams:
            try:
                sw, sh = self.ctx.cameras.source_size(self.ctx.cameras.camera(e.get("camera_id") or ""))
            except KeyError:
                sw, sh = W, H
            aspects.append(sw / sh)
        boxes = arrange(aspects, W, H)
        new = copy.deepcopy(els)
        byid = {e["id"]: e for e in new}
        for e, (x, y, w, h) in zip(cams, boxes):
            byid[e["id"]].update(x=round(x, 4), y=round(y, 4), w=round(w, 4), h=round(h, 4),
                                 crop={"l": 0.0, "t": 0.0, "r": 0.0, "b": 0.0}, fit="fit")
        if new != els:
            with self.ctx.db.session() as s:
                s.get(Layout, l.id).elements = new
        return new

    def view(self, l: Layout) -> dict:
        return {"id": l.id, "name": l.name, "elements": l.elements, "obs_scene": layout_scene(l.name),
                "auto_arrange": self.auto_arrange(l.id),
                "updated_at": l.updated_at.isoformat() if l.updated_at else None,
                "sync": self.sync_state.get(l.id, {"state": "unsynced"}),
                "live": self.ctx.obs.state.get("program_scene") == layout_scene(l.name)}

    def _unique_name(self, name: str, skip: str | None = None) -> str:
        name = (name or "Layout").strip()[:60] or "Layout"
        names = {l.name for l in self.all() if l.id != skip}
        base, n = name, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        return name

    async def create(self, name: str, elements: list[dict] | None = None) -> Layout:
        els = [clean_element(e) for e in (elements or [])]
        with self.ctx.db.session() as s:
            l = Layout(name=self._unique_name(name), elements=els)
            s.add(l)
        await self.sync(l.id)
        self.publish()
        return l

    async def update(self, lid: str, name: str | None = None, elements: list[dict] | None = None,
                     sync: bool = True, auto_arrange: bool | None = None) -> Layout:
        old = self.get(lid)
        if auto_arrange is not None:
            self.ctx.db.set(f"layout_auto:{lid}", bool(auto_arrange))
        with self.ctx.db.session() as s:
            l = s.get(Layout, lid)
            if name is not None and name.strip() and name.strip() != l.name:
                l.name = self._unique_name(name, skip=lid)
            if elements is not None:
                ids: set[str] = set()
                cleaned = []
                for e in elements:
                    ce = clean_element(e)
                    while ce["id"] in ids:
                        ce["id"] = new_id()
                    ids.add(ce["id"])
                    cleaned.append(ce)
                l.elements = cleaned
        if old.name != l.name and self.ctx.obs.connected:
            await self.ctx.obs.rename_scene(layout_scene(old.name), layout_scene(l.name))
        if sync:
            await self.sync(lid)
            l = self.get(lid)                  # auto arrange may have placed the camera tiles
        self.publish()
        return l

    async def duplicate(self, lid: str) -> Layout:
        l = self.get(lid)
        els = copy.deepcopy(l.elements)
        for e in els:
            e["id"] = new_id()
        return await self.create(l.name + " copy", els)

    async def delete(self, lid: str) -> None:
        l = self.get(lid)
        if self.ctx.obs.connected:
            scene = layout_scene(l.name)
            if self.ctx.obs.state.get("program_scene") == scene:
                raise ValueError("This layout is live on programme. Switch to another scene first.")
            for row in self.ctx.obs.owned_by_ref(lid):
                try:
                    if row.kind == "scene":
                        await self.ctx.obs.remove_owned_scene(row.name)
                    else:
                        await self.ctx.obs.remove_owned_input(row.name)
                except ObsError as e:
                    self.ctx.bus.notify("warning", f"OBS cleanup: {e.comment}", "layouts")
        with self.ctx.db.session() as s:
            s.delete(s.get(Layout, lid))
        self.sync_state.pop(lid, None)
        self.publish()

    def export(self, lid: str) -> dict:
        l = self.get(lid)
        return {"festcast_layout": 1, "name": l.name, "elements": l.elements}

    async def import_(self, data: dict) -> Layout:
        if not isinstance(data, dict) or data.get("festcast_layout") != 1:
            raise ValueError("not a FestCast layout file")
        els = data.get("elements") or []
        for e in els:
            e["id"] = new_id()
        return await self.create(str(data.get("name") or "Imported"), els)

    def forget_camera(self, cid: str) -> None:
        with self.ctx.db.session() as s:
            for l in s.query(Layout):
                els = [e for e in l.elements if not (e.get("type") == "camera" and e.get("camera_id") == cid)]
                if len(els) != len(l.elements):
                    l.elements = els

    # -- OBS materialisation ----------------------------------------------------------------
    def _graphic(self, e: dict, W: int, H: int) -> tuple[str, dict]:
        obs = self.ctx.obs
        if e["type"] == "text":
            kind = obs.kind("text_gdiplus", "") or obs.kind("text_ft2_source", "text_ft2_source_v2")
            size = int(e["size"] * H / 1080)
            settings = {"text": e["text"], "font": {"face": e["font"], "size": size,
                                                    "style": "Bold" if e.get("bold") else "Regular",
                                                    "flags": 1 if e.get("bold") else 0}}
            col = hex_to_abgr(e["color"])
            if kind.startswith("text_gdiplus"):
                settings.update(color=col & 0xFFFFFF, opacity=int(e.get("opacity", 1) * 100))
            else:
                settings.update(color1=col, color2=col)
            return kind, settings
        if e["type"] == "image":
            path = (self.ctx.media_dir / e["src"]).resolve()
            return "image_source", {"file": str(path)}
        return obs.kind("color_source", "color_source_v3"), {
            "color": hex_to_abgr(e["color"], e.get("opacity", 1)),
            "width": max(1, int(e["w"] * W)), "height": max(1, int(e["h"] * H))}

    def _transform(self, e: dict, W: int, H: int, src_w: int, src_h: int) -> dict:
        t: dict[str, Any] = {"positionX": e["x"] * W, "positionY": e["y"] * H, "alignment": 5, "rotation": 0.0}
        if e["type"] in ("camera", "image"):
            c = e["crop"]
            if e["type"] == "camera" and turned_crop(c):
                c = {"l": 0, "t": 0, "r": 0, "b": 0}
            t.update(boundsType="OBS_BOUNDS_STRETCH" if e["fit"] == "stretch" else "OBS_BOUNDS_SCALE_INNER",
                     boundsAlignment=0, boundsWidth=max(1.0, e["w"] * W), boundsHeight=max(1.0, e["h"] * H),
                     cropLeft=int(c["l"] * src_w), cropRight=int(c["r"] * src_w),
                     cropTop=int(c["t"] * src_h), cropBottom=int(c["b"] * src_h))
        else:
            t.update(boundsType="OBS_BOUNDS_NONE", scaleX=1.0, scaleY=1.0,
                     cropLeft=0, cropRight=0, cropTop=0, cropBottom=0)
        return t

    async def sync(self, lid: str) -> dict:
        async with self._locks.setdefault(lid, asyncio.Lock()):
            return await self._sync(lid)

    async def _sync(self, lid: str) -> dict:
        obs = self.ctx.obs
        if not obs.connected:
            self.sync_state[lid] = {"state": "pending", "error": "OBS not connected - will sync on reconnect"}
            return self.sync_state[lid]
        l = self.get(lid)
        scene = layout_scene(l.name)
        errors: list[str] = []
        try:
            video = obs.state.get("video") or {}
            W, H = int(video.get("base_w") or 1920), int(video.get("base_h") or 1080)
            await obs.ensure_scene(scene, "layout", lid)
            cams = {c.id for c in self.ctx.cameras.cameras()}
            desired: list[tuple[dict, str]] = []
            for e in sorted(self._arranged(l, W, H), key=lambda e: e["z"]):
                if e["type"] == "camera":
                    if e.get("camera_id") not in cams:
                        errors.append(f"'{e['name']}' has no camera assigned")
                        continue
                    desired.append((e, cam_input_name(e["camera_id"])))
                else:
                    name = graphic_name(e["id"])
                    if e["type"] == "image" and (not e.get("src") or not (self.ctx.media_dir / e["src"]).exists()):
                        errors.append(f"'{e['name']}' image file missing")
                        continue
                    kind, settings = self._graphic(e, W, H)
                    await obs.ensure_input(name, kind, settings, scene, "graphic", lid, enabled=not e["hidden"])
                    desired.append((e, name))
            want = {src for _, src in desired}
            # remove stale items (scene is FestCast-owned) and graphics no longer used by this layout
            for it in await obs.items(scene):
                if it["sourceName"] not in want and it["sourceName"] != BUS_SCENE:
                    await obs.call("RemoveSceneItem", {"sceneName": scene, "sceneItemId": it["sceneItemId"]})
            for row in obs.owned_by_ref(lid, "graphic"):
                if row.name not in want:
                    await obs.remove_owned_input(row.name)
            items = {it["sourceName"]: it["sceneItemId"] for it in await obs.items(scene)}
            for idx, (e, src) in enumerate(desired):
                iid = items.get(src)
                if iid is None:
                    iid = await obs.add_item(scene, src, enabled=not e["hidden"])
                sw, sh = (1920, 1080)
                if e["type"] == "camera":
                    sw, sh = self.ctx.cameras.source_size(self.ctx.cameras.camera(e["camera_id"]))
                await obs.call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": iid,
                                                         "sceneItemTransform": self._transform(e, W, H, sw, sh)})
                await obs.call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": iid,
                                                       "sceneItemEnabled": not e["hidden"]})
                await obs.call("SetSceneItemLocked", {"sceneName": scene, "sceneItemId": iid,
                                                      "sceneItemLocked": e["locked"]})
                await obs.call("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": iid, "sceneItemIndex": idx})
            await self.ctx.audio.attach(scene)
            self.sync_state[lid] = {"state": "warning" if errors else "synced", "error": "; ".join(errors) or None}
        except ObsError as e:
            log.warning("layout sync failed: %s", e)
            self.sync_state[lid] = {"state": "error", "error": f"{e.request}: {e.comment}"}
            self.ctx.bus.notify("error", f"Layout '{l.name}' did not fully apply in OBS: {e.comment}", "layouts")
        self.ctx.bus.publish("layouts", self.snapshot())
        return self.sync_state[lid]

    async def sync_all(self) -> None:
        with self.ctx.db.session() as s:          # tidy layouts saved with the old Landscape/Portrait crop
            for l in s.query(Layout):
                if any(e.get("type") == "camera" and turned_crop(e.get("crop") or {"l": 0, "t": 0, "r": 0, "b": 0})
                       for e in l.elements):
                    l.elements = [clean_element(e) for e in l.elements]
        for l in self.all():
            await self.sync(l.id)

    async def obs_state(self, lid: str) -> list[dict]:
        """What OBS actually has, for reconciliation in the editor."""
        l = self.get(lid)
        return await self.ctx.obs.items(layout_scene(l.name))

    def snapshot(self) -> list[dict]:
        return [self.view(l) for l in self.all()]

    def publish(self) -> None:
        self.ctx.bus.publish("layouts", self.snapshot())


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)
