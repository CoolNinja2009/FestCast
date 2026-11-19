"""Production + camera lifecycle: room, slots, invitations, OBS sources and live connection monitoring."""
from __future__ import annotations

import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..database.models import Camera, Invitation, Production, RetiredStream, now
from ..obs.client import ObsError
from ..transports.vdoninja import RESOLUTIONS, VdoNinjaInputAdapter, new_password, new_stream_id

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.cameras")

STALE_AFTER = 8.0          # seconds without a probe report -> probe offline
NO_VIDEO_AFTER = 6.0       # probe reporting but no inbound video for this long -> disconnected


def cam_input_name(cid: str) -> str:
    return f"FC_CAM_{cid}"


def cam_scene_name(name: str) -> str:
    return f"FC Cam · {name}"


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(\.\d+)?", v)
        return float(m.group()) if m else None
    return None


def parse_vdo_stats(raw: Any) -> dict:
    """Pull the fields we display out of a VDO.Ninja getStats inbound object. Missing fields stay None
    (unknown) - never 0. Field names verified in vdo.ninja lib.js; we accept both spellings it uses."""
    out: dict[str, Any] = {"width": None, "height": None, "fps": None, "bitrate_kbps": None, "jitter_ms": None,
                           "packet_loss_pct": None, "rtt_ms": None, "audio_kbps": None, "codec": None}
    if not isinstance(raw, dict):
        return out
    flat: dict[str, Any] = {}

    def walk(d: dict, depth: int = 0) -> None:
        for k, v in d.items():
            if isinstance(v, dict) and depth < 3:
                walk(v, depth + 1)
            elif k not in flat:
                flat[k] = v
    walk(raw)
    res = flat.get("Resolution") or flat.get("resolution")
    if isinstance(res, str):
        m = re.search(r"(\d+)\s*[x×X]\s*(\d+)", res)
        if m:
            out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    for k in ("_FPS", "fps", "framesPerSecond"):
        if flat.get(k) is not None and _num(flat[k]) is not None:
            out["fps"] = _num(flat[k])
            break
    for k in ("video_bitrate_kbps", "videoBitrate_kbps"):
        if _num(flat.get(k)) is not None:
            out["bitrate_kbps"] = _num(flat[k])
            break
    out["jitter_ms"] = _num(flat.get("video_jitter_ms"))
    out["rtt_ms"] = _num(flat.get("average_roundTripTime_ms"))
    out["audio_kbps"] = _num(flat.get("audio_bitrate_kbps"))
    out["codec"] = flat.get("video_codec") if isinstance(flat.get("video_codec"), str) else None
    for k, v in flat.items():
        lk = k.lower()
        if ("packetloss" in lk or "packet_loss" in lk or "lost_percent" in lk) and _num(v) is not None:
            out["packet_loss_pct"] = _num(v)
            break
    return out


class CameraService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.adapter = VdoNinjaInputAdapter(ctx.settings.vdo_base)
        self.live: dict[str, dict] = {}       # transient runtime state per camera
        self.history: deque[dict] = deque(maxlen=300)  # connection history
        self.reload_nonce: dict[str, int] = {}

    # -- persistence helpers --------------------------------------------------------
    def production(self) -> Production | None:
        with self.ctx.db.session() as s:
            return s.query(Production).filter(Production.active.is_(True)).order_by(
                Production.created_at.desc()).first()

    def cameras(self) -> list[Camera]:
        p = self.production()
        if not p:
            return []
        with self.ctx.db.session() as s:
            return list(s.query(Camera).filter(Camera.production_id == p.id).order_by(Camera.slot))

    def camera(self, cid: str) -> Camera:
        with self.ctx.db.session() as s:
            c = s.get(Camera, cid)
            if not c:
                raise KeyError(cid)
            return c

    def _unique_stream_id(self) -> str:
        with self.ctx.db.session() as s:
            while True:
                sid = new_stream_id()
                if not s.get(RetiredStream, sid) and not s.query(Camera).filter(Camera.stream_id == sid).first():
                    return sid

    def cam_dict(self, c: Camera, prod: Production | None = None) -> dict:
        prod = prod or self.production()
        return {"id": c.id, "name": c.name, "stream_id": c.stream_id, "password": prod.vdo_password if prod else "",
                "resolution_target": c.resolution_target, "bitrate_kbps": c.bitrate_kbps, "fps": prod.fps if prod else 30,
                "canvas_w": prod.canvas_w if prod else 1920, "canvas_h": prod.canvas_h if prod else 1080}

    # -- production -------------------------------------------------------------------
    async def create_production(self, name: str, cameras: int = 3) -> Production:
        if self.production():
            raise ValueError("A production already exists. Close it first to start a new one.")
        with self.ctx.db.session() as s:
            p = Production(name=name or "FestCast Production", room_id="fcroom_" + secrets.token_hex(6),
                           vdo_password=new_password())
            s.add(p)
        log.info("production created %s (room %s)", p.name, p.room_id)
        for i in range(max(0, min(cameras, 16))):
            await self.add_camera(None)
        self.ctx.bus.notify("info", f"Production '{p.name}' created with {cameras} camera slots", "cameras")
        return p

    async def close_production(self) -> None:
        p = self.production()
        if not p:
            return
        for c in self.cameras():
            await self.remove_camera(c.id)
        with self.ctx.db.session() as s:
            s.get(Production, p.id).active = False
        self.ctx.bus.notify("info", f"Production '{p.name}' closed", "cameras")

    # -- cameras ---------------------------------------------------------------------------
    async def add_camera(self, name: str | None) -> Camera:
        p = self.production()
        if not p:
            raise ValueError("Create a production first")
        with self.ctx.db.session() as s:
            slots = [c.slot for c in s.query(Camera).filter(Camera.production_id == p.id)]
            slot = max(slots, default=0) + 1
            c = Camera(production_id=p.id, slot=slot, name=(name or f"Camera {slot}").strip()[:80],
                       stream_id=self._unique_stream_id())
            s.add(c)
        self.new_invitation(c.id)
        log.info("camera added %s (%s)", c.name, c.id)
        await self.sync_camera_obs(c.id)
        self.publish()
        return c

    async def remove_camera(self, cid: str) -> None:
        c = self.camera(cid)
        # OBS cleanup first (only FestCast-owned objects), then the record
        if self.ctx.obs.connected:
            for row in self.ctx.obs.owned_by_ref(cid):
                try:
                    if row.kind == "input":
                        await self.ctx.obs.remove_owned_input(row.name)
                    else:
                        await self.ctx.obs.remove_owned_scene(row.name)
                except ObsError as e:
                    self.ctx.bus.notify("warning", f"Could not remove {row.name} from OBS: {e.comment}", "cameras")
        with self.ctx.db.session() as s:
            s.add(RetiredStream(stream_id=c.stream_id))
            for inv in s.query(Invitation).filter(Invitation.camera_id == cid, Invitation.revoked_at.is_(None)):
                inv.revoked_at = now()
            s.delete(s.get(Camera, cid))
        self.live.pop(cid, None)
        self.ctx.layouts.forget_camera(cid)
        log.info("camera removed %s", c.name)
        self.publish()

    async def update_camera(self, cid: str, **fields: Any) -> Camera:
        old = self.camera(cid)
        with self.ctx.db.session() as s:
            c = s.get(Camera, cid)
            if "name" in fields and fields["name"]:
                c.name = str(fields["name"]).strip()[:80]
            if "enabled" in fields:
                c.enabled = bool(fields["enabled"])
            if "resolution_target" in fields:
                if fields["resolution_target"] not in ("auto", *RESOLUTIONS):
                    raise ValueError("resolution must be auto, 720p, 1080p or 1440p")
                c.resolution_target = fields["resolution_target"]
            if "bitrate_kbps" in fields:
                c.bitrate_kbps = max(500, min(int(fields["bitrate_kbps"]), 40000))
            if "rotation" in fields:
                if int(fields["rotation"]) % 360 not in (0, 90, 180, 270):
                    raise ValueError("rotation must be 0, 90, 180 or 270")
                c.rotation = int(fields["rotation"]) % 360
            if "frame" in fields:
                if fields["frame"] not in ("fit", "fill"):
                    raise ValueError("frame must be fit or fill")
                c.frame = fields["frame"]
        if old.name != c.name and self.ctx.obs.connected:
            await self.ctx.obs.rename_scene(cam_scene_name(old.name), cam_scene_name(c.name))
        if "resolution_target" in fields and old.resolution_target != c.resolution_target:
            log.info("camera %s resolution target %s -> %s", c.name, old.resolution_target, c.resolution_target)
            self.ctx.bus.notify("info", f"{c.name}: target set to {c.resolution_target}. The phone must re-open its "
                                "invitation link (or press Reconnect on the phone) for the new capture size to apply; "
                                "FestCast will show the resolution actually received.", "cameras")
            self.live.setdefault(cid, {})["target_pending"] = True
        if "bitrate_kbps" in fields or "enabled" in fields:
            await self.sync_camera_obs(cid)
            self.bump_reload(cid)
        self.publish()
        return c

    def bump_reload(self, cid: str) -> None:
        self.reload_nonce[cid] = self.reload_nonce.get(cid, 0) + 1

    async def reconnect(self, cid: str) -> None:
        self.bump_reload(cid)
        self.live.setdefault(cid, {})["reconnecting"] = time.time()
        log.info("camera %s: viewer reconnect requested", cid)
        self.publish()

    # -- invitations --------------------------------------------------------------------------
    def new_invitation(self, cid: str, rotate_stream: bool = False) -> Invitation:
        ttl = timedelta(hours=self.ctx.settings.invite_ttl_hours)
        with self.ctx.db.session() as s:
            c = s.get(Camera, cid)
            for inv in s.query(Invitation).filter(Invitation.camera_id == cid, Invitation.revoked_at.is_(None)):
                inv.revoked_at = now()
            if rotate_stream:
                s.add(RetiredStream(stream_id=c.stream_id))
                s.flush()
                c.stream_id = self._unique_stream_id()
            inv = Invitation(token=secrets.token_urlsafe(18), camera_id=cid, stream_id=c.stream_id,
                             expires_at=now() + ttl)
            s.add(inv)
        if rotate_stream:
            self.bump_reload(cid)
            self.live.pop(cid, None)
        return inv

    async def revoke(self, cid: str) -> None:
        """Revoke: the old link stops working AND the old stream id is retired, so a phone still holding the old
        VDO.Ninja URL publishes into nothing. A fresh invitation is issued."""
        self.new_invitation(cid, rotate_stream=True)
        log.info("camera %s invitation revoked; stream id rotated", cid)
        self.ctx.bus.notify("info", "Invitation revoked. Send the new link to the operator.", "cameras")
        self.publish()

    def invitation(self, cid: str) -> Invitation | None:
        with self.ctx.db.session() as s:
            return s.query(Invitation).filter(Invitation.camera_id == cid, Invitation.revoked_at.is_(None)).order_by(
                Invitation.created_at.desc()).first()

    def resolve_invite(self, token: str) -> tuple[Camera, str] | None:
        with self.ctx.db.session() as s:
            inv = s.get(Invitation, token)
            if not inv or inv.revoked_at is not None:
                return None
            exp = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return None
            c = s.get(Camera, inv.camera_id)
            if not c or c.stream_id != inv.stream_id:
                return None
            if inv.opened_at is None:
                inv.opened_at = now()
        return c, self.adapter.publish_url(self.cam_dict(c))

    def invite_link(self, token: str) -> str:
        return f"{self.ctx.public_base}/join/{token}"

    # -- OBS -------------------------------------------------------------------------------------------
    def wrapper_url(self, cid: str) -> str:
        return f"http://127.0.0.1:{self.ctx.settings.port}/obs/cam/{cid}?k={self.ctx.local_key}"

    async def sync_camera_obs(self, cid: str) -> None:
        if not self.ctx.obs.connected:
            return
        c = self.camera(cid)
        scene = cam_scene_name(c.name)
        try:
            await self.ctx.obs.ensure_scene(scene, "camscene", cid)
            kind, settings = self.adapter.obs_source(self.cam_dict(c), self.wrapper_url(cid))
            await self.ctx.obs.ensure_input(cam_input_name(cid), kind, settings, scene, "camera", cid)
            await self.ctx.obs.fit_full(scene, cam_input_name(cid))
            await self.ctx.obs.call("SetInputMute", {"inputName": cam_input_name(cid),
                                                     "inputMuted": not c.audio_active})
        except ObsError as e:
            self.ctx.bus.notify("error", f"OBS: could not set up {c.name}: {e.comment}", "cameras")

    async def sync_all(self) -> None:
        for c in self.cameras():
            await self.sync_camera_obs(c.id)

    async def set_audio(self, cid: str, active: bool, exclusive: bool = True) -> None:
        with self.ctx.db.session() as s:
            for c in s.query(Camera):
                if c.id == cid:
                    c.audio_active = active
                elif exclusive and active:
                    c.audio_active = False
        if self.ctx.obs.connected:
            for c in self.cameras():
                await self.ctx.obs.try_call("SetInputMute", {"inputName": cam_input_name(c.id),
                                                             "inputMuted": not c.audio_active})
        log.info("camera audio %s -> %s", cid, active)
        self.publish()

    # -- live monitoring (reports from the OBS-side probe page) ------------------------------------------
    def report(self, cid: str, payload: dict) -> dict:
        try:
            c = self.camera(cid)
        except KeyError:
            return {"ok": False}
        st = self.live.setdefault(cid, {})
        t = time.time()
        st["probe_seen"] = t
        stats = payload.get("stats")
        if payload.get("event"):
            st["last_event"] = payload["event"]
        parsed = parse_vdo_stats(stats) if stats else None
        has_video = bool(parsed and (parsed["width"] or (parsed["bitrate_kbps"] or 0) > 0))
        prev_status = st.get("status")
        if has_video:
            st.update(parsed)
            st["last_seen"] = t
            st["raw"] = stats
            st.pop("reconnecting", None)
            if st.get("target_pending") and parsed["height"]:
                st.pop("target_pending", None)
        status = self._status(c, st, t)
        if status != prev_status:
            st["status"] = status
            self._history(c, status)
        return {"ok": True, "reload": self.reload_nonce.get(cid, 0), "rotation": c.rotation or 0,
                "frame": c.frame or "fit"}

    def _status(self, c: Camera, st: dict, t: float) -> str:
        if not c.enabled:
            return "disabled"
        if not st.get("probe_seen") or t - st["probe_seen"] > STALE_AFTER:
            return "no_probe"           # OBS browser source not running -> we can't observe the feed
        if st.get("last_seen") and t - st["last_seen"] <= NO_VIDEO_AFTER:
            return "live"
        if st.get("reconnecting") and t - st["reconnecting"] < 20:
            return "reconnecting"
        return "waiting" if not st.get("last_seen") else "disconnected"

    def _history(self, c: Camera, status: str) -> None:
        self.history.appendleft({"ts": time.time(), "kind": "camera", "id": c.id, "name": c.name, "status": status})
        if status == "live":
            log.info("camera %s connected", c.name)
        elif status == "disconnected":
            log.warning("camera %s disconnected", c.name)
            self.ctx.bus.notify("warning", f"{c.name} lost its feed. The programme was NOT changed.", "cameras")

    def tick(self) -> None:
        """Re-evaluate statuses (called by the metrics loop) so silent drops are noticed."""
        t = time.time()
        for c in self.cameras():
            st = self.live.setdefault(c.id, {})
            s = self._status(c, st, t)
            if s != st.get("status"):
                st["status"] = s
                self._history(c, s)

    def usable_count(self) -> int:
        return sum(1 for c in self.cameras() if c.enabled and self.live.get(c.id, {}).get("status") == "live")

    # -- views -------------------------------------------------------------------------------------------
    def view(self, c: Camera, prod: Production | None = None) -> dict:
        st = self.live.get(c.id, {})
        inv = self.invitation(c.id)
        target = RESOLUTIONS.get(c.resolution_target)
        below = None
        if target and st.get("status") == "live" and st.get("height"):
            below = min(st["width"] or 0, st["height"] or 0) < min(target) - 8
        return {
            "id": c.id, "slot": c.slot, "name": c.name, "input_type": c.input_type, "transport": c.transport,
            "enabled": c.enabled, "resolution_target": c.resolution_target, "bitrate_kbps": c.bitrate_kbps,
            "audio_active": c.audio_active, "obs_input": cam_input_name(c.id), "obs_scene": cam_scene_name(c.name),
            "rotation": c.rotation or 0, "frame": c.frame or "fit",
            "portrait": bool(st.get("status") == "live" and st.get("width") and st.get("height")
                             and st["height"] > st["width"] and (c.rotation or 0) in (0, 180)),
            "status": st.get("status", "no_probe"), "last_seen": st.get("last_seen"),
            "width": st.get("width"), "height": st.get("height"), "fps": st.get("fps"),
            "bitrate_in_kbps": st.get("bitrate_kbps"), "jitter_ms": st.get("jitter_ms"),
            "packet_loss_pct": st.get("packet_loss_pct"), "rtt_ms": st.get("rtt_ms"),
            "audio_kbps": st.get("audio_kbps"), "codec": st.get("codec"),
            "below_target": below, "target_pending": bool(st.get("target_pending")),
            "reconnecting": bool(st.get("reconnecting")),
            "invite": ({"link": self.invite_link(inv.token), "expires_at": inv.expires_at.isoformat(),
                        "opened": inv.opened_at is not None} if inv else None),
        }

    def snapshot(self) -> dict:
        p = self.production()
        return {"production": ({"id": p.id, "name": p.name, "room_id": p.room_id, "canvas_w": p.canvas_w,
                                "canvas_h": p.canvas_h, "fps": p.fps,
                                "started_at": p.started_at.isoformat() if p.started_at else None} if p else None),
                "cameras": [self.view(c, p) for c in self.cameras()]}

    def publish(self) -> None:
        self.ctx.bus.publish("cameras", self.snapshot())
