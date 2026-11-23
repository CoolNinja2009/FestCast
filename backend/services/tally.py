"""Tally lights for the camera phones: red border = this camera is on programme, green = in preview.

OBS is the source of truth: a camera counts as on programme / in preview when its OBS input (FC_CAM_<id>) is a visible
item of that scene, directly or through nested scenes and groups (layouts, picture-in-picture, operator-made scenes).
Recomputed at once on OBS scene / scene-item events, and every second as a safety net. Phones get it by long-polling
/api/public/cam/<token>/tally (local and VDO.Ninja camera pages alike), so the light changes within a moment of the cut.
When OBS is not connected the tally is "unknown" (no light): never a guess."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.tally")

PREFIX = "FC_CAM_"
EVENTS = ("CurrentProgramSceneChanged", "CurrentPreviewSceneChanged", "StudioModeStateChanged",
          "SceneItemEnableStateChanged", "SceneItemCreated", "SceneItemRemoved", "SceneListChanged")


class TallyService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.known = False                          # False = OBS not connected / not read yet
        self.program: set[str] = set()
        self.preview: set[str] = set()
        self.version = 0
        self._changed = asyncio.Event()
        self._pending: asyncio.Task | None = None
        for ev in EVENTS:
            ctx.obs.on_event(ev, lambda d: self.kick())

    def state(self, cid: str) -> str:
        if not self.known:
            return "unknown"
        return "program" if cid in self.program else "preview" if cid in self.preview else "off"

    def kick(self) -> None:
        """Recompute soon (coalesces bursts of OBS events into one read)."""
        if not self._pending or self._pending.done():
            self._pending = asyncio.create_task(self.refresh())

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("tally refresh")
            await asyncio.sleep(1)

    async def _cams_in(self, scene: str | None, scenes: set[str], seen: set[str] | None = None,
                       group: bool = False) -> set[str]:
        if not scene:
            return set()
        seen = seen if seen is not None else set()
        if scene in seen:                       # a scene nested in itself (OBS forbids it, but never loop)
            return set()
        seen.add(scene)
        req = "GetGroupSceneItemList" if group else "GetSceneItemList"
        items = (await self.ctx.obs.call(req, {"sceneName": scene})).get("sceneItems", [])
        out: set[str] = set()
        for it in items:
            if not it.get("sceneItemEnabled", True):
                continue
            name = it.get("sourceName") or ""
            if name.startswith(PREFIX):
                out.add(name[len(PREFIX):])
            elif it.get("isGroup"):
                out |= await self._cams_in(name, scenes, seen, group=True)
            elif it.get("sourceType") == "OBS_SOURCE_TYPE_SCENE" or name in scenes:
                out |= await self._cams_in(name, scenes, seen)
        return out

    async def refresh(self) -> None:
        obs = self.ctx.obs
        known, program, preview = False, set(), set()
        if obs.connected and obs.state.get("program_scene"):
            try:
                scenes = {s["sceneName"] for s in (await obs.call("GetSceneList")).get("scenes", [])}
                program = await self._cams_in(obs.state.get("program_scene"), scenes)
                if obs.state.get("studio_mode") and obs.state.get("preview_scene") != obs.state.get("program_scene"):
                    preview = await self._cams_in(obs.state.get("preview_scene"), scenes) - program
                known = True
            except ObsError as e:              # scene vanished mid-read: the next event / tick reads it again
                log.debug("tally read failed: %s", e)
                return
            except (ConnectionError, OSError):
                pass
        if (known, program, preview) != (self.known, self.program, self.preview):
            self.known, self.program, self.preview = known, program, preview
            self.version += 1
            self._changed.set()
            self._changed = asyncio.Event()
            self.ctx.cameras.publish()          # dashboard camera cards show the same tally

    async def wait(self, since: int, timeout: float = 20.0) -> None:
        """Long-poll: return when the tally changes after `since`, or after `timeout`."""
        if since != self.version:
            return
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass
