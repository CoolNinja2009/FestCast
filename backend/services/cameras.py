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
import httpx

from ..transports.local import LocalInputAdapter, mtx_path
from ..transports.vdoninja import RESOLUTIONS, CameraInputAdapter, VdoNinjaInputAdapter, new_password, new_stream_id

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.cameras")

TRANSPORTS = ("local", "vdoninja")
TRANSPORT_LABEL = {"local": "locally (through this PC, no internet)", "vdoninja": "through VDO.Ninja (internet)"}
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
        self.adapters: dict[str, CameraInputAdapter] = {"vdoninja": self.adapter, "local": LocalInputAdapter()}
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

    def adapter_of(self, c: Camera) -> CameraInputAdapter:
        return self.adapters.get(c.transport or "vdoninja", self.adapter)

    def picture_aspect(self, c: Camera) -> float | None:
        """Aspect of the camera's picture as it appears (after its rotation), from the resolution actually received;
        None while unknown."""
        st = self.live.get(c.id) or {}
        w, h = st.get("width"), st.get("height")
        if not w or not h:
            return None
        return h / w if (c.rotation or 0) in (90, 270) else w / h

    def auto_turn(self, cid: str) -> bool:
        """Operator switch (Cameras page / layout editor): follow the phone's motion sensor. On by default."""
        return self.ctx.db.get(f"cam_autoturn:{cid}", True) is not False

    async def phone_held(self, cid: str, held: int) -> int:
        """The phone page reports how it is held (motion sensor; see the /orientation endpoint)."""
        st = self.live.setdefault(cid, {})
        if st.get("held") != held:
            log.info("camera %s: phone reports it is held %s", self.camera(cid).name,
                     "upright" if held == 0 else f"sideways ({held})")
        st["held"], st["held_at"] = held, time.time()
        rot = await self.apply_held(cid)
        self.publish()
        return rot

    async def apply_held(self, cid: str) -> int:
        """Rotation for a phone from how it is held: the held turn while the received frames are tall, 0 when they
        are wide. Does nothing while the operator has auto-turn off."""
        c = self.camera(cid)
        st = self.live.get(cid) or {}
        if not self.auto_turn(cid) or "held" not in st or not st.get("width") or not st.get("height"):
            return c.rotation or 0
        rot = st["held"] if st["height"] > st["width"] else 0
        if (c.rotation or 0) != rot:
            log.info("camera %s: phone held %s, frames %sx%s -> picture turned %s", c.name, st["held"], st["width"],
                     st["height"], rot)
            await self.update_camera(cid, rotation=rot)
        return rot

    def source_size(self, c: Camera, prod: Production | None = None) -> tuple[int, int]:
        """Size of the camera's OBS browser source: exactly the picture's shape (an upright phone gives a tall
        source), so the source has no bars and a layout tile simply fits it. Until the picture is known, and for a
        camera set to fill its frame, it is the production canvas (16:9)."""
        prod = prod or self.production()
        cw, ch = (prod.canvas_w, prod.canvas_h) if prod else (1920, 1080)
        a = self.picture_aspect(c)
        if not a or (c.frame or "fit") == "fill" or abs(a - cw / ch) < 0.01:
            return cw, ch
        long = max(cw, ch)
        return (long, max(2, round(long / a))) if a >= 1 else (max(2, round(long * a)), long)

    def cam_dict(self, c: Camera, prod: Production | None = None) -> dict:
        prod = prod or self.production()
        sw, sh = self.source_size(c, prod)
        return {"id": c.id, "name": c.name, "stream_id": c.stream_id, "password": prod.vdo_password if prod else "",
                "resolution_target": c.resolution_target, "bitrate_kbps": c.bitrate_kbps, "fps": prod.fps if prod else 30,
                "canvas_w": sw, "canvas_h": sh}

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
            transport = self.ctx.settings.camera_transport
            c = Camera(production_id=p.id, slot=slot, name=(name or f"Camera {slot}").strip()[:80],
                       stream_id=self._unique_stream_id(), transport=transport if transport in TRANSPORTS else "local")
            s.add(c)
        self.new_invitation(c.id)
        log.info("camera added %s (%s, %s)", c.name, c.id, c.transport)
        await self.ensure_mtx()
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
            if "transport" in fields:
                if fields["transport"] not in TRANSPORTS:
                    raise ValueError("transport must be local or vdoninja")
                c.transport = fields["transport"]
            if "auto_turn" in fields:
                self.ctx.db.set(f"cam_autoturn:{cid}", bool(fields["auto_turn"]))
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
        if "transport" in fields and old.transport != c.transport:
            log.info("camera %s transport %s -> %s", c.name, old.transport, c.transport)
            self.live.pop(cid, None)
            await self.ensure_mtx()
            self.ctx.bus.notify("info", f"{c.name} now connects {TRANSPORT_LABEL[c.transport]}. The phone must open "
                                "its camera link again.", "cameras")
        if "bitrate_kbps" in fields or "enabled" in fields or "transport" in fields:
            await self.sync_camera_obs(cid)
            self.bump_reload(cid)
        if "transport" in fields and old.transport != c.transport:
            await self.refresh_obs_page(cid)
        if fields.get("auto_turn"):
            await self.apply_held(cid)                  # switched on: follow the phone right away
            c = self.camera(cid)
        if (old.rotation or 0, old.frame or "fit") != (c.rotation or 0, c.frame or "fit"):
            self.ctx.layouts.camera_changed_shape(cid)
        self.publish()
        return c

    def bump_reload(self, cid: str) -> None:
        self.reload_nonce[cid] = self.reload_nonce.get(cid, 0) + 1

    async def refresh_obs_page(self, cid: str) -> None:
        """Reload the camera's OBS browser source from scratch (its "Refresh cache of current page" button), so OBS
        runs the current page even if it loaded an older FestCast version before."""
        if self.ctx.obs.connected:
            await self.ctx.obs.try_call("PressInputPropertiesButton", {"inputName": cam_input_name(cid),
                                                                        "propertyName": "refreshnocache"})

    async def reconnect(self, cid: str) -> None:
        self.bump_reload(cid)
        await self.refresh_obs_page(cid)
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
        old = self.camera(cid)
        self.new_invitation(cid, rotate_stream=True)
        if old.transport == "local":
            await self.kick_local(mtx_path({"stream_id": old.stream_id}))
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
        return c, self.publish_link(c, token)

    def publish_link(self, c: Camera, token: str, ip: bool = False) -> str | None:
        """Where the phone sends video from: the local https camera page, or the VDO.Ninja push URL."""
        if c.transport == "local":
            return f"{self.ctx.https_base(ip)}/cam/{token}" if self.ctx.https_port else None
        return self.adapter.publish_url(self.cam_dict(c))

    def invite_link(self, token: str, ip: bool = False) -> str:
        return f"{self.ctx.ip_base if ip else self.ctx.public_base}/join/{token}"

    def direct_link(self, cid: str) -> str | None:
        """The VDO.Ninja publish URL itself, for phones that cannot reach this PC (other network, mobile data, a
        VPN or blocker on the phone). It skips the /join instruction page. Revoking rotates the stream id, so an old
        direct link publishes into nothing, same as an old join link."""
        with self.ctx.db.session() as s:
            c = s.get(Camera, cid)
        if not c:
            return None
        if c.transport == "local":
            inv = self.invitation(cid)
            return self.publish_link(c, inv.token, ip=True) if inv else None
        return self.adapter.publish_url(self.cam_dict(c))

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
            kind, settings = self.adapter_of(c).obs_source(self.cam_dict(c), self.wrapper_url(cid))
            await self.ctx.obs.ensure_input(cam_input_name(cid), kind, settings, scene, "camera", cid)
            await self.ctx.obs.fit_full(scene, cam_input_name(cid))
            await self.ctx.audio.attach(scene)
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
        if isinstance(payload.get("parsed"), dict):     # local transport: the OBS page measures WebRTC itself
            raw = payload["parsed"]
            parsed = {k: _num(raw.get(k)) for k in parse_vdo_stats(None) if k != "codec"}
            parsed["codec"] = raw.get("codec") if isinstance(raw.get("codec"), str) else None
            for k in ("width", "height"):
                parsed[k] = int(parsed[k]) if parsed[k] else None
            stats = raw
        else:
            parsed = parse_vdo_stats(stats) if stats else None
        has_video = bool(parsed and (parsed["width"] or (parsed["bitrate_kbps"] or 0) > 0))
        prev_status = st.get("status")
        if has_video:
            def shape(d: dict) -> float | None:
                return round(d["width"] / d["height"], 2) if d.get("width") and d.get("height") else None
            if shape(parsed) and shape(parsed) != shape(st):
                self.ctx.layouts.camera_changed_shape(cid)
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
                "frame": c.frame or "fit", "width": st.get("width"), "height": st.get("height")}

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

    # -- local transport (MediaMTX) ---------------------------------------------------------------------------
    def _mtx(self) -> tuple[str, str] | None:
        o = self.ctx.outputs
        if o.mtx_state != "running":
            return None
        return f"http://127.0.0.1:{o.ports['webrtc']}", f"http://127.0.0.1:{o.ports['api']}"

    async def ensure_mtx(self) -> None:
        """Local cameras need MediaMTX: start it (the outputs watchdog then keeps it alive)."""
        if not any(c.transport == "local" for c in self.cameras()):
            return
        o = self.ctx.outputs
        if o.mtx_state == "running" and o.want_mtx:
            return
        if not self.ctx.settings.mediamtx:
            self.ctx.bus.notify("error", "Local cameras need MediaMTX, which was not found. Run "
                                "scripts\\get-mediamtx.ps1 (see docs/SETUP.md).", "cameras")
            return
        try:
            await o.start_mtx()
        except ValueError as e:
            self.ctx.bus.notify("error", f"Local cameras: {e}", "cameras")

    async def _forward(self, method: str, url: str, body: str | None = None) -> tuple[int, str, str | None]:
        try:
            async with httpx.AsyncClient(timeout=10) as cl:
                r = await cl.request(method, url, content=body.encode() if body is not None else None,
                                     headers={"Content-Type": "application/sdp"} if body is not None else None)
        except httpx.HTTPError as e:
            return 502, f"MediaMTX unreachable: {e}", None
        return r.status_code, r.text, r.headers.get("location")

    def local_cam_for_token(self, token: str) -> Camera | None:
        with self.ctx.db.session() as s:
            inv = s.get(Invitation, token)
            if not inv or inv.revoked_at is not None:
                return None
            exp = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=timezone.utc)
            c = s.get(Camera, inv.camera_id)
            if exp < datetime.now(timezone.utc) or not c or c.stream_id != inv.stream_id or c.transport != "local":
                return None
            if inv.opened_at is None:
                inv.opened_at = now()
            return c

    async def phone_whip(self, token: str, offer_sdp: str) -> tuple[int, str, str | None]:
        """Forward a phone's WHIP offer to MediaMTX (invitation-checked)."""
        c = self.local_cam_for_token(token)
        if not c:
            return 403, "This camera link is invalid, expired or was revoked. Ask the producer for a new one.", None
        if not c.enabled:
            return 409, "This camera is switched off in FestCast. Ask the producer to enable it.", None
        await self.ensure_mtx()
        m = self._mtx()
        if not m:
            return 503, "FestCast's media server (MediaMTX) is not running yet. Retrying.", None
        path = mtx_path(self.cam_dict(c))
        await self.kick_local(path)      # a phone re-joining replaces its own stale session
        st, text, loc = await self._forward("POST", f"{m[0]}/{path}/whip", offer_sdp)
        if st == 201:
            log.info("camera %s: phone connected (local)", c.name)
        return st, text, loc

    async def phone_whip_delete(self, token: str, session: str) -> None:
        if session.startswith("/") and self.local_cam_for_token(token) and (m := self._mtx()):
            await self._forward("DELETE", m[0] + session)

    async def obs_whep(self, cid: str, offer_sdp: str) -> tuple[int, str, str | None]:
        c = self.camera(cid)
        if c.transport != "local":
            return 400, "not a local camera", None
        m = self._mtx()
        if not m:
            await self.ensure_mtx()
            return 503, "MediaMTX not running", None
        return await self._forward("POST", f"{m[0]}/{mtx_path(self.cam_dict(c))}/whep", offer_sdp)

    async def obs_whep_delete(self, session: str) -> None:
        if session.startswith("/") and (m := self._mtx()):
            await self._forward("DELETE", m[0] + session)

    async def kick_local(self, path: str) -> None:
        """Drop the phone publishing to this MediaMTX path (MediaMTX API: webrtcsessions list / kick)."""
        m = self._mtx()
        if not m:
            return
        try:
            async with httpx.AsyncClient(timeout=3) as cl:
                items = (await cl.get(f"{m[1]}/v3/webrtcsessions/list")).json().get("items") or []
                for x in items:
                    if x.get("path") == path and x.get("state") == "publish":
                        await cl.post(f"{m[1]}/v3/webrtcsessions/kick/{x['id']}")
        except (httpx.HTTPError, ValueError):
            pass

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
            "auto_turn": self.auto_turn(c.id),
            "tally": self.ctx.tally.state(c.id),       # program | preview | off | unknown (what the phone shows)
            # what the phone's motion sensor last said: None = never reported (no motion access / old page)
            "held": st.get("held"), "held_at": st.get("held_at"),
            "portrait": bool(st.get("status") == "live" and st.get("width") and st.get("height")
                             and st["height"] > st["width"] and (c.rotation or 0) in (0, 180)),
            "status": st.get("status", "no_probe"), "last_seen": st.get("last_seen"),
            "width": st.get("width"), "height": st.get("height"), "fps": st.get("fps"),
            "bitrate_in_kbps": st.get("bitrate_kbps"), "jitter_ms": st.get("jitter_ms"),
            "packet_loss_pct": st.get("packet_loss_pct"), "rtt_ms": st.get("rtt_ms"),
            "audio_kbps": st.get("audio_kbps"), "codec": st.get("codec"),
            "below_target": below, "target_pending": bool(st.get("target_pending")),
            "reconnecting": bool(st.get("reconnecting")),
            "https_ok": bool(self.ctx.https_port) if c.transport == "local" else None,
            "invite": ({"link": self.invite_link(inv.token), "ip_link": self.invite_link(inv.token, ip=True), "expires_at": inv.expires_at.isoformat(),
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
