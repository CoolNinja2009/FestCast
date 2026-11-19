"""Programme output: OBS --WHIP--> MediaMTX --WHEP--> TV browser receivers, plus HDMI projectors.

MediaMTX endpoints verified in its docs (publish: /<path>/whip, read: /<path>/whep, port 8889, ICE UDP 8189).
MediaMTX only accepts requests from this PC. TV receivers never talk to MediaMTX's HTTP API directly: their WHEP
offer goes to FestCast (/api/public/tv/<token>/whep), which checks the receiver token and forwards it. The media then
flows directly MediaMTX -> TV over UDP 8189. Revoking a receiver makes its token useless immediately."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..database.models import Receiver
from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.outputs")


class OutputService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.proc: asyncio.subprocess.Process | None = None
        self.mtx_log: deque[str] = deque(maxlen=200)
        self.mtx_state = "stopped"     # stopped | starting | running | crashed | missing
        self.want_mtx = False
        self.live: dict[str, dict] = {}      # receiver id -> runtime
        self.mtx_stats: dict[str, Any] = {}
        self._saved_service: dict | None = None
        self._watch: asyncio.Task | None = None

    # -- receivers ----------------------------------------------------------------------------------
    def receivers(self) -> list[Receiver]:
        with self.ctx.db.session() as s:
            return list(s.query(Receiver).filter(Receiver.revoked.is_(False)).order_by(Receiver.created_at))

    def add(self, name: str, kind: str = "webrtc", monitor_index: int | None = None) -> Receiver:
        if kind not in ("webrtc", "hdmi"):
            raise ValueError("kind must be webrtc or hdmi")
        with self.ctx.db.session() as s:
            r = Receiver(name=(name or "TV").strip()[:80], kind=kind, token=secrets.token_urlsafe(18),
                         monitor_index=monitor_index)
            s.add(r)
        log.info("receiver added %s (%s)", r.name, kind)
        self.publish()
        return r

    def rename(self, rid: str, name: str) -> None:
        with self.ctx.db.session() as s:
            r = s.get(Receiver, rid)
            if not r:
                raise KeyError(rid)
            r.name = name.strip()[:80] or r.name
        self.publish()

    async def revoke(self, rid: str) -> None:
        with self.ctx.db.session() as s:
            r = s.get(Receiver, rid)
            if not r:
                raise KeyError(rid)
            r.revoked = True
            kind = r.kind
        self.live.pop(rid, None)
        log.info("receiver %s revoked", rid)
        self.publish()

    def by_token(self, token: str) -> Receiver | None:
        with self.ctx.db.session() as s:
            r = s.query(Receiver).filter(Receiver.token == token).first()
            return r if r and not r.revoked else None

    async def whep(self, token: str, offer_sdp: str) -> tuple[int, str, str | None]:
        """Forward a receiver's WHEP offer to MediaMTX (token-checked). Returns (status, body, session path)."""
        if not self.by_token(token):
            return 403, "receiver revoked or unknown", None
        if self.mtx_state != "running":
            return 503, "programme output is not running", None
        s = self.ctx.settings
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"http://127.0.0.1:{s.mediamtx_webrtc_port}/{s.mediamtx_stream_path}/whep",
                                 content=offer_sdp.encode(), headers={"Content-Type": "application/sdp"})
        except httpx.HTTPError as e:
            return 502, f"MediaMTX unreachable: {e}", None
        return r.status_code, r.text, r.headers.get("location")

    async def whep_delete(self, token: str, session: str) -> None:
        if not self.by_token(token) or not session.startswith("/"):
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.delete(f"http://127.0.0.1:{self.ctx.settings.mediamtx_webrtc_port}{session}")
        except httpx.HTTPError:
            pass

    def report(self, token: str, payload: dict) -> bool:
        r = self.by_token(token)
        if not r:
            return False
        st = self.live.setdefault(r.id, {})
        prev = st.get("state")
        st.update({k: payload.get(k) for k in ("state", "width", "height", "fps", "bitrate_kbps", "jitter_ms",
                                               "packets_lost", "rtt_ms", "error", "user_agent")})
        st["last_seen"] = time.time()
        if prev != st.get("state"):
            self.ctx.cameras.history.appendleft({"ts": time.time(), "kind": "receiver", "id": r.id, "name": r.name,
                                                 "status": st.get("state")})
            log.info("receiver %s: %s", r.name, st.get("state"))
            if st.get("state") == "failed":
                st["failures"] = st.get("failures", 0) + 1
        return True

    def link(self, r: Receiver) -> str:
        return f"{self.ctx.public_base}/tv/{r.token}"

    # -- MediaMTX ---------------------------------------------------------------------------------------
    def mtx_config(self) -> str:
        s = self.ctx.settings
        return f"""# generated by FestCast - edits are overwritten
logLevel: info
logDestinations: [stdout]
api: yes
apiAddress: 127.0.0.1:9997
rtsp: no
rtmp: no
hls: no
srt: no
webrtc: yes
webrtcAddress: :{s.mediamtx_webrtc_port}
webrtcLocalUDPAddress: :8189
webrtcLocalTCPAddress: :8189
webrtcIPsFromInterfaces: yes
webrtcAdditionalHosts: [{self.ctx.lan_ip}]
authMethod: internal
authInternalUsers:
  - user: any
    pass:
    ips: ['127.0.0.1', '::1']
    permissions:
      - action: publish
      - action: read
      - action: api
paths:
  all_others:
"""

    async def start_mtx(self) -> None:
        exe = self.ctx.settings.mediamtx
        if not exe:
            self.mtx_state = "missing"
            self.publish()
            raise ValueError("MediaMTX not found. Run scripts\\get-mediamtx.ps1 (or put mediamtx.exe in "
                             "tools\\mediamtx\\) - see docs/SETUP.md")
        if self.proc and self.proc.returncode is None:
            return
        cfg = self.ctx.settings.data_dir / "mediamtx.yml"
        cfg.write_text(self.mtx_config(), "utf-8")
        self.want_mtx = True
        self.mtx_state = "starting"
        self.proc = await asyncio.create_subprocess_exec(exe, str(cfg), stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT,
                                                         cwd=str(Path(exe).parent))
        asyncio.create_task(self._pump(self.proc))
        await asyncio.sleep(1.0)
        if self.proc.returncode is not None:
            self.mtx_state = "crashed"
            self.publish()
            raise ValueError("MediaMTX exited immediately: " + " | ".join(list(self.mtx_log)[-5:]))
        self.mtx_state = "running"
        log.info("MediaMTX started (pid %s)", self.proc.pid)
        if not self._watch:
            self._watch = asyncio.create_task(self._watchdog())
        self.publish()

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for line in proc.stdout:
            t = line.decode(errors="replace").rstrip()
            self.mtx_log.append(t)
            if " ERR " in t or "error" in t.lower():
                log.warning("mediamtx: %s", t)

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(3)
            if self.want_mtx and self.proc and self.proc.returncode is not None:
                self.mtx_state = "crashed"
                self.ctx.bus.notify("error", "MediaMTX stopped unexpectedly - restarting. Recording is unaffected.",
                                    "outputs")
                self.proc = None
                try:
                    await self.start_mtx()
                except Exception as e:  # noqa: BLE001
                    log.warning("MediaMTX restart failed: %s", e)

    async def stop_mtx(self) -> None:
        self.want_mtx = False
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.mtx_state = "stopped"
        self.publish()

    async def restart_mtx(self) -> None:
        await self.stop_mtx()
        await self.start_mtx()

    async def poll_mtx(self) -> None:
        if self.mtx_state != "running":
            self.mtx_stats = {}
            return
        s = self.ctx.settings
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                p = (await c.get(f"http://127.0.0.1:9997/v3/paths/get/{s.mediamtx_stream_path}"))
                sess = (await c.get("http://127.0.0.1:9997/v3/webrtcsessions/list"))
            path = p.json() if p.status_code == 200 else {}
            sessions = sess.json().get("items", []) if sess.status_code == 200 else []
            t = time.time()
            prev = self.mtx_stats.get("_bytes", {})
            readers = []
            out_kbps = 0.0
            for x in sessions:
                if x.get("path") != s.mediamtx_stream_path or x.get("state") == "publish":
                    continue
                b = x.get("bytesSent") or 0
                kb = None
                if x["id"] in prev:
                    pb, pt = prev[x["id"]]
                    kb = max(0.0, (b - pb) * 8 / 1000 / max(0.5, t - pt))
                    out_kbps += kb
                readers.append({"id": x["id"], "remote": x.get("remoteAddr"), "kbps": kb, "bytes_sent": b,
                                "established": x.get("peerConnectionEstablished")})
            self.mtx_stats = {"ready": path.get("ready"), "source": (path.get("source") or {}).get("type"),
                              "readers": readers, "out_kbps": out_kbps if readers else 0.0,
                              "bytes_received": path.get("bytesReceived"),
                              "_bytes": {x["id"]: (x["bytes_sent"], t) for x in readers}}
        except (httpx.HTTPError, ValueError) as e:
            self.mtx_stats = {"error": str(e)}

    # -- programme publishing from OBS ----------------------------------------------------------------------
    async def start_programme(self) -> None:
        await self.start_mtx()
        obs = self.ctx.obs
        if obs.state.get("streaming"):
            raise ValueError("OBS is already streaming somewhere else. Stop that stream first "
                             "(OBS has one streaming output).")
        cur = await obs.call("GetStreamServiceSettings")
        if cur.get("streamServiceType") != "whip_custom" or "festcast" not in str(cur.get("streamServiceSettings")):
            self._saved_service = cur
            self.ctx.db.set("saved_stream_service", cur)
        s = self.ctx.settings
        await obs.call("SetStreamServiceSettings", {
            "streamServiceType": "whip_custom",
            "streamServiceSettings": {"server": f"http://127.0.0.1:{s.mediamtx_webrtc_port}/{s.mediamtx_stream_path}"
                                                f"/whip", "bearer_token": ""}})
        await obs.call("StartStream")
        log.info("programme publishing to MediaMTX via WHIP")
        self.ctx.bus.notify("info", "Programme output to TVs started (OBS → MediaMTX)", "outputs")
        self.publish()

    async def stop_programme(self, restore_service: bool = True) -> None:
        obs = self.ctx.obs
        if obs.state.get("streaming"):
            await obs.call("StopStream")
        saved = self._saved_service or self.ctx.db.get("saved_stream_service")
        if restore_service and saved and saved.get("streamServiceType"):
            for _ in range(20):
                if not obs.state.get("streaming"):
                    break
                await asyncio.sleep(0.25)
            await obs.try_call("SetStreamServiceSettings", {"streamServiceType": saved["streamServiceType"],
                                                            "streamServiceSettings": saved.get(
                                                                "streamServiceSettings") or {}})
        self.publish()

    # -- HDMI --------------------------------------------------------------------------------------------------
    async def monitors(self) -> list[dict]:
        return (await self.ctx.obs.call("GetMonitorList")).get("monitors", [])

    async def open_projector(self, monitor_index: int) -> None:
        await self.ctx.obs.call("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
                                                          "monitorIndex": int(monitor_index)})
        log.info("programme projector opened on monitor %s", monitor_index)

    # -- views -------------------------------------------------------------------------------------------------
    def view(self) -> dict:
        t = time.time()
        recs = []
        for r in self.receivers():
            st = self.live.get(r.id, {})
            online = bool(st.get("last_seen") and t - st["last_seen"] < 10)
            recs.append({"id": r.id, "name": r.name, "kind": r.kind, "monitor_index": r.monitor_index,
                         "link": self.link(r) if r.kind == "webrtc" else None, "online": online,
                         "state": (st.get("state") if online else ("offline" if st else "never_connected")),
                         **{k: st.get(k) for k in ("width", "height", "fps", "bitrate_kbps", "jitter_ms",
                                                   "packets_lost", "rtt_ms", "error", "last_seen", "failures")}})
        return {"receivers": recs, "mediamtx": {"state": self.mtx_state, "found": bool(self.ctx.settings.mediamtx),
                                                "log": list(self.mtx_log)[-30:],
                                                **{k: v for k, v in self.mtx_stats.items() if not k.startswith("_")}},
                "programme_streaming": self.ctx.obs.state.get("streaming")}

    def publish(self) -> None:
        self.ctx.bus.publish("outputs", self.view())

    async def shutdown(self) -> None:
        await self.stop_mtx()
        if self._watch:
            self._watch.cancel()


_ = ObsError
