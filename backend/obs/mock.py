"""In-memory OBS for simulation mode and tests. Implements the subset of OBS WebSocket v5 requests FestCast uses,
with the same request/response field names, so the real request-building code is exercised.
Everything it produces is labelled simulated; it never pretends to be a real OBS."""
from __future__ import annotations

import asyncio
import base64
import io
import itertools
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .client import ObsError, ObsNotConnected

OUT = "OBS_WEBSOCKET_OUTPUT_"


def _tiny_jpeg() -> str:
    try:
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (160, 90), (40, 40, 48))
        ImageDraw.Draw(im).text((8, 38), "SIMULATED", fill=(200, 200, 200))
        b = io.BytesIO()
        im.save(b, "JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


class MockObsTransport:
    simulated = True

    def __init__(self, on_event: Callable) -> None:
        self.on_event = on_event
        self.closed = asyncio.Event()
        self._connected = False
        self.fail_connect = False
        self.scenes: list[dict] = [{"sceneName": "Scene", "sceneUuid": uuid.uuid4().hex, "items": []}]
        self.inputs: dict[str, dict] = {}
        self.program = "Scene"
        self.preview: str | None = None
        self.studio = False
        self.rec = {"active": False, "paused": False, "start": 0.0, "dir": str(Path.home() / "Videos"), "path": None}
        self.stream = {"active": False, "start": 0.0}
        self.service = {"streamServiceType": "rtmp_common", "streamServiceSettings": {}}
        self.profile: dict[tuple[str, str], str] = {("Output", "Mode"): "Simple",
                                                     ("SimpleOutput", "RecFormat2"): "mkv",
                                                     ("SimpleOutput", "RecEncoder"): "x264",
                                                     ("Output", "FilenameFormatting"): "%CCYY-%MM-%DD %hh-%mm-%ss"}
        self.transition = "Fade"
        self.duration = 300
        self.ids = itertools.count(1)
        self.calls: list[tuple[str, dict]] = []
        self.projectors: list[dict] = []
        self.profiles = ["Untitled"]
        self.profile_name = "Untitled"
        self.reloads = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self.fail_connect:
            raise OSError("connection refused (simulated)")
        self.closed = asyncio.Event()
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self.closed.set()

    def drop(self) -> None:
        """Simulate OBS crashing / closing."""
        self._connected = False
        self.closed.set()

    async def _emit(self, t: str, d: dict) -> None:
        r = self.on_event(t, d)
        if asyncio.iscoroutine(r):
            await r

    def _scene(self, d: dict) -> dict:
        name = d.get("sceneName")
        for s in self.scenes:
            if s["sceneName"] == name or s["sceneUuid"] == d.get("sceneUuid"):
                return s
        raise ObsError("scene", 600, f"No source was found by the name of `{name}`.")

    def _item(self, d: dict) -> dict:
        for it in self._scene(d)["items"]:
            if it["sceneItemId"] == d.get("sceneItemId"):
                return it
        raise ObsError("item", 600, "scene item not found")

    async def call(self, req: str, d: dict | None = None) -> dict:  # noqa: C901
        if not self._connected:
            raise ObsNotConnected(req)
        d = d or {}
        self.calls.append((req, d))
        await asyncio.sleep(0)
        fn = getattr(self, "r_" + req, None)
        if fn is None:
            raise ObsError(req, 204, "unknown request (mock)")
        r = fn(d)
        if asyncio.iscoroutine(r):
            r = await r
        return r or {}

    # general
    def r_GetVersion(self, d): return {"obsVersion": "SIMULATED", "obsWebSocketVersion": "5.x-mock", "rpcVersion": 1,
                                       "platformDescription": "simulation", "availableRequests": []}
    def r_GetInputKindList(self, d): return {"inputKinds": ["browser_source", "image_source", "color_source_v3",
                                                            "text_gdiplus_v3", "ffmpeg_source"]}
    def r_GetVideoSettings(self, d): return {"baseWidth": 1920, "baseHeight": 1080, "outputWidth": 1920,
                                             "outputHeight": 1080, "fpsNumerator": 30, "fpsDenominator": 1}
    def r_GetStats(self, d): return {"cpuUsage": 3.1, "memoryUsage": 512.0, "activeFps": 30.0,
                                     "averageFrameRenderTime": 1.2, "renderSkippedFrames": 0, "renderTotalFrames": 100,
                                     "outputSkippedFrames": 0, "outputTotalFrames": 100}
    def r_GetMonitorList(self, d): return {"monitors": [{"monitorName": "Simulated 1", "monitorIndex": 0,
                                                         "monitorWidth": 1920, "monitorHeight": 1080}]}
    def r_OpenVideoMixProjector(self, d): self.projectors.append(d)
    def r_GetSourceScreenshot(self, d): return {"imageData": _tiny_jpeg()}

    # scenes
    def r_GetSceneList(self, d):
        return {"currentProgramSceneName": self.program, "currentPreviewSceneName": self.preview,
                "scenes": [{"sceneName": s["sceneName"], "sceneUuid": s["sceneUuid"], "sceneIndex": i}
                           for i, s in enumerate(reversed(self.scenes))]}

    def r_CreateScene(self, d):
        if any(s["sceneName"] == d["sceneName"] for s in self.scenes):
            raise ObsError("CreateScene", 601, "exists")
        s = {"sceneName": d["sceneName"], "sceneUuid": uuid.uuid4().hex, "items": []}
        self.scenes.append(s)
        return {"sceneUuid": s["sceneUuid"]}

    def r_RemoveScene(self, d): self.scenes.remove(self._scene(d))
    def r_SetSceneName(self, d): self._scene(d)["sceneName"] = d["newSceneName"]
    def r_GetCurrentProgramScene(self, d): return {"currentProgramSceneName": self.program, "sceneName": self.program}
    def r_GetCurrentPreviewScene(self, d):
        if not self.studio:
            raise ObsError("GetCurrentPreviewScene", 506, "studio mode not active")
        return {"currentPreviewSceneName": self.preview}

    async def r_SetCurrentProgramScene(self, d):
        self._scene(d)
        self.program = d["sceneName"]
        await self._emit("CurrentProgramSceneChanged", {"sceneName": self.program})

    async def r_SetCurrentPreviewScene(self, d):
        if not self.studio:
            raise ObsError("SetCurrentPreviewScene", 506, "studio mode not active")
        self._scene(d)
        self.preview = d["sceneName"]
        await self._emit("CurrentPreviewSceneChanged", {"sceneName": self.preview})

    def r_GetStudioModeEnabled(self, d): return {"studioModeEnabled": self.studio}

    async def r_SetStudioModeEnabled(self, d):
        self.studio = d["studioModeEnabled"]
        self.preview = self.program if self.studio else None
        await self._emit("StudioModeStateChanged", {"studioModeEnabled": self.studio})

    async def r_TriggerStudioModeTransition(self, d):
        if not self.studio:
            raise ObsError("TriggerStudioModeTransition", 506, "studio mode not active")
        self.program, self.preview = self.preview, self.program
        await self._emit("CurrentProgramSceneChanged", {"sceneName": self.program})
        await self._emit("CurrentPreviewSceneChanged", {"sceneName": self.preview})

    def r_GetSceneTransitionList(self, d):
        return {"currentSceneTransitionName": self.transition, "currentSceneTransitionDuration": self.duration,
                "transitions": [{"transitionName": n} for n in ("Cut", "Fade")]}
    def r_SetCurrentSceneTransition(self, d): self.transition = d["transitionName"]
    def r_SetCurrentSceneTransitionDuration(self, d): self.duration = d["transitionDuration"]

    # inputs
    def r_GetInputList(self, d):
        return {"inputs": [{"inputName": n, "inputUuid": i["uuid"], "inputKind": i["kind"]}
                           for n, i in self.inputs.items()]}

    def r_CreateInput(self, d):
        if d["inputName"] in self.inputs:
            raise ObsError("CreateInput", 601, "exists")
        u = uuid.uuid4().hex
        self.inputs[d["inputName"]] = {"uuid": u, "kind": d["inputKind"], "settings": dict(d.get("inputSettings", {})),
                                       "muted": False, "vol": 1.0, "sync": 0, "monitor": "OBS_MONITORING_TYPE_NONE"}
        iid = next(self.ids)
        self._scene(d)["items"].append({"sceneItemId": iid, "sourceName": d["inputName"], "sourceUuid": u,
                                        "sceneItemEnabled": d.get("sceneItemEnabled", True), "sceneItemLocked": False,
                                        "sceneItemTransform": {}})
        return {"inputUuid": u, "sceneItemId": iid}

    def r_RemoveInput(self, d):
        name = d["inputName"]
        if name not in self.inputs:
            raise ObsError("RemoveInput", 600, "not found")
        del self.inputs[name]
        for s in self.scenes:
            s["items"] = [i for i in s["items"] if i["sourceName"] != name]

    def r_SetInputSettings(self, d): self._inp(d)["settings"].update(d["inputSettings"])
    def r_GetInputSettings(self, d): return {"inputSettings": self._inp(d)["settings"], "inputKind": self._inp(d)["kind"]}
    def r_GetInputMute(self, d): return {"inputMuted": self._inp(d)["muted"]}
    def r_SetInputMute(self, d): self._inp(d)["muted"] = d["inputMuted"]
    def r_GetInputVolume(self, d): return {"inputVolumeMul": self._inp(d)["vol"], "inputVolumeDb": 0.0}
    def r_SetInputVolume(self, d):
        if "inputVolumeMul" in d:
            self._inp(d)["vol"] = d["inputVolumeMul"]
        else:
            self._inp(d)["vol"] = 10 ** (d["inputVolumeDb"] / 20)
    def r_GetInputAudioSyncOffset(self, d): return {"inputAudioSyncOffset": self._inp(d)["sync"]}
    def r_SetInputAudioSyncOffset(self, d): self._inp(d)["sync"] = d["inputAudioSyncOffset"]
    def r_GetInputAudioMonitorType(self, d): return {"monitorType": self._inp(d)["monitor"]}
    def r_SetInputAudioMonitorType(self, d): self._inp(d)["monitor"] = d["monitorType"]

    def _inp(self, d) -> dict:
        n = d.get("inputName")
        if n not in self.inputs:
            raise ObsError("input", 600, f"No source was found by the name of `{n}`.")
        return self.inputs[n]

    # scene items
    def r_GetSceneItemList(self, d):
        items = self._scene(d)["items"]
        return {"sceneItems": [dict(i, sceneItemIndex=n) for n, i in enumerate(items)]}

    def r_CreateSceneItem(self, d):
        src = d["sourceName"]
        if src not in self.inputs and not any(s["sceneName"] == src for s in self.scenes):
            raise ObsError("CreateSceneItem", 600, "source not found")
        iid = next(self.ids)
        self._scene(d)["items"].append({"sceneItemId": iid, "sourceName": src, "sourceUuid": "",
                                        "sceneItemEnabled": d.get("sceneItemEnabled", True),
                                        "sceneItemLocked": False, "sceneItemTransform": {}})
        return {"sceneItemId": iid}

    def r_RemoveSceneItem(self, d):
        s = self._scene(d)
        s["items"].remove(self._item(d))

    def r_SetSceneItemTransform(self, d): self._item(d)["sceneItemTransform"].update(d["sceneItemTransform"])
    def r_SetSceneItemEnabled(self, d): self._item(d)["sceneItemEnabled"] = d["sceneItemEnabled"]
    def r_SetSceneItemLocked(self, d): self._item(d)["sceneItemLocked"] = d["sceneItemLocked"]

    def r_SetSceneItemIndex(self, d):
        s = self._scene(d)
        it = self._item(d)
        s["items"].remove(it)
        s["items"].insert(min(d["sceneItemIndex"], len(s["items"])), it)

    # record
    def r_GetRecordDirectory(self, d): return {"recordDirectory": self.rec["dir"]}
    def r_SetRecordDirectory(self, d): self.rec["dir"] = d["recordDirectory"]
    def r_GetProfileParameter(self, d):
        v = self.profile.get((d["parameterCategory"], d["parameterName"]))
        return {"parameterValue": v, "defaultParameterValue": None}
    def r_GetProfileList(self, d):
        return {"currentProfileName": self.profile_name, "profiles": list(self.profiles)}

    def r_CreateProfile(self, d):
        if d["profileName"] in self.profiles:
            raise ObsError("CreateProfile", 601, "exists")
        self.profiles.append(d["profileName"])
        self.profile_name = d["profileName"]
        self.reloads += 1

    def r_SetCurrentProfile(self, d):
        if d["profileName"] not in self.profiles:
            raise ObsError("SetCurrentProfile", 600, "no such profile")
        self.profile_name = d["profileName"]
        self.reloads += 1

    def r_RemoveProfile(self, d):
        if d["profileName"] == self.profile_name:
            raise ObsError("RemoveProfile", 505, "current profile")
        self.profiles.remove(d["profileName"])

    def r_SetProfileParameter(self, d): self.profile[(d["parameterCategory"], d["parameterName"])] = d["parameterValue"]

    def r_GetRecordStatus(self, d):
        dur = int((time.time() - self.rec["start"]) * 1000) if self.rec["active"] else 0
        size = Path(self.rec["path"]).stat().st_size if self.rec["active"] and self.rec["path"] and Path(
            self.rec["path"]).exists() else 0
        return {"outputActive": self.rec["active"], "outputPaused": self.rec["paused"], "outputDuration": dur,
                "outputBytes": size, "outputTimecode": ""}

    async def r_StartRecord(self, d):
        if self.rec["active"]:
            raise ObsError("StartRecord", 500, "recording already active")
        fmt = self.profile.get(("SimpleOutput", "RecFormat2"), "mkv")
        p = Path(self.rec["dir"]) / f"SIMULATED_{time.strftime('%H%M%S')}.{fmt}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"FESTCAST SIMULATED RECORDING - not a real video file\n")
        self.rec.update(active=True, paused=False, start=time.time(), path=str(p))
        await self._emit("RecordStateChanged", {"outputActive": True, "outputState": OUT + "STARTED",
                                                "outputPath": str(p)})

    async def r_StopRecord(self, d):
        if not self.rec["active"]:
            raise ObsError("StopRecord", 501, "recording not active")
        self.rec["active"] = False
        await self._emit("RecordStateChanged", {"outputActive": False, "outputState": OUT + "STOPPED",
                                                "outputPath": self.rec["path"]})
        return {"outputPath": self.rec["path"]}

    async def r_PauseRecord(self, d):
        self.rec["paused"] = True
        await self._emit("RecordStateChanged", {"outputActive": True, "outputState": OUT + "PAUSED"})

    async def r_ResumeRecord(self, d):
        self.rec["paused"] = False
        await self._emit("RecordStateChanged", {"outputActive": True, "outputState": OUT + "RESUMED"})

    # stream
    def r_GetStreamServiceSettings(self, d): return dict(self.service)
    def r_SetStreamServiceSettings(self, d): self.service = {"streamServiceType": d["streamServiceType"],
                                                            "streamServiceSettings": d["streamServiceSettings"]}
    def r_GetStreamStatus(self, d): return {"outputActive": self.stream["active"], "outputReconnecting": False,
                                            "outputBytes": 0, "outputDuration": 0, "outputCongestion": 0.0,
                                            "outputSkippedFrames": 0, "outputTotalFrames": 0}

    async def r_StartStream(self, d):
        self.stream.update(active=True, start=time.time())
        await self._emit("StreamStateChanged", {"outputActive": True, "outputState": OUT + "STARTED"})

    async def r_StopStream(self, d):
        self.stream["active"] = False
        await self._emit("StreamStateChanged", {"outputActive": False, "outputState": OUT + "STOPPED"})
