"""Audio: OBS mixer state, live meters (InputVolumeMeters events), mute/volume/sync/monitoring and clipping
warnings. FestCast never switches audio automatically when a camera disconnects.

Hall audio inputs (mic in / line in): each one is an OBS WASAPI capture input `FC_AUDIO_<id>` with device timing off
(OBS stamps audio on arrival, so a drifting sound-card clock cannot push OBS's audio buffering up) and only
zero-latency processing (noise gate, gain, limiter: no look-ahead; noise suppression is deliberately not offered,
it adds 10-20 ms+). They sit in one FestCast scene, `FC Audio`, nested into every FestCast programme scene (layouts,
camera scenes, standby), so the hall sound is on air whichever scene is live and survives every cut and transition
without a dip. Each input is on track 1 (programme / stream / TV) and can also get its own isolated recording track
(2-6) for the edit; with OBS in Advanced output mode the recording then writes those tracks too."""
from __future__ import annotations

import logging
import math
import secrets
import sys
import time
from typing import TYPE_CHECKING, Any

from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.audio")
MONITOR = {"none": "OBS_MONITORING_TYPE_NONE", "monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY",
           "both": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"}
CLIP_DB = -0.5
BUS_SCENE = "FC Audio"
BUS_ROLES = ("layout", "camscene", "standby")      # FestCast scenes that carry the hall audio
KEY = "audio_inputs"
KINDS = ("mic", "line")
F_GATE, F_GAIN, F_LIMIT = "FC Gate", "FC Gain", "FC Limiter"
ISO_TRACKS = (2, 3, 4, 5, 6)
PROBE = "FC_AUDIO_probe"
# processing defaults per input kind: a mic gets a gate (stage noise between speeches); a line input (mixing desk,
# playback) is already processed upstream. Both get a limiter so a shout or a dropped mic never clips the recording.
DEFAULTS = {"mic": {"gate": True, "limiter": True}, "line": {"gate": False, "limiter": True}}


def input_name(aid: str) -> str:
    return f"FC_AUDIO_{aid}"


def db(mul: float) -> float | None:
    return round(20 * math.log10(mul), 1) if mul and mul > 0 else None


class AudioService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.levels: dict[str, dict] = {}
        self._last_pub = 0.0
        self._clip_warn: dict[str, float] = {}

    # -- hall audio inputs (mic in / line in) -------------------------------------------------------------------
    def inputs(self) -> list[dict]:
        return [dict(a) for a in self.ctx.db.get(KEY, []) or []]

    def _save(self, items: list[dict]) -> None:
        self.ctx.db.set(KEY, items)

    def iso_mask(self) -> int:
        """Recording track bitmask: track 1 (programme mix) + every isolated track in use."""
        m = 1
        for a in self.inputs():
            if a.get("iso_track") in ISO_TRACKS:
                m |= 1 << (a["iso_track"] - 1)
        return m

    def _wasapi(self) -> str:
        """The OBS audio-input source kind of the PC OBS runs on: WASAPI (Windows), PulseAudio/PipeWire (Linux),
        Core Audio (macOS). All three take the same `device_id` setting and list devices the same way."""
        for kind in ("wasapi_input_capture", "pulse_input_capture", "coreaudio_input_capture"):
            if self.ctx.obs.kind(kind, "") == kind:
                return kind
        return {"win32": "wasapi_input_capture", "darwin": "coreaudio_input_capture"}.get(sys.platform,
                                                                                          "pulse_input_capture")

    @staticmethod
    def _settings(a: dict) -> dict:
        return {"device_id": a.get("device_id") or "default", "use_device_timing": False}

    def _check_track(self, items: list[dict], track: int | None, aid: str | None = None) -> None:
        if track is None:
            return
        if track not in ISO_TRACKS or any(i.get("iso_track") == track and i["id"] != aid for i in items):
            raise ValueError(f"recording track must be a free track {ISO_TRACKS[0]}-{ISO_TRACKS[-1]}")

    async def devices(self) -> list[dict]:
        """Capture devices OBS can open (WASAPI: microphones, line in, USB interfaces, mixing desks). Read from OBS's
        own property list, so the ids are exactly what OBS stores."""
        obs = self.ctx.obs
        have = await obs.inputs()
        src = next((input_name(a["id"]) for a in self.inputs() if input_name(a["id"]) in have), None)
        probe = src is None
        if probe:     # no FestCast audio input yet: a short-lived, muted probe input in the bus scene
            await obs.ensure_scene(BUS_SCENE, "audiobus", "audio")
            await obs.ensure_input(PROBE, self._wasapi(), {"device_id": "festcast_probe"}, BUS_SCENE, "audioprobe",
                                   "audio")
            await obs.try_call("SetInputMute", {"inputName": PROBE, "inputMuted": True})
            src = PROBE
        try:
            r = await obs.call("GetInputPropertiesListPropertyItems", {"inputName": src, "propertyName": "device_id"})
        finally:
            if probe:
                await obs.remove_owned_input(PROBE)
        out = []
        for it in r.get("propertyItems", []):
            v = it.get("itemValue")
            if not isinstance(v, str) or not v or v == "festcast_probe":
                continue
            name = it.get("itemName") or v
            low = name.lower()
            out.append({"id": v, "name": name, "enabled": it.get("itemEnabled", True),
                        "guess": "line" if ("line" in low or "aux" in low) else "mic"})
        return out

    async def add_input(self, name: str, kind: str, device_id: str, iso_track: int | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError("kind must be mic or line")
        items = self.inputs()
        self._check_track(items, iso_track)
        a = {"id": secrets.token_hex(3), "name": (name or ("Mic" if kind == "mic" else "Line in")).strip()[:40],
             "kind": kind, "device_id": device_id or "default", "gain_db": 0.0, "iso_track": iso_track,
             **DEFAULTS[kind]}
        items.append(a)
        self._save(items)
        log.info("audio input added: %s (%s, device %s)", a["name"], kind, a["device_id"])
        await self.sync_all()
        self.publish()
        return a

    async def update_input(self, aid: str, ch: dict[str, Any]) -> dict:
        items = self.inputs()
        a = next((i for i in items if i["id"] == aid), None)
        if a is None:
            raise KeyError(aid)
        ch = {k: v for k, v in ch.items() if v is not None or k == "iso_track"}   # iso_track None = off
        if "kind" in ch and ch["kind"] not in KINDS:
            raise ValueError("kind must be mic or line")
        if "iso_track" in ch:
            self._check_track(items, ch["iso_track"], aid)
        if "gain_db" in ch:
            ch["gain_db"] = round(max(-30.0, min(30.0, float(ch["gain_db"]))), 1)
        if "name" in ch:
            ch["name"] = str(ch["name"]).strip()[:40] or a["name"]
        a.update({k: v for k, v in ch.items() if k in ("name", "kind", "device_id", "gain_db", "gate", "limiter",
                                                       "iso_track")})
        self._save(items)
        try:
            await self.sync_input(a)
            if "iso_track" in ch:
                await self._sync_tracks()
        except ObsError as e:
            self.ctx.bus.notify("error", f"OBS: {a['name']} did not fully apply: {e.comment}", "audio")
        self.publish()
        return a

    async def remove_input(self, aid: str) -> None:
        items = self.inputs()
        if not any(i["id"] == aid for i in items):
            raise KeyError(aid)
        self._save([i for i in items if i["id"] != aid])
        if self.ctx.obs.connected and self.ctx.obs.owned(input_name(aid)):
            await self.ctx.obs.remove_owned_input(input_name(aid))
        log.info("audio input removed: %s", aid)
        self.publish()

    async def sync_input(self, a: dict) -> None:
        """Create/repair the OBS input and its processing chain. Settings are overlaid, so a live input is never
        recreated (no dropout)."""
        obs = self.ctx.obs
        if not obs.connected:
            return
        name = input_name(a["id"])
        await obs.ensure_scene(BUS_SCENE, "audiobus", "audio")
        await obs.ensure_input(name, self._wasapi(), self._settings(a), BUS_SCENE, "audio", a["id"])
        have = {f.get("filterName"): f for f in
                (await obs.call("GetSourceFilterList", {"sourceName": name})).get("filters", [])}
        chain = [  # order matters: gate on the raw signal, then make-up gain, limiter last as the safety net
            (F_GATE, "noise_gate_filter", {"open_threshold": -36.0, "close_threshold": -42.0, "attack_time": 5,
                                           "hold_time": 150, "release_time": 120}, bool(a.get("gate"))),
            (F_GAIN, "gain_filter", {"db": float(a.get("gain_db") or 0.0)}, True),
            (F_LIMIT, "limiter_filter", {"threshold": -1.0, "release_time": 60}, bool(a.get("limiter"))),
        ]
        for fname, fkind, fset, on in chain:
            if fname not in have:
                await obs.call("CreateSourceFilter", {"sourceName": name, "filterName": fname, "filterKind": fkind,
                                                      "filterSettings": fset})
            else:
                await obs.call("SetSourceFilterSettings", {"sourceName": name, "filterName": fname,
                                                           "filterSettings": fset, "overlay": True})
            if fname not in have or have[fname].get("filterEnabled", True) != on:
                await obs.call("SetSourceFilterEnabled", {"sourceName": name, "filterName": fname,
                                                          "filterEnabled": on})

    async def _sync_tracks(self) -> None:
        """Track 1 = programme mix for every hall input; an isolated track carries only its own hall input.
        Only FestCast-owned inputs are changed; other OBS inputs on an isolated track are reported, not touched."""
        obs = self.ctx.obs
        if not obs.connected:
            return
        iso = {input_name(a["id"]): a.get("iso_track") for a in self.inputs()}
        used = {t for t in iso.values() if t}
        for name in await obs.inputs():
            row = obs.owned(name)
            if not row or row.kind != "input":
                continue
            cur = await obs.try_call("GetInputAudioTracks", {"inputName": name})
            if cur is None:
                continue        # no audio (image, colour source...)
            tracks = cur.get("inputAudioTracks") or {}
            if name in iso:
                want = {str(t): (t == 1 or t == iso[name]) for t in range(1, 7)}
            else:
                want = {str(t): bool(tracks.get(str(t), True)) and t not in used for t in range(1, 7)}
            if want != tracks:
                await obs.call("SetInputAudioTracks", {"inputName": name, "inputAudioTracks": want})

    async def attach(self, scene: str) -> None:
        """Nest the FC Audio scene into a FestCast programme scene (only once hall inputs exist)."""
        obs = self.ctx.obs
        if not self.inputs() or not obs.connected:
            return
        await obs.ensure_scene(BUS_SCENE, "audiobus", "audio")
        if await obs.item_id(scene, BUS_SCENE) is None:
            await obs.add_item(scene, BUS_SCENE)

    async def sync_all(self) -> None:
        obs = self.ctx.obs
        if not obs.connected or not self.inputs():
            return
        try:
            for a in self.inputs():
                await self.sync_input(a)
            for name in await obs.scenes():
                row = obs.owned(name)
                if row and row.role in BUS_ROLES:
                    await self.attach(name)
            await self._sync_tracks()
        except ObsError as e:
            log.warning("audio sync failed: %s", e)
            self.ctx.bus.notify("error", f"OBS: hall audio inputs did not fully apply: {e.comment}", "audio")

    async def inputs_view(self) -> dict:
        obs = self.ctx.obs
        have = await obs.inputs() if obs.connected else {}
        items = [dict(a, obs_input=input_name(a["id"]), in_obs=input_name(a["id"]) in have) for a in self.inputs()]
        mode = None
        shared: dict[str, list[str]] = {}
        if obs.connected:
            r = await obs.try_call("GetProfileParameter", {"parameterCategory": "Output", "parameterName": "Mode"})
            mode = (r or {}).get("parameterValue") or "Simple"
            used = {a["iso_track"] for a in items if a.get("iso_track")}
            for name in have if used else ():
                if obs.owned(name):
                    continue
                t = (await obs.try_call("GetInputAudioTracks", {"inputName": name}) or {}).get("inputAudioTracks", {})
                for k in used:
                    if t.get(str(k)):
                        shared.setdefault(str(k), []).append(name)
        return {"inputs": items, "iso_tracks": list(ISO_TRACKS), "output_mode": mode,
                "rec_tracks_mask": self.iso_mask(), "iso_shared": shared}

    def publish(self) -> None:
        self.ctx.bus.publish("audio", {"changed": time.time()})

    # -- mixer -----------------------------------------------------------------------------------------------------
    async def mixer(self) -> list[dict]:
        obs = self.ctx.obs
        out = []
        cams = {f"FC_CAM_{c.id}": c.name for c in self.ctx.cameras.cameras()}
        hall = {input_name(a["id"]): a["name"] for a in self.inputs()}
        for name, inp in (await obs.inputs()).items():
            try:
                vol = await obs.call("GetInputVolume", {"inputName": name})
            except ObsError:
                continue   # not an audio source
            mute = await obs.try_call("GetInputMute", {"inputName": name}) or {}
            sync = await obs.try_call("GetInputAudioSyncOffset", {"inputName": name}) or {}
            mon = await obs.try_call("GetInputAudioMonitorType", {"inputName": name}) or {}
            out.append({"name": name, "label": cams.get(name) or hall.get(name) or name, "kind": inp.get("inputKind"),
                        "is_camera": name in cams, "is_hall": name in hall, "muted": mute.get("inputMuted"),
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
