"""OBSController: connection lifecycle, cached state, typed helpers and the ownership registry.

Rules enforced here:
* the OBS password never leaves the server;
* FestCast only removes/modifies inputs and scenes recorded in the `obs_owned` registry;
* every OBS failure is raised as ObsError so callers can report and reconcile.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..core.events import EventBus
from ..database.db import Database
from ..database.models import ObsOwned
from .client import ObsError, ObsNotConnected, ObsTransport

log = logging.getLogger("festcast.obs")

OUT_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OUT_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"


class OBSController:
    def __init__(self, bus: EventBus, db: Database, transport_factory: Callable[[Callable], Any]) -> None:
        self.bus, self.db = bus, db
        self.transport = transport_factory(self._dispatch)
        self.simulated = bool(getattr(self.transport, "simulated", False))
        self.state: dict[str, Any] = {"connected": False, "simulated": self.simulated, "error": None,
                                      "version": None, "program_scene": None, "preview_scene": None,
                                      "studio_mode": False, "recording": False, "record_paused": False,
                                      "streaming": False, "connected_since": None, "video": None,
                                      "transitions": [], "transition": None}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._connect_hooks: list[Callable] = []
        self._task: asyncio.Task | None = None
        self._kinds: list[str] = []
        self.connected_event = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------
    def on_event(self, event_type: str, cb: Callable) -> None:
        self._event_handlers.setdefault(event_type, []).append(cb)

    def on_connect(self, cb: Callable) -> None:
        self._connect_hooks.append(cb)

    @property
    def connected(self) -> bool:
        return self.transport.connected

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.transport.close()

    async def _run(self) -> None:
        delay = 1.0
        last_err = None
        while True:
            try:
                await self.transport.connect()
                delay = 1.0
                last_err = None
                await self._on_connected()
                await self.transport.closed.wait()
                self._set_disconnected("OBS connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e) or e.__class__.__name__
                if isinstance(e, OSError) or "refused" in msg.lower() or isinstance(e, asyncio.TimeoutError):
                    msg = "OBS not reachable - is OBS running with its WebSocket server enabled?"
                if msg != last_err:
                    log.warning("OBS connect failed: %s", msg)
                    last_err = msg
                self._set_disconnected(msg, quiet=True)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    def _set_disconnected(self, reason: str, quiet: bool = False) -> None:
        was = self.state["connected"]
        self.connected_event.clear()
        self.state.update(connected=False, error=reason, connected_since=None)
        self.bus.publish("obs.status", self.public_state())
        if was and not quiet:
            self.bus.notify("error", f"{reason}. Recording and the programme continue inside OBS; "
                            "FestCast will reconnect automatically.", "obs")

    async def _on_connected(self) -> None:
        v = await self.call("GetVersion")
        self._kinds = (await self.call("GetInputKindList", {"unversioned": False})).get("inputKinds", [])
        self.state.update(connected=True, error=None, connected_since=time.time(), version={
            "obs": v.get("obsVersion"), "websocket": v.get("obsWebSocketVersion"), "rpc": v.get("rpcVersion"),
            "platform": v.get("platformDescription") or v.get("platform")})
        await self.refresh_state()
        self.connected_event.set()
        log.info("OBS connected (%s, websocket %s)%s", v.get("obsVersion"), v.get("obsWebSocketVersion"),
                 " [SIMULATION]" if self.simulated else "")
        self.bus.notify("info", "Connected to OBS" + (" (simulation)" if self.simulated else ""), "obs")
        for hook in self._connect_hooks:
            try:
                await hook()
            except Exception as e:  # noqa: BLE001
                log.exception("OBS connect hook failed")
                self.bus.notify("error", f"OBS resync failed: {e}", "obs")
        self.bus.publish("obs.status", self.public_state())

    async def refresh_state(self) -> None:
        vs = await self.call("GetVideoSettings")
        prog = await self.call("GetCurrentProgramScene")
        studio = (await self.call("GetStudioModeEnabled")).get("studioModeEnabled", False)
        prev = None
        if studio:
            try:
                prev = (await self.call("GetCurrentPreviewScene")).get("currentPreviewSceneName")
            except ObsError:
                prev = None
        rec = await self.call("GetRecordStatus")
        stream = await self.call("GetStreamStatus")
        tr = await self.call("GetSceneTransitionList")
        self.state.update(
            video={"base_w": vs.get("baseWidth"), "base_h": vs.get("baseHeight"), "out_w": vs.get("outputWidth"),
                   "out_h": vs.get("outputHeight"),
                   "fps": round(vs.get("fpsNumerator", 30) / max(1, vs.get("fpsDenominator", 1)), 2)},
            program_scene=prog.get("currentProgramSceneName") or prog.get("sceneName"),
            preview_scene=prev, studio_mode=studio,
            recording=rec.get("outputActive", False), record_paused=rec.get("outputPaused", False),
            streaming=stream.get("outputActive", False),
            transitions=[t.get("transitionName") for t in tr.get("transitions", [])],
            transition=tr.get("currentSceneTransitionName"))

    def public_state(self) -> dict:
        return dict(self.state)

    async def _dispatch(self, event_type: str, data: dict) -> None:
        st = self.state
        if event_type == "CurrentProgramSceneChanged":
            st["program_scene"] = data.get("sceneName")
        elif event_type == "CurrentPreviewSceneChanged":
            st["preview_scene"] = data.get("sceneName")
        elif event_type == "StudioModeStateChanged":
            st["studio_mode"] = data.get("studioModeEnabled", False)
            if not st["studio_mode"]:
                st["preview_scene"] = None
        elif event_type == "RecordStateChanged":
            s = data.get("outputState")
            if s in (OUT_STARTED, "OBS_WEBSOCKET_OUTPUT_RESUMED"):
                st["recording"], st["record_paused"] = True, False
            elif s == "OBS_WEBSOCKET_OUTPUT_PAUSED":
                st["record_paused"] = True
            elif s == OUT_STOPPED:
                st["recording"], st["record_paused"] = False, False
        elif event_type == "StreamStateChanged":
            s = data.get("outputState")
            if s == OUT_STARTED:
                st["streaming"] = True
            elif s == OUT_STOPPED:
                st["streaming"] = False
        elif event_type == "CurrentSceneTransitionChanged":
            st["transition"] = data.get("transitionName")
        elif event_type == "ExitStarted":
            self.bus.notify("warning", "OBS is closing", "obs")
        if event_type != "InputVolumeMeters":
            self.bus.publish("obs.status", self.public_state())
        for cb in self._event_handlers.get(event_type, []):
            try:
                r = cb(data)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("handler for %s failed", event_type)

    # -- raw ------------------------------------------------------------------
    async def call(self, request: str, data: dict | None = None) -> dict:
        if not self.transport.connected:
            raise ObsNotConnected(request)
        return await self.transport.call(request, data)

    async def try_call(self, request: str, data: dict | None = None) -> dict | None:
        try:
            return await self.call(request, data)
        except ObsError as e:
            log.warning("%s", e)
            return None

    # -- ownership registry -------------------------------------------------------
    def owned(self, name: str) -> ObsOwned | None:
        with self.db.session() as s:
            return s.get(ObsOwned, name)

    def owned_by_ref(self, ref_id: str, role: str | None = None) -> list[ObsOwned]:
        with self.db.session() as s:
            q = s.query(ObsOwned).filter(ObsOwned.ref_id == ref_id)
            if role:
                q = q.filter(ObsOwned.role == role)
            return list(q)

    def register(self, name: str, kind: str, role: str, ref_id: str, uuid: str | None) -> None:
        with self.db.session() as s:
            row = s.get(ObsOwned, name)
            if row is None:
                s.add(ObsOwned(name=name, kind=kind, role=role, ref_id=ref_id, uuid=uuid))
            else:
                row.uuid, row.ref_id, row.role, row.kind = uuid, ref_id, role, kind

    def unregister(self, name: str) -> None:
        with self.db.session() as s:
            row = s.get(ObsOwned, name)
            if row:
                s.delete(row)

    # -- scenes -------------------------------------------------------------------
    async def scenes(self) -> list[str]:
        r = await self.call("GetSceneList")
        return [s["sceneName"] for s in reversed(r.get("scenes", []))]

    async def ensure_scene(self, name: str, role: str, ref_id: str) -> None:
        if name not in await self.scenes():
            r = await self.call("CreateScene", {"sceneName": name})
            log.info("created OBS scene %s", name)
            self.register(name, "scene", role, ref_id, r.get("sceneUuid"))
        elif not self.owned(name):
            # a scene with that name exists but FestCast did not create it - adopt only by explicit registry
            self.register(name, "scene", role, ref_id, None)

    async def rename_scene(self, old: str, new: str) -> None:
        if old == new or not self.owned(old):
            return
        row = self.owned(old)
        if old in await self.scenes():
            await self.call("SetSceneName", {"sceneName": old, "newSceneName": new})
        self.unregister(old)
        self.register(new, "scene", row.role, row.ref_id, row.uuid)

    async def remove_owned_scene(self, name: str) -> None:
        if not self.owned(name):
            raise ObsError("RemoveScene", 0, f"refusing to remove scene '{name}' not owned by FestCast")
        if name in await self.scenes():
            if self.state.get("program_scene") == name:
                raise ObsError("RemoveScene", 0, f"'{name}' is live on programme - switch away first")
            await self.call("RemoveScene", {"sceneName": name})
            log.info("removed OBS scene %s", name)
        self.unregister(name)

    # -- inputs ---------------------------------------------------------------------
    async def inputs(self) -> dict[str, dict]:
        r = await self.call("GetInputList")
        return {i["inputName"]: i for i in r.get("inputs", [])}

    def kind(self, prefix: str, fallback: str) -> str:
        for k in sorted(self._kinds, reverse=True):
            if k.startswith(prefix):
                return k
        return fallback

    async def ensure_input(self, name: str, kind: str, settings: dict, scene: str, role: str, ref_id: str,
                           enabled: bool = True) -> None:
        existing = await self.inputs()
        if name in existing:
            await self.call("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": True})
            self.register(name, "input", role, ref_id, existing[name].get("inputUuid"))
            if await self.item_id(scene, name) is None:
                await self.call("CreateSceneItem", {"sceneName": scene, "sourceName": name,
                                                    "sceneItemEnabled": enabled})
            return
        r = await self.call("CreateInput", {"sceneName": scene, "inputName": name, "inputKind": kind,
                                            "inputSettings": settings, "sceneItemEnabled": enabled})
        self.register(name, "input", role, ref_id, r.get("inputUuid"))
        log.info("created OBS input %s (%s)", name, kind)

    async def remove_owned_input(self, name: str) -> None:
        if not self.owned(name):
            raise ObsError("RemoveInput", 0, f"refusing to remove input '{name}' not owned by FestCast")
        if name in await self.inputs():
            await self.call("RemoveInput", {"inputName": name})
            log.info("removed OBS input %s", name)
        self.unregister(name)

    # -- scene items -------------------------------------------------------------------
    async def items(self, scene: str) -> list[dict]:
        return (await self.call("GetSceneItemList", {"sceneName": scene})).get("sceneItems", [])

    async def item_id(self, scene: str, source: str) -> int | None:
        for it in await self.items(scene):
            if it.get("sourceName") == source:
                return it["sceneItemId"]
        return None

    async def add_item(self, scene: str, source: str, enabled: bool = True) -> int:
        r = await self.call("CreateSceneItem", {"sceneName": scene, "sourceName": source,
                                                "sceneItemEnabled": enabled})
        return r["sceneItemId"]

    async def fit_full(self, scene: str, source: str) -> None:
        """Make `source` fill the whole OBS canvas in `scene` (whatever the canvas size is)."""
        iid = await self.item_id(scene, source)
        if iid is None:
            return
        v = self.state.get("video") or {}
        await self.call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": iid, "sceneItemTransform": {
            "positionX": 0.0, "positionY": 0.0, "alignment": 5, "rotation": 0.0,
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
            "boundsWidth": float(v.get("base_w") or 1920), "boundsHeight": float(v.get("base_h") or 1080)}})

    async def screenshot(self, source: str, width: int = 480, quality: int = 60) -> str | None:
        r = await self.try_call("GetSourceScreenshot", {"sourceName": source, "imageFormat": "jpg",
                                                         "imageWidth": width, "imageCompressionQuality": quality})
        return r.get("imageData") if r else None
