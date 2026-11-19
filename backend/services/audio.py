"""Audio: OBS mixer state, live meters (InputVolumeMeters events), mute/volume/sync/monitoring and clipping
warnings. FestCast never switches audio automatically when a camera disconnects."""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.audio")
MONITOR = {"none": "OBS_MONITORING_TYPE_NONE", "monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY",
           "both": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"}
CLIP_DB = -0.5


def db(mul: float) -> float | None:
    return round(20 * math.log10(mul), 1) if mul and mul > 0 else None


class AudioService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.levels: dict[str, dict] = {}
        self._last_pub = 0.0
        self._clip_warn: dict[str, float] = {}

    async def mixer(self) -> list[dict]:
        obs = self.ctx.obs
        out = []
        cams = {f"FC_CAM_{c.id}": c.name for c in self.ctx.cameras.cameras()}
        for name, inp in (await obs.inputs()).items():
            try:
                vol = await obs.call("GetInputVolume", {"inputName": name})
            except ObsError:
                continue   # not an audio source
            mute = await obs.try_call("GetInputMute", {"inputName": name}) or {}
            sync = await obs.try_call("GetInputAudioSyncOffset", {"inputName": name}) or {}
            mon = await obs.try_call("GetInputAudioMonitorType", {"inputName": name}) or {}
            out.append({"name": name, "label": cams.get(name, name), "kind": inp.get("inputKind"),
                        "is_camera": name in cams, "muted": mute.get("inputMuted"),
                        "volume_db": round(vol.get("inputVolumeDb", 0.0), 1),
                        "sync_offset_ms": sync.get("inputAudioSyncOffset"),
                        "monitor": next((k for k, v in MONITOR.items() if v == mon.get("monitorType")), None)})
        return out

    async def set(self, name: str, muted: bool | None = None, volume_db: float | None = None,
                  sync_offset_ms: int | None = None, monitor: str | None = None) -> None:
        obs = self.ctx.obs
        if name not in await obs.inputs():
            raise KeyError(name)
        if muted is not None:
            await obs.call("SetInputMute", {"inputName": name, "inputMuted": bool(muted)})
        if volume_db is not None:
            await obs.call("SetInputVolume", {"inputName": name, "inputVolumeDb": max(-100.0, min(26.0, volume_db))})
        if sync_offset_ms is not None:
            await obs.call("SetInputAudioSyncOffset", {"inputName": name,
                                                       "inputAudioSyncOffset": max(-950, min(20000, int(sync_offset_ms)))})
        if monitor is not None:
            if monitor not in MONITOR:
                raise ValueError("monitor must be none, monitor or both")
            await obs.call("SetInputAudioMonitorType", {"inputName": name, "monitorType": MONITOR[monitor]})
        log.info("audio %s: muted=%s vol=%s sync=%s monitor=%s", name, muted, volume_db, sync_offset_ms, monitor)

    def on_meters(self, data: dict) -> None:
        t = time.time()
        for inp in data.get("inputs", []):
            chans = inp.get("inputLevelsMul") or []
            if not chans:
                continue
            mag = max((c[0] for c in chans if len(c) > 0), default=0)
            peak = max((c[1] for c in chans if len(c) > 1), default=0)
            name = inp.get("inputName")
            pdb = db(peak)
            self.levels[name] = {"rms_db": db(mag), "peak_db": pdb, "ts": t}
            if pdb is not None and pdb >= CLIP_DB:
                self.levels[name]["clip"] = t
                if t - self._clip_warn.get(name, 0) > 15:
                    self._clip_warn[name] = t
                    self.ctx.bus.notify("warning", f"Audio clipping on {name} ({pdb} dBFS) - lower its gain", "audio")
        if t - self._last_pub > 0.12:
            self._last_pub = t
            self.ctx.bus.publish("meters", self.levels)
